"""A record as an extractor reads it: :func:`record_input` from the record's stored payload.

The text is the record's normalized text as ingest stores it, and each part (a message, a
segment, the summary, the action items) carries its anchor, its own text, its rows in the
normalized text, who said it and when. Pure: the decision eval builds its inputs from fixture
files with it, the pipeline from stored records, and both get the same input for the same
content.
"""

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, Final

from memini.extraction.model import EXTRACTED_KINDS, Part, RecordInput, RecordRef
from memini.ingest.filtering import stored_form
from memini.payloads import (
    Meeting,
    RenderedMeeting,
    RenderedThread,
    Thread,
    one_line,
    parse_payload,
    render,
    thread_label,
)
from memini.redaction import redact_text

__all__ = ["ACTION_ITEMS_ANCHOR", "SUMMARY_ANCHOR", "record_input"]

SUMMARY_ANCHOR: Final = "summary"
"""The anchor of a meeting's summary, as chunks and evidence name it."""
ACTION_ITEMS_ANCHOR: Final = "action-items"


def _redacted(text: str) -> str:
    return redact_text(text).value


def _thread_parts(thread: Thread, rendered: RenderedThread) -> tuple[Part, ...]:
    return tuple(
        Part(
            anchor=message.id,
            kind="message",
            text=_redacted(message.text),
            rows=rows,
            who=message.author,
            at=message.sent_at,
        )
        for message, rows in zip(thread.messages, rendered.messages, strict=True)
    )


def _meeting_parts(meeting: Meeting, rendered: RenderedMeeting) -> tuple[Part, ...]:
    lines = rendered.text.split("\n")
    parts: list[Part] = []
    if rendered.summary is not None and meeting.summary is not None:
        parts.append(
            Part(
                anchor=SUMMARY_ANCHOR,
                kind="summary",
                text=_redacted(meeting.summary.strip()),
                rows=rendered.summary,
            )
        )
    if rendered.action_items is not None:
        rows = rendered.action_items
        parts.append(
            Part(
                anchor=ACTION_ITEMS_ANCHOR,
                kind="action-items",
                text=_redacted("\n".join(lines[rows.first : rows.last + 1])),
                rows=rows,
            )
        )
    for segment, rows in zip(meeting.transcript, rendered.segments, strict=True):
        at: datetime | None = None
        if segment.start is not None:
            at = meeting.started_at + timedelta(seconds=segment.start)
        parts.append(
            Part(
                anchor=segment.id,
                kind="segment",
                text=_redacted(segment.text),
                rows=rows,
                who=segment.speaker,
                at=at,
            )
        )
    return tuple(parts)


def record_input(
    *,
    workspace: str,
    ref: RecordRef,
    occurred_at: datetime,
    payload: Mapping[str, Any],
    external_url: str | None = None,
    record_id: str | None = None,
) -> RecordInput:
    """The extractor's view of a thread or a meeting with ``payload``, as stored: redacted
    (:mod:`memini.redaction`; the eval redacts a fixture's payload first, as ingest does).

    Raises:
        ValueError: ``ref`` is not of a kind decisions are extracted from.
        memini.payloads.PayloadError: the payload is not one of its kind.
    """
    if ref.kind not in EXTRACTED_KINDS:
        raise ValueError("decisions are extracted from threads and meetings only")
    _, digest, text = stored_form(ref.kind, payload)
    parsed = parse_payload(ref.kind, payload)
    rendered = render(parsed)
    if isinstance(parsed, Thread) and isinstance(rendered, RenderedThread):
        parts = _thread_parts(parsed, rendered)
        title = thread_label(parsed) or None
    elif isinstance(parsed, Meeting) and isinstance(rendered, RenderedMeeting):
        parts = _meeting_parts(parsed, rendered)
        title = one_line(parsed.title)
    else:  # pragma: no cover - parse_payload gives the kind asked for
        raise ValueError(f"a {ref.kind} payload parsed as something else")
    return RecordInput(
        workspace=workspace,
        ref=ref,
        occurred_at=occurred_at,
        content_hash=digest,
        text=text,
        parts=parts,
        title=title,
        external_url=external_url,
        record_id=record_id,
    )
