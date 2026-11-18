"""The payloads of pushed records: the fields of each kind, the ids of messages and segments, and
the normalized text. Every value here is synthetic."""

from datetime import UTC, datetime
from typing import Any

import pytest

from memini.payloads import (
    ActionItem,
    Document,
    Meeting,
    Message,
    PayloadError,
    Segment,
    Thread,
    load_payload,
    normalize,
    parse_payload,
)

PRIVATE = "private-canary-7f3a"
"""Stands for a value of a payload: no error may repeat it."""


def message(**fields: Any) -> dict[str, Any]:
    return {"author": "Ann Example", "sent_at": "2026-03-01T09:30:00Z", "text": "Hello."} | fields


def thread(*messages: dict[str, Any], **fields: Any) -> Thread:
    parsed = parse_payload("thread", {"messages": list(messages), **fields})
    assert isinstance(parsed, Thread)
    return parsed


def meeting(**fields: Any) -> Meeting:
    payload = {"title": "Release planning", "started_at": "2026-03-01T14:00:00Z"} | fields
    parsed = parse_payload("meeting", payload)
    assert isinstance(parsed, Meeting)
    return parsed


def refused(kind: str, payload: dict[str, Any]) -> PayloadError:
    with pytest.raises(PayloadError) as error:
        parse_payload(kind, payload)
    assert PRIVATE not in str(error.value)
    return error.value


# --- Threads -------------------------------------------------------------------------------------


def test_a_thread_is_parsed_with_its_optional_fields() -> None:
    parsed = thread(
        message(
            id="1772357400.000100",
            recipients=["Bo Sample", " ", "Cy Placeholder"],
            mentions=["Bo Sample"],
            url="https://chat.example.invalid/p1",
            reactions=[PRIVATE],  # a field no parser reads: kept in the payload, not parsed
        ),
        message(author="Bo Sample", sent_at="2026-03-01T12:31:00+03:00", text=""),
        title="Release",
        channel=" ",
    )
    first, second = parsed.messages
    assert first == Message(
        id="1772357400.000100",
        author="Ann Example",
        sent_at=datetime(2026, 3, 1, 9, 30, tzinfo=UTC),
        text="Hello.",
        recipients=("Bo Sample", "Cy Placeholder"),
        mentions=("Bo Sample",),
        url="https://chat.example.invalid/p1",
    )
    assert (second.author, second.text, second.recipients, second.url) == (
        "Bo Sample",
        "",
        (),
        None,
    )
    assert (parsed.title, parsed.channel) == ("Release", None)


def test_a_message_without_an_id_gets_one_from_its_author_and_time() -> None:
    one = thread(message(text="First version."), message(author="Bo Sample"))
    edited = thread(message(text="Edited later."), message(author="Bo Sample"))
    moved = thread(message(author="Cy Placeholder"), message(author="Bo Sample"), message())

    assert [m.id for m in one.messages] == [m.id for m in edited.messages]
    assert one.messages[0].id == moved.messages[2].id
    assert one.messages[1].id == moved.messages[1].id
    assert all(len(m.id) == 16 and int(m.id, 16) >= 0 for m in one.messages)
    # The same instant in another zone is the same message.
    zoned = thread(message(sent_at="2026-03-01T12:30:00+03:00"))
    assert zoned.messages[0].id == one.messages[0].id


def test_two_messages_with_one_author_and_time_get_distinct_ids() -> None:
    parsed = thread(message(), message(text="Again."), message(text="And again."))
    first, second, third = (m.id for m in parsed.messages)
    assert (second, third) == (f"{first}-2", f"{first}-3")
    # A derived id never takes a given one.
    given = thread(message(id=first), message())
    assert [m.id for m in given.messages] == [first, f"{first}-2"]


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({"messages": [message(id="m1"), message(id="m1")]}, "payload.messages[1].id"),
        ({"messages": [message(id=" ")]}, "payload.messages[0].id"),
        ({"messages": [message(id="a\nb" + PRIVATE)]}, "payload.messages[0].id"),
        ({"messages": [message(id="x" * 257)]}, "payload.messages[0].id"),
        ({"messages": [message(id=7)]}, "payload.messages[0].id"),
        ({"messages": [message(recipients=PRIVATE)]}, "payload.messages[0].recipients"),
        ({"messages": [message(recipients=[PRIVATE, 3])]}, "payload.messages[0].recipients[1]"),
        ({"messages": [message(mentions={"a": PRIVATE})]}, "payload.messages[0].mentions"),
        ({"messages": [message(url=f"ftp://{PRIVATE}")]}, "payload.messages[0].url"),
        ({"messages": [message(url=f"https://x.invalid/{PRIVATE} b")]}, "payload.messages[0].url"),
        ({"messages": [message()], "title": 5}, "payload.title"),
        ({"messages": [message()], "channel": [PRIVATE]}, "payload.channel"),
        ({"messages": {"0": message()}}, "payload.messages"),
    ],
)
def test_a_thread_field_of_the_wrong_shape_is_refused(payload: dict[str, Any], field: str) -> None:
    assert refused("thread", payload).field == field


