"""Reading a window with its recipe and the completeness checks (memini.recipes.reading), and
item parsers (memini.recipes.parsing). No database: the windows are run in memory against fake
servers of the three example recipes of docs/recipes.md (Hearth, Millnotes, Quillpost). Every
name, id and text is made up."""

import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from zoneinfo import ZoneInfo

import pytest

from hearth_helpers import (
    CHANNEL,
    DAY,
    HEARTH_RECIPE_PATH,
    HOUR,
    T0,
    WINDOW_1,
    FakeHearth,
    LossyHearth,
    TruncatedHearth,
    drive,
    hearth_parser,
    hearth_recipe,
    rewrite,
    ts,
)
from memini.ingest import RecordIn
from memini.intake import PlannedWindow
from memini.recipes import (
    CHECKS,
    IncompleteItem,
    Item,
    MissingCall,
    RawResponse,
    Recipe,
    WindowInput,
    item_key,
    item_parser,
    load_recipe,
    missing_calls,
    parse_window,
    read_window,
)
from memini.redaction import redact_json, redact_text
from redaction_helpers import GITHUB

RECIPES: Final = HEARTH_RECIPE_PATH.parent
FIRST_CALL: Final = {
    "channel": CHANNEL,
    "oldest": int(WINDOW_1.start.timestamp()),
    "latest": int(WINDOW_1.end.timestamp()),
    "limit": 100,
}

type CallTool = Callable[[str, Mapping[str, Any]], dict[str, Any]]


