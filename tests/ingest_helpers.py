"""Shared by the ingest tests: synthetic threads, meetings and documents. Every name, id, URL and
text here is made up."""

from datetime import UTC, datetime, timedelta
from typing import Any

from memini.ingest import RecordIn

T0 = datetime(2026, 3, 1, tzinfo=UTC)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)


def iso(when: datetime) -> str:
    return when.isoformat().replace("+00:00", "Z")


def thread_payload(
    at: datetime, text: str = "Shall we move the release to Friday?"
) -> dict[str, Any]:
    return {
        "channel": "general",
        "messages": [
            {
                "author": "Ann Example",
                "sent_at": iso(at - HOUR),
                "text": "The release candidate is ready.",
                "mentions": ["Bo Sample"],
            },
            {"author": "Bo Sample", "sent_at": iso(at), "text": text},
        ],
    }


def thread(
    external_id: str, at: datetime = T0 + 9 * HOUR, *, text: str = "Shall we move it to Friday?"
) -> RecordIn:
    return RecordIn(
        kind="thread",
        external_id=external_id,
        occurred_at=at,
        payload=thread_payload(at, text),
        external_url=f"https://chat.example.invalid/archives/{external_id}",
    )


def meeting(external_id: str, at: datetime = T0 + 14 * HOUR) -> RecordIn:
    return RecordIn(
        kind="meeting",
        external_id=external_id,
        occurred_at=at,
        payload={
            "title": "Release planning",
            "started_at": iso(at),
            "ended_at": iso(at + HOUR / 2),
            "participants": ["Ann Example", "Bo Sample", "Cy Placeholder"],
            "summary": "The release moves to Friday; Bo writes the notes.",
            "action_items": [{"owner": "Bo Sample", "text": "Write the release notes"}],
        },
    )


def document(external_id: str, text: str, at: datetime = T0) -> RecordIn:
    return RecordIn(
        kind="document",
        external_id=external_id,
        occurred_at=at,
        payload={"title": "Greenhouse handbook", "text": text},
    )


def tombstone(external_id: str, kind: str = "thread") -> RecordIn:
    return RecordIn(kind=kind, external_id=external_id, deleted=True)