# --- Meetings ------------------------------------------------------------------------------------


def test_a_meeting_is_parsed_with_its_notes() -> None:
    parsed = meeting(
        ended_at="2026-03-01T14:30:00Z",
        participants=["Ann Example", "Bo Sample"],
        summary="## Decisions\n\nFriday.",
        action_items=[
            "Book a room",
            {"text": "Write the notes", "owner": "Bo Sample", "due": "2026-03-05", "id": "a1"},
        ],
        transcript=[
            {"speaker": "Ann Example", "start": 5, "end": 9.5, "text": "Let us start."},
            {"text": "(silence)"},
        ],
    )
    assert parsed.ended_at == datetime(2026, 3, 1, 14, 30, tzinfo=UTC)
    assert parsed.participants == ("Ann Example", "Bo Sample")
    assert parsed.summary == "## Decisions\n\nFriday."
    assert parsed.action_items == (
        ActionItem(text="Book a room"),
        ActionItem(text="Write the notes", owner="Bo Sample", due="2026-03-05", id="a1"),
    )
    first, second = parsed.transcript
    assert (first.speaker, first.start, first.end, first.text) == (
        "Ann Example",
        5.0,
        9.5,
        "Let us start.",
    )
    assert second == Segment(id=second.id, text="(silence)")
    assert first.id != second.id


def test_a_segment_id_comes_from_its_speaker_and_start_or_its_position() -> None:
    base = [{"speaker": "Ann Example", "start": 5, "text": "One."}, {"text": "Two."}]
    edited = [{"speaker": "Ann Example", "start": 5, "text": "One, edited."}, {"text": "Two."}]
    a, b = meeting(transcript=base).transcript, meeting(transcript=edited).transcript
    assert [s.id for s in a] == [s.id for s in b]
    given = meeting(transcript=[{"id": "s-1", "text": "One."}]).transcript
    assert given[0].id == "s-1"


@pytest.mark.parametrize(
    ("fields", "field"),
    [
        ({"participants": PRIVATE}, "payload.participants"),
        ({"summary": {"text": PRIVATE}}, "payload.summary"),
        ({"action_items": PRIVATE}, "payload.action_items"),
        ({"action_items": [" "]}, "payload.action_items[0]"),
        ({"action_items": [{"owner": PRIVATE}]}, "payload.action_items[0].text"),
        ({"action_items": [{"text": "a", "owner": 3}]}, "payload.action_items[0].owner"),
        (
            {"action_items": [{"text": "a", "id": "x"}, {"text": "b", "id": "x"}]},
            "payload.action_items[1].id",
        ),
        ({"transcript": [PRIVATE]}, "payload.transcript[0]"),
        ({"transcript": [{"text": " "}]}, "payload.transcript[0].text"),
        ({"transcript": [{"text": "a", "start": -1}]}, "payload.transcript[0].start"),
        ({"transcript": [{"text": "a", "start": True}]}, "payload.transcript[0].start"),
        ({"transcript": [{"text": "a", "start": PRIVATE}]}, "payload.transcript[0].start"),
        ({"transcript": [{"text": "a", "start": 9, "end": 3}]}, "payload.transcript[0].end"),
        ({"transcript": [{"text": "a", "speaker": 1}]}, "payload.transcript[0].speaker"),
        (
            {"transcript": [{"text": "a", "id": "s"}, {"text": "b", "id": "s"}]},
            "payload.transcript[1].id",
        ),
    ],
)
def test_a_meeting_field_of_the_wrong_shape_is_refused(fields: dict[str, Any], field: str) -> None:
    payload = {"title": "Plan", "started_at": "2026-03-01T14:00:00Z"} | fields
    assert refused("meeting", payload).field == field


