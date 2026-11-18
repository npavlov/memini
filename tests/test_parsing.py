"""The parser protocol (memini.recipes.parsing): decoding stored results, the checks of a parser's
output, the registry. No database. Every value is synthetic."""

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from hearth_helpers import WINDOW_1, hearth_recipe
from memini.ingest import RecordIn
from memini.recipes import (
    MissingCall,
    ParsedRecord,
    ParsedWindow,
    ParserError,
    ParserNotFoundError,
    RawResponse,
    ResponseDecodeError,
    WindowInput,
    get_parser,
    parse_window,
    register_parser,
    registered_parsers,
    unregister_parser,
)

AT = datetime(2026, 3, 2, 9, tzinfo=UTC)
FIRST_CALL = {
    "channel": "C0042",
    "oldest": int(WINDOW_1.start.timestamp()),
    "latest": int(WINDOW_1.end.timestamp()),
    "limit": 100,
}
"""The hearth recipe's first list call for channel C0042 and WINDOW_1."""
EMPTY = '{"messages": [], "has_more": false}'


def response(position: int = 1, **result: Any) -> RawResponse:
    """The first list call of the window; by default an empty listing, its only page."""
    return RawResponse(
        position=position,
        call="list",
        tool="channel_history",
        arguments=FIRST_CALL,
        page=0,
        item_id=None,
        result=result or {"content": [{"type": "text", "text": EMPTY}]},
    )


def window(*responses: RawResponse) -> WindowInput:
    return WindowInput(
        recipe=hearth_recipe(),
        stream="C0042",
        start=WINDOW_1.start,
        end=WINDOW_1.end,
        responses=responses or (response(),),
    )


def thread(external_id: str = "C0042/1", kind: str = "thread") -> RecordIn:
    return RecordIn(
        kind=kind,
        external_id=external_id,
        occurred_at=AT,
        payload={
            "messages": [{"author": "Ann Example", "sent_at": "2026-03-02T09:00:00Z", "text": "Hi"}]
        },
    )


def returning(*records: ParsedRecord, complete: bool = True) -> Any:
    def parse(_: WindowInput) -> ParsedWindow:
        return ParsedWindow(records=records, complete=complete)

    return parse


def test_a_result_decodes_as_the_recipe_reads_it() -> None:
    blocks = [
        {"type": "text", "text": '{"messages":'},
        {"type": "image", "data": "AAAA"},
        {"type": "text", "text": "[]}"},
    ]
    found = response(content=blocks, structuredContent={"total": 3}, isError=False)

    assert found.text() == '{"messages":\n[]}'
    assert found.decode("json") == {"messages": []}
    assert found.decode("structured") == {"total": 3}
    assert found.decode("text") == found.text()
    assert not found.is_error
    assert response(isError=True, content=[]).is_error
    with pytest.raises(ResponseDecodeError, match="no structuredContent"):
        response(content=blocks).decode("structured")
    with pytest.raises(ResponseDecodeError, match="not JSON") as raised:
        response(content=[{"type": "text", "text": "secret-ish {"}]).decode("json")
    assert "secret-ish" not in str(raised.value)


def test_parse_window_keeps_a_valid_output() -> None:
    record = ParsedRecord(thread(), responses=(1,))

    parsed = parse_window(returning(record), window())

    assert parsed == ParsedWindow(records=(record,), complete=True)


def test_a_failed_call_leaves_the_window_incomplete() -> None:
    parsed = parse_window(returning(), window(response(1), response(2, content=[], isError=True)))

    assert not parsed.complete
    assert parsed.incomplete == ("call 2 (channel_history) failed",)
    assert parsed.missing == (
        MissingCall(
            call="list",
            tool="channel_history",
            arguments=FIRST_CALL,
            page=0,
            item_id=None,
            check="failed",
            reason="call 2 (channel_history) failed",
            repeat=True,
        ),
    )


def test_the_recipe_s_checks_apply_to_every_parser() -> None:
    """A parser that says its window is complete does not make it so: this listing names a next
    page that is not in the window."""
    more = '{"messages": [], "has_more": true, "next_cursor": "cur-2"}'

    parsed = parse_window(returning(), window(response(content=[{"type": "text", "text": more}])))

    assert not parsed.complete
    assert parsed.incomplete == ("the cursor of call 1 (the listing) was not followed",)
    assert [(c.check, c.arguments, c.page, c.repeat) for c in parsed.missing] == [
        ("page", {**FIRST_CALL, "cursor": "cur-2"}, 1, False)
    ]


@pytest.mark.parametrize(
    ("records", "message"),
    [
        ((ParsedRecord(thread(kind="meeting"), (1,)),), "of kind meeting"),
        ((ParsedRecord(thread(), (1,)), ParsedRecord(thread(), (1,))), "an earlier record"),
        ((ParsedRecord(thread(), ()),), "names no response"),
        ((ParsedRecord(thread(), (7,)),), "not in the window"),
    ],
)
def test_parse_window_refuses_an_output_the_protocol_does_not_allow(
    records: tuple[ParsedRecord, ...], message: str
) -> None:
    with pytest.raises(ParserError, match=message):
        parse_window(returning(*records), window())


def test_a_parser_that_raises_is_named_without_the_value() -> None:
    def broken(_: WindowInput) -> ParsedWindow:
        raise KeyError("Ann Example's private note")

    with pytest.raises(ParserError, match=r"the parser hearth\.thread failed: KeyError") as raised:
        parse_window(broken, window())
    assert "private" not in str(raised.value)

    def wrong(_: WindowInput) -> Any:
        return [thread()]

    with pytest.raises(ParserError, match="no ParsedWindow"):
        parse_window(wrong, window())


@pytest.fixture
def scratch_name() -> Iterator[str]:
    name = "test.scratch"
    yield name
    unregister_parser(name)


def test_parsers_are_found_by_name(scratch_name: str) -> None:
    parser = returning()
    register_parser(scratch_name, parser)

    assert get_parser(scratch_name) is parser
    assert scratch_name in registered_parsers()
    with pytest.raises(ValueError, match="registered already"):
        register_parser(scratch_name, returning())
    register_parser(scratch_name, returning(), replace=True)
    unregister_parser(scratch_name)
    with pytest.raises(ParserNotFoundError, match=r"no parser named test\.scratch"):
        get_parser(scratch_name)
