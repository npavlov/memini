"""Reading a window's stored calls with its recipe: the listed items, the detail call of each, and
the completeness checks every recipe gets (docs/recipes.md, ``[checks]``).

:func:`read_window` takes a :class:`~memini.recipes.calls.WindowInput` (the recipe, the stream,
the window and its stored calls) and returns a :class:`WindowReading`: the window's items, each
with the values the list call read and the pages of its detail call, decoded; whether every check
passed; and, for each check that failed, the call that would make it pass
(:class:`~memini.recipes.calls.MissingCall`). It reads values with the recipe's selectors only, no
model and no clock (principle 5), so the same calls always give the same reading.

**How a window is read.**

1. Every call that failed (``isError``), or whose result ``decode`` cannot read, fails the window;
   the same call again is missing.
2. Every list page gives its items, read with ``list.items`` and ``list.item``. An item without
   an id, or with a time or a reply count that cannot be read, fails the window; its page again
   is missing. An item listed twice (pages that overlap) is one item, held by both pages.
3. The listing is walked by its paging rule from its first call: the first call has the recipe's
   arguments rendered for the stream and the window, and each next call the paging argument the
   rule gives (the cursor a page names, the next page number, the items seen so far). A call is
   found by its identity, as the store keeps it: the call kind, the tool and the arguments. The
   walk ends at the last page by the rule; a call it needs that is not in the window is missing,
   and so is the next page after ``max_pages``. A cursor page that says more follow
   (``has_more``) but names no cursor is truncated: the same call again is missing.
4. A list call whose arguments hold no ``{from}`` and no ``{to}`` lists everything: an ordered
   listing ends after the first page that reaches past the window (``newest_first``: an item
   older than its start; ``oldest_first``: one at or after its end), and only the items inside
   the window are the window's. A windowed listing keeps every item it returns.
5. Each item that ``detail.when`` selects needs its detail call, walked the same way from the
   arguments rendered for the item; ``reply_counts`` compares its replies with the listed count.
   A detail call without ``detail.items`` has no count of items per page: its pages end by
   ``total_pages``, ``has_more`` or ``total`` alone, or after the first.
6. The declared checks apply to each walk: ``page_numbers``, that each page states the number it
   was asked for, and that the walk reads on to the stated count of pages even when a short page
   would end it; ``totals``, that the items are as many as the last stated total.

Detail calls the walks did not reach (a page of an item whose list page forget removed) still
belong to their item: an item may have detail pages and no listing. Calls of other tools are not
read.

**Missing calls.** The executors complete a window with :func:`missing_calls`: the service
executor makes them, the agent executor hands them out as its next calls. A window with no calls
yet misses the first call of its listing; once that is stored, its next page or the detail calls
of its items, until nothing is missing. A missing call with ``repeat`` set is already in the
window (a failed call, a truncated page, a detail call with fewer replies than listed): making it
again is a retry, within the recipe's budget. A ``max_pages`` call is past the recipe's cap: the
window stays incomplete.
"""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Final, cast

from memini.recipes.calls import (
    CallKind,
    Check,
    MissingCall,
    RawResponse,
    ResponseDecodeError,
    WindowInput,
)
from memini.recipes.model import (
    CursorPaging,
    Decode,
    NoPaging,
    OffsetPaging,
    PagePaging,
    Recipe,
)
from memini.recipes.selectors import JsonPath, TextPattern, parse_path, parse_pattern
from memini.recipes.templates import placeholders, render_arguments
from memini.recipes.times import parse_timestamp
from memini.redaction import RedactionCollisionError, redact_json, redact_text

__all__ = [
    "DetailPage",
    "Item",
    "ListedItem",
    "WindowReading",
    "item_key",
    "missing_calls",
    "read_window",
    "repeat_call",
]

type Paging = CursorPaging | PagePaging | OffsetPaging | NoPaging

_WINDOW_PLACEHOLDERS: Final = frozenset({"from", "to"})
_MAX_WHOLE: Final = 10**15
_TRUE: Final = frozenset({"true", "yes", "1"})
_FALSE: Final = frozenset({"false", "no", "0", ""})


