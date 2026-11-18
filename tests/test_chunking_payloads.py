"""The chunkers of pushed records: golden output for a thread, a meeting and a document (English and
Russian), groups of messages and their stability, windows, anchors and the normalized text.

Golden files sit next to their payloads in ``tests/fixtures/chunking`` as ``<name>.chunks.json``.
After a deliberate change of the rules, bump ``PAYLOAD_CHUNKER_VERSION``, run
``MEMINI_UPDATE_GOLDENS=1 make test`` and review the diff. Every payload here is synthetic.
"""

import json
import os
import re
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from memini.chunking import (
    PAYLOAD_CHUNKER_VERSION,
    Chunk,
    FileChunks,
    PayloadSizes,
    chunk_document_payload,
    chunk_id,
    chunk_markdown,
    chunk_meeting,
    chunk_payload,
    chunk_thread,
)
from memini.chunking.payloads import MAX_ANCHOR_LENGTH, is_boundary
from memini.ids import record_id
from memini.payloads import (
    Message,
    PayloadError,
    Thread,
    normalize,
    parse_payload,
)

FIXTURES = Path(__file__).parent / "fixtures" / "chunking"
UPDATE_GOLDENS = os.environ.get("MEMINI_UPDATE_GOLDENS") == "1"
SOURCE = uuid.UUID("0190a0a0-0000-7000-8000-0000000c0de2")
GOLDEN = {
    "thread_greenhouse": "thread",
    "meeting_planning": "meeting",
    "document_handbook": "document",
}
T0 = datetime(2026, 3, 1, 9, tzinfo=UTC)
CONTROL = re.compile("[\\x00-\\x1f\\x7f-\\x9f\\u2028\\u2029]")


def rid(kind: str, external_id: str) -> str:
    return record_id(SOURCE, kind, external_id)  # pyright: ignore[reportArgumentType]


def payload_of(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text())
    return data


def as_json(kind: str, payload: dict[str, Any], result: FileChunks) -> dict[str, Any]:
    return {
        "chunker_version": PAYLOAD_CHUNKER_VERSION,
        "normalized": normalize(kind, payload).split("\n"),
        "chunks": [
            {
                "key": c.key,
                "id": c.id,
                "kind": c.kind,
                "lines": [c.start_line, c.end_line],
                "bytes": [c.start_byte, c.end_byte],
                "anchor": c.anchor,
                "url": c.url,
                "occurred_at": None if c.occurred_at is None else c.occurred_at.isoformat(),
                "content_hash": c.content_hash,
                "text": c.text.split("\n"),
            }
            for c in result.chunks
        ],
    }


def thread_of(count: int, *, text: str = "Message {n} about the watering schedule.") -> Thread:
    """``count`` messages, a minute apart, by three authors in turn; given ids ``m0`` …"""
    authors = ("Ann Example", "Bo Sample", "Cy Placeholder")
    return Thread(
        messages=tuple(
            Message(
                id=f"m{n}",
                author=authors[n % 3],
                sent_at=T0 + timedelta(minutes=n),
                text=text.format(n=n),
            )
            for n in range(count)
        ),
        channel="greenhouse",
    )


def edit(thread: Thread, index: int, text: str) -> Thread:
    messages = list(thread.messages)
    messages[index] = replace(messages[index], text=text)
    return replace(thread, messages=tuple(messages))


def hashes(result: FileChunks) -> dict[str, str]:
    return {c.id: c.content_hash for c in result.chunks}


# --- Golden output -------------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(GOLDEN))
def test_output_matches_the_golden_file(name: str) -> None:
    kind = GOLDEN[name]
    payload = payload_of(name)
    data = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    actual = as_json(kind, payload, chunk_payload(rid(kind, name), kind, data.encode()))
    golden = FIXTURES / f"{name}.chunks.json"
    if UPDATE_GOLDENS:
        golden.write_text(json.dumps(actual, indent=2, ensure_ascii=False) + "\n")
    assert golden.exists(), f"no golden file; run with MEMINI_UPDATE_GOLDENS=1 to write {golden}"
    assert actual == json.loads(golden.read_text())


