"""The recipe as a model: what each table and key of a recipe file means.

The models check what one key can check alone: types, ranges, unknown keys, the syntax of a
duration, a time format or a time zone. What depends on other keys (the language of a selector
follows from its call's ``decode``, the names a placeholder may use, whether a check has the
values it needs) is checked by :func:`memini.recipes.validate.check_recipe`. A recipe is valid
only when both pass; build one with :func:`memini.recipes.validate.parse_recipe` or
:func:`memini.recipes.validate.load_recipe`, not with :meth:`Recipe.model_validate` alone.

The attribute docstrings are the descriptions of the JSON Schema (``schemas/recipe.schema.json``),
which :func:`recipe_json_schema` generates.
"""

from datetime import timedelta
from typing import Annotated, Any, Final, Literal, cast

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    PlainSerializer,
    WithJsonSchema,
)

from memini.ingest.model import IngestKind
from memini.recipes.times import (
    DURATION_PATTERN,
    check_timestamp_format,
    check_timezone,
    format_duration,
    parse_duration,
)
from memini.workspaces import HANDLE_PATTERN

__all__ = [
    "PAGING_MODES",
    "RECIPE_FORMAT",
    "SCHEMA_ID",
    "Checks",
    "CursorPaging",
    "Decode",
    "DetailCall",
    "ListCall",
    "ListItem",
    "NoPaging",
    "OffsetPaging",
    "Order",
    "PagePaging",
    "Paging",
    "Parser",
    "Rate",
    "Recipe",
    "Server",
    "Stream",
    "Window",
    "recipe_json_schema",
]

RECIPE_FORMAT: Final = 1
"""The version of the recipe format this build reads. A change of meaning takes a new number."""

SCHEMA_ID: Final = (
    "https://raw.githubusercontent.com/npavlov/memini/main/schemas/recipe.schema.json"
)

Decode = Literal["json", "structured", "text"]
"""How a call's result is read before selectors apply.

- ``json``: the text of the result's text content blocks, joined by newlines, parsed as JSON;
  selectors are paths.
- ``structured``: the result's ``structuredContent`` object; selectors are paths.
- ``text``: the text of the text content blocks, joined by newlines; selectors are patterns.
"""

Order = Literal["newest_first", "oldest_first", "any"]
"""The order of a listing's items."""

Duration = Annotated[
    timedelta,
    BeforeValidator(parse_duration),
    PlainSerializer(format_duration, return_type=str),
    WithJsonSchema(
        {
            "type": "string",
            "pattern": DURATION_PATTERN,
            "description": "numbers with units s, m, h, d, w, such as 90d, 12h or 1w2d",
        }
    ),
]

Handle = Annotated[str, Field(pattern=f"^{HANDLE_PATTERN.pattern}$")]
Text = Annotated[str, Field(min_length=1, max_length=1000)]
ToolName = Annotated[str, Field(pattern=r"^[A-Za-z0-9_./-]{1,128}$")]
ArgumentName = Annotated[str, Field(min_length=1, max_length=128)]
FieldName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")]
ParserName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$", max_length=128)]
Selector = Annotated[str, Field(min_length=1, max_length=1000)]
"""A path or a pattern, by the call's ``decode`` (:mod:`memini.recipes.selectors`)."""
TimestampFormat = Annotated[str, AfterValidator(check_timestamp_format)]
TimeZone = Annotated[str, AfterValidator(check_timezone)]
MaxPages = Annotated[int, Field(ge=1, le=100_000)]


class _Model(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        use_attribute_docstrings=True,
        validate_by_name=True,
        validate_by_alias=True,
    )


# --- Server and stream ---------------------------------------------------------------------------


class Server(_Model):
    """The MCP server the recipe was written for. A server that reports another name or version
    has drifted: the executor stops on another name and warns on another version."""

    name: Text
    """The name the server reports when a session starts (``serverInfo.name``)."""
    versions: list[Annotated[str, Field(min_length=1, max_length=128)]] = []
    """Versions (``serverInfo.version``) the recipe was checked against; ``*`` matches any
    characters, as in ``1.4.*``. Empty: any version."""


class Stream(_Model):
    """What one stream of a source is and what its records become."""

    kind: IngestKind
    """The kind of record the parser makes of the stream's items."""
    key_description: Text
    """What a stream key is, for the person who configures a source: "a channel id, such as
    C0001", "a mailbox search query"."""
    key_pattern: Selector | None = None
    """A regular expression a stream key must match in full."""
    snapshot: bool = False
    """Whether a complete listing of a window holds every live item of the window, so an item
    left out has been deleted at the source (an ingest snapshot)."""