# --- What a reading holds ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ListedItem:
    """An item of the listing and the values the list call read for it."""

    key: str
    """The id as text (:func:`item_key`): what its detail call's ``item_id`` holds."""
    id: Any
    """The id as the response held it."""
    value: Any
    """The item: the value ``list.items`` selected, or the text of the match with ``text``."""
    timestamp: datetime | None
    """The item's time, read in ``timestamp_format``; ``None`` when not stated."""
    reply_count: int | None
    """The replies the listing announces; ``None`` when not stated."""
    fields: Mapping[str, Any]
    """The values of ``list.item.fields`` the item states, by name."""
    values: Mapping[str, Any]
    """Every value the list call read, as the response held it, by the name ``{item.NAME}``
    renders: ``id``, ``timestamp``, ``reply_count`` and the fields."""
    positions: tuple[int, ...]
    """The list pages that hold it, by position: one, or more when pages overlapped."""
    index: int = 0
    """Its place among the items of the first page that holds it, from 0."""

    @property
    def label(self) -> str:
        """The item in a message, by its place: "item 2 of call 1"."""
        return f"item {self.index + 1} of call {self.positions[0]}"


@dataclass(frozen=True, slots=True)
class DetailPage:
    """One page of an item's detail call."""

    response: RawResponse
    document: Any
    """The result as ``detail.decode`` reads it."""
    items: tuple[Any, ...]
    """What ``detail.items`` selects in it; empty without ``detail.items``."""


@dataclass(frozen=True, slots=True)
class Item:
    """One item of the window: its listing, its detail pages, or both."""

    key: str
    listed: ListedItem | None
    """``None`` when only its detail call is in the window (forget removed its list page)."""
    details: tuple[DetailPage, ...]
    """Its detail pages, in page order."""
    replies: tuple[Any, ...]
    """The ``detail.items`` of its pages, in order, less the item itself with
    ``includes_root``: what ``reply_counts`` counts."""

    @property
    def detail_items(self) -> tuple[Any, ...]:
        """The ``detail.items`` of every page, in order, the item itself included."""
        return tuple(value for page in self.details for value in page.items)

    @property
    def responses(self) -> tuple[int, ...]:
        """The positions of every response that holds content of the item: its list pages and
        its detail pages. Forget removes them with the record built from it."""
        found = set(self.listed.positions if self.listed else ())
        found.update(page.response.position for page in self.details)
        return tuple(sorted(found))

    @property
    def label(self) -> str:
        """The item in a message: by the position of the call that lists or fetched it."""
        if self.listed is not None:
            return self.listed.label
        return f"the item of call {self.details[0].response.position}"


@dataclass(frozen=True, slots=True)
class WindowReading:
    """A window read with its recipe."""

    items: tuple[Item, ...]
    """Listed items in the order of the listing (each once), then items with only a detail
    call, by key."""
    incomplete: tuple[str, ...] = ()
    """Each check that failed: the call, the page or the item by its position, never a value."""
    missing: tuple[MissingCall, ...] = ()
    """The calls that would make the failed checks pass, each once, in the order found."""

    @property
    def complete(self) -> bool:
        """Whether every check passed."""
        return not self.incomplete


def item_key(value: Any) -> str:
    """The text of a listed item's id, the ``item_id`` a detail call is stored with: a string as
    it is, any other value as compact JSON (``42``)."""
    if isinstance(value, str):
        return value
    return _json(value)


def read_window(window: WindowInput) -> WindowReading:
    """Read the stored calls of ``window`` with its recipe: its items and its checks (see the
    module). Pure: the same input gives the same reading."""
    return _Reader(window).read()


def missing_calls(window: WindowInput) -> tuple[MissingCall, ...]:
    """The calls that would complete ``window``, each once; empty when every check passes. For
    a window with no calls yet, the first call of its listing."""
    return read_window(window).missing


def repeat_call(response: RawResponse, check: Check, reason: str) -> MissingCall:
    """``response``'s call made again: a retry."""
    return MissingCall(
        call=response.call,
        tool=response.tool,
        arguments=response.arguments,
        page=response.page,
        item_id=response.item_id,
        check=check,
        reason=reason,
        repeat=True,
    )


# --- Values --------------------------------------------------------------------------------------


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _identity(arguments: Mapping[str, Any]) -> str:
    return _json(arguments)


class _Unreadable(ValueError):
    """A value a selector found is not what its role needs."""