@pytest.mark.parametrize("name", sorted(GOLDEN))
def test_a_chunk_quotes_the_normalized_text_at_its_lines_and_bytes(name: str) -> None:
    kind = GOLDEN[name]
    payload = payload_of(name)
    text = normalize(kind, payload)
    data = text.encode()
    rows = text.split("\n")
    parsed = parse_payload(kind, payload)
    chunks = chunk_payload(rid(kind, name), kind, json.dumps(payload).encode()).chunks
    assert chunks
    for c in chunks:
        assert c.content == "\n".join(rows[c.start_line - 1 : c.end_line])
        assert data[c.start_byte : c.end_byte].decode() == c.content
        assert c.header
        assert "\n" not in c.header
        assert c.anchor is None or (0 < len(c.anchor) <= MAX_ANCHOR_LENGTH)
        assert c.anchor is None or not CONTROL.search(c.anchor)
        assert c.symbol is None
        assert c.language is None
    assert parsed.kind == kind


def test_the_ids_of_the_chunks_hang_on_the_record_and_the_first_item() -> None:
    record = rid("thread", "greenhouse/1")
    result = chunk_thread(record, thread_of(30))
    for c in result.chunks:
        first = c.key.removeprefix("msg-").split("@")[0]
        assert c.id == chunk_id(record, ("messages", first))
        assert c.anchor == f"msg-{first}"
        assert c.kind == "messages"
    # Another record, the same thread: other ids, the same texts.
    other = chunk_thread(rid("thread", "greenhouse/2"), thread_of(30))
    assert {c.id for c in other.chunks}.isdisjoint(c.id for c in result.chunks)
    assert [c.content_hash for c in other.chunks] == [c.content_hash for c in result.chunks]


# --- Groups of messages --------------------------------------------------------------------------


def test_messages_are_grouped_by_their_ids_within_the_limits() -> None:
    thread = thread_of(60)
    rule = PayloadSizes().messages
    chunks = chunk_thread(rid("thread", "t"), thread).chunks
    firsts = [c.key.removeprefix("msg-") for c in chunks]
    ids = [m.id for m in thread.messages]
    starts = [ids.index(first) for first in firsts]
    sizes = [b - a for a, b in zip(starts, [*starts[1:], len(ids)], strict=True)]
    assert starts[0] == 0
    assert all(size <= rule.max_items for size in sizes)
    # Every group but the last ends at a boundary, the item limit or the size limit.
    for start, size in zip(starts[1:], sizes, strict=False):
        assert size >= rule.min_items or not is_boundary(ids[start], rule.spread)
    assert 4 <= len(chunks) <= 20


def test_a_changed_message_rechunks_only_its_group() -> None:
    record = rid("thread", "t")
    before = chunk_thread(record, thread_of(40))
    after = chunk_thread(record, edit(thread_of(40), 17, "Message 17 says Tuesday instead."))

    assert [c.id for c in after.chunks] == [c.id for c in before.chunks]
    changed = {i for i, h in hashes(after).items() if hashes(before)[i] != h}
    assert len(changed) == 1
    (only,) = changed
    holder = next(c for c in after.chunks if c.id == only)
    assert "Message 17 says Tuesday" in holder.content


def test_a_longer_message_moves_the_lines_of_the_groups_after_it_but_not_their_text() -> None:
    record = rid("thread", "t")
    before = chunk_thread(record, thread_of(40))
    longer = edit(thread_of(40), 5, "Message 5.\nIt has\nthree lines now.")
    after = chunk_thread(record, longer)

    assert [c.id for c in after.chunks] == [c.id for c in before.chunks]
    changed = [c.id for c in after.chunks if hashes(before)[c.id] != c.content_hash]
    assert len(changed) == 1
    assert after.chunks[-1].start_line == before.chunks[-1].start_line + 2


def test_a_reply_changes_the_last_group_only() -> None:
    record = rid("thread", "t")
    before = chunk_thread(record, thread_of(40))
    after = chunk_thread(record, thread_of(41))

    kept = [c for c in after.chunks if hashes(before).get(c.id) == c.content_hash]
    assert len(kept) >= len(before.chunks) - 1
    assert [c.id for c in after.chunks[: len(before.chunks) - 1]] == [
        c.id for c in before.chunks[:-1]
    ]


