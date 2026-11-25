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
from memini.ingest.catalog import write_record_catalog
from memini.ingest.checkpoint import advance_checkpoint
from memini.ingest.filtering import filter_batch, read_source_filters
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
)
from memini.ingest.validate import MAX_CLOCK_SKEW, prepare_batch
from memini.jobs.extraction import enqueue_extraction
from memini.persons import LinkCounts, link_records
from memini.removal import forgotten_record_ids
from memini.runs import Run, current_run, record_run
from memini.workspaces import NotFoundError, get_writable_workspace

__all__ = ["RUN_KIND", "ingest"]

type Conn = AsyncConnection[TupleRow]

RUN_KIND: Final = "ingest"
"""The kind of the run row each batch writes."""

_EXTRACTED_KINDS: Final = frozenset({"thread", "meeting"})
"""The kinds of record whose new or changed content gets an extraction job (requirements,
section 5; :data:`memini.extraction.EXTRACTED_KINDS`): documents and code are not extracted."""


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

# A record pushed as sensitive is marked; one marked already keeps its mark, reason and time.
_MARK: Final = (
    "UPDATE record SET sensitive = true, sensitive_at = now()"
    " WHERE id = ANY(%s::text[]) AND workspace_id = %s AND NOT sensitive"
)

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
    "UPDATE stream SET checked_until = %s, checked_from = %s, cursor = %s"
    " WHERE id = %s AND checked_until IS NOT DISTINCT FROM %s"
    " AND checked_from IS NOT DISTINCT FROM %s AND cursor IS NOT DISTINCT FROM %s::jsonb"
)


@dataclass(slots=True)
class _Tally:
    inserted: int = 0
    updated: int = 0
    revived: int = 0
    deleted: int = 0
    unchanged: int = 0
    reconciled: int = 0
    forgotten: int = 0
    checkpoint: Checkpoint | None = None
    moved: bool = False
    index: IndexCounts = field(default_factory=IndexCounts)
    """Chunks and symbols the indexing hook wrote and the tombstones deleted; reported only after
    the batch commits."""
    not_indexed: int = 0
    """Records written without an indexing hook."""
    persons: LinkCounts = field(default_factory=LinkCounts)
    """Persons, aliases and links the batch's records made; reported after it commits."""
    extractions: int = 0
    """Extraction jobs the batch enqueued (or found queued) for its threads and meetings."""
    placed: int = 0
    """Rows of record_catalog the batch wrote or deleted: projects and domains pushed."""


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
    extract: bool,
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
                "SELECT now(), checked_until, cursor, checked_from FROM stream WHERE id = %s"
                " FOR NO KEY UPDATE",
                (target.stream_id,),
            )
            row = await cur.fetchone()
            if row is None:  # the source was deleted since the stream was found
                raise NotFoundError("source")
            before = Checkpoint(checked_until=row[1], cursor=row[2], checked_from=row[3])
        if row is None:  # SELECT now() returns a row
            raise RuntimeError("SELECT now() returned no row")
        now: datetime = row[0]
        tally.checkpoint = before

        after: Checkpoint | None = None
        if window is not None:
            if window.end > now + MAX_CLOCK_SKEW:
                raise InvalidBatchError("window.to", "later than the service's clock")
            if before is not None:
                # Where a complete window moves the checked range; a gap is refused here.
                after = advance_checkpoint(before, window, batch.cursor)

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
        # Records forgotten by hand are not stored again. Checked after the lock: forget deletes
        # the row and writes its entry in one transaction, so a batch that waited for the row
        # sees the entry here (memini.removal.forgotten_record_ids).
        forgotten = await forgotten_record_ids(conn, [ids[item.index] for item in batch.records])
        tally.forgotten = sum(1 for item in batch.records if ids[item.index] in forgotten)

        written: list[StoredRecord] = []
        if len(batch.records) > tally.forgotten:
            records = sorted(
                (item for item in batch.records if ids[item.index] not in forgotten),
                key=lambda item: ids[item.index],
            )
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
                        payload=item.payload,  # redacted in prepare_batch, as stored
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

        if written:
            # The persons hook (memini.persons): who appears in each record written, resolved to
            # persons by their aliases, and the record's links to them. It reads the payload as
            # stored. After the indexer, so the lock it may take on the workspace's persons is
            # held only until the commit.
            tally.persons = await link_records(
                conn,
                workspace_id=target.workspace_id,
                owner_id=access.owner_id,
                source_id=target.source_id,
                records=written,
            )

        # The catalog hook (memini.ingest.catalog): the project and domains each stored record
        # was pushed with, as a diff, also for a record whose row was not written again.
        tally.placed = await write_record_catalog(
            conn,
            workspace_id=target.workspace_id,
            owner_id=access.owner_id,
            source_id=target.source_id,
            records=[item for item in batch.records if ids[item.index] not in forgotten],
            ids=ids,
        )

        # The sensitivity hook (memini.sensitivity): a record pushed as sensitive is marked, also
        # when its row was not written again. A push never clears a mark.
        marked = sorted(
            ids[item.index]
            for item in batch.records
            if item.sensitive and ids[item.index] not in forgotten
        )
        if marked:
            await conn.execute(_MARK, (marked, target.workspace_id))
        sensitive = frozenset(marked)

        if extract:
            # The extraction hook (memini.jobs.extraction): a job for each thread and meeting whose
            # content is new or changed, visible to workers when the batch commits. A sensitive
            # record is never sent to a model.
            for record in sorted(written, key=lambda record: record.id):
                if record.id in sensitive:
                    continue
                if record.kind in _EXTRACTED_KINDS and record.content_changed:
                    await enqueue_extraction(
                        conn,
                        workspace_id=target.workspace_id,
                        owner_id=access.owner_id,
                        source_id=target.source_id,
                        record_id=record.id,
                        content_hash=record.content_hash,
                    )
                    tally.extractions += 1

        if after is not None and before is not None:
            cur = await conn.execute(
                _MOVE_CHECKPOINT,
                (
                    after.checked_until,
                    after.checked_from,
                    _jsonb(after.cursor),
                    target.stream_id,
                    before.checked_until,
                    before.checked_from,
                    _jsonb(before.cursor),
                ),
            )
            if cur.rowcount != 1:
                raise StreamChangedError("the stream's checkpoint changed under the batch")
            tally.checkpoint = after
            tally.moved = after.checked_until != before.checked_until
    return tally