def _whole(value: Any) -> int | None:
    """A count or a number read from a response: a non-negative integer, as a number or as
    digits; ``None`` when not stated."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise _Unreadable
    if isinstance(value, int):
        number = value
    elif isinstance(value, float) and value.is_integer():
        number = int(value)
    elif isinstance(value, str) and value.strip().isdigit() and value.strip().isascii():
        number = int(value.strip())
    else:
        raise _Unreadable
    if number < 0 or number > _MAX_WHOLE:
        raise _Unreadable
    return number


def _flag(value: Any) -> bool | None:
    """Whether more pages follow, as a path reads it; ``None`` when not stated or not clear."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value != 0
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
    return None


def _cursor(value: Any) -> Any | None:
    """The next cursor: missing, null, empty or a boolean is none."""
    if value is None or isinstance(value, bool) or value == "" or value == [] or value == {}:
        return None
    return value


def _is_id(value: Any) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, str):
        return bool(value) and "\0" not in value
    return isinstance(value, int | float)


class _Selectors:
    """The selectors of one call, read in the language its ``decode`` sets."""

    def __init__(self, decode: Decode) -> None:
        self.text = decode == "text"
        self._compiled: dict[str, JsonPath | TextPattern] = {}

    def _get(self, selector: str) -> JsonPath | TextPattern:
        found = self._compiled.get(selector)
        if found is None:
            found = parse_pattern(selector) if self.text else parse_path(selector)
            self._compiled[selector] = found
        return found

    def many(self, selector: str, document: Any) -> list[Any]:
        compiled = self._get(selector)
        if isinstance(compiled, TextPattern):
            return cast(list[Any], compiled.items(document)) if isinstance(document, str) else []
        return compiled.select(document)

    def one(self, selector: str, document: Any) -> Any | None:
        compiled = self._get(selector)
        if isinstance(compiled, TextPattern):
            return compiled.value(document) if isinstance(document, str) else None
        return compiled.first(document)

    def flag(self, selector: str, document: Any) -> bool | None:
        compiled = self._get(selector)
        if isinstance(compiled, TextPattern):
            return compiled.matches(document) if isinstance(document, str) else None
        return _flag(compiled.first(document))


# --- The reader ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Page:
    response: RawResponse
    document: Any
    count: int | None
    """Items on the page; ``None`` when the call has no items selector."""


@dataclass(slots=True)
class _Walk:
    pages: list[_Page]
    ended: bool
    """Whether the walk reached the last page by the paging rule."""


@dataclass(frozen=True, slots=True)
class _Call:
    """A call to walk: the list call, or the detail call of one item."""

    call: CallKind
    tool: str
    paging: Paging
    base: Mapping[str, Any]
    selectors: _Selectors
    items: str | None
    item_id: str | None
    label: str
    """The call in a message: "the listing", "the detail call of item 2 of call 1"."""
    owner: str
    """What the call is for, in a message: "the listing", "item 2 of call 1"."""
    stop: Callable[[RawResponse], bool] | None = None
    """For an ordered listing that lists everything: whether a page reaches past the window."""