def test_a_message_loaded_late_changes_few_groups() -> None:
    record = rid("thread", "t")
    thread = thread_of(40)
    late = Message(id="late", author="Dee Sampleton", sent_at=T0, text="A message found later.")
    inserted = replace(thread, messages=(*thread.messages[:20], late, *thread.messages[20:]))
    before, after = hashes(chunk_thread(record, thread)), hashes(chunk_thread(record, inserted))

    assert len(set(after.items()) - set(before.items())) <= 2


def test_a_huge_message_is_cut_into_windows_of_its_own() -> None:
    long = "\n".join(f"Line {n} of a very long message about the greenhouse." for n in range(120))
    thread = edit(thread_of(9), 4, long)
    chunks = chunk_thread(rid("thread", "t"), thread).chunks
    windows = [c for c in chunks if c.key.startswith("msg-m4")]

    assert len(windows) >= 3
    assert [c.key for c in windows] == [
        f"msg-m4@{n}" if n else "msg-m4" for n in range(len(windows))
    ]
    assert all(c.anchor == "msg-m4" for c in windows)
    assert all(len(c.content) <= 2000 and c.end_line - c.start_line < 50 for c in windows)
    assert windows[1].id == chunk_id(rid("thread", "t"), ("messages", "m4", "1"))
    # Its neighbours are in other chunks.
    assert not any("Message 3 " in c.content or "Message 5 " in c.content for c in windows)


def test_the_header_names_the_thread_the_authors_and_the_time() -> None:
    thread = replace(thread_of(3), title="Watering")
    (only,) = chunk_thread(rid("thread", "t"), thread).chunks
    assert only.header == (
        "greenhouse · Watering · Ann Example, Bo Sample, Cy Placeholder"
        " · 2026-03-01 09:00 to 09:02 UTC"
    )
    assert only.occurred_at == T0
    days = replace(
        thread_of(1),
        messages=(
            thread_of(1).messages[0],
            Message(id="x", author="Bo Sample", sent_at=T0 + timedelta(days=1), text="Next day."),
        ),
        channel=None,
    )
    (only,) = chunk_thread(rid("thread", "t"), days).chunks
    assert only.header == "Ann Example, Bo Sample · 2026-03-01 09:00 to 2026-03-02 09:00 UTC"


def test_many_authors_are_counted_in_the_header() -> None:
    messages = tuple(
        Message(id=f"m{n}", author=f"Person {n}", sent_at=T0, text="Hi.") for n in range(10)
    )
    sizes = PayloadSizes(messages=replace(PayloadSizes().messages, min_items=10, max_items=10))
    (only,) = chunk_thread(rid("thread", "t"), Thread(messages=messages), sizes=sizes).chunks
    assert only.header.startswith("Person 0, Person 1, Person 2, Person 3, Person 4, Person 5,")
    assert "Person 7 and 2 more" in only.header


# --- Meetings and documents ----------------------------------------------------------------------


def test_a_meeting_gives_summary_sections_action_items_and_transcript_windows() -> None:
    meeting = parse_payload("meeting", payload_of("meeting_planning"))
    record = rid("meeting", "m-1")
    chunks = chunk_meeting(record, meeting).chunks  # pyright: ignore[reportArgumentType]
    kinds = [c.kind for c in chunks]

    assert kinds.count("action_items") == 1
    assert kinds[0] == "summary"
    assert kinds.count("transcript") >= 2
    summary = [c for c in chunks if c.kind == "summary"]
    anchors = {"summary", "summary/Решения", "summary/Риски", "summary/Open questions"}
    assert {c.anchor for c in summary} <= anchors
    assert all(
        c.header.startswith("Планёрка по теплице · 2026-03-03 07:00 to 07:40 UTC") for c in chunks
    )
    assert all("with Аня Пример, Борис Образец, Cy Placeholder" in c.header for c in summary)
    transcript = [c for c in chunks if c.kind == "transcript"]
    first = transcript[0]
    assert first.anchor is not None
    assert first.anchor.startswith("seg-")
    assert first.occurred_at == datetime(2026, 3, 3, 7, 0, 4, tzinfo=UTC)
    assert "Transcript 00:04 to" in first.header
    assert "Аня Пример" in first.header
    assert "Борис Образец" in first.header
    assert "[00:04] Аня Пример: Начнём" in first.content
    assert transcript[-1].header.split(" · ")[2].endswith("to 1:00:10")


