"""The parser of a recipe (``[parser]``, docs/recipes.md): a deterministic function from the raw
responses of one window to records, the completeness checks every recipe gets, and the registry
that finds a parser by name.

Both executors and replay run a window through :func:`parse_window` and nothing else, so the same
responses always give the same records and the same checks (principle 5: no model, no clock, no
network in a parser).

**Input** (:class:`WindowInput`): the recipe, the stream key, the window ``[start, end)``, and the
window's stored responses in the order the calls were made (:class:`RawResponse`). A response
carries its call (``list`` or ``detail``, the tool, the arguments as sent, the page, the listed
item a detail call was for) and the tool result as stored: the MCP result object (``content``
blocks, ``structuredContent``, ``isError``) with secrets and card numbers redacted, otherwise as
the server returned it. :meth:`RawResponse.decode` reads it by the recipe's ``decode``.
A response whose body was removed by forget is not in the input.

**Output** (:class:`ParsedWindow`): the records of the stream's kind (:class:`ParsedRecord`: an
ingest :class:`~memini.ingest.RecordIn` and the positions of the responses it was built from),
whether the window is complete, the reasons when it is not, and the calls that would complete it
(:class:`~memini.recipes.calls.MissingCall`). The positions are how forget finds the stored
responses that hold a record's content (:mod:`memini.responses`), so a parser names every
response it read for a record: the list page that holds its item and each page of its detail
call.

**Two kinds of parser.** A parser is registered as a :class:`WindowParser`, a function of the
whole window. Most are written as an :class:`ItemParser` and wrapped with :func:`item_parser`:
the recipe's selectors read the window (:func:`memini.recipes.reading.read_window`: the listed
items, the values the list call read, the decoded pages of each item's detail call), and the
item parser knows only the payload: it turns one :class:`~memini.recipes.reading.Item` into one
record of the stream's kind, or none. The wrapper names the item's responses for each record, so
an item parser cannot forget one. An item whose responses do not hold all of it yet (a meeting
whose transcript is still being made) is an :class:`IncompleteItem`: the window stays incomplete
and the call that holds the item is named again.

**Checks.** :func:`parse_window` reads the window with the recipe whatever the kind of parser,
and adds the checks every recipe gets and those it declares (:mod:`memini.recipes.reading`): each
call succeeded, each listing reached its last page and each cursor was followed, each listed item
got its detail call, page numbers, reply counts and totals. The window is complete only when the
parser and every check say so.

**Registry.** :func:`register_parser` binds a name (the recipe's ``parser.name``, such as
``hearth.thread``) to a parser; :func:`get_parser` finds it. The parsers of the shipped recipes
register themselves when their module is imported (the recipe issues add them).
"""

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from memini.ingest.model import RecordIn
from memini.recipes.calls import (
    CallKind,
    MissingCall,
    RawResponse,
    ResponseDecodeError,
    WindowInput,
)
from memini.recipes.reading import Item, WindowReading, read_window, repeat_call

__all__ = [
    "CallKind",
    "IncompleteItem",
    "ItemParser",
    "ParsedRecord",
    "ParsedWindow",
    "ParserError",
    "ParserNotFoundError",
    "RawResponse",
    "RecipeParser",
    "ResponseDecodeError",
    "WindowInput",
    "WindowParser",
    "get_parser",
    "item_parser",
    "parse_window",
    "register_parser",
    "registered_parsers",
    "unregister_parser",
]


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
    missing: tuple[MissingCall, ...] = ()
    """The calls that would complete the window, each once: what the executors make next and
    the run report lists. Empty for a complete window; an incomplete one may name none (a
    parser's own reason that no call fixes)."""


class WindowParser(Protocol):
    """A recipe's parser: pure and deterministic; the same input always gives the same output."""

    def __call__(self, window: WindowInput, /) -> ParsedWindow: ...


@dataclass(frozen=True, slots=True)
class IncompleteItem:
    """What an item parser returns for an item its responses do not hold all of yet: the
    window stays incomplete, and the call that holds the item is named again."""

    reason: str
    """Why, for the run report: never a value of a response."""
    record: RecordIn | None = None
    """What the item gave so far, stored as an incomplete window stores what it got."""


class ItemParser(Protocol):
    """The part of a parser that knows the payload: one item of a window (its listing, its
    detail pages) to one record of the recipe's ``stream.kind``. ``None`` leaves the item out.
    ``window`` gives the recipe (``parser.options``), the stream key and the window."""

    def __call__(self, item: Item, window: WindowInput, /) -> RecordIn | IncompleteItem | None: ...


class ParserError(Exception):
    """A parser failed on a window, or returned what the protocol does not allow. The message
    names the parser and the problem, never a value of a response."""