class _Reader:
    def __init__(self, window: WindowInput) -> None:
        self.window = window
        self.recipe: Recipe = window.recipe
        self.listing = self.recipe.listing
        self.detail = self.recipe.detail
        self.checks = self.recipe.checks
        self.list_selectors = _Selectors(self.listing.decode)
        self.detail_selectors = _Selectors(self.detail.decode if self.detail else "json")
        self.incomplete: list[str] = []
        self.missing: list[MissingCall] = []
        self._missing_ids: set[tuple[str, str, str]] = set()
        self.documents: dict[int, Any] = {}
        self.page_items: dict[int, list[ListedItem | None]] = {}
        self._by_item: dict[str, list[RawResponse]] | None = None
        self._index: dict[tuple[str, str, str], RawResponse] = {}
        for response in window.responses:
            self._index[(response.call, response.tool, _identity(response.arguments))] = response
        self.windowed = any(
            placeholder.name in _WINDOW_PLACEHOLDERS
            for _, placeholder in placeholders(self.listing.arguments)
        )

    # -- Findings

    def fail(self, reason: str, call: MissingCall) -> None:
        self.incomplete.append(reason)
        identity = (call.call, call.tool, _identity(call.arguments))
        if identity not in self._missing_ids:
            self._missing_ids.add(identity)
            self.missing.append(call)

    def need(
        self, spec: _Call, arguments: Mapping[str, Any], page: int, check: Check, reason: str
    ) -> None:
        """Fail the window for want of the call ``spec`` with ``arguments`` as page ``page``."""
        found = self.find(spec.call, spec.tool, arguments)
        self.fail(
            reason,
            MissingCall(
                call=spec.call,
                tool=spec.tool,
                arguments=arguments,
                page=page,
                item_id=spec.item_id,
                check=check,
                reason=reason,
                repeat=found is not None,
            ),
        )

    def find(self, call: CallKind, tool: str, arguments: Mapping[str, Any]) -> RawResponse | None:
        """The stored call with this identity. Stored arguments are redacted, so the arguments
        are looked up as they are and, failing that, redacted."""
        found = self._index.get((call, tool, _identity(arguments)))
        if found is not None:
            return found
        try:
            redacted = redact_json(arguments).value
        except RedactionCollisionError:
            return None
        return self._index.get((call, tool, _identity(redacted)))

    # -- The reading

    def read(self) -> WindowReading:
        self._decode_all()
        self._read_list_pages()
        list_call = _Call(
            call="list",
            tool=self.listing.tool,
            paging=self.listing.paging,
            base=render_arguments(
                self.listing.arguments,
                stream=self.window.stream,
                window=(self.window.start, self.window.end),
            ),
            selectors=self.list_selectors,
            items=self.listing.items,
            item_id=None,
            label="the listing",
            owner="the listing",
            stop=self._order_stop(),
        )
        self._walk(list_call)
        listed, outside = self._listed_items()
        items = [self._item(entry) for entry in listed]
        items.extend(self._detail_only(listed, outside))
        return WindowReading(
            items=tuple(items),
            incomplete=tuple(self.incomplete),
            missing=tuple(self.missing),
        )

    def _decode_all(self) -> None:
        """Step 1: every call succeeded and can be read."""
        for response in self.window.responses:
            if response.is_error:
                reason = f"call {response.position} ({response.tool}) failed"
                self.fail(reason, repeat_call(response, "failed", reason))
                continue
            decode = self._decode_of(response)
            if decode is None:
                continue
            try:
                self.documents[response.position] = response.decode(decode)
            except ResponseDecodeError:
                reason = (
                    f"call {response.position} ({response.tool}): the result cannot be read as"
                    f" {decode}"
                )
                self.fail(reason, repeat_call(response, "failed", reason))

    def _decode_of(self, response: RawResponse) -> Decode | None:
        if response.call == "list" and response.tool == self.listing.tool:
            return self.listing.decode
        if response.call == "detail" and self.detail and response.tool == self.detail.tool:
            return self.detail.decode
        return None

    # -- The listing

    def _list_responses(self) -> list[RawResponse]:
        found = [
            response
            for response in self.window.responses
            if response.call == "list" and response.position in self.documents
        ]
        return sorted(found, key=lambda response: (response.page, response.position))

    def _read_list_pages(self) -> None:
        """Step 2: the items of every list page."""
        for response in self._list_responses():
            values = self.list_selectors.many(self.listing.items, self.documents[response.position])
            self.page_items[response.position] = [
                self._read_item(response, index, value) for index, value in enumerate(values)
            ]

    def _read_item(self, response: RawResponse, index: int, value: Any) -> ListedItem | None:
        spec = self.listing.item
        selectors = self.list_selectors
        where = f"item {index + 1} of call {response.position}"
        raw_id = selectors.one(spec.id, value)
        if not _is_id(raw_id):
            self._bad_item(response, f"{where} has no id")
            return None
        values: dict[str, Any] = {"id": raw_id}
        timestamp: datetime | None = None
        if spec.timestamp is not None:
            raw_time = selectors.one(spec.timestamp, value)
            if raw_time is not None:
                try:
                    timestamp = parse_timestamp(raw_time, spec.timestamp_format, spec.timezone)
                except ValueError:
                    self._bad_item(response, f"{where}: its time cannot be read")
                    return None
                values["timestamp"] = raw_time
        reply_count: int | None = None
        if spec.reply_count is not None:
            raw_count = selectors.one(spec.reply_count, value)
            try:
                reply_count = _whole(raw_count)
            except _Unreadable:
                self._bad_item(response, f"{where}: its reply count is not a count")
                return None
            if raw_count is not None:
                values["reply_count"] = raw_count
        fields: dict[str, Any] = {}
        for name, selector in spec.fields.items():
            found = selectors.one(selector, value)
            if found is not None:
                fields[name] = found
                values[name] = found
        return ListedItem(
            key=item_key(raw_id),
            id=raw_id,
            value=value,
            timestamp=timestamp,
            reply_count=reply_count,
            fields=fields,
            values=values,
            positions=(response.position,),
            index=index,
        )

    def _bad_item(self, response: RawResponse, reason: str) -> None:
        self.fail(reason, repeat_call(response, "item", reason))

    def _order_stop(self) -> Callable[[RawResponse], bool] | None:
        """For an ordered listing without window arguments: whether a page reaches past the
        window, so that it is the last page."""
        order = self.listing.order
        if self.windowed or order == "any":
            return None
        start, end = self.window.start, self.window.end

        def past(response: RawResponse) -> bool:
            for item in self.page_items.get(response.position, []):
                if item is None or item.timestamp is None:
                    continue
                if order == "newest_first" and item.timestamp < start:
                    return True
                if order == "oldest_first" and item.timestamp >= end:
                    return True
            return False

        return past

    def _inside(self, item: ListedItem) -> bool:
        """Whether a listed item is the window's: every item of a windowed listing; of a listing
        that lists everything, those whose time is in the window or not stated."""
        if self.windowed or item.timestamp is None:
            return True
        return self.window.start <= item.timestamp < self.window.end

    def _listed_items(self) -> tuple[list[ListedItem], set[str]]:
        """The window's listed items, each once, in the order of the listing; and the keys of
        the items it listed outside the window."""
        merged: dict[str, ListedItem] = {}
        outside: set[str] = set()
        for response in self._list_responses():
            for item in self.page_items[response.position]:
                if item is None:
                    continue
                if not self._inside(item):
                    outside.add(item.key)
                    continue
                known = merged.get(item.key)
                if known is None:
                    merged[item.key] = item
                elif response.position not in known.positions:
                    merged[item.key] = replace(
                        known, positions=(*known.positions, response.position)
                    )
        return list(merged.values()), outside - set(merged)

    # -- Detail calls

    def _selected(self, item: ListedItem) -> bool:
        if self.detail is None:
            return False
        if self.detail.when == "has_replies":
            return (item.reply_count or 0) > 0
        return True

    def _item(self, listed: ListedItem) -> Item:
        walked: list[_Page] = []
        detail = self.detail
        if detail is not None and self._selected(listed):
            label = listed.label
            try:
                base = render_arguments(
                    detail.arguments,
                    stream=self.window.stream,
                    window=(self.window.start, self.window.end),
                    item=listed.values,
                )
            except (KeyError, ValueError):
                response = self._by_position(listed.positions[0])
                self._bad_item(response, f"{label}: a value its detail call needs is missing")
            else:
                spec = _Call(
                    call="detail",
                    tool=detail.tool,
                    paging=detail.paging,
                    base=base,
                    selectors=self.detail_selectors,
                    items=detail.items,
                    item_id=listed.key,
                    label=f"the detail call of {label}",
                    owner=label,
                )
                walk = self._walk(spec)
                walked = walk.pages
                if walk.ended:
                    self._check_replies(listed, walk)
        pages = [self._detail_page(page.response) for page in walked]
        seen = {page.response.position for page in pages}
        pages.extend(
            self._detail_page(response)
            for response in self._detail_responses(listed.key)
            if response.position not in seen
        )
        return self._assemble(listed.key, listed, pages)

    def _check_replies(self, listed: ListedItem, walk: _Walk) -> None:
        detail = self.detail
        if not self.checks.reply_counts or detail is None or detail.items is None:
            return
        expected = listed.reply_count
        if expected is None:
            return
        count = sum(page.count or 0 for page in walk.pages)
        if detail.includes_root and count:
            count -= 1
        if count == expected:
            return
        first = walk.pages[0].response
        holds = "fewer" if count < expected else "more"
        reason = f"{listed.label}: its detail call holds {holds} replies than the listing counts"
        self.fail(reason, repeat_call(first, "reply_counts", reason))

    def _detail_responses(self, key: str) -> list[RawResponse]:
        """The readable detail calls stored for the item ``key`` (stored redacted, if it was)."""
        if self._by_item is None:
            self._by_item = {}
            for response in self.window.responses:
                if (
                    response.call == "detail"
                    and response.item_id is not None
                    and response.position in self.documents
                ):
                    self._by_item.setdefault(response.item_id, []).append(response)
        found = list(self._by_item.get(key, ()))
        redacted = redact_text(key).value
        if redacted != key:
            found.extend(self._by_item.get(redacted, ()))
        return found

    def _detail_page(self, response: RawResponse) -> DetailPage:
        document = self.documents[response.position]
        detail = self.detail
        items: tuple[Any, ...] = ()
        if detail is not None and detail.items is not None:
            items = tuple(self.detail_selectors.many(detail.items, document))
        return DetailPage(response=response, document=document, items=items)

    def _assemble(self, key: str, listed: ListedItem | None, pages: list[DetailPage]) -> Item:
        pages.sort(key=lambda page: (page.response.page, page.response.position))
        found = tuple(value for page in pages for value in page.items)
        replies = found[1:] if self.detail is not None and self.detail.includes_root else found
        return Item(key=key, listed=listed, details=tuple(pages), replies=replies)

    def _detail_only(self, listed: list[ListedItem], outside: set[str]) -> list[Item]:
        """Items with detail pages and no listing in the window, by key."""
        known: set[str] = set()
        for item in listed:
            known.update((item.key, redact_text(item.key).value))
        for key in outside:
            known.update((key, redact_text(key).value))
        by_key: dict[str, list[DetailPage]] = {}
        for response in self.window.responses:
            if (
                response.call != "detail"
                or response.item_id is None
                or response.item_id in known
                or response.position not in self.documents
            ):
                continue
            by_key.setdefault(response.item_id, []).append(self._detail_page(response))
        return [self._assemble(key, None, by_key[key]) for key in sorted(by_key)]

    def _by_position(self, position: int) -> RawResponse:
        return next(r for r in self.window.responses if r.position == position)

    # -- Walking a call's pages

    def _walk(self, spec: _Call) -> _Walk:
        """Follow ``spec``'s pages by its paging rule from its first call (step 3)."""
        paging = spec.paging
        walk = _Walk(pages=[], ended=False)
        arguments = _paged(paging, spec.base, 0, 0)
        visited: set[str] = set()
        seen_items = 0
        total: int | None = None
        page = 0
        why: tuple[Check, str] | None = None  # a declared check asks for the next page
        while True:
            response = self.find(spec.call, spec.tool, arguments)
            if response is None:
                self.need(spec, arguments, page, *(why or self._absent(spec, walk)))
                return walk
            why = None
            if response.position not in self.documents:
                return walk  # failed or unreadable: step 1 named it
            visited.add(_identity(arguments))
            document = self.documents[response.position]
            count = self._count(spec, response, document)
            walk.pages.append(_Page(response, document, count))
            seen_items += count or 0
            where = f"call {response.position}"
            if isinstance(paging, NoPaging):
                walk.ended = True
                return walk
            try:
                stated_total = (
                    _whole(spec.selectors.one(paging.total, document))
                    if not isinstance(paging, CursorPaging) and paging.total is not None
                    else None
                )
            except _Unreadable:
                self._unreadable(response, f"{where}: its total is not a count")
                return walk
            if stated_total is not None:
                total = stated_total
            has_more = (
                spec.selectors.flag(paging.has_more, document)
                if paging.has_more is not None
                else None
            )
            stop = spec.stop is not None and spec.stop(response)
            following: dict[str, Any] = {}
            match paging:
                case CursorPaging():
                    cursor = _cursor(spec.selectors.one(paging.next, document))
                    if has_more is False or stop:
                        walk.ended = True
                    elif cursor is None:
                        if has_more:
                            reason = (
                                f"{where} of {spec.label} says more pages follow but names no"
                                " cursor"
                            )
                            self.fail(reason, repeat_call(response, "truncated", reason))
                            return walk
                        walk.ended = True
                    following = {**spec.base, paging.argument: cursor}
                case PagePaging():
                    number = paging.first + page
                    try:
                        stated = (
                            _whole(spec.selectors.one(paging.number, document))
                            if paging.number is not None
                            else None
                        )
                        pages_total = (
                            _whole(spec.selectors.one(paging.total_pages, document))
                            if paging.total_pages is not None
                            else None
                        )
                    except _Unreadable:
                        self._unreadable(response, f"{where}: its page number is not a number")
                        return walk
                    numbered = self.checks.page_numbers and paging.number is not None
                    if numbered and stated != number:
                        reason = f"{where} of {spec.label} does not state the page it was asked"
                        self.fail(reason, repeat_call(response, "page_numbers", reason))
                        return walk
                    reached = stated if stated is not None else number
                    following = _paged(paging, spec.base, page + 1, seen_items)
                    walk.ended = (
                        stop
                        or has_more is False
                        or count == 0
                        or (count is not None and paging.size is not None and count < paging.size)
                        or (pages_total is not None and reached >= pages_total)
                        or (count is None and pages_total is None and has_more is None)
                    )
                    if (
                        walk.ended
                        and numbered
                        and not stop
                        and pages_total is not None
                        and reached < pages_total
                    ):
                        # page_numbers: the numbers end at the stated count of pages.
                        walk.ended = False
                        why = (
                            "page_numbers",
                            f"{spec.label} ends at {where}, before its last page number",
                        )
                case OffsetPaging():
                    following = _paged(paging, spec.base, page + 1, seen_items)
                    if (
                        stop
                        or has_more is False
                        or count is None
                        or count == 0
                        or (paging.size is not None and count < paging.size)
                        or (total is not None and seen_items >= total)
                    ):
                        walk.ended = True
            if walk.ended:
                self._check_total(spec, walk, total, seen_items, following, page + 1)
                return walk
            page += 1
            if page >= paging.max_pages:
                self.need(
                    spec,
                    following,
                    page,
                    "max_pages",
                    f"{spec.label} reached max_pages at {where} before its last page",
                )
                return walk
            if _identity(following) in visited:
                reason = f"{where} of {spec.label} names a page read before"
                self.fail(reason, repeat_call(response, "truncated", reason))
                return walk
            arguments = following

    def _absent(self, spec: _Call, walk: _Walk) -> tuple[Check, str]:
        if not walk.pages:
            if spec.call == "detail":
                return "detail", f"{spec.owner} has no detail call"
            return "page", "the listing has no first call"
        last = walk.pages[-1].response.position
        if isinstance(spec.paging, CursorPaging):
            return "page", f"the cursor of call {last} ({spec.label}) was not followed"
        return "page", f"{spec.label} stops at call {last}: its next page is missing"

    def _count(self, spec: _Call, response: RawResponse, document: Any) -> int | None:
        if spec.call == "list":
            return len(self.page_items.get(response.position, []))
        if spec.items is None:
            return None
        return len(spec.selectors.many(spec.items, document))

    def _unreadable(self, response: RawResponse, reason: str) -> None:
        self.fail(reason, repeat_call(response, "failed", reason))

    def _check_total(
        self,
        spec: _Call,
        walk: _Walk,
        total: int | None,
        seen: int,
        following: Mapping[str, Any],
        page: int,
    ) -> None:
        """``totals``: the walk's items are as many as the stated total."""
        if not self.checks.totals or total is None or seen == total:
            return
        if spec.call == "detail" and spec.items is None:
            return
        if seen < total:
            reason = f"{spec.label} holds fewer items than its total"
            self.need(spec, following, page, "totals", reason)
        else:
            reason = f"{spec.label} holds more items than its total"
            self.fail(reason, repeat_call(walk.pages[0].response, "totals", reason))


def _paged(paging: Paging, base: Mapping[str, Any], page: int, seen: int) -> dict[str, Any]:
    """The arguments of page ``page`` (from 0) of a call, ``seen`` items read before it."""
    arguments = dict(base)
    match paging:
        case CursorPaging() | NoPaging():
            pass
        case PagePaging():
            arguments[paging.argument] = paging.first + page
            if paging.size_argument is not None:
                arguments[paging.size_argument] = paging.size
        case OffsetPaging():
            arguments[paging.argument] = paging.start + seen
            if paging.size_argument is not None:
                arguments[paging.size_argument] = paging.size
    return arguments