def calling(server: object) -> CallTool:
    """A client of a fake server: a tool is the server's method of the same name."""

    def call(tool: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        method: Callable[[Mapping[str, Any]], dict[str, Any]] = getattr(server, tool)
        return method(arguments)

    return call


def text_result(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": False}


def without(window: WindowInput, *positions: int) -> WindowInput:
    """The window without the responses at ``positions``, as forget leaves it."""
    kept = tuple(r for r in window.responses if r.position not in positions)
    return replace(window, responses=kept)


def with_recipe(recipe: Recipe, **paging: Any) -> Recipe:
    """``recipe`` with its list call's paging rule changed."""
    listing = recipe.listing.model_copy(
        update={"paging": recipe.listing.paging.model_copy(update=paging)}
    )
    return recipe.model_copy(update={"listing": listing})


# --- Hearth: JSON, cursor paging, a detail call per thread with replies --------------------------


def test_a_complete_window_reads_every_item_and_misses_nothing() -> None:
    server = FakeHearth()

    window = drive(hearth_recipe(), WINDOW_1, calling(server))
    reading = read_window(window)

    assert reading.complete
    assert (reading.incomplete, reading.missing) == ((), ())
    # Two list pages, and one detail call for each of the three threads with replies.
    assert [(r.call, r.page) for r in window.responses] == [
        ("list", 0),
        ("list", 1),
        ("detail", 0),
        ("detail", 0),
        ("detail", 0),
    ]
    newest_first = [t.root.ts for t in reversed(server.threads)]
    assert [item.key for item in reading.items] == newest_first
    kiln = reading.items[0]
    assert kiln.listed is not None
    assert (kiln.listed.reply_count, kiln.listed.positions, kiln.listed.label) == (
        1,
        (1,),
        "item 1 of call 1",
    )
    assert kiln.listed.timestamp == T0 + 2 * DAY + 6 * HOUR + timedelta(microseconds=100)
    assert [m["text"] for m in kiln.replies] == ["Cones look right."]
    assert len(kiln.detail_items) == 2  # the root, then its reply
    assert kiln.responses == (1, 3)
    glaze = reading.items[2]
    assert (glaze.details, glaze.replies, glaze.responses) == ((), (), (2,))  # no replies

    parsed = parse_window(hearth_parser(), window)
    assert parsed.complete
    assert [p.record.external_id for p in parsed.records] == [
        f"{CHANNEL}/{k}" for k in newest_first
    ]
    assert [p.responses for p in parsed.records] == [(1, 3), (1, 4), (2,), (2, 5)]


def test_a_window_without_calls_misses_the_first_call_of_its_listing() -> None:
    empty = WindowInput(hearth_recipe(), CHANNEL, WINDOW_1.start, WINDOW_1.end, ())

    assert missing_calls(empty) == (
        MissingCall(
            call="list",
            tool="channel_history",
            arguments=FIRST_CALL,
            page=0,
            item_id=None,
            check="page",
            reason="the listing has no first call",
        ),
    )


def test_a_truncated_page_names_the_page_again() -> None:
    window = drive(hearth_recipe(), WINDOW_1, calling(TruncatedHearth()), max_repeats=1)

    parsed = parse_window(hearth_parser(), window)

    assert not parsed.complete
    reason = "call 1 of the listing says more pages follow but names no cursor"
    assert parsed.incomplete == (reason,)
    assert parsed.missing == (
        MissingCall(
            call="list",
            tool="channel_history",
            arguments=FIRST_CALL,
            page=0,
            item_id=None,
            check="truncated",
            reason=reason,
            repeat=True,
        ),
    )
    # The page was asked for again once, and the items it lists got their detail calls.
    assert [r.call for r in window.responses] == ["list", "detail", "detail"]
    assert len(parsed.records) == 2


def test_a_missing_reply_names_the_detail_call() -> None:
    server = LossyHearth()
    keys = server.threads[2]  # three replies listed, two returned

    window = drive(hearth_recipe(), WINDOW_1, calling(server), max_repeats=1)
    parsed = parse_window(hearth_parser(), window)

    assert not parsed.complete
    reason = "item 2 of call 1: its detail call holds fewer replies than the listing counts"
    assert parsed.incomplete == (reason,)
    assert parsed.missing == (
        MissingCall(
            call="detail",
            tool="thread_replies",
            arguments={"channel": CHANNEL, "thread": keys.root.ts},
            page=0,
            item_id=keys.root.ts,
            check="reply_counts",
            reason=reason,
            repeat=True,
        ),
    )
    thread = next(p.record for p in parsed.records if p.record.external_id.endswith(keys.root.ts))
    assert thread.payload is not None
    assert len(thread.payload["messages"]) == 3  # the root and the two replies it got


def test_an_unfollowed_cursor_names_the_next_page_and_the_detail_calls() -> None:
    server = FakeHearth()

    window = drive(hearth_recipe(), WINDOW_1, calling(server), max_calls=1)
    reading = read_window(window)

    assert not reading.complete
    kiln, keys = server.threads[3].root.ts, server.threads[2].root.ts
    assert [
        (c.call, c.arguments, c.page, c.item_id, c.check, c.repeat) for c in reading.missing
    ] == [
        ("list", {**FIRST_CALL, "cursor": "cur-2"}, 1, None, "page", False),
        ("detail", {"channel": CHANNEL, "thread": kiln}, 0, kiln, "detail", False),
        ("detail", {"channel": CHANNEL, "thread": keys}, 0, keys, "detail", False),
    ]
    assert reading.incomplete == (
        "the cursor of call 1 (the listing) was not followed",
        "item 1 of call 1 has no detail call",
        "item 2 of call 1 has no detail call",
    )


@dataclass
class FlakyHearth(FakeHearth):
    """``thread_replies`` fails."""

    def thread_replies(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": "upstream timeout"}], "isError": True}


def test_a_failed_call_is_named_again() -> None:
    window = drive(hearth_recipe(), WINDOW_1, calling(FlakyHearth()))

    reading = read_window(window)

    failed = [c for c in reading.missing if c.check == "failed"]
    assert [(c.tool, c.repeat) for c in failed] == [("thread_replies", True)] * 3
    assert reading.incomplete == tuple(f"call {n} (thread_replies) failed" for n in (3, 4, 5))
    assert len(reading.missing) == 3  # nothing else: the detail calls were made


def test_a_listing_cut_by_max_pages_is_incomplete() -> None:
    recipe = with_recipe(hearth_recipe(), max_pages=1)

    window = drive(recipe, WINDOW_1, calling(FakeHearth()))
    reading = read_window(window)

    assert [(c.check, c.arguments) for c in reading.missing] == [
        ("max_pages", {**FIRST_CALL, "cursor": "cur-2"})
    ]
    assert reading.incomplete == ("the listing reached max_pages at call 1 before its last page",)


def test_an_item_that_cannot_be_read_names_its_page_again() -> None:
    page = text_result(
        json.dumps(
            {
                "messages": [
                    {"text": "no ts", "reply_count": 0},
                    {"ts": ts(T0), "reply_count": "two"},
                    {"ts": "yesterday", "reply_count": 0},
                    {"ts": ts(T0 + HOUR), "user": "U0001", "user_name": "A", "text": "fine"},
                ],
                "has_more": False,
            }
        )
    )
    response = RawResponse(1, "list", "channel_history", FIRST_CALL, 0, None, page)
    window = WindowInput(hearth_recipe(), CHANNEL, WINDOW_1.start, WINDOW_1.end, (response,))

    reading = read_window(window)

    assert reading.incomplete == (
        "item 1 of call 1 has no id",
        "item 2 of call 1: its reply count is not a count",
        "item 3 of call 1: its time cannot be read",
    )
    assert [(c.check, c.repeat, c.arguments) for c in reading.missing] == [
        ("item", True, FIRST_CALL)
    ]
    assert [item.key for item in reading.items] == [ts(T0 + HOUR)]


def test_an_item_whose_list_page_is_gone_is_read_from_its_detail_call() -> None:
    """Forget removed the first list page with a record built from it: the other items of the
    page still have their detail calls, and the window misses its first page."""
    server = FakeHearth()
    window = without(drive(hearth_recipe(), WINDOW_1, calling(server)), 1)

    reading = read_window(window)
    parsed = parse_window(hearth_parser(), window)

    assert [(c.check, c.page, c.repeat) for c in reading.missing] == [("page", 0, False)]
    keys, kiln = server.threads[2].root.ts, server.threads[3].root.ts
    assert [(item.key, item.listed is None) for item in reading.items] == [
        (server.threads[1].root.ts, False),
        (server.threads[0].root.ts, False),
        *sorted([(keys, True), (kiln, True)]),
    ]
    assert len(parsed.records) == 4
    assert not parsed.complete


def test_the_same_responses_give_the_same_records_in_the_same_order() -> None:
    window = drive(hearth_recipe(), WINDOW_1, calling(FakeHearth()))
    lists = [r for r in window.responses if r.call == "list"]
    details = [r for r in window.responses if r.call == "detail"]
    # With concurrency the detail calls come back in any order: the same calls, other positions.
    shuffled = replace(
        window,
        responses=tuple(
            replace(r, position=n) for n, r in enumerate([*lists, *reversed(details)], start=1)
        ),
    )

    first = parse_window(hearth_parser(), window)
    assert parse_window(hearth_parser(), window) == first
    assert read_window(window) == read_window(window)
    other = parse_window(hearth_parser(), shuffled)
    assert [p.record for p in other.records] == [p.record for p in first.records]
    assert (other.complete, other.incomplete, other.missing) == (True, (), ())

    cut = without(window, 2)  # the second list page is missing
    assert parse_window(hearth_parser(), cut) == parse_window(hearth_parser(), cut)
    assert missing_calls(cut) == missing_calls(cut)


@dataclass
class SecretCursors(FakeHearth):
    """Cursors shaped like a token: redaction replaces them where they are stored."""

    def channel_history(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        asked = dict(arguments)
        if "cursor" in asked:
            asked["cursor"] = "cur-2" if asked["cursor"] == GITHUB else "cur-unknown"

        def hide(document: dict[str, Any]) -> None:
            if document["next_cursor"]:
                document["next_cursor"] = GITHUB

        return rewrite(super().channel_history(asked), hide)


def test_a_window_reads_the_same_once_stored_and_redacted() -> None:
    window = drive(hearth_recipe(), WINDOW_1, calling(SecretCursors()))
    assert any(r.arguments.get("cursor") == GITHUB for r in window.responses)

    stored = replace(
        window,
        responses=tuple(
            replace(
                r,
                arguments=redact_json(r.arguments).value,
                result=redact_json(r.result).value,
                item_id=redact_text(r.item_id).value if r.item_id is not None else None,
            )
            for r in window.responses
        ),
    )

    assert GITHUB not in json.dumps([[r.arguments, r.result] for r in stored.responses])
    assert read_window(stored).complete
    assert parse_window(hearth_parser(), stored).complete


def test_item_keys_are_the_id_as_text() -> None:
    assert (item_key("1794300000.000200"), item_key(42), item_key(1.5)) == (
        "1794300000.000200",
        "42",
        "1.5",
    )
    assert set(CHECKS) >= {"page", "truncated", "detail", "reply_counts", "page_numbers", "totals"}


# --- Item parsers --------------------------------------------------------------------------------


def test_an_item_parser_s_incomplete_item_names_the_call_that_holds_it() -> None:
    window = drive(hearth_recipe(), WINDOW_1, calling(FakeHearth()))
    kiln = read_window(window).items[0]

    def parse(item: Item, _: WindowInput) -> RecordIn | IncompleteItem | None:
        record = RecordIn(
            kind="thread",
            external_id=item.key,
            occurred_at=T0,
            payload={
                "messages": [{"author": "Ann", "sent_at": "2026-03-02T00:00:00Z", "text": "x"}]
            },
        )
        if item.key == kiln.key:
            return IncompleteItem("its last reply is still being written", record)
        if item.listed is not None and item.listed.reply_count == 0:
            return None  # left out
        return record

    parsed = parse_window(item_parser(parse), window)
    items = read_window(window).items

    reason = "item 1 of call 1: its last reply is still being written"
    assert (parsed.complete, parsed.incomplete) == (False, (reason,))
    assert [(c.call, c.item_id, c.check, c.repeat) for c in parsed.missing] == [
        ("detail", kiln.key, "item", True)
    ]
    assert [p.record.external_id for p in parsed.records] == [
        items[0].key,
        items[1].key,
        items[3].key,  # the third has no replies: the parser leaves it out
    ]
    assert parsed.records[0].responses == kiln.responses


# --- Millnotes: structured, numbered pages, a listing without dates ------------------------------

LISBON: Final = ZoneInfo("Europe/Lisbon")
AS_OF: Final = datetime(2026, 6, 10, tzinfo=UTC)


def millnotes_recipe() -> Recipe:
    return load_recipe(RECIPES / "millnotes.toml")


@dataclass
class FakeMillnotes:
    """A meeting a day at 10:00 UTC for the hundred days before ``AS_OF``, newest first."""

    days: int = 100
    wrong_number_on: int | None = None
    """The page that states the next page's number instead of its own."""
    extra_pages: int = 0
    """Pages more than there are, in the stated page count."""

    @property
    def meetings(self) -> list[tuple[str, datetime]]:
        return [
            (f"M-{day:04d}", AS_OF - timedelta(days=self.days - day) + 10 * HOUR)
            for day in reversed(range(self.days))
        ]

    def list_meetings(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        number, size = int(arguments["page"]), int(arguments["per_page"])
        assert arguments["include_shared"] is True
        listed = self.meetings
        chunk = listed[(number - 1) * size : number * size]
        stated = number + 1 if number == self.wrong_number_on else number
        return {
            "content": [{"type": "text", "text": f"{len(chunk)} meetings"}],
            "structuredContent": {
                "meetings": [
                    {
                        "meeting_id": key,
                        "started at": start.astimezone(LISBON).strftime("%Y-%m-%d %H:%M"),
                        "title": f"Standup {key}",
                    }
                    for key, start in chunk
                ],
                "page": stated,
                "pages": math.ceil(len(listed) / size) + self.extra_pages,
            },
            "isError": False,
        }

    def get_meeting(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        assert arguments["sections"] == ["summary", "action_items", "transcript"]
        return {
            "content": [],
            "structuredContent": {"id": arguments["id"], "summary": "Kiln schedule agreed."},
            "isError": False,
        }


def days_back(start: int, end: int) -> PlannedWindow:
    return PlannedWindow(
        kind="backfill", start=AS_OF - timedelta(days=start), end=AS_OF - timedelta(days=end)
    )


def test_a_listing_without_dates_stops_after_the_page_past_the_window() -> None:
    window = drive(
        millnotes_recipe(), days_back(30, 0), calling(FakeMillnotes()), stream="meetings"
    )
    reading = read_window(window)

    assert reading.complete
    lists = [r for r in window.responses if r.call == "list"]
    assert [r.arguments for r in lists] == [
        {"include_shared": True, "page": 1, "per_page": 25},
        {"include_shared": True, "page": 2, "per_page": 25},
    ]  # page 2 reaches past the window's start: page 3 is not asked for
    assert len(reading.items) == 30  # only the meetings inside the window
    assert all(item.listed and item.listed.timestamp for item in reading.items)
    assert [r.item_id for r in window.responses if r.call == "detail"] == [
        item.key for item in reading.items
    ]


def test_a_page_that_states_another_number_names_the_page_again() -> None:
    server = FakeMillnotes(wrong_number_on=2)

    window = drive(millnotes_recipe(), days_back(30, 0), calling(server), stream="meetings")
    reading = read_window(window)

    reason = "call 2 of the listing does not state the page it was asked"
    assert reading.incomplete == (reason,)
    assert [(c.check, c.page, c.repeat, c.arguments["page"]) for c in reading.missing] == [
        ("page_numbers", 1, True, 2)
    ]


def test_a_gap_in_the_page_numbers_names_the_missing_page() -> None:
    window = drive(
        millnotes_recipe(), days_back(80, 50), calling(FakeMillnotes()), stream="meetings"
    )
    pages = [r for r in window.responses if r.call == "list"]
    assert [r.arguments["page"] for r in pages] == [1, 2, 3, 4]
    second = pages[1].position

    reading = read_window(without(window, second))

    assert reading.incomplete == (
        f"the listing stops at call {pages[0].position}: its next page is missing",
    )
    assert [(c.check, c.page, c.repeat, c.arguments) for c in reading.missing] == [
        ("page", 1, False, {"include_shared": True, "page": 2, "per_page": 25})
    ]
    assert len(reading.items) == 30  # the pages after the gap are read all the same


def test_page_numbers_read_on_to_the_stated_count_of_pages() -> None:
    """The second page is short, which ends a listing; but the server states three pages, so
    ``page_numbers`` asks for the third as well."""
    server = FakeMillnotes(days=30, extra_pages=1)

    window = drive(millnotes_recipe(), days_back(31, 0), calling(server), stream="meetings")

    pages = [r for r in window.responses if r.call == "list"]
    assert [r.arguments["page"] for r in pages] == [1, 2, 3]
    assert read_window(window).complete
    reading = read_window(without(window, pages[2].position))
    assert [(c.check, c.arguments["page"], c.repeat) for c in reading.missing] == [
        ("page_numbers", 3, False)
    ]
    assert reading.incomplete == (
        f"the listing ends at call {pages[1].position}, before its last page number",
    )


# --- Quillpost: text read with patterns, offset paging, totals -----------------------------------


def quillpost_recipe() -> Recipe:
    return load_recipe(RECIPES / "quillpost.toml")


QUERY: Final = "in:inbox -from:noreply"


@dataclass
class FakeQuillpost:
    """Mail threads in text meant for a model: two threads a call, whatever the page size."""

    threads: list[tuple[str, str, int]] = field(
        default_factory=lambda: [
            ("T-1001", "inbox", 3),
            ("T-1002", "inbox", 1),
            ("T-1003", "archive", 2),
            ("T-1004", "inbox", 4),
            ("T-1005", "inbox", 1),
        ]
    )
    total_bias: int = 0
    short_thread: str | None = None
    """A thread whose read leaves out its last message."""

    def search_threads(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        assert arguments["query"] == f"{QUERY} after:2026/03/02 before:2026/03/05"
        offset = int(arguments["offset"])
        lines = ["Threads that match the query, newest activity first:", ""]
        for number, (key, box, count) in enumerate(self.threads[offset : offset + 2]):
            when = (T0 + timedelta(hours=offset + number)).strftime("%Y-%m-%dT%H:%M:%SZ")
            lines.append(f"Thread {key} in {box}: {count} messages, last activity {when}")
        lines += ["", f"Total: {len(self.threads) + self.total_bias} threads"]
        return text_result("\n".join(lines))

    def read_thread(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        key, box, count = next(t for t in self.threads if t[0] == arguments["thread_id"])
        assert arguments["mailbox"] == box
        if key == self.short_thread:
            count -= 1
        parts = [
            f"--- message {n} ---\nFrom: Ann Example\n\nNote {n} on the kiln." for n in range(count)
        ]
        return text_result("\n".join(parts))


QUILL_WINDOW: Final = PlannedWindow(kind="initial", start=T0, end=T0 + timedelta(days=2, hours=3))


def test_a_text_listing_is_read_with_patterns() -> None:
    window = drive(quillpost_recipe(), QUILL_WINDOW, calling(FakeQuillpost()), stream=QUERY)
    reading = read_window(window)

    assert reading.complete, reading.incomplete
    lists = [r for r in window.responses if r.call == "list"]
    assert [r.arguments["offset"] for r in lists] == [0, 2, 4]
    assert [item.key for item in reading.items] == [f"T-100{n}" for n in range(1, 6)]
    archived = reading.items[2]
    assert archived.listed is not None
    assert archived.listed.fields == {"mailbox": "archive"}
    assert archived.listed.reply_count == 2
    assert archived.listed.value.startswith("Thread T-1003 in archive:")
    assert archived.listed.timestamp == T0 + 2 * HOUR
    assert archived.details[0].response.arguments == {"thread_id": "T-1003", "mailbox": "archive"}
    assert len(archived.replies) == 2  # the count includes the first message: so do the items


def test_a_text_listing_short_of_its_total_names_the_next_offset() -> None:
    window = drive(
        quillpost_recipe(), QUILL_WINDOW, calling(FakeQuillpost(total_bias=1)), stream=QUERY
    )

    reading = read_window(window)

    assert reading.incomplete == ("the listing holds fewer items than its total",)
    [call] = reading.missing
    assert (call.check, call.arguments["offset"], call.repeat) == ("totals", 5, True)


def test_a_text_thread_short_of_its_count_names_its_read_again() -> None:
    server = FakeQuillpost(short_thread="T-1004")

    window = drive(quillpost_recipe(), QUILL_WINDOW, calling(server), stream=QUERY)
    reading = read_window(window)

    assert [(c.tool, c.arguments, c.check, c.repeat) for c in reading.missing] == [
        ("read_thread", {"thread_id": "T-1004", "mailbox": "inbox"}, "reply_counts", True)
    ]


@pytest.mark.parametrize(
    "result",
    [
        text_result("not JSON at all"),
        {"content": [], "isError": False},
    ],
)
def test_a_result_that_cannot_be_decoded_fails_the_call(result: dict[str, Any]) -> None:
    response = RawResponse(1, "list", "channel_history", FIRST_CALL, 0, None, result)
    window = WindowInput(hearth_recipe(), CHANNEL, WINDOW_1.start, WINDOW_1.end, (response,))

    reading = read_window(window)

    assert reading.incomplete == ("call 1 (channel_history): the result cannot be read as json",)
    assert [(c.check, c.repeat) for c in reading.missing] == [("failed", True)]
