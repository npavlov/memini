"""A synthetic message set for the benchmark: threads and meetings made up from a seed.

Nothing here comes from real data (CLAUDE.md, "Never commit"): people are invented names built from
syllables, addresses are under ``example.test``, and the text is drawn from a small vocabulary of
software-team topics. The same seed always gives the same records, so two runs ingest the same
bytes.

The shapes are the payloads of :mod:`memini.payloads`, sized like a busy team's: a thread has
2 to 40 messages in one channel, a meeting a summary with a few sections, action items and a
transcript of 20 to 200 segments. :func:`message_batches` yields them in batches for
:func:`memini.ingest.ingest`, so the whole set is never held in memory at once.
"""

import random
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from memini.ingest import RecordIn

__all__ = [
    "PHRASES",
    "TOPICS",
    "MessageSet",
    "message_batches",
    "person_names",
]

_FIRST: Final = ("Ar", "Bel", "Cor", "Dav", "El", "Fen", "Gar", "Hal", "Is", "Jor", "Kel", "Lun")
_MIDDLE: Final = ("a", "e", "i", "o", "u", "ae", "io", "ou")
_LAST: Final = ("n", "r", "th", "s", "x", "l", "ndra", "vin", "mar", "dek")

TOPICS: Final = (
    "billing export",
    "retry queue",
    "search latency",
    "onboarding flow",
    "invoice reminder",
    "rate limiter",
    "schema migration",
    "release train",
    "mobile login",
    "audit log",
    "webhook delivery",
    "cache warmup",
    "feature flag",
    "data retention",
    "access token",
    "report builder",
    "sync worker",
    "error budget",
    "nightly backup",
    "price table",
)
"""What the synthetic team talks about; queries of the benchmark are made from them too."""

PHRASES: Final = (
    "we decided to",
    "let's go with",
    "agreed to postpone",
    "the root cause was",
    "can we roll back",
    "who owns",
    "the deadline for",
    "please review",
    "blocked on",
    "shipped the fix for",
)

_VERBS: Final = (
    "fails",
    "times out",
    "retries",
    "drops",
    "duplicates",
    "slows down",
    "recovers",
    "needs a test",
    "needs an owner",
    "is ready",
)
_DETAILS: Final = (
    "after the last deploy",
    "for large accounts",
    "on the staging stack",
    "when the queue is full",
    "behind the feature flag",
    "in the eu region",
    "only on mondays",
    "with the new client",
    "since the schema change",
    "under load",
)
_IDENTIFIERS: Final = (
    "sync_stream",
    "RateLimiter.acquire",
    "export_invoices",
    "retry_with_backoff",
    "WebhookSender",
    "build_report",
    "TokenStore.rotate",
    "warm_cache",
)
_CHANNELS: Final = 24
_BASE: Final = datetime(2026, 6, 1, tzinfo=UTC)
_SPAN_DAYS: Final = 365


def person_names(rng: random.Random, count: int) -> list[str]:
    """``count`` distinct invented names: syllables, not names of anyone."""
    names: list[str] = []
    seen: set[str] = set()
    while len(names) < count:
        first = rng.choice(_FIRST) + rng.choice(_MIDDLE) + rng.choice(_LAST)
        last = rng.choice(_FIRST) + rng.choice(_MIDDLE) + rng.choice(_LAST) + "en"
        name = f"{first} {last}"
        if name not in seen:
            seen.add(name)
            names.append(name)
    return names


def _sentence(rng: random.Random) -> str:
    topic = rng.choice(TOPICS)
    match rng.randrange(5):
        case 0:
            return f"{rng.choice(PHRASES).capitalize()} the {topic} {rng.choice(_DETAILS)}."
        case 1:
            return f"The {topic} {rng.choice(_VERBS)} {rng.choice(_DETAILS)}."
        case 2:
            return f"I looked at `{rng.choice(_IDENTIFIERS)}`: the {topic} {rng.choice(_VERBS)}."
        case 3:
            return f"{rng.choice(PHRASES).capitalize()} {topic}? It {rng.choice(_VERBS)}."
        case _:
            return f"Notes on the {topic}: it {rng.choice(_VERBS)} {rng.choice(_DETAILS)}."


def _text(rng: random.Random, low: int, high: int) -> str:
    return " ".join(_sentence(rng) for _ in range(rng.randint(low, high)))


