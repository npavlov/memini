"""The ingest contract (requirements, section 4): a batch of records for a source, stored in one
transaction with the move of its stream's checkpoint.

Everything that is not code enters here: the push API (``POST …/sources/{source}/records``,
:mod:`memini.api.ingest`) and, from M3, the executors of MCP recipes. :func:`ingest` takes a
:class:`Batch` and does the following.

**Checks** (:func:`prepare_batch`, before the database). Each item is a record of kind
``thread``, ``meeting`` or ``document`` with an ``external_id``, an ``occurred_at`` with a zone
and a ``payload`` (a JSON object with the fields its kind requires), or a tombstone: a kind and an
external id with ``deleted``. A kind and external id appear once per batch. Secrets and card
numbers in a payload, its external URL and the window's cursor are redacted
(:mod:`memini.redaction`, principle 11); the payload is then stored as its canonical JSON and
hashed (:mod:`memini.ingest.validate`). The limits (:class:`IngestLimits`) are the number of
items and the stored size of a payload. A refused batch touches nothing.

**Target.** The source must be a push or mcp source the caller may change; a git source is
refused, its records come from its mirror. The stream the batch names is created on first use,
in a transaction of its own, so that the batch's run can name it.

**One transaction** holds everything else:

1. The stream's row is locked: batches of one stream take turns, and each sees the checkpoint
   the one before it left.
2. A window claim (``from``, ``to``, ``complete``) is checked against that checkpoint
   (``stream.checked_until``). ``to`` may not be later than the database clock (with
   :data:`~memini.ingest.validate.MAX_CLOCK_SKEW` of slack). A complete window must start at or
   before the checkpoint, as an overlap margin does; one that starts after it would leave a gap
   counted as checked, and the batch is refused (:class:`WindowGapError`). An incomplete window
   may start anywhere: it never moves the checkpoint.
3. Records are upserted by their deterministic id (:func:`memini.ids.record_id` of the source,
   kind and external id). A record equal to its row (payload, time, URL; and stream, when the
   batch names one) is not written at all; one that differs is written, a tombstone revived. Its
   ``normalized_text`` (:func:`normalize`, the payload as plain text) is written with it.
4. Tombstones turn live records into tombstones (``deleted_at``) and delete their chunks and
   symbols. A tombstone for a record never stored, or already deleted, changes nothing.
5. A snapshot (a complete window with ``snapshot``) holds every live record of the stream whose
   ``occurred_at`` is in ``[from, to)``: the stream's records in that window that it does not
   hold become tombstones the same way.
6. The indexing hook (:class:`RecordIndexer`), if given, derives chunks from the records whose
   rows were written: :class:`PayloadIndexer` chunks each payload by its kind
   (:func:`memini.chunking.chunk_payload`) and writes the chunks through the index writer, which
   embeds only texts it has no vector for. Without a hook the records are stored without chunks
   and counted as not indexed; ``memini reindex`` indexes them.
7. A complete window that ends after the checkpoint moves it to ``to``, with the claim's cursor;
   one that ends at or after the stream's history frontier (``stream.checked_from``, where its
   checked range starts) moves the frontier back to ``from``. Both by compare-and-set against the
   row read in step 1 (:func:`~memini.ingest.checkpoint.advance_checkpoint`). The checkpoint
   never moves back nor the frontier forward, and an incomplete window leaves both where they
   were. A complete window older than the frontier, with a gap between them, stores its records
   and moves nothing.

Any failure rolls the whole transaction back: records, tombstones, chunks and checkpoint
(principle 7). Sending the same batch twice changes nothing the second time: no row is written,
the checkpoint is where the first batch left it (principle 6). Each batch writes a run row of kind
``ingest`` with its counts when ``runs`` is given, the values redacted by kind among them
(``redacted_card``); an incomplete window ends it as ``partial``.
"""

from memini.ingest.indexing import PayloadIndexer
from memini.ingest.model import (
    DEFAULT_LIMITS,
    INGESTIBLE_KINDS,
    INGESTIBLE_SOURCE_KINDS,
    Batch,
    BatchConflictError,
    BatchTooLargeError,
    Checkpoint,
    IngestKind,
    IngestLimits,
    IngestResult,
    InvalidBatchError,
    NotIngestibleError,
    PreparedBatch,
    PreparedRecord,
    PreparedTombstone,
    RecordIn,
    RecordIndexer,
    StoredRecord,
    StreamChangedError,
    WindowClaim,
    WindowGapError,
)
from memini.ingest.store import RUN_KIND, ingest
from memini.ingest.validate import (
    MAX_CLOCK_SKEW,
    MAX_CURSOR_SIZE,
    MAX_EXTERNAL_ID_BYTES,
    MAX_EXTERNAL_URL_LENGTH,
    MAX_PAYLOAD_DEPTH,
    MAX_STREAM_KEY_BYTES,
    canonical_json,
    content_hash,
    prepare_batch,
)
from memini.payloads import normalize

__all__ = [
    "DEFAULT_LIMITS",
    "INGESTIBLE_KINDS",
    "INGESTIBLE_SOURCE_KINDS",
    "MAX_CLOCK_SKEW",
    "MAX_CURSOR_SIZE",
    "MAX_EXTERNAL_ID_BYTES",
    "MAX_EXTERNAL_URL_LENGTH",
    "MAX_PAYLOAD_DEPTH",
    "MAX_STREAM_KEY_BYTES",
    "RUN_KIND",
    "Batch",
    "BatchConflictError",
    "BatchTooLargeError",
    "Checkpoint",
    "IngestKind",
    "IngestLimits",
    "IngestResult",
    "InvalidBatchError",
    "NotIngestibleError",
    "PayloadIndexer",
    "PreparedBatch",
    "PreparedRecord",
    "PreparedTombstone",
    "RecordIn",
    "RecordIndexer",
    "StoredRecord",
    "StreamChangedError",
    "WindowClaim",
    "WindowGapError",
    "canonical_json",
    "content_hash",
    "ingest",
    "normalize",
    "prepare_batch",
]
