"""The chunkers of pushed records: threads, meetings and documents. The rules are in
:mod:`memini.chunking` (Pushed records); the payloads in :mod:`memini.payloads`."""

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Final, assert_never

from memini.chunking.chunker import Key, chunk_id
from memini.chunking.markdown import markdown_chunks
from memini.chunking.model import DEFAULT_SIZES, Chunk, ChunkKind, ChunkSizes, FileChunks
from memini.chunking.text import Lines, Span, check_record_id, fits, windows
from memini.ids import hash_fields
from memini.payloads import (
    Document,
    Meeting,
    Parsed,
    Rows,
    Segment,
    Thread,
    format_minute,
    format_offset,
    load_payload,
    meeting_time,
    one_line,
    render_document,
    render_meeting,
    render_thread,
    thread_label,
)

__all__ = [
    "BOUNDARY_DOMAIN",
    "DEFAULT_PAYLOAD_SIZES",
    "MAX_ANCHOR_LENGTH",
    "GroupRule",
    "PayloadSizes",
    "chunk_document_payload",
    "chunk_meeting",
    "chunk_parsed",
    "chunk_payload",
    "chunk_thread",
    "is_boundary",
]

BOUNDARY_DOMAIN: Final = "memini.chunk.boundary.v1"
"""Domain tag of the hash that says whether a group may start at a message or a segment."""

MAX_ANCHOR_LENGTH: Final = 1000
"""The longest anchor, as evidence takes one (:data:`memini.knowledge.MAX_ANCHOR_LENGTH`)."""

_MAX_HEADER_NAMES: Final = 8
"""Names in a header before the rest are counted: ``Ann, Bo … and 3 more``."""


_WINDOW_SIZES: Final = ChunkSizes(
    max_lines=50, max_chars=2000, merge_lines=40, small_lines=6, window_lines=50, window_overlap=5
)


@dataclass(frozen=True, slots=True)
class GroupRule:
    """Where a group of messages or segments ends (see :mod:`memini.chunking`).

    A group closes before an item when it holds ``max_items`` already, when the item would take
    it over the size limit, or when it holds at least ``min_items`` and the item's id hashes to
    a boundary, which one id in ``spread`` does.
    """

    min_items: int
    spread: int
    max_items: int

    def __post_init__(self) -> None:
        if not 1 <= self.min_items <= self.max_items or self.spread < 1:
            raise ValueError("expected 1 <= min_items <= max_items and spread >= 1")


@dataclass(frozen=True, slots=True)
class PayloadSizes:
    """The sizes of the chunks of pushed records."""

    windows: ChunkSizes = _WINDOW_SIZES
    """A group of messages or segments, and the action items of a meeting, span at most
    ``max_lines`` and ``max_chars``; one message or segment larger than that alone, and action
    items that do not fit, are cut into line windows by these sizes
    (:func:`memini.chunking.text.windows`)."""
    messages: GroupRule = GroupRule(min_items=3, spread=4, max_items=20)
    """A thread chunk holds six messages on average, at most twenty."""
    segments: GroupRule = GroupRule(min_items=8, spread=8, max_items=60)
    """A transcript window holds fifteen segments on average, at most sixty."""
    markdown: ChunkSizes = DEFAULT_SIZES
    """The summary of a meeting and a document are cut by the Markdown chunker with these."""


DEFAULT_PAYLOAD_SIZES: Final = PayloadSizes()


# --- Shared --------------------------------------------------------------------------------------


def is_boundary(item_id: str, spread: int) -> bool:
    """Whether a group may start at the message or segment ``item_id``: one id in ``spread``."""
    digest = hash_fields(BOUNDARY_DOMAIN, item_id.encode())
    return int(digest[:8], 16) % spread == 0


def _groups(
    lines: Lines, ids: Sequence[str], rows: Sequence[Rows], rule: GroupRule, limits: ChunkSizes
) -> list[list[int]]:
    """Consecutive items in groups, by index (see :class:`GroupRule`). The boundaries hang on the
    ids, not on the text, so an edited item changes its own group and seldom more."""
    groups: list[list[int]] = []
    current: list[int] = []
    for index, item_id in enumerate(ids):
        if current and (
            len(current) >= rule.max_items
            or not fits(lines, limits, rows[current[0]].first, rows[index].last)
            or (len(current) >= rule.min_items and is_boundary(item_id, rule.spread))
        ):
            groups.append(current)
            current = []
        current.append(index)
    if current:
        groups.append(current)
    return groups


def _names(names: Sequence[str]) -> str:
    """Distinct names in order, at most :data:`_MAX_HEADER_NAMES` of them, on one line."""
    distinct = list(dict.fromkeys(one_line(name) for name in names if one_line(name)))
    shown = ", ".join(distinct[:_MAX_HEADER_NAMES])
    more = len(distinct) - _MAX_HEADER_NAMES
    return f"{shown} and {more} more" if more > 0 else shown


def _header(*parts: str) -> str:
    return " · ".join(part for part in parts if part)