class ParserNotFoundError(LookupError):
    """No parser is registered under the name a recipe gives."""

    def __init__(self, name: str) -> None:
        super().__init__(f"no parser named {name} in this build")
        self.name = name


class RecipeParser:
    """A :class:`WindowParser` made of an :class:`ItemParser` (:func:`item_parser`): the
    recipe's selectors read the window, the item parser builds one record per item."""

    __slots__ = ("parse",)

    def __init__(self, parse: ItemParser) -> None:
        self.parse = parse

    def __call__(self, window: WindowInput, /) -> ParsedWindow:
        """The records of ``window`` and what its items lack; :func:`parse_window` adds the
        checks of the recipe."""
        return self.parse_reading(window, read_window(window))

    def parse_reading(self, window: WindowInput, reading: WindowReading) -> ParsedWindow:
        """As calling it, from a reading of the window made already."""
        responses = {response.position: response for response in window.responses}
        records: list[ParsedRecord] = []
        incomplete: list[str] = []
        missing: list[MissingCall] = []
        for item in reading.items:
            found = self.parse(item, window)
            if isinstance(found, IncompleteItem):
                reason = f"{item.label}: {found.reason}"
                incomplete.append(reason)
                holder = item.details[0].response if item.details else responses[item.responses[0]]
                missing.append(repeat_call(holder, "item", reason))
                found = found.record
            if found is not None:
                records.append(ParsedRecord(record=found, responses=item.responses))
        return ParsedWindow(
            records=tuple(records),
            complete=not incomplete,
            incomplete=tuple(incomplete),
            missing=tuple(missing),
        )


def item_parser(parse: ItemParser) -> RecipeParser:
    """``parse`` as a window parser, to register with :func:`register_parser`."""
    return RecipeParser(parse)


# --- The registry --------------------------------------------------------------------------------

_PARSERS: dict[str, WindowParser] = {}


def register_parser(name: str, parser: WindowParser, *, replace: bool = False) -> None:
    """Make ``parser`` the parser called ``name`` (a recipe's ``parser.name``). An item parser is
    registered wrapped: ``register_parser(name, item_parser(parse))``.

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
    for call in parsed.missing:
        if not isinstance(call, MissingCall):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise ParserError(f"the parser {name}: a missing call that is not a MissingCall")
    return parsed


def _call_identity(call: MissingCall) -> tuple[str, str, str]:
    arguments = json.dumps(
        call.arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return (call.call, call.tool, arguments)


def _once(calls: Iterable[MissingCall]) -> tuple[MissingCall, ...]:
    """Each call once, the first naming of it kept."""
    found: dict[tuple[str, str, str], MissingCall] = {}
    for call in calls:
        found.setdefault(_call_identity(call), call)
    return tuple(found.values())


def _failed(label: str, error: Exception) -> ParserError:
    return ParserError(
        f"the parser {label} failed: {type(error).__name__}"
        + (f": {error}" if isinstance(error, ResponseDecodeError) else "")
    )


def parse_window(
    parser: WindowParser | Callable[[WindowInput], ParsedWindow],
    window: WindowInput,
    *,
    name: str | None = None,
) -> ParsedWindow:
    """Run ``parser`` over ``window``, check what it returns, and add the recipe's checks.

    The window is read with its recipe (:func:`memini.recipes.reading.read_window`); a
    :class:`RecipeParser` builds its records from that reading, any other parser from the
    window. The window is complete only when the parser says so and every check passes; the
    reasons and the missing calls are the checks' first, then the parser's, each once. ``name``
    is the parser's name for messages; the recipe's ``parser.name`` by default.

    Raises:
        ParserError: the parser raised, or its output breaks the protocol: a record of another
            kind, an external id twice, a record that names no response or one not in the
            window.
    """
    label = name or window.recipe.parser.name
    try:
        reading = read_window(window)
    except Exception as error:  # a recipe the checks of memini.recipes.validate did not pass
        raise ParserError(
            f"the recipe {window.recipe.name} cannot read the window: {type(error).__name__}"
        ) from error
    parsed: Any
    try:
        if isinstance(parser, RecipeParser):
            parsed = parser.parse_reading(window, reading)
        else:
            parsed = parser(window)
    except Exception as error:
        raise _failed(label, error) from error
    parsed = _check_output(label, window, parsed)
    if reading.complete and not parsed.missing:
        return parsed
    return ParsedWindow(
        records=parsed.records,
        complete=parsed.complete and reading.complete,
        incomplete=tuple(dict.fromkeys((*reading.incomplete, *parsed.incomplete))),
        missing=_once((*reading.missing, *parsed.missing)),
    )