def _count(run: Run | None, tally: _Tally, batch: PreparedBatch) -> None:
    if run is None:
        return
    # What redaction replaced in the batch, by kind (memini.redaction): counts, never values.
    for kind, value in sorted(batch.redactions.items()):
        run.count(f"redacted_{kind}", value)
    # What the source's filters left out (memini.ingest.filtering): counts, never values.
    for name, value in sorted(batch.filtered.items()):
        run.count(name, value)
    for name, value in (
        ("records_inserted", tally.inserted),
        ("records_updated", tally.updated),
        ("records_revived", tally.revived),
        ("records_deleted", tally.deleted),
        ("records_unchanged", tally.unchanged),
        ("records_reconciled", tally.reconciled),
        ("records_forgotten", tally.forgotten),
        ("records_not_indexed", tally.not_indexed),
    ):
        run.count(name, value)
    for attribute, name in RUN_COUNTS.items():
        run.count(name, getattr(tally.index, attribute))
    for name, value in (
        ("persons_created", tally.persons.persons_created),
        ("aliases_created", tally.persons.aliases_created),
        ("person_links_inserted", tally.persons.links_inserted),
        ("person_links_updated", tally.persons.links_updated),
        ("person_links_deleted", tally.persons.links_deleted),
        ("extractions_enqueued", tally.extractions),
    ):
        run.count(name, value)
    if tally.placed:
        run.count("record_places_written", tally.placed)


async def _store_once(
    conn: Conn,
    access: WriteAccess,
    target: _Target,
    batch: PreparedBatch,
    indexer: RecordIndexer | None,
    extract: bool,
) -> _Tally:
    try:
        return await _store(conn, access, target, batch, indexer, extract)
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
    extract: bool = False,
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
        extract: enqueue an extraction job (:data:`memini.jobs.EXTRACT_KIND`) for each thread and
            meeting the batch writes with new or changed content, in the batch's transaction.
            The service passes it when an LLM provider is configured, and so a worker runs them;
            without one, nothing is enqueued and nothing waits for a handler that never comes.

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
    # --- Source filters (memini.filters, memini.ingest.filtering) ------------------------------
    # The source's rules, applied to the payloads in their stored form (redacted, canonical), as
    # prune reads them: an excluded record becomes a tombstone of itself, a thread loses the
    # messages left out. Everything below, the stored bytes, hashes and every hook, sees the
    # filtered batch.
    prepared = filter_batch(prepared, await read_source_filters(conn, target.source_id))
    # --- End of source filters -----------------------------------------------------------------
    if runs is None:
        tally = await _store_once(conn, access, target, prepared, indexer, extract)
        _count(current_run(), tally, prepared)
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
            tally = await _store_once(conn, access, target, prepared, indexer, extract)
        _count(run, tally, prepared)
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
        forgotten=tally.forgotten,
    )