# --- Documents and stored payloads ---------------------------------------------------------------


def test_a_document_is_a_title_and_a_text() -> None:
    assert parse_payload("document", {"title": "Notes", "text": "", "tags": [1]}) == Document(
        title="Notes", text=""
    )


def test_a_stored_payload_is_loaded_by_its_kind() -> None:
    assert load_payload("document", b'{"text":"T","title":"N"}') == Document(title="N", text="T")
    with pytest.raises(PayloadError, match="payload: a JSON object"):
        load_payload("document", b"[]")
    with pytest.raises(PayloadError, match="payload: a JSON object"):
        load_payload("document", b"\xff")
    with pytest.raises(ValueError, match="carry no payload"):
        load_payload("code_file", b"{}")


def test_the_field_prefix_names_the_record() -> None:
    with pytest.raises(PayloadError) as error:
        parse_payload("document", {"title": "N"}, field="records[3].payload")
    assert error.value.field == "records[3].payload.text"


# --- Normalized text -----------------------------------------------------------------------------


def test_a_thread_is_rendered_as_plain_text() -> None:
    payload = {
        "channel": "release",
        "title": "Friday\nor Monday?",
        "messages": [
            message(
                text="\n\nShall we move it to Friday?\r\nThe notes are ready.\n\n",
                recipients=["Bo Sample"],
            ),
            message(author="Bo Sample", sent_at="2026-03-01T13:05:00+03:00", text=""),
            message(author="Борис Образец", sent_at="2026-03-02T08:00:00Z", text="Да,\rв пятницу"),
        ],
    }
    assert normalize("thread", payload) == (
        "release · Friday or Monday?\n"
        "\n"
        "Ann Example → Bo Sample · 2026-03-01 09:30 UTC\n"
        "Shall we move it to Friday?\n"
        "The notes are ready.\n"
        "\n"
        "Bo Sample · 2026-03-01 10:05 UTC\n"
        "\n"
        "Борис Образец · 2026-03-02 08:00 UTC\n"
        "Да,\n"
        "в пятницу"
    )


def test_a_meeting_is_rendered_as_plain_text() -> None:
    payload = {
        "title": "Планёрка",
        "started_at": "2026-03-01T23:30:00Z",
        "ended_at": "2026-03-02T00:15:00Z",
        "participants": ["Аня Пример", "Bo Sample"],
        "summary": "Решили перенести релиз.\n\n## Риски\n\n(пока никаких)",
        "action_items": [{"text": "Write\nthe notes", "owner": "Bo Sample", "due": "Friday"}],
        "transcript": [
            {"speaker": "Аня Пример", "start": 3725.9, "text": "Начнём."},
            {"start": 12, "text": "(шум)"},
            {"speaker": "Bo Sample", "text": "Agreed."},
        ],
    }
    assert normalize("meeting", payload) == (
        "Планёрка\n"
        "2026-03-01 23:30 to 2026-03-02 00:15 UTC\n"
        "Participants: Аня Пример, Bo Sample\n"
        "\n"
        "Summary\n"
        "Решили перенести релиз.\n"
        "\n"
        "## Риски\n"
        "\n"
        "(пока никаких)\n"
        "\n"
        "Action items\n"
        "- Bo Sample: Write (due Friday)\n"
        "  the notes\n"
        "\n"
        "Transcript\n"
        "[1:02:05] Аня Пример: Начнём.\n"
        "[00:12] (шум)\n"
        "Bo Sample: Agreed."
    )
    bare = {"title": "Sync", "started_at": "2026-03-01T14:00:00+01:00"}
    assert normalize("meeting", bare) == "Sync\n2026-03-01 13:00 UTC"


def test_a_document_is_rendered_as_its_title_and_text() -> None:
    assert normalize("document", {"title": "Notes", "text": "\n# A\r\n\nB\n"}) == (
        "Notes\n\n# A\n\nB"
    )
    assert normalize("document", {"title": " Notes ", "text": " \n "}) == "Notes"


def test_the_normalized_text_holds_no_nul_character() -> None:
    text = normalize("document", {"title": "N\x00", "text": "a\x00b"})
    assert "\x00" not in text
    assert text == "N\N{REPLACEMENT CHARACTER}\n\na\N{REPLACEMENT CHARACTER}b"
