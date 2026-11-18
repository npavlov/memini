"""Storing a batch: records, tombstones, the snapshot reconcile and the checkpoint, in one
transaction. See :mod:`memini.ingest` for the contract."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from psycopg import AsyncConnection, errors, pq, sql
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from memini.auth import WriteAccess
from memini.ids import record_id
from memini.index import IndexCounts, delete_record_chunks
from memini.index.writer import RUN_COUNTS
from memini.ingest.model import (
    DEFAULT_LIMITS,
    INGESTIBLE_SOURCE_KINDS,
    Batch,
    BatchConflictError,
    Checkpoint,
    IngestLimits,
    IngestResult,
    InvalidBatchError,
    NotIngestibleError,
    PreparedBatch,
    RecordIndexer,
    StoredRecord,
    StreamChangedError,
    WindowGapError,
)
from memini.ingest.validate import MAX_CLOCK_SKEW, prepare_batch
from memini.runs import Run, current_run, record_run
from memini.workspaces import NotFoundError, get_writable_workspace

__all__ = ["RUN_KIND", "ingest"]

type Conn = AsyncConnection[TupleRow]

RUN_KIND: Final = "ingest"
"""The kind of the run row each batch writes."""


@dataclass(frozen=True, slots=True)
class _Target:
    workspace_id: UUID
    source_id: UUID
    stream_id: UUID | None


async def _resolve(
    conn: Conn, access: WriteAccess, workspace: str, source: str, stream: str | None
) -> _Target:
    """Find the source the batch is for and the stream it names, creating the stream on first use.

    Commits on its own, before the batch's transaction, so that the batch's run row can name the
    stream. A stream created for a batch that is then refused stays, without a checkpoint.
    """
    async with conn.transaction():
        found = await get_writable_workspace(conn, access, workspace)
        query = sql.SQL(
            "SELECT s.id, s.kind FROM source s WHERE s.workspace_id = %s AND s.name = %s AND {}"
        ).format(
            access.where(
                workspace_id=sql.Identifier("s", "workspace_id"),
                owner_id=sql.Identifier("s", "owner_id"),
                source_id=sql.Identifier("s", "id"),
            )
        )
        cur = await conn.execute(query, (found.id, source))
        row = await cur.fetchone()
        if row is None:
            raise NotFoundError("source")
        source_id: UUID = row[0]
        kind: str = row[1]
        if kind not in INGESTIBLE_SOURCE_KINDS:
            raise NotIngestibleError(kind)
        if stream is None:
            return _Target(found.id, source_id, None)
        await conn.execute(
            "INSERT INTO stream (workspace_id, owner_id, source_id, key) VALUES (%s, %s, %s, %s)"
            " ON CONFLICT (source_id, key) DO NOTHING",
            (found.id, access.owner_id, source_id, stream),
        )
        cur = await conn.execute(
            "SELECT id FROM stream WHERE source_id = %s AND key = %s", (source_id, stream)
        )
        row = await cur.fetchone()
        if row is None:  # inserted or found just now, under the source's foreign key
            raise RuntimeError("the stream of the batch vanished")
        return _Target(found.id, source_id, row[0])


# The records of the batch, by id: each one inserted, or written again where anything differs.
# The stream stays when the batch names none. The normalized text is derived from the payload, so
# it is written with it.
_UPSERT: Final = """
INSERT INTO record AS r (id, workspace_id, owner_id, source_id, stream_id, kind, external_id,
                         external_url, content_hash, occurred_at, payload, normalized_text)
SELECT u.id, %(workspace_id)s, %(owner_id)s, %(source_id)s, %(stream_id)s::uuid, u.kind,
       u.external_id, u.external_url, u.content_hash, u.occurred_at, u.payload, u.normalized_text
FROM unnest(%(ids)s::text[], %(kinds)s::text[], %(external_ids)s::text[], %(urls)s::text[],
            %(hashes)s::text[], %(times)s::timestamptz[], %(payloads)s::bytea[],
            %(texts)s::text[])
     AS u (id, kind, external_id, external_url, content_hash, occurred_at, payload,
           normalized_text)