class Window(_Model):
    """How the planner cuts a stream's history into windows ``[from, to)``. A source may set its
    own values; these are the defaults for the server."""

    overlap: Duration
    """How far behind the checkpoint a window starts, to catch late replies and edits."""
    initial: Duration
    """How much recent history the first sync of a stream loads."""
    backfill: Duration | None = None
    """How much older history each later run loads, until the source's retention start. Left
    out: no history beyond the initial window."""
    max_span: Duration | None = None
    """The longest window one listing covers; a longer window is split. Left out: no limit."""


# --- Paging --------------------------------------------------------------------------------------


class CursorPaging(_Model):
    """The response names the next page by a cursor, which the next call sends back."""

    mode: Literal["cursor"]
    argument: ArgumentName
    """The argument that carries the cursor; left out of the first call."""
    next: Selector
    """Where the next cursor is. Missing, null or empty: the last page."""
    has_more: Selector | None = None
    """Where the response says whether more pages follow. When set, a page that says no is the
    last, and a page that says yes without a cursor fails the run's completeness."""
    max_pages: MaxPages = 1000
    """Pages one listing may take; a listing that reaches it is incomplete."""


class PagePaging(_Model):
    """Pages are numbered; the next call asks for the next number."""

    mode: Literal["page"]
    argument: ArgumentName
    """The argument that carries the page number."""
    first: Annotated[int, Field(ge=0, le=1)] = 1
    """The number of the first page: 1 or 0."""
    size_argument: ArgumentName | None = None
    """The argument that carries the page size; needs ``size``."""
    size: Annotated[int, Field(ge=1, le=10_000)] | None = None
    """Items per page. A page with fewer is the last."""
    number: Selector | None = None
    """Where the response states its page number (for ``checks.page_numbers``)."""
    total_pages: Selector | None = None
    """Where the response states the number of pages. The page with that number is the last."""
    total: Selector | None = None
    """Where the response states the number of items of the listing (for ``checks.totals``)."""
    has_more: Selector | None = None
    """Where the response says whether more pages follow."""
    max_pages: MaxPages = 1000


class OffsetPaging(_Model):
    """The next call skips the items seen so far."""

    mode: Literal["offset"]
    argument: ArgumentName
    """The argument that carries the number of items to skip."""
    start: Annotated[int, Field(ge=0, le=1)] = 0
    """The offset of the first item: 0 or 1."""
    size_argument: ArgumentName | None = None
    """The argument that carries the page size; needs ``size``."""
    size: Annotated[int, Field(ge=1, le=10_000)] | None = None
    """Items per page. A page with fewer is the last."""
    total: Selector | None = None
    """Where the response states the number of items of the listing; the listing ends there."""
    has_more: Selector | None = None
    """Where the response says whether more pages follow."""
    max_pages: MaxPages = 1000


class NoPaging(_Model):
    """One call returns the whole listing."""

    mode: Literal["none"]


PAGING_MODES: Final = ("cursor", "page", "offset", "none")
"""The values of ``mode`` in a paging rule, one per model above."""

Paging = Annotated[CursorPaging | PagePaging | OffsetPaging | NoPaging, Field(discriminator="mode")]


# --- Calls ---------------------------------------------------------------------------------------


class ListItem(_Model):
    """Where the values of one listed item are, relative to the item (``@`` paths, or patterns
    over the item's text)."""

    id: Selector
    """The item's id at the server: what a detail call asks for."""
    timestamp: Selector | None = None
    """The item's time, read in ``timestamp_format``. Needed for an ordered listing."""
    timestamp_format: TimestampFormat = "rfc3339"
    """rfc3339, unix, unix_ms, or a strptime pattern (%Y %m %d %H %M %S %f %z)."""
    timezone: TimeZone | None = None
    """The IANA zone of a timestamp that carries none; UTC when not set."""
    reply_count: Selector | None = None
    """The number of replies the listing announces for the item."""
    fields: dict[FieldName, Selector] = {}
    """Other values a detail call needs, by name: ``{item.NAME}`` in its arguments."""


class ListCall(_Model):
    """The call that lists a stream's items in a window, page by page."""

    tool: ToolName
    """The MCP tool."""
    arguments: dict[ArgumentName, JsonValue] = {}
    """The tool's arguments, with placeholders (:mod:`memini.recipes.templates`)."""
    decode: Decode = "json"
    """How the result is read: json, structured or text."""
    items: Selector
    """Where the items are: a path with ``*``, or a pattern that matches each item."""
    order: Order = "any"
    """The order of the items. With ``newest_first`` a listing stops after the first page that
    reaches past the window's start; with ``oldest_first``, after one that reaches its end.
    ``any``: every page is read."""
    item: ListItem
    """Where each item's values are."""
    paging: Paging
    """How the next page is asked for and when the listing ends."""


