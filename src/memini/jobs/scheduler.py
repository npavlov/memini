"""The scheduler: enqueue a sync job for every stream whose schedule is due.

A stream with a `schedule` (an interval) is due when its `next_run_at` has passed, or is NULL. For
each due stream the scheduler enqueues one job and moves `next_run_at` to now() plus the schedule,
in one statement. A stream with no schedule runs only when something enqueues a job for it.

The job kind follows the kind of the stream's source (`SYNC_KINDS`); streams of a source kind
without a sync kind are not scheduled. A scheduled job carries the stream's workspace, owner,
source and stream, and `stream_dedupe_key` as its dedupe key, so a stream never has more than one
sync queued or running. A stream that is due while its sync still runs skips that turn.

Several schedulers may run at once, one per service process. Each locks the due streams it takes
with FOR UPDATE SKIP LOCKED, and a stream another scheduler has just moved is no longer due, so a
due stream yields one job. Time is the database's now(); nothing compares the clock of this
process with it.
"""

import logging
import uuid
from collections.abc import Mapping
from datetime import timedelta
from typing import Any, Final, NamedTuple

import anyio
from psycopg import AsyncConnection

from memini.db import Pool
from memini.jobs.queue import check_kind

__all__ = ["SYNC_KINDS", "Fired", "Scheduler", "enqueue_due_streams", "stream_dedupe_key"]

log = logging.getLogger(__name__)

SYNC_KINDS: Final[Mapping[str, str]] = {"git": "git_sync", "mcp": "mcp_sync"}
"""Job kind of a stream sync, by the kind of the stream's source. Push sources are not synced."""


def stream_dedupe_key(stream_id: uuid.UUID) -> str:
    """The dedupe key of the sync jobs of a stream.

    A job enqueued by hand for a stream ("sync now") with this key and the stream's sync kind
    merges with a scheduled one, and the other way round.
    """
    return str(stream_id)


_ENQUEUE_DUE: Final = """
WITH due AS (
    SELECT s.id, s.workspace_id, s.owner_id, s.source_id, s.schedule, m.job_kind
    FROM stream s
    JOIN source src ON src.id = s.source_id
    JOIN unnest(%(source_kinds)s::text[], %(job_kinds)s::text[]) AS m (source_kind, job_kind)
        ON m.source_kind = src.kind
    WHERE s.schedule IS NOT NULL AND (s.next_run_at IS NULL OR s.next_run_at <= now())
    ORDER BY s.next_run_at NULLS FIRST, s.id
    LIMIT %(limit)s
    FOR UPDATE OF s SKIP LOCKED
), moved AS (
    UPDATE stream SET next_run_at = now() + due.schedule
    FROM due
    WHERE stream.id = due.id
    RETURNING due.id, due.workspace_id, due.owner_id, due.source_id, due.job_kind
), created AS (
    -- The dedupe key is stream_dedupe_key: the stream id as text.
    INSERT INTO job (workspace_id, owner_id, source_id, stream_id, kind, dedupe_key)
    SELECT workspace_id, owner_id, source_id, id, job_kind, id::text FROM moved
    ON CONFLICT (workspace_id, kind, dedupe_key) WHERE status IN ('queued', 'running') DO NOTHING
    RETURNING id
)
SELECT (SELECT count(*) FROM moved), (SELECT count(*) FROM created)
"""


class Fired(NamedTuple):
    """What one call of `enqueue_due_streams` did."""

    streams: int
    """Due streams taken; each has its next_run_at moved on."""
    jobs: int
    """Jobs created. Fewer than streams when a stream's previous sync is still queued or running."""


async def enqueue_due_streams(
    conn: AsyncConnection[Any],
    *,
    sync_kinds: Mapping[str, str] = SYNC_KINDS,
    limit: int = 100,
) -> Fired:
    """Take up to `limit` due streams, enqueue a sync of each, and move their next_run_at on.

    A stream whose previous sync is still queued or running gets no new job, but its next_run_at
    moves on all the same.
    """
    for kind in sync_kinds.values():
        check_kind(kind)
    source_kinds = list(sync_kinds)
    job_kinds = [sync_kinds[kind] for kind in source_kinds]
    async with conn.transaction():
        cur = await conn.execute(
            _ENQUEUE_DUE,
            {"source_kinds": source_kinds, "job_kinds": job_kinds, "limit": limit},
        )
        row = await cur.fetchone()
    if row is None:  # a query of aggregates returns one row
        raise RuntimeError("the scheduler query returned no row")
    return Fired(streams=int(row[0]), jobs=int(row[1]))


class Scheduler:
    """Calls `enqueue_due_streams` every `interval` until stopped.

    The interval bounds how late a due stream is enqueued, not how often a stream syncs: that is
    the stream's schedule.
    """

    def __init__(
        self,
        pool: Pool,
        *,
        sync_kinds: Mapping[str, str] = SYNC_KINDS,
        interval: timedelta = timedelta(seconds=10),
        batch: int = 100,
    ) -> None:
        for kind in sync_kinds.values():
            check_kind(kind)
        if interval <= timedelta(0) or batch < 1:
            raise ValueError("the interval must be positive and the batch at least 1")
        self._pool = pool
        self._sync_kinds: Final = dict(sync_kinds)
        self._interval = interval
        self._batch = batch
        self._stop_requested = False
        self._stopping: anyio.Event | None = None

    def stop(self) -> None:
        """Ask `run` to return after the current pass. Safe to call more than once, and early."""
        self._stop_requested = True
        if self._stopping is not None:
            self._stopping.set()

    async def tick(self) -> int:
        """One pass: enqueue every due stream, a batch at a time; return the jobs created."""
        created = 0
        async with self._pool.connection() as conn:
            while True:
                fired = await enqueue_due_streams(
                    conn, sync_kinds=self._sync_kinds, limit=self._batch
                )
                created += fired.jobs
                if fired.streams < self._batch:
                    return created

    async def run(self) -> None:
        """Pass over the streams every interval until `stop` is called."""
        self._stopping = stopping = anyio.Event()
        if self._stop_requested:
            stopping.set()
        while not stopping.is_set():
            try:
                created = await self.tick()
                if created:
                    log.info("scheduler enqueued %d stream syncs", created)
            except Exception:
                log.exception("scheduler pass failed")
            with anyio.move_on_after(self._interval.total_seconds()):
                await stopping.wait()