ORDER BY u.id
ON CONFLICT (id) DO UPDATE SET
    stream_id = coalesce(EXCLUDED.stream_id, r.stream_id),
    external_url = EXCLUDED.external_url,
    content_hash = EXCLUDED.content_hash,
    occurred_at = EXCLUDED.occurred_at,
    payload = EXCLUDED.payload,
    normalized_text = EXCLUDED.normalized_text,
    deleted_at = NULL,
    updated_at = now()
WHERE r.content_hash <> EXCLUDED.content_hash
   OR r.deleted_at IS NOT NULL
   OR r.occurred_at <> EXCLUDED.occurred_at
   OR r.external_url IS DISTINCT FROM EXCLUDED.external_url
   OR (EXCLUDED.stream_id IS NOT NULL AND r.stream_id IS DISTINCT FROM EXCLUDED.stream_id)
RETURNING new.id, old.id IS NULL, old.deleted_at IS NOT NULL,
          old.content_hash IS DISTINCT FROM new.content_hash
"""

_TOMBSTONE: Final = (
    "UPDATE record SET deleted_at = now(), updated_at = now()"
    " WHERE source_id = %s AND id = ANY(%s::text[]) AND deleted_at IS NULL RETURNING id"
)

# The live records of the stream in the snapshot's window that the snapshot does not hold.
_ABSENT: Final = (
    "SELECT id FROM record WHERE stream_id = %s AND source_id = %s AND deleted_at IS NULL"
    " AND occurred_at >= %s AND occurred_at < %s AND id <> ALL(%s::text[])"
    " ORDER BY id FOR NO KEY UPDATE"
)

_MOVE_CHECKPOINT: Final = (
    "UPDATE stream SET checked_until = %s, cursor = %s"
    " WHERE id = %s AND checked_until IS NOT DISTINCT FROM %s"
    " AND cursor IS NOT DISTINCT FROM %s::jsonb"
)


@dataclass(slots=True)
class _Tally:
    inserted: int = 0
    updated: int = 0
    revived: int = 0
    deleted: int = 0
    unchanged: int = 0
    reconciled: int = 0
    checkpoint: Checkpoint | None = None
    moved: bool = False
    index: IndexCounts = field(default_factory=IndexCounts)
    """Chunks and symbols the indexing hook wrote and the tombstones deleted; reported only after
    the batch commits."""
    not_indexed: int = 0
    """Records written without an indexing hook."""


async def _tombstone(conn: Conn, tally: _Tally, source_id: UUID, ids: Sequence[str]) -> int:
    """Turn the live records among ``ids`` into tombstones and delete their chunks and symbols.

    Runs in the batch's transaction, so :func:`delete_record_chunks` does not report its counts:
    they go into ``tally.index`` and reach the run report once the batch commits.
    """
    if not ids:
        return 0
    cur = await conn.execute(_TOMBSTONE, (source_id, list(ids)))
    deleted: list[str] = sorted(row[0] for row in await cur.fetchall())
    for rid in deleted:
        tally.index += await delete_record_chunks(conn, record_id=rid)
    return len(deleted)


def _jsonb(value: object) -> Jsonb | None:
    return None if value is None else Jsonb(value)


async def _store(
    conn: Conn,
    access: WriteAccess,
    target: _Target,
    batch: PreparedBatch,
    indexer: RecordIndexer | None,
) -> _Tally:
    tally = _Tally()
    window = batch.window
    async with conn.transaction():
        before: Checkpoint | None = None
        if target.stream_id is None:
            cur = await conn.execute("SELECT now()")
            row = await cur.fetchone()
        else:
            # Batches of one stream take turns here; each sees the checkpoint the last one left.
            cur = await conn.execute(
                "SELECT now(), checked_until, cursor FROM stream WHERE id = %s FOR NO KEY UPDATE",
                (target.stream_id,),
            )
            row = await cur.fetchone()
            if row is None:  # the source was deleted since the stream was found
                raise NotFoundError("source")
            before = Checkpoint(checked_until=row[1], cursor=row[2])
        if row is None:  # SELECT now() returns a row
            raise RuntimeError("SELECT now() returned no row")
        now: datetime = row[0]
        tally.checkpoint = before

        if window is not None:
            if window.end > now + MAX_CLOCK_SKEW:
                raise InvalidBatchError("window.to", "later than the service's clock")
            if (
                window.complete
                and before is not None
                and before.checked_until is not None
                and window.start > before.checked_until
            ):
                raise WindowGapError(before.checked_until)

        ids = {
            item.index: record_id(target.source_id, item.kind, item.external_id)
            for item in (*batch.records, *batch.tombstones)
        }
        if ids:
            # Locks in one order, so that two batches holding the same records cannot deadlock.
            await conn.execute(
                "SELECT id FROM record WHERE id = ANY(%s::text[]) ORDER BY id FOR NO KEY UPDATE",
                (sorted(ids.values()),),
            )

        written: list[StoredRecord] = []
        if batch.records:
            records = sorted(batch.records, key=lambda item: ids[item.index])
            by_id = {ids[item.index]: item for item in records}
            cur = await conn.execute(
                _UPSERT,
                {
                    "workspace_id": target.workspace_id,
                    "owner_id": access.owner_id,
                    "source_id": target.source_id,
                    "stream_id": target.stream_id,
                    "ids": [ids[item.index] for item in records],
                    "kinds": [item.kind for item in records],
                    "external_ids": [item.external_id for item in records],
                    "urls": [item.external_url for item in records],
                    "hashes": [item.content_hash for item in records],
                    "times": [item.occurred_at for item in records],
                    "payloads": [item.payload for item in records],
                    "texts": [item.normalized_text for item in records],
                },
            )
            for rid, inserted, revived, content_changed in await cur.fetchall():
                item = by_id[rid]
                if inserted:
                    tally.inserted += 1
                elif revived:
                    tally.revived += 1
                else:
                    tally.updated += 1
                written.append(
                    StoredRecord(
                        id=rid,
                        kind=item.kind,
                        external_id=item.external_id,
                        occurred_at=item.occurred_at,
                        content_hash=item.content_hash,
                        payload=item.payload,
                        content_changed=bool(inserted or revived or content_changed),
                    )
                )
            tally.unchanged += len(records) - len(written)

        tombstone_ids = [ids[item.index] for item in batch.tombstones]
        tally.deleted = await _tombstone(conn, tally, target.source_id, tombstone_ids)
        tally.unchanged += len(tombstone_ids) - tally.deleted

        if window is not None and window.snapshot:
            cur = await conn.execute(
                _ABSENT,
                (
                    target.stream_id,
                    target.source_id,
                    window.start,
                    window.end,
                    sorted(ids.values()),
                ),
            )
            absent = [row[0] for row in await cur.fetchall()]
            tally.reconciled = await _tombstone(conn, tally, target.source_id, absent)

        if indexer is None:
            tally.not_indexed = len(written)
        elif written:
            # The indexing hook: the parsers of the kinds chunk and index the records here, so a
            # record and its chunks change in one transaction, as a file of a git source does.
            counts = await indexer(conn, sorted(written, key=lambda record: record.id))
            if counts is not None:
                tally.index += counts

        if (
            window is not None
            and window.complete
            and before is not None
            and (before.checked_until is None or window.end > before.checked_until)
        ):
            cur = await conn.execute(
                _MOVE_CHECKPOINT,
                (
                    window.end,
                    _jsonb(batch.cursor),
                    target.stream_id,
                    before.checked_until,
                    _jsonb(before.cursor),
                ),
            )
            if cur.rowcount != 1:
                raise StreamChangedError("the stream's checkpoint changed under the batch")
            tally.checkpoint = Checkpoint(checked_until=window.end, cursor=batch.cursor)
            tally.moved = True
    return tally


def _count(run: Run | None, tally: _Tally) -> None:
    if run is None:
        return
    for name, value in (
        ("records_inserted", tally.inserted),
        ("records_updated", tally.updated),
        ("records_revived", tally.revived),
        ("records_deleted", tally.deleted),
        ("records_unchanged", tally.unchanged),
        ("records_reconciled", tally.reconciled),
        ("records_not_indexed", tally.not_indexed),
    ):
        run.count(name, value)
    for attribute, name in RUN_COUNTS.items():
        run.count(name, getattr(tally.index, attribute))


async def _store_once(
    conn: Conn,
    access: WriteAccess,
    target: _Target,
    batch: PreparedBatch,
    indexer: RecordIndexer | None,
) -> _Tally:
    try:
        return await _store(conn, access, target, batch, indexer)
    except errors.DeadlockDetected:
        raise BatchConflictError(
            "another batch held the same records at the same time; send the batch again"
        ) from None
    except errors.ForeignKeyViolation:  # the source or stream was deleted meanwhile
        raise NotFoundError("source") from None


async def ingest(
    conn: Conn,
    access: WriteAccess,
    workspace: str,
    source: str,
    batch: Batch,
    *,
    limits: IngestLimits = DEFAULT_LIMITS,
    runs: AsyncConnectionPool[Any] | AsyncConnection[Any] | None = None,
    indexer: RecordIndexer | None = None,
) -> IngestResult:
    """Store a batch of records for the source ``source`` of the workspace ``workspace``.

    The batch is checked first (:func:`~memini.ingest.prepare_batch`); a refused batch touches
    nothing. Then its stream is created if it is new, and the batch is stored in one transaction:
    see :mod:`memini.ingest`. ``conn`` is idle (no transaction open): the batch commits its own.

    Args:
        runs: where the batch's run row is written (kind ``ingest``): the pool, or a connection
            used for nothing else. ``None``: no run row.
        indexer: the indexing hook (:class:`~memini.ingest.RecordIndexer`), the service's being
            :class:`~memini.ingest.PayloadIndexer`; ``None``: records are stored without their
            chunks (counted as ``records_not_indexed``), and ``memini reindex`` indexes them
            later.

    Raises:
        ValueError: ``conn`` has a transaction open.
        InvalidBatchError: the batch is not valid, or claims a window that ends in the future;
            :class:`~memini.ingest.BatchTooLargeError` if it is over a limit.
        NotFoundError: no such workspace or source that ``access`` may change.
        NotIngestibleError: the source is a git source.
        WindowGapError: a complete window starts after the stream's checkpoint.
        StreamChangedError, BatchConflictError: another writer got in the way; nothing was stored,
            and sending the batch again is safe.
        Exception: whatever ``indexer`` raises; nothing was stored. The service's hook raises
            :class:`~memini.index.IndexMismatchError` for a workspace whose index must be
            rebuilt first, and :class:`~memini.embeddings.EmbeddingError` when the model fails.
    """
    if conn.info.transaction_status != pq.TransactionStatus.IDLE:
        raise ValueError("ingest commits its own transactions: pass an idle connection")
    prepared = prepare_batch(batch, limits)
    target = await _resolve(conn, access, workspace, source, prepared.stream)
    if runs is None:
        tally = await _store_once(conn, access, target, prepared, indexer)
        _count(current_run(), tally)
        return _result(target, prepared, tally, run_id=None)
    async with record_run(
        runs,
        workspace_id=target.workspace_id,
        owner_id=access.owner_id,
        kind=RUN_KIND,
        source_id=target.source_id,
        stream_id=target.stream_id,
        progress_interval=None,
    ) as run:
        async with run.step("store"):
            tally = await _store_once(conn, access, target, prepared, indexer)
        _count(run, tally)
        window = prepared.window
        if window is not None and not window.complete:
            run.set_outcome("partial", "the window is incomplete; the checkpoint stays")
        return _result(target, prepared, tally, run_id=run.id)


def _result(
    target: _Target, batch: PreparedBatch, tally: _Tally, *, run_id: UUID | None
) -> IngestResult:
    return IngestResult(
        workspace_id=target.workspace_id,
        source_id=target.source_id,
        stream_id=target.stream_id,
        stream=batch.stream,
        run_id=run_id,
        inserted=tally.inserted,
        updated=tally.updated,
        revived=tally.revived,
        deleted=tally.deleted,
        unchanged=tally.unchanged,
        reconciled=tally.reconciled,
        checkpoint=tally.checkpoint,
        checkpoint_moved=tally.moved,
        window_complete=None if batch.window is None else batch.window.complete,
        index=tally.index,
        not_indexed=tally.not_indexed,
    )
