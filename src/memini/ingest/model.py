"""The values of the ingest contract: what a batch holds, what it becomes, what it did, and how it
can be refused. Independent of the transport; see :mod:`memini.ingest` for the semantics."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Literal, Protocol, get_args
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import TupleRow

from memini.config import Settings
from memini.index import IndexCounts

__all__ = [
    "DEFAULT_LIMITS",
    "INGESTIBLE_KINDS",
    "INGESTIBLE_SOURCE_KINDS",
    "Batch",
    "BatchConflictError",
    "BatchTooLargeError",
    "Checkpoint",
    "IngestKind",
    "IngestLimits",
    "IngestResult",
    "InvalidBatchError",
    "NotIngestibleError",
    "PreparedBatch",
    "PreparedRecord",
    "PreparedTombstone",
    "RecordIn",
    "RecordIndexer",
    "StoredRecord",
    "StreamChangedError",
    "WindowClaim",
    "WindowGapError",
]

type Conn = AsyncConnection[TupleRow]

IngestKind = Literal["thread", "meeting", "document"]
"""Kinds of record a batch may hold. ``code_file`` records come from git sources only."""

INGESTIBLE_KINDS: Final[tuple[IngestKind, ...]] = get_args(IngestKind)

INGESTIBLE_SOURCE_KINDS: Final[frozenset[str]] = frozenset({"push", "mcp"})
"""Kinds of source that take batches: push sources from clients, mcp sources from their recipe's
executors (M3). A git source's records come from its mirror, so a batch for one is refused."""


# --- Input ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class RecordIn:
    """One item of a batch: a record to store, or a tombstone (``deleted``) for one.

    A record has a ``kind``, an ``external_id``, an ``occurred_at`` with a zone and a ``payload``,
    a JSON object whose required fields depend on the kind (:mod:`memini.ingest.validate`). A
    tombstone has only the kind and the external id.
    """

    kind: str
    external_id: str
    occurred_at: datetime | None = None
    payload: Mapping[str, Any] | None = None
    external_url: str | None = None
    deleted: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class WindowClaim:
    """A claim that the batch covers the half-open window ``[start, end)`` of its stream (``from``
    and ``to`` on the wire).

    ``complete``: every record of the stream with activity in the window is in the batch or was
    stored before, so the stream's checkpoint may move to ``end``. ``snapshot`` (needs
    ``complete``): the batch holds every live record of the stream whose ``occurred_at`` is in the
    window, so the stream's stored records in the window that are not in it are deleted at the
    source. ``cursor``: where the client's next window starts, in its own terms (a page token, a
    time); stored as the stream's cursor when the checkpoint moves.
    """

    start: datetime
    end: datetime
    complete: bool
    snapshot: bool = False
    cursor: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Batch:
    """Records for one source, optionally through one stream, optionally claiming a window of it."""

    records: Sequence[RecordIn] = ()
    """Records and tombstones. None: a window in which nothing happened, or an empty snapshot."""
    stream: str | None = None
    """The key of the stream: a channel, a mailbox query, a meeting list. Created on first use."""
    window: WindowClaim | None = None
    """Needs ``stream``."""


@dataclass(frozen=True, slots=True)
class IngestLimits:
    """How much one batch may hold. The request body is limited by the transport."""

    max_records: int = 1000
    """Records and tombstones in one batch."""
    max_payload_size: int = 1024**2
    """Bytes of one record's payload in its stored form."""

    @classmethod
    def from_settings(cls, settings: Settings) -> "IngestLimits":
        return cls(
            max_records=settings.ingest_max_records,
            max_payload_size=settings.ingest_max_payload_size,
        )


DEFAULT_LIMITS: Final = IngestLimits()


# --- Checked -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PreparedRecord:
    """A record of the batch, checked, with its payload in the form it is stored."""

    index: int
    """Its position in the batch."""
    kind: IngestKind
    external_id: str
    occurred_at: datetime
    external_url: str | None
    payload: bytes
    """Canonical JSON of the payload (:func:`memini.ingest.validate.canonical_json`)."""
    content_hash: str
    """SHA-256 of ``payload``, 64 lowercase hex characters."""
    normalized_text: str
    """The payload as plain text (:func:`memini.payloads.normalize`)."""


@dataclass(frozen=True, slots=True)
class PreparedTombstone:
    """A tombstone of the batch, checked."""

    index: int
    kind: IngestKind
    external_id: str


@dataclass(frozen=True, slots=True)
class PreparedBatch:
    """A batch that passed every check that needs no database."""

    records: tuple[PreparedRecord, ...]
    tombstones: tuple[PreparedTombstone, ...]
    stream: str | None
    window: WindowClaim | None
    cursor: dict[str, Any] | None
    """The window's cursor as a plain JSON object, checked."""


