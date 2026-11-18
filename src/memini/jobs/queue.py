"""The job queue in Postgres: enqueue, claim, renew, finish. Plain SQL on one connection.

Table and life cycle: migration 0003. The worker (memini.jobs.worker) strings these functions
together; they are public so that a test or a command can drive the queue by hand.

Every function that changes a claimed job checks that the claim is still the caller's: the job is
running, under the same attempt and by the same worker. A claim that was lost (the lease ran out
and the job was put back) changes nothing, and the function says so by its result. A lease that
has run out cannot be renewed, even before the job is put back.

Time is the database's ``now()`` throughout: run_after, leases and backoff never compare a clock
of this process with one of the database.
"""

import hashlib
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final

from psycopg import AsyncConnection
from psycopg.rows import class_row
from psycopg.types.json import Jsonb

__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "JOB_KIND",
    "SOURCE_LOCK_NAMESPACE",
    "Enqueued",
    "Job",
    "check_kind",
    "claim",
    "complete",
    "enqueue",
    "fail",
    "release",
    "renew_lease",
    "source_lock_key",
    "unlock_source",
]

JOB_KIND: Final = re.compile(r"[a-z][a-z0-9_]*")
"""The form of a job kind; the same as run.kind, which a run of the job is filed under."""

DEFAULT_MAX_ATTEMPTS: Final = 5
"""Attempts before a job is quarantined, unless enqueue asks for another number."""

SOURCE_LOCK_NAMESPACE: Final = 0x6D737263
"""First key of every per-source advisory lock: "msrc" in ASCII. The second key comes from the
source id (`source_lock_key`). Two-key locks never collide with the 64-bit migration lock."""


@dataclass(frozen=True, slots=True)
class Job:
    """A claimed job, as its handler sees it."""

    id: uuid.UUID
    workspace_id: uuid.UUID
    owner_id: uuid.UUID
    source_id: uuid.UUID | None
    stream_id: uuid.UUID | None
    kind: str
    args: Mapping[str, Any]
    attempt: int
    """1 on the first claim. A claim given back at shutdown does not count."""
    max_attempts: int
    worker: str
    """The worker slot holding the claim."""


@dataclass(frozen=True, slots=True)
class Enqueued:
    """The result of `enqueue`."""

    id: uuid.UUID
    created: bool
    """False when a queued or running job with the same dedupe key was returned instead."""


def check_kind(kind: str) -> str:
    """Return `kind` if it is a valid job kind, else raise `ValueError`."""
    if not JOB_KIND.fullmatch(kind):
        raise ValueError(f"job kind {kind!r} does not match {JOB_KIND.pattern}")
    return kind


def source_lock_key(source_id: uuid.UUID) -> tuple[int, int]:
    """The two int4 keys of the advisory lock on a source.

    The second key is 32 bits of a hash of the source id. Two sources may share a key; then their
    jobs take turns, which costs parallelism and nothing else.
    """
    digest = hashlib.blake2b(source_id.bytes, digest_size=4).digest()
    return SOURCE_LOCK_NAMESPACE, int.from_bytes(digest, "big", signed=True)


_ENQUEUE: Final = """
INSERT INTO job (workspace_id, owner_id, source_id, stream_id, kind, args, dedupe_key,
                 max_attempts, run_after)
VALUES (%(workspace_id)s, %(owner_id)s, %(source_id)s, %(stream_id)s, %(kind)s, %(args)s,
        %(dedupe_key)s, %(max_attempts)s, now() + %(delay)s)
ON CONFLICT (workspace_id, kind, dedupe_key) WHERE status IN ('queued', 'running')
    -- A no-op update, so that RETURNING yields the job that is already there.
    DO UPDATE SET dedupe_key = excluded.dedupe_key
RETURNING id, old.id IS NULL
"""