class DetailCall(_Model):
    """The call made for each listed item: the replies of a thread, the body of a meeting."""

    tool: ToolName
    """The MCP tool."""
    arguments: dict[ArgumentName, JsonValue] = {}
    """The tool's arguments; they name the item with ``{item.id}`` or another ``{item.NAME}``."""
    decode: Decode = "json"
    """How the result is read: json, structured or text."""
    when: Literal["always", "has_replies"] = "always"
    """For which items: every item, or those whose ``reply_count`` is above zero."""
    items: Selector | None = None
    """Where the replies are, to count them (``checks.reply_counts``): a path with ``*``, or a
    pattern that matches each reply. Counted over every page of the detail call."""
    includes_root: bool = False
    """Whether ``items`` holds the item itself besides its replies; it is then not counted."""
    paging: Paging = NoPaging(mode="none")
    """How the detail call pages, if it does; one call by default."""


# --- Checks, rate, parser ------------------------------------------------------------------------


class Checks(_Model):
    """Completeness checks a window must pass before its checkpoint moves, besides those every
    recipe gets: each call succeeded, each listing reached its stop condition, each cursor was
    followed and each listed item got its detail call."""

    page_numbers: bool = False
    """Page numbers run from ``first`` without a gap or a repeat, and end at ``total_pages``
    when the response states it. Needs page paging with ``number``."""
    reply_counts: bool = False
    """The ``items`` a detail call returns, less the item itself with ``includes_root``, are as
    many as the ``reply_count`` of the listed item. Needs ``list.item.reply_count`` and
    ``detail.items``. A server whose count includes the item (``3 messages``) leaves
    ``includes_root`` out, so both sides count it."""
    totals: bool = False
    """The items of a listing equal the ``total`` its responses state. Needs ``total``."""


class Rate(_Model):
    """The budget of calls to the server."""

    calls_per_minute: Annotated[int, Field(ge=1, le=6000)]
    """At most this many calls in any minute."""
    concurrency: Annotated[int, Field(ge=1, le=16)] = 1
    """Calls in flight at once."""
    max_calls: Annotated[int, Field(ge=1, le=1_000_000)] = 5000
    """Calls one run may make; a run that reaches it is incomplete."""
    timeout: Duration = timedelta(seconds=60)
    """How long one call may take."""


class Parser(_Model):
    """The deterministic parser that turns the raw responses of a window into records."""

    name: ParserName
    """A parser registered in the service, such as ``hearth.thread``."""
    options: dict[Annotated[str, Field(min_length=1, max_length=128)], JsonValue] = {}
    """Settings the parser reads, such as a URL pattern for records; it checks them."""


# --- The recipe ----------------------------------------------------------------------------------


class Recipe(_Model):
    """How to sync one MCP server: a declarative file per server (docs/recipes.md)."""

    model_config = ConfigDict(title="Memini MCP recipe")

    format: Literal[1]
    """The version of the recipe format: 1."""
    name: Handle
    """The recipe's name: lowercase letters, digits, '-' and '_'."""
    description: Annotated[str, Field(max_length=2000)] = ""
    server: Server
    stream: Stream
    window: Window
    listing: ListCall = Field(alias="list")
    """The list call: ``[list]``."""
    detail: DetailCall | None = None
    """The call per listed item, if the listing alone does not hold what the parser needs."""
    checks: Checks = Checks()
    rate: Rate
    parser: Parser

    @property
    def tools(self) -> tuple[str, ...]:
        """The tools the recipe calls, in order, without repeats."""
        names = [self.listing.tool] + ([self.detail.tool] if self.detail else [])
        return tuple(dict.fromkeys(names))


def recipe_json_schema() -> dict[str, Any]:
    """The JSON Schema of a recipe file, as committed in ``schemas/recipe.schema.json``.

    It checks the structure; the rules that span keys are checked by
    :func:`memini.recipes.validate.check_recipe` alone. TOML has no null, so a key that may be
    left out is not offered null either: absent is how a recipe says "none".
    """
    schema = cast(dict[str, Any], _without_null(Recipe.model_json_schema(mode="validation")))
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_ID,
        **schema,
    }


def _without_null(node: Any) -> Any:
    """``node`` with each ``anyOf: [X, null]`` replaced by X and each ``default: null`` dropped."""
    if isinstance(node, list):
        return [_without_null(item) for item in cast(list[Any], node)]
    if not isinstance(node, dict):
        return node
    mapping = cast(dict[str, Any], node)
    result: dict[str, Any] = {}
    for key, value in mapping.items():
        if key == "default" and value is None:
            continue
        if key == "anyOf":
            branches = [b for b in cast(list[Any], value) if b != {"type": "null"}]
            if len(branches) == 1:
                result.update(cast(dict[str, Any], _without_null(branches[0])))
                continue
            value = branches
        result[key] = _without_null(value)
    return result