# --- Output --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StoredRecord:
    """A record whose row a batch wrote, as the indexing hook receives it."""

    id: str
    kind: IngestKind
    external_id: str
    occurred_at: datetime
    content_hash: str
    payload: bytes
    content_changed: bool
    """New, revived from a tombstone, or with another payload; false when only ``occurred_at``,
    ``external_url`` or the stream changed."""


class RecordIndexer(Protocol):
    """The indexing hook: derives what the index needs from the records a batch wrote.

    Called once per batch, inside the batch's transaction, after its records and tombstones are
    written and before the checkpoint moves, with the records whose rows were written, by id. If
    it raises, nothing of the batch is stored. Tombstones need no hook: their chunks are deleted
    by the batch. It may return what it wrote to the index; those counts reach the batch's run
    report and result once the batch has committed. :class:`memini.ingest.PayloadIndexer` is the
    service's hook.
    """

    async def __call__(
        self, conn: Conn, records: Sequence[StoredRecord], /
    ) -> IndexCounts | None: ...


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """Where a stream's sync stands: everything from ``checked_from`` up to ``checked_until`` is
    stored (:mod:`memini.ingest.checkpoint`)."""

    checked_until: datetime | None
    cursor: Mapping[str, Any] | None
    checked_from: datetime | None = None
    """The history frontier. ``None`` before the first complete window, and where the start of
    the range was never recorded (it is then taken to be ``checked_until``)."""


@dataclass(frozen=True, slots=True)
class IngestResult:
    """What a batch did. Each item of the batch is counted once, in ``inserted``, ``updated``,
    ``revived``, ``deleted`` or ``unchanged``."""

    workspace_id: UUID
    source_id: UUID
    stream_id: UUID | None
    stream: str | None
    run_id: UUID | None
    """The run row of the batch, if one was recorded."""
    inserted: int
    """Records stored for the first time."""
    updated: int
    """Live records written again: another payload, time, URL or stream."""
    revived: int
    """Tombstones that came back as records."""
    deleted: int
    """Live records the batch's tombstones turned into tombstones."""
    unchanged: int
    """Records equal to their stored row, and tombstones of records that were not live: no write."""
    reconciled: int
    """Live records of the stream in a snapshot's window that the snapshot left out: tombstones
    now."""
    checkpoint: Checkpoint | None
    """The stream's checkpoint after the batch; ``None`` without a stream."""
    checkpoint_moved: bool
    window_complete: bool | None
    """``None`` without a window claim."""
    index: IndexCounts = field(default_factory=IndexCounts)
    """What the batch wrote to the index: the chunks the indexing hook wrote, and the chunks
    deleted with tombstones."""
    not_indexed: int = 0
    """Records written without their chunks, because no indexing hook was given: they are
    searchable once ``memini reindex`` has indexed them."""


# --- Errors --------------------------------------------------------------------------------------


class InvalidBatchError(ValueError):
    """The batch was refused before anything was stored. ``field`` names where (``records[3].
    payload.messages[0].sent_at``) and ``reason`` why; neither ever holds a value of the batch."""

    def __init__(self, field: str, reason: str) -> None:
        super().__init__(f"{field}: {reason}")
        self.field = field
        self.reason = reason


class BatchTooLargeError(InvalidBatchError):
    """The batch holds more records, or a record a larger payload, than the limits allow."""

    def __init__(
        self, field: str, reason: str, *, code: Literal["too_many_records", "payload_too_large"]
    ) -> None:
        super().__init__(field, reason)
        self.code: Literal["too_many_records", "payload_too_large"] = code


class NotIngestibleError(Exception):
    """The source's kind takes no batches: a git source's records come from its mirror."""

    def __init__(self, kind: str) -> None:
        super().__init__(f"{kind} sources take no batches of records")
        self.kind = kind


class WindowGapError(Exception):
    """A complete window starts after the stream's checkpoint: the time between them would count
    as checked without having been. Nothing was stored."""

    def __init__(self, checked_until: datetime) -> None:
        super().__init__(
            f"the window starts after the stream's checkpoint {checked_until.isoformat()}; a"
            " complete window starts at or before it"
        )
        self.checked_until = checked_until


class StreamChangedError(Exception):
    """The stream's checkpoint changed under the batch; nothing was stored."""


class BatchConflictError(Exception):
    """Another batch held the same records in another order (a deadlock) and this one was rolled
    back. Sending it again is safe."""