def _when(rng: random.Random) -> datetime:
    return _BASE - timedelta(seconds=rng.randrange(_SPAN_DAYS * 86400))


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _people(names: list[str]) -> list[dict[str, str]]:
    return [{"name": name, "email": _address(name)} for name in names]


def _address(name: str) -> str:
    return name.lower().replace(" ", ".") + "@example.test"


def _thread(rng: random.Random, number: int, team: list[str]) -> RecordIn:
    start = _when(rng)
    members = rng.sample(team, rng.randint(2, 6))
    messages: list[dict[str, Any]] = []
    moment = start
    for index in range(rng.randint(2, 40)):
        author = rng.choice(members)
        message: dict[str, Any] = {
            "id": f"m{number}-{index}",
            "author": author,
            "sent_at": _iso(moment),
            "text": _text(rng, 1, 4),
        }
        if rng.random() < 0.2:
            message["mentions"] = [rng.choice(members)]
        messages.append(message)
        moment += timedelta(minutes=rng.randint(1, 240))
    topic = rng.choice(TOPICS)
    payload = {
        "channel": f"#team-{number % _CHANNELS:02d}",
        "title": f"{topic.capitalize()}: {rng.choice(_VERBS)}",
        "messages": messages,
        "people": _people(members),
    }
    return RecordIn(
        kind="thread",
        external_id=f"thread-{number:06d}",
        occurred_at=start,
        payload=payload,
        external_url=f"https://chat.example.test/t/{number}",
    )


def _meeting(rng: random.Random, number: int, team: list[str]) -> RecordIn:
    start = _when(rng)
    minutes = rng.choice((15, 30, 45, 60, 90))
    participants = rng.sample(team, rng.randint(2, 8))
    topics = rng.sample(TOPICS, 3)
    summary = "\n\n".join(
        f"## {topic.capitalize()}\n\n{_text(rng, 2, 6)}\n\n- {_sentence(rng)}\n- {_sentence(rng)}"
        for topic in topics
    )
    action_items = [
        {
            "text": f"Follow up on the {rng.choice(topics)}: {rng.choice(_VERBS)}",
            "owner": rng.choice(participants),
            "due": _iso(start + timedelta(days=rng.randint(1, 14)))[:10],
        }
        for _ in range(rng.randint(1, 5))
    ]
    transcript: list[dict[str, Any]] = []
    offset = 0.0
    for _ in range(rng.randint(20, 200)):
        length = rng.uniform(4.0, 40.0)
        transcript.append(
            {
                "speaker": rng.choice(participants),
                "start": round(offset, 1),
                "end": round(offset + length, 1),
                "text": _text(rng, 1, 3),
            }
        )
        offset += length + rng.uniform(0.0, 3.0)
    payload = {
        "title": f"{topics[0].capitalize()} sync #{number}",
        "started_at": _iso(start),
        "ended_at": _iso(start + timedelta(minutes=minutes)),
        "participants": participants,
        "summary": summary,
        "action_items": action_items,
        "transcript": transcript,
        "people": _people(participants),
    }
    return RecordIn(
        kind="meeting",
        external_id=f"meeting-{number:06d}",
        occurred_at=start,
        payload=payload,
        external_url=f"https://meet.example.test/m/{number}",
    )


@dataclass(frozen=True, slots=True)
class MessageSet:
    """How many threads and meetings to make, from which seed, among how many people."""

    threads: int
    meetings: int
    seed: int = 1
    people: int = 120

    def __post_init__(self) -> None:
        if self.threads < 0 or self.meetings < 0:
            raise ValueError("the numbers of threads and meetings are 0 or more")
        if self.people < 8:
            raise ValueError("a message set needs at least 8 people")

    @property
    def records(self) -> int:
        return self.threads + self.meetings


def message_batches(spec: MessageSet, *, batch_size: int) -> Iterator[list[RecordIn]]:
    """The records of ``spec`` in batches of at most ``batch_size``: threads, then meetings."""
    if batch_size < 1:
        raise ValueError("a batch holds at least one record")
    rng = random.Random(spec.seed)  # noqa: S311 - synthetic data, not cryptography
    team = person_names(rng, spec.people)
    batch: list[RecordIn] = []
    makers = [(_thread, number) for number in range(spec.threads)] + [
        (_meeting, number) for number in range(spec.meetings)
    ]
    for make, number in makers:
        batch.append(make(rng, number, team))
        if len(batch) >= batch_size:
            yield batch
            batch = []
    if batch:
        yield batch
