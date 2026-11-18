"""The parser of a recipe (``[parser]``, docs/recipes.md): a deterministic function from the raw
responses of one window to records, and the registry that finds it by name.

This is the minimal protocol the raw response store and replay need (:mod:`memini.responses`);
the completeness checks every recipe gets (each listing reached its last page, each cursor was
followed, each listed item got its detail call, the declared page, reply and total checks) are
added in :func:`parse_window` by memini-n74, which keeps the protocol below. Both executors and
replay run a window through :func:`parse_window` and nothing else, so the same responses always
give the same records (principle 5: no model, no clock, no network in a parser).

**Input** (:class:`WindowInput`): the recipe, the stream key, the window ``[start, end)``, and the
window's stored responses in the order the calls were made (:class:`RawResponse`). A response
carries its call (``list`` or ``detail``, the tool, the arguments as sent, the page, the listed
item a detail call was for) and the tool result as stored: the MCP result object (``content``
blocks, ``structuredContent``, ``isError``) with secrets and card numbers redacted, otherwise as
the server returned it. :meth:`RawResponse.decode` reads it by the recipe's ``decode``.
A response whose body was removed by forget is not in the input.

**Output** (:class:`ParsedWindow`): the records of the stream's kind (:class:`ParsedRecord`: an
ingest :class:`~memini.ingest.RecordIn` and the positions of the responses it was built from) and
whether the window is complete, with the reasons when it is not. The positions are how forget
finds the stored responses that hold a record's content (:mod:`memini.responses`), so a parser
names every response it read for a record: the list page that holds its item and each page of its
detail call.

**Registry.** :func:`register_parser` binds a name (the recipe's ``parser.name``, such as
``hearth.thread``) to a parser; :func:`get_parser` finds it. The parsers of the shipped recipes
register themselves when their module is imported (memini-n74 and the recipe issues add them).
"""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal, Protocol, cast

from memini.ingest.model import RecordIn
from memini.recipes.model import Decode, Recipe

__all__ = [
    "CallKind",
    "ParsedRecord",
    "ParsedWindow",
    "ParserError",
    "ParserNotFoundError",
    "RawResponse",
    "ResponseDecodeError",
    "WindowInput",
    "WindowParser",
    "get_parser",
    "parse_window",
    "register_parser",
    "registered_parsers",
    "unregister_parser",
]

CallKind = Literal["list", "detail"]
"""Which call of the recipe a response answers: the list call or the detail call of an item."""

TEXT_BLOCK: Final = "text"


class ResponseDecodeError(ValueError):
    """A stored result cannot be read as the recipe's ``decode`` asks. The message never holds a
    value of the result."""


@dataclass(frozen=True, slots=True)
class RawResponse:
    """One call of a window and its result, as stored (:mod:`memini.responses`)."""

    position: int
    """The call's place in the window, from 1, in the order the calls were made."""
    call: CallKind
    tool: str
    arguments: Mapping[str, Any]
    """The arguments as sent, with secrets redacted."""
    page: int
    """The page's place in its listing (``list``) or in its item's detail call, from 0."""
    item_id: str | None
    """For a detail call, the id of the listed item it was made for."""
    result: Mapping[str, Any]
    """The MCP tool result: ``content`` blocks, ``structuredContent`` and ``isError``, with
    secrets and card numbers redacted, otherwise as the server returned it."""

    @property
    def is_error(self) -> bool:
        """Whether the server reported the call as failed (``isError``)."""
        return self.result.get("isError") is True

    def text(self) -> str:
        """The text of the result's text content blocks, joined by newlines."""
        blocks = self.result.get("content")
        if not isinstance(blocks, list):
            return ""
        texts: list[str] = []
        for block in cast(list[Any], blocks):
            if isinstance(block, Mapping):
                entry = cast(Mapping[str, Any], block)
                text = entry.get("text")
                if entry.get("type") == TEXT_BLOCK and isinstance(text, str):
                    texts.append(text)
        return "\n".join(texts)

    def decode(self, decode: Decode) -> Any:
        """The result read as a recipe's call reads it (docs/recipes.md, Decoding): ``json``, the
        text parsed as JSON; ``structured``, the ``structuredContent`` object; ``text``, the text.

        Raises:
            ResponseDecodeError: the text is not JSON, or there is no ``structuredContent``.
        """
        match decode:
            case "text":
                return self.text()
            case "structured":
                structured = self.result.get("structuredContent")
                if not isinstance(structured, Mapping):
                    raise ResponseDecodeError(
                        f"response {self.position}: the result has no structuredContent object"
                    )
                return cast(Mapping[str, Any], structured)
            case "json":
                try:
                    return json.loads(self.text())
                except (ValueError, RecursionError):
                    raise ResponseDecodeError(
                        f"response {self.position}: the text of the result is not JSON"
                    ) from None