async def enqueue(
    conn: AsyncConnection[Any],
    *,
    workspace_id: uuid.UUID,
    owner_id: uuid.UUID,
    kind: str,
    source_id: uuid.UUID | None = None,
    stream_id: uuid.UUID | None = None,
    args: Mapping[str, object] | None = None,
    dedupe_key: str | None = None,
    delay: timedelta = timedelta(0),
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> Enqueued:
    """Add a job to the queue; it is due `delay` after now.

    Runs in the caller's transaction, if one is open: a job enqueued together with the rows it
    is about becomes visible to workers with them, or not at all.

    With a `dedupe_key`, a queued or running job of the same workspace, kind and key is returned
    instead of a new one (`Enqueued.created` is False); it keeps its own arguments and due time.
    A finished job does not count, so enqueuing after it creates a new job.
    """
    check_kind(kind)
    cur = await conn.execute(
        _ENQUEUE,
        {
            "workspace_id": workspace_id,
            "owner_id": owner_id,
            "source_id": source_id,
            "stream_id": stream_id,
            "kind": kind,
            "args": Jsonb(dict(args or {})),
            "dedupe_key": dedupe_key,
            "max_attempts": max_attempts,
            "delay": delay,
        },
    )
    row = await cur.fetchone()
    if row is None:  # INSERT ... ON CONFLICT DO UPDATE returns one row either way
        raise RuntimeError("enqueue returned no row")
    return Enqueued(id=row[0], created=bool(row[1]))


# Put back running jobs whose lease ran out: their worker died or hung. The lost attempt counts.
_REAP: Final = """
UPDATE job SET
    status = CASE WHEN attempts >= max_attempts THEN 'quarantined' ELSE 'queued' END,
    finished_at = CASE WHEN attempts >= max_attempts THEN now() END,
    run_after = now(),
    lease_until = NULL,
    last_error = 'lease of ' || coalesce(worker, 'a worker') || ' expired; it died or hung',
    updated_at = now()
WHERE status = 'running' AND lease_until < now()
"""

_NEXT_DUE: Final = """
SELECT id, source_id FROM job
WHERE status = 'queued' AND run_after <= now() AND kind = ANY(%(kinds)s::text[])
  AND (source_id IS NULL OR source_id <> ALL(%(busy)s::uuid[]))
ORDER BY run_after, id
LIMIT 1
FOR UPDATE SKIP LOCKED
"""

_TAKE: Final = """
UPDATE job SET
    status = 'running',
    attempts = attempts + 1,
    lease_until = now() + %(lease)s,
    worker = %(worker)s,
    started_at = now(),
    updated_at = now()
WHERE id = %(id)s
RETURNING id, workspace_id, owner_id, source_id, stream_id, kind, args,
          attempts AS attempt, max_attempts, worker
"""


async def claim(
    conn: AsyncConnection[Any], *, kinds: Sequence[str], worker: str, lease: timedelta
) -> Job | None:
    """Claim the oldest due job of one of `kinds`; None when there is none.

    First puts back the jobs whose lease ran out. A job of a source is claimed only together with
    the session advisory lock of the source, taken on `conn`; a job whose source is locked by
    another session is passed over, and the next one is tried. The caller holds the returned job
    on `conn`: renews the lease there, finishes the job there, then calls `unlock_source`.

    If this function raises, `conn` may hold the lock of a source: close it rather than reuse it.
    """
    busy: list[uuid.UUID] = []
    async with conn.transaction():
        await conn.execute(_REAP)
        while True:
            cur = await conn.execute(_NEXT_DUE, {"kinds": list(kinds), "busy": busy})
            due = await cur.fetchone()
            if due is None:
                return None
            job_id, source_id = due
            if source_id is not None and not await _try_lock_source(conn, source_id):
                busy.append(source_id)
                continue
            async with conn.cursor(row_factory=class_row(Job)) as taken:
                await taken.execute(_TAKE, {"id": job_id, "lease": lease, "worker": worker})
                return await taken.fetchone()


async def _try_lock_source(conn: AsyncConnection[Any], source_id: uuid.UUID) -> bool:
    # Session level: it outlives the claim's transaction and lasts while the job runs.
    cur = await conn.execute("SELECT pg_try_advisory_lock(%s, %s)", source_lock_key(source_id))
    row = await cur.fetchone()
    return row is not None and bool(row[0])


async def unlock_source(conn: AsyncConnection[Any], source_id: uuid.UUID) -> bool:
    """Release the lock on a source taken by `claim` on `conn`; False if it was not held."""
    async with conn.transaction():
        cur = await conn.execute("SELECT pg_advisory_unlock(%s, %s)", source_lock_key(source_id))
        row = await cur.fetchone()
    return row is not None and bool(row[0])


def _mine(job: Job) -> dict[str, object]:
    """Parameters of the condition that a claim is still the caller's: the job runs, under the
    same attempt and the same worker. Every query below that changes a claimed job has it."""
    return {"id": job.id, "attempt": job.attempt, "worker": job.worker}


_RENEW: Final = """
UPDATE job SET lease_until = now() + %(lease)s, updated_at = now()
WHERE id = %(id)s AND status = 'running' AND attempts = %(attempt)s AND worker = %(worker)s
  AND lease_until >= now()
"""


async def renew_lease(conn: AsyncConnection[Any], job: Job, *, lease: timedelta) -> bool:
    """Extend the lease of a claimed job to `lease` from now; False if the claim was lost.

    A lease that has run out is lost even before another claim puts the job back: the worker
    counts as dead from then on, and must stop the job.
    """
    async with conn.transaction():
        cur = await conn.execute(_RENEW, {**_mine(job), "lease": lease})
    return cur.rowcount == 1


_COMPLETE: Final = """
UPDATE job SET status = 'succeeded', finished_at = now(), lease_until = NULL, updated_at = now()
WHERE id = %(id)s AND status = 'running' AND attempts = %(attempt)s AND worker = %(worker)s
"""


async def complete(conn: AsyncConnection[Any], job: Job) -> bool:
    """Mark a claimed job succeeded; False if the claim was lost."""
    async with conn.transaction():
        cur = await conn.execute(_COMPLETE, _mine(job))
    return cur.rowcount == 1


_FAIL: Final = """
UPDATE job SET
    status = CASE WHEN attempts >= max_attempts THEN 'quarantined' ELSE 'queued' END,
    finished_at = CASE WHEN attempts >= max_attempts THEN now() END,
    run_after = CASE WHEN attempts >= max_attempts THEN run_after ELSE now() + %(retry_in)s END,
    lease_until = NULL,
    last_error = %(error)s,
    updated_at = now()
WHERE id = %(id)s AND status = 'running' AND attempts = %(attempt)s AND worker = %(worker)s
RETURNING status
"""


async def fail(
    conn: AsyncConnection[Any], job: Job, *, error: str, retry_in: timedelta
) -> str | None:
    """Record a failed attempt of a claimed job.

    The job goes back to the queue, due `retry_in` from now, or into quarantine if it has used
    all its attempts. Returns the new status, ``queued`` or ``quarantined``; None if the claim
    was lost.
    """
    async with conn.transaction():
        cur = await conn.execute(_FAIL, {**_mine(job), "error": error, "retry_in": retry_in})
        row = await cur.fetchone()
    return None if row is None else str(row[0])


_RELEASE: Final = """
UPDATE job SET
    status = 'queued', attempts = attempts - 1, run_after = now(), lease_until = NULL,
    updated_at = now()
WHERE id = %(id)s AND status = 'running' AND attempts = %(attempt)s AND worker = %(worker)s
"""


async def release(conn: AsyncConnection[Any], job: Job) -> bool:
    """Put a claimed job back in the queue, due now, and give its attempt back.

    For a worker that stops before the job is done. False if the claim was lost.
    """
    async with conn.transaction():
        cur = await conn.execute(_RELEASE, _mine(job))
    return cur.rowcount == 1