def _anchor(text: str) -> str:
    """``text`` as an anchor: at most :data:`MAX_ANCHOR_LENGTH` characters. Ids have no control
    characters (:mod:`memini.payloads`); a heading trail is put on one line first."""
    return text[:MAX_ANCHOR_LENGTH]


def _label(base: str, part: int) -> str:
    return f"{base}@{part}" if part else base


@dataclass(slots=True)
class _Out:
    """The chunks of one record as they are made."""

    record_id: str
    lines: Lines
    chunks: list[Chunk] = field(default_factory=list[Chunk])

    def add(
        self,
        span: Span,
        key: Key,
        label: str,
        kind: ChunkKind,
        header: str,
        *,
        anchor: str | None,
        url: str | None = None,
        occurred_at: datetime | None = None,
    ) -> None:
        first, last, byte_range = span
        if byte_range is not None:
            start, end = byte_range
        else:
            start, end = self.lines.start(first), self.lines.end(last)
        content = self.lines.slice(first, last, byte_range)
        self.chunks.append(
            Chunk(
                id=chunk_id(self.record_id, key),
                key=label,
                kind=kind,
                symbol=None,
                language=None,
                start_line=first + 1,
                end_line=last + 1,
                start_byte=start,
                end_byte=end,
                header=header,
                content=content,
                content_hash=hashlib.sha256(f"{header}\n{content}".encode()).hexdigest(),
                anchor=None if anchor is None else _anchor(anchor),
                url=url,
                occurred_at=occurred_at,
            )
        )

    def windows(
        self,
        rows: Rows,
        key: Key,
        label: str,
        kind: ChunkKind,
        header: str,
        sizes: ChunkSizes,
        *,
        anchor: str | None,
        url: str | None = None,
        occurred_at: datetime | None = None,
    ) -> None:
        """Rows ``rows`` as one chunk if they fit ``sizes``, else as line windows; a later window
        adds its number to the key and the label."""
        for part, span in enumerate(windows(self.lines, sizes, rows.first, rows.last)):
            suffix: Key = (str(part),) if part else ()
            self.add(
                span,
                (*key, *suffix),
                _label(label, part),
                kind,
                header,
                anchor=anchor,
                url=url,
                occurred_at=occurred_at,
            )

    def markdown(
        self,
        rows: Rows,
        header: str,
        sizes: ChunkSizes,
        *,
        key_prefix: Key,
        kind: ChunkKind | None,
        label: str | None,
        anchor: str | None,
        occurred_at: datetime | None = None,
    ) -> None:
        """Rows ``rows``, Markdown, cut by headings (:func:`memini.chunking.chunk_markdown`):
        ``header`` stands where the path stands. With ``label`` the chunks are named
        ``<label>:<trail>#n``, with ``anchor`` their anchors ``<anchor>/<trail>``; else a
        chunk's anchor is its trail (None before the first heading)."""
        content = "\n".join(self.lines.text[rows.first : rows.last + 1]).encode()
        offset = self.lines.start(rows.first)
        for chunk, trail in markdown_chunks(
            self.record_id, header, content, sizes=sizes, key_prefix=key_prefix
        ):
            name = chunk.key
            if label is not None:
                name = label if name == "(preamble)" else f"{label}:{name}"
            trail = one_line(trail)
            place = trail or None
            if anchor is not None:
                place = f"{anchor}/{trail}" if trail else anchor
            self.chunks.append(
                replace(
                    chunk,
                    key=name,
                    kind=kind or chunk.kind,
                    start_line=chunk.start_line + rows.first,
                    end_line=chunk.end_line + rows.first,
                    start_byte=chunk.start_byte + offset,
                    end_byte=chunk.end_byte + offset,
                    anchor=None if place is None else _anchor(place),
                    occurred_at=occurred_at,
                )
            )

    def result(self) -> FileChunks:
        if len({chunk.id for chunk in self.chunks}) != len(self.chunks):
            raise RuntimeError(f"two chunks of record {self.record_id} have the same key")
        return FileChunks(None, tuple(self.chunks), (), None, False)


# --- Threads -------------------------------------------------------------------------------------


def _time_range(first: datetime, last: datetime) -> str:
    """``2026-03-01 09:30 UTC``, ``2026-03-01 09:30 to 11:05 UTC`` or, over days, both dates."""
    start, end = format_minute(first), format_minute(last)
    if start == end:
        return start
    if start[:10] == end[:10]:
        return f"{start[:-4]} to {end[11:]}"
    return f"{start[:-4]} to {end}"


def chunk_thread(
    record_id: str, thread: Thread, *, sizes: PayloadSizes = DEFAULT_PAYLOAD_SIZES
) -> FileChunks:
    """The chunks of a thread: groups of consecutive messages (see :mod:`memini.chunking`)."""
    check_record_id(record_id)
    rendered = render_thread(thread)
    out = _Out(record_id, Lines(rendered.text.encode()))
    label = thread_label(thread)
    ids = [message.id for message in thread.messages]
    for group in _groups(out.lines, ids, rendered.messages, sizes.messages, sizes.windows):
        messages = [thread.messages[index] for index in group]
        first = messages[0]
        header = _header(
            label,
            _names([message.author for message in messages]),
            _time_range(
                min(message.sent_at for message in messages),
                max(message.sent_at for message in messages),
            ),
        )
        rows = Rows(rendered.messages[group[0]].first, rendered.messages[group[-1]].last)
        out.windows(
            rows,
            ("messages", first.id),
            f"msg-{first.id}",
            "messages",
            header,
            sizes.windows,
            anchor=f"msg-{first.id}",
            url=first.url,
            occurred_at=first.sent_at,
        )
    return out.result()