@dataclass(frozen=True, slots=True)
class WindowInput:
    """What a parser reads: the stored responses of one window of one stream."""

    recipe: Recipe
    stream: str
    """The stream key: ``{stream}`` in the recipe's arguments and URL patterns."""
    start: datetime
    end: datetime
    responses: tuple[RawResponse, ...]
    """In the order the calls were made. Parsers find a call by its identity (tool, arguments,
    item), not by its position alone: with ``rate.concurrency`` above 1 the detail calls of a
    window may come back in any order."""


@dataclass(frozen=True, slots=True)
class ParsedRecord:
    """A record a parser built, and the responses it read for it."""

    record: RecordIn
    """A record of the recipe's ``stream.kind``, or a tombstone of that kind."""
    responses: tuple[int, ...]
    """The positions of every response that holds content of the record: forget removes them
    with the record. At least one."""


@dataclass(frozen=True, slots=True)
class ParsedWindow:
    """What a parser returns for a window."""

    records: tuple[ParsedRecord, ...] = ()
    complete: bool = False
    """Whether every check passed, so a claim of the window may move the stream's checkpoint."""
    incomplete: tuple[str, ...] = ()
    """Why the window is not complete, for the run report: the call that failed, the page or
    the detail call that is missing. Never a value of a response."""


class WindowParser(Protocol):
    """A recipe's parser: pure and deterministic; the same input always gives the same output."""

    def __call__(self, window: WindowInput, /) -> ParsedWindow: ...


class ParserError(Exception):
    """A parser failed on a window, or returned what the protocol does not allow. The message
    names the parser and the problem, never a value of a response."""


class ParserNotFoundError(LookupError):
    """No parser is registered under the name a recipe gives."""

    def __init__(self, name: str) -> None:
        super().__init__(f"no parser named {name} in this build")
        self.name = name


_PARSERS: dict[str, WindowParser] = {}


def register_parser(name: str, parser: WindowParser, *, replace: bool = False) -> None:
    """Make ``parser`` the parser called ``name`` (a recipe's ``parser.name``).

    Raises:
        ValueError: ``name`` is registered already and ``replace`` is not set.
    """
    if name in _PARSERS and not replace:
        raise ValueError(f"a parser named {name} is registered already")
    _PARSERS[name] = parser


def unregister_parser(name: str) -> None:
    """Forget the parser called ``name``, if there is one."""
    _PARSERS.pop(name, None)


def get_parser(name: str) -> WindowParser:
    """The parser called ``name``.

    Raises:
        ParserNotFoundError: none is registered under that name.
    """
    try:
        return _PARSERS[name]
    except KeyError:
        raise ParserNotFoundError(name) from None


def registered_parsers() -> tuple[str, ...]:
    """The names of the registered parsers, sorted."""
    return tuple(sorted(_PARSERS))


# --- Running a parser ----------------------------------------------------------------------------


def _generic_checks(window: WindowInput) -> list[str]:
    """The checks every recipe gets that need no selector. memini-n74 adds the others (pages,
    cursors, detail calls, and the declared ``[checks]``) here."""
    return [
        f"call {response.position} ({response.tool}) failed"
        for response in window.responses
        if response.is_error
    ]


def _check_output(name: str, window: WindowInput, parsed: object) -> ParsedWindow:
    if not isinstance(parsed, ParsedWindow):
        raise ParserError(f"the parser {name} returned no ParsedWindow")
    kind = window.recipe.stream.kind
    positions = {response.position for response in window.responses}
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(parsed.records):
        where = f"the parser {name}, record {index}"
        if not isinstance(item, ParsedRecord):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise ParserError(f"{where}: not a ParsedRecord")
        record = item.record
        if record.kind != kind:
            raise ParserError(f"{where}: of kind {record.kind}; the recipe's streams hold {kind}")
        key = (record.kind, record.external_id)
        if key in seen:
            raise ParserError(f"{where}: its external id is that of an earlier record")
        seen.add(key)
        if not item.responses:
            raise ParserError(f"{where}: names no response it was built from")
        if not set(item.responses) <= positions:
            raise ParserError(f"{where}: names a response that is not in the window")
    return parsed


def parse_window(
    parser: WindowParser | Callable[[WindowInput], ParsedWindow],
    window: WindowInput,
    *,
    name: str | None = None,
) -> ParsedWindow:
    """Run ``parser`` over ``window`` and check what it returns.

    The window is complete only when the parser says so and every generic check passes (a call
    that failed makes it incomplete). ``name`` is the parser's name for messages; the recipe's
    ``parser.name`` by default.

    Raises:
        ParserError: the parser raised, or its output breaks the protocol: a record of another
            kind, an external id twice, a record that names no response or one not in the
            window.
    """
    label = name or window.recipe.parser.name
    try:
        parsed = parser(window)
    except Exception as error:
        raise ParserError(
            f"the parser {label} failed: {type(error).__name__}"
            + (f": {error}" if isinstance(error, ResponseDecodeError) else "")
        ) from error
    parsed = _check_output(label, window, parsed)
    failures = _generic_checks(window)
    if not failures:
        return parsed
    return ParsedWindow(
        records=parsed.records,
        complete=False,
        incomplete=(*parsed.incomplete, *failures),
    )