def test_a_meeting_without_notes_is_one_chunk_of_its_overview() -> None:
    meeting = parse_payload(
        "meeting",
        {"title": "Sync", "started_at": "2026-03-01T14:00:00Z", "participants": ["Ann Example"]},
    )
    (only,) = chunk_meeting(rid("meeting", "m"), meeting).chunks  # pyright: ignore[reportArgumentType]
    assert (only.kind, only.key, only.anchor, only.header) == (
        "summary",
        "overview",
        "overview",
        "Sync · 2026-03-01 14:00 UTC",
    )
    assert only.content == "Sync\n2026-03-01 14:00 UTC\nParticipants: Ann Example"


def test_a_pushed_document_is_cut_by_headings_under_its_title() -> None:
    document = parse_payload("document", payload_of("document_handbook"))
    record = rid("document", "handbook")
    chunks = chunk_document_payload(record, document).chunks  # pyright: ignore[reportArgumentType]
    text = payload_of("document_handbook")["text"].replace("\r\n", "\n").strip("\n")
    markdown = chunk_markdown(record, "Greenhouse handbook", text.encode()).chunks

    assert [c.id for c in chunks] == [c.id for c in markdown]
    assert [c.header for c in chunks] == [c.header for c in markdown]
    assert [c.start_line for c in chunks] == [c.start_line + 2 for c in markdown]
    assert chunks[0].anchor is None  # the text before the first heading
    trails = {"Watering", "Watering > Water source", "Watering > Полив зимой", "Tools", "Journal"}
    assert {c.anchor for c in chunks[1:]} <= trails
    assert all(c.header.startswith("Greenhouse handbook") for c in chunks)


def test_a_document_without_text_is_one_chunk_of_its_title() -> None:
    document = parse_payload("document", {"title": "Empty\nnotes", "text": "  \n"})
    (only,) = chunk_document_payload(rid("document", "d"), document).chunks  # pyright: ignore[reportArgumentType]
    assert (only.key, only.kind, only.header, only.content, only.anchor) == (
        "(title)",
        "section",
        "Empty notes",
        "Empty notes",
        None,
    )


def test_a_long_heading_trail_is_cut_to_the_anchor_limit() -> None:
    title = "Very long heading " * 100
    document = parse_payload("document", {"title": "Notes", "text": f"# {title}\n\nText.\n"})
    chunks = chunk_document_payload(rid("document", "d"), document).chunks  # pyright: ignore[reportArgumentType]
    assert all(c.anchor is None or len(c.anchor) <= MAX_ANCHOR_LENGTH for c in chunks)


def test_a_stored_payload_that_is_not_of_its_kind_is_refused() -> None:
    with pytest.raises(PayloadError):
        chunk_payload(rid("thread", "t"), "thread", b'{"title": "not a thread"}')
    with pytest.raises(PayloadError):
        chunk_payload(rid("thread", "t"), "thread", b"[1, 2]")
    with pytest.raises(ValueError, match="carry no payload"):
        chunk_payload(rid("thread", "t"), "code_file", b"{}")
    with pytest.raises(ValueError, match="record id"):
        chunk_payload("not-an-id", "document", b'{"title": "T", "text": ""}')


def chunk_ids(chunks: tuple[Chunk, ...]) -> list[str]:
    return [c.id for c in chunks]


def test_the_same_payload_in_another_key_order_gives_the_same_chunks() -> None:
    payload = payload_of("meeting_planning")
    reordered = dict(reversed(list(payload.items())))
    record = rid("meeting", "m")
    first = chunk_payload(record, "meeting", json.dumps(payload).encode())
    second = chunk_payload(record, "meeting", json.dumps(reordered, indent=2).encode())
    assert first == second
    assert chunk_ids(first.chunks) == chunk_ids(second.chunks)
