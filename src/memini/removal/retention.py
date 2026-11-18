"""Retention: the records of a source older than its ``retention_days`` are deleted with
everything derived from them. See :mod:`memini.removal`."""

from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal
from uuid import UUID

from psycopg import AsyncConnection, pq
from psycopg.rows import TupleRow

from memini.db import Pool
from memini.jobs import RETENTION_KIND, Handler, Job
from memini.removal.core import RemovalCounts, delete_records
from memini.runs import current_run

__all__ = [
    "DEFAULT_RETENTION_BATCH",
    "RETENTION",
    "RetentionResult",
    "expire_source",
    "retention_handler",
]

RETENTION: Final = RETENTION_KIND
"""The job kind of a retention pass over one source: ``retention``."""

DEFAULT_RETENTION_BATCH: Final = 1000
"""Records one transaction of a retention pass deletes at most (``retention_batch_size``)."""

type Conn = AsyncConnection[TupleRow]

ExpiryRule = Literal["occurred_at", "deleted_at"]
"""What makes a record expire: its ``occurred_at``, or for a git source its ``deleted_at``."""

_SOURCE: Final = """
SELECT workspace_id, kind, retention_days,
       coalesce(%s::timestamptz, now()) - make_interval(days => retention_days)
FROM source WHERE id = %s
"""

# A batch of expired records, oldest first along the index, locked; one an ingest batch holds is
# left for the next pass. Skipping instead of waiting, a pass never deadlocks with a batch.
_EXPIRED_BY_TIME: Final = (
    "SELECT id FROM record WHERE source_id = %s AND occurred_at < %s"
    " ORDER BY occurred_at LIMIT %s FOR UPDATE SKIP LOCKED"
)
_EXPIRED_TOMBSTONES: Final = (
    "SELECT id FROM record WHERE source_id = %s AND deleted_at < %s"
    " ORDER BY deleted_at LIMIT %s FOR UPDATE SKIP LOCKED"
)

# The expired records that knowledge evidence cites: their evidence reads as expired.
_FLAG_CITED: Final = """
INSERT INTO expired_record (workspace_id, owner_id, record_id, source_id, record_kind)
SELECT r.workspace_id, r.owner_id, r.id, r.source_id, r.kind
FROM record r
WHERE r.id = ANY(%s::text[]) AND EXISTS (
    SELECT 1 FROM knowledge_evidence e WHERE e.record_id = r.id AND e.workspace_id = r.workspace_id
)
ON CONFLICT (record_id) DO UPDATE SET expired_at = now()
"""


@dataclass(frozen=True, slots=True)
class RetentionResult:
    """What a retention pass over one source did."""

    source_id: UUID
    retention_days: int | None
    """``None``: the source keeps its records; nothing was deleted."""
    cutoff: datetime | None
    """Records older than this expired: by ``occurred_at``, or for a git source by
    ``deleted_at``."""
    rule: ExpiryRule | None
    removed: RemovalCounts
    cited: int
    """Expired records that knowledge evidence cites; their evidence reads as expired."""
    batches: int


async def expire_source(
    conn: Conn,
    source_id: UUID,
    *,
    as_of: datetime | None = None,
    batch_size: int = DEFAULT_RETENTION_BATCH,
) -> RetentionResult:
    """Delete the expired records of a source with their chunks, symbols, person links and raw
    responses (:func:`~memini.removal.delete_records`), ``batch_size`` records per transaction.

    A record expires when it is older than ``retention_days`` before ``as_of`` (the database clock
    by default): for a push or mcp source when its ``occurred_at`` is, live or a tombstone; for a
    git source when it is a tombstone deleted that long ago (a file of the tree at the pinned
    commit is current whatever its age). Expired records that knowledge evidence cites get an
    ``expired_record`` row in the same transaction, so their evidence reads as expired; nothing
    else keeps a trace of them.

    A record that an ingest batch holds is passed over and left for the next pass. The run being
    recorded, if any, gets each batch's counts once it has committed: ``records_expired``,
    ``records_expired_cited``, ``chunks_deleted``, ``symbols_deleted``, and the detail
    ``retention``. ``conn`` is idle: each batch commits its own transaction.

    Raises:
        ValueError: ``conn`` has a transaction open, or ``batch_size`` is below 1.
        LookupError: there is no such source.
    """
    if conn.info.transaction_status != pq.TransactionStatus.IDLE:
        raise ValueError("expire_source commits its own transactions: pass an idle connection")
    if batch_size < 1:
        raise ValueError("a retention batch holds at least one record")
    async with conn.transaction():
        cur = await conn.execute(_SOURCE, (as_of, source_id))
        row = await cur.fetchone()
    if row is None:
        raise LookupError(f"no source {source_id}")
    workspace_id: UUID = row[0]
    kind: str = row[1]
    days: int | None = row[2]
    cutoff: datetime | None = row[3]
    if days is None or cutoff is None:
        return RetentionResult(source_id, None, None, None, RemovalCounts(), 0, 0)

    rule: ExpiryRule = "deleted_at" if kind == "git" else "occurred_at"
    select = _EXPIRED_TOMBSTONES if rule == "deleted_at" else _EXPIRED_BY_TIME
    run = current_run()
    if run is not None:
        run.detail(
            "retention", {"retention_days": days, "cutoff": cutoff.isoformat(), "rule": rule}
        )
        run.count("records_expired", 0)  # reported when nothing expires too
    removed = RemovalCounts()
    cited = batches = 0
    while True:
        async with conn.transaction():
            cur = await conn.execute(select, (source_id, cutoff, batch_size))
            ids: list[str] = [row[0] for row in await cur.fetchall()]
            if not ids:
                break
            flagged = await conn.execute(_FLAG_CITED, (ids,))
            step = await delete_records(
                conn,
                workspace_id=workspace_id,
                source_id=source_id,
                record_ids=ids,
                raw_responses=False,
            )
        # The batch has committed: only now do its counts go to the run report.
        batches += 1
        removed += step
        cited += flagged.rowcount
        if run is not None:
            run.count("records_expired", step.records)
            run.count("records_expired_cited", flagged.rowcount)
            run.count("chunks_deleted", step.chunks)
            run.count("symbols_deleted", step.symbols)
            if step.raw_responses:
                run.count("raw_responses_deleted", step.raw_responses)
        if len(ids) < batch_size:
            break
    # Raw responses expire by age: the windows that ended at or before the cutoff. Imported here:
    # memini.responses imports memini.ingest, which imports this package.
    from memini.responses import delete_raw_responses_before

    await delete_raw_responses_before(conn, source_id=source_id, before=cutoff)
    return RetentionResult(source_id, days, cutoff, rule, removed, cited, batches)


def retention_handler(pool: Pool, *, batch_size: int = DEFAULT_RETENTION_BATCH) -> Handler:
    """The handler of ``retention`` jobs: :func:`expire_source` for the job's source.

    The scheduler enqueues one per source with a retention, every ``retention_interval``
    (:func:`memini.jobs.enqueue_due_retention`); the worker runs it under the source's lock, so it
    takes turns with the source's syncs.
    """

    async def handle(job: Job) -> None:
        if job.source_id is None:
            raise ValueError(f"{RETENTION} job {job.id} names no source")
        async with pool.connection() as conn:
            await expire_source(conn, job.source_id, batch_size=batch_size)

    return handle