# --- Meetings ------------------------------------------------------------------------------------


def _offsets(segments: Sequence[Segment]) -> str:
    """The part of a meeting segments span, as far as they say: ``00:05 to 12:40``."""
    starts = [s.start for s in segments if s.start is not None]
    ends = [e for s in segments for e in (s.start, s.end) if e is not None]
    if not starts:
        return ""
    since, until = format_offset(min(starts)), format_offset(max(ends))
    return since if since == until else f"{since} to {until}"


def chunk_meeting(
    record_id: str, meeting: Meeting, *, sizes: PayloadSizes = DEFAULT_PAYLOAD_SIZES
) -> FileChunks:
    """The chunks of a meeting: the sections of its summary, its action items and windows of its
    transcript; its overview when it has none of them (see :mod:`memini.chunking`)."""
    check_record_id(record_id)
    rendered = render_meeting(meeting)
    out = _Out(record_id, Lines(rendered.text.encode()))
    base = _header(one_line(meeting.title), meeting_time(meeting))
    people = _header(base, f"with {_names(meeting.participants)}" if meeting.participants else "")
    start = meeting.started_at

    if rendered.summary is not None:
        out.markdown(
            rendered.summary,
            f"{people} · Summary",
            sizes.markdown,
            key_prefix=("summary",),
            kind="summary",
            label="summary",
            anchor="summary",
            occurred_at=start,
        )
    if rendered.action_items is not None:
        out.windows(
            rendered.action_items,
            ("action_items",),
            "action-items",
            "action_items",
            f"{people} · Action items",
            sizes.windows,
            anchor="action-items",
            occurred_at=start,
        )
    segments = meeting.transcript
    ids = [segment.id for segment in segments]
    for group in _groups(out.lines, ids, rendered.segments, sizes.segments, sizes.windows):
        members = [segments[index] for index in group]
        first = members[0]
        header = _header(
            base,
            f"Transcript {_offsets(members)}".rstrip(),
            _names([s.speaker for s in members if s.speaker is not None]),
        )
        out.windows(
            Rows(rendered.segments[group[0]].first, rendered.segments[group[-1]].last),
            ("transcript", first.id),
            f"seg-{first.id}",
            "transcript",
            header,
            sizes.windows,
            anchor=f"seg-{first.id}",
            occurred_at=start if first.start is None else start + timedelta(seconds=first.start),
        )
    if not out.chunks:
        out.windows(
            rendered.overview,
            ("overview",),
            "overview",
            "summary",
            base,
            sizes.windows,
            anchor="overview",
            occurred_at=start,
        )
    return out.result()


# --- Documents -----------------------------------------------------------------------------------


def chunk_document_payload(
    record_id: str, document: Document, *, sizes: PayloadSizes = DEFAULT_PAYLOAD_SIZES
) -> FileChunks:
    """The chunks of a pushed document: its text cut by headings, with its title where a file of
    a repository has its path; one chunk of the title when it has no text."""
    check_record_id(record_id)
    rendered = render_document(document)
    out = _Out(record_id, Lines(rendered.text.encode()))
    title = one_line(document.title)
    if rendered.body is None:
        out.windows(rendered.title, ("title",), "(title)", "section", title, sizes.windows,
                    anchor=None)  # fmt: skip
    else:
        out.markdown(
            rendered.body, title, sizes.markdown, key_prefix=(), kind=None, label=None, anchor=None
        )
    return out.result()


# --- Any payload ---------------------------------------------------------------------------------


def chunk_parsed(
    record_id: str, parsed: Parsed, *, sizes: PayloadSizes = DEFAULT_PAYLOAD_SIZES
) -> FileChunks:
    """The chunks of a parsed payload, by its kind."""
    match parsed:
        case Thread():
            return chunk_thread(record_id, parsed, sizes=sizes)
        case Meeting():
            return chunk_meeting(record_id, parsed, sizes=sizes)
        case Document():
            return chunk_document_payload(record_id, parsed, sizes=sizes)
        case _:  # pyright: ignore[reportUnnecessaryComparison]
            assert_never(parsed)


def chunk_payload(
    record_id: str, kind: str, payload: bytes, *, sizes: PayloadSizes = DEFAULT_PAYLOAD_SIZES
) -> FileChunks:
    """The chunks of a record of ``kind`` from its stored payload (canonical JSON).

    Raises:
        PayloadError: the payload is not one of its kind (:func:`memini.payloads.load_payload`).
        ValueError: ``kind`` carries no payload, or the record id is not 64 lowercase hex
            characters.
    """
    return chunk_parsed(record_id, load_payload(kind, payload), sizes=sizes)
