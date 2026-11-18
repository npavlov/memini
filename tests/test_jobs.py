"""The job queue, workers and scheduler (memini.jobs) against a real Postgres.

Leases, backoffs and poll intervals are fractions of a second, so the tests take seconds. Time
passing for a schedule is simulated by moving next_run_at back, never by sleeping it out.
"""

import dataclasses
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol

import anyio
import pytest
from psycopg import AsyncConnection, errors, sql
from psycopg.rows import TupleRow, dict_row

from memini.db import Pool, connect, open_pool
from memini.jobs import (
    Job,
    RetryPolicy,
    Scheduler,
    Worker,
    enqueue,
    enqueue_due_streams,
    source_lock_key,
    stream_dedupe_key,
)
from memini.jobs.queue import claim, complete, fail, unlock_source
from memini.runs import current_run

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

type Conn = AsyncConnection[TupleRow]

SYNC = "git_sync"
FAST_POLL = timedelta(milliseconds=20)


class Runnable(Protocol):
    async def run(self) -> None: ...
    def stop(self) -> None: ...


@dataclass(frozen=True)
class Seed:
    """One principal and workspace with two git sources, a and b, and one stream in each."""

    owner: uuid.UUID
    workspace: uuid.UUID
    source_a: uuid.UUID
    source_b: uuid.UUID
    stream_a: uuid.UUID
    stream_b: uuid.UUID


@pytest.fixture
async def pool(migrated_database_url: str) -> AsyncIterator[Pool]:
    async with open_pool(migrated_database_url, min_size=2, max_size=12, timeout=5) as pool:
        yield pool


@pytest.fixture
async def db(migrated_database_url: str) -> AsyncIterator[Conn]:
    """A connection outside the pool, in autocommit: each statement and each queue call commits."""
    async with connect(migrated_database_url, autocommit=True) as conn:
        yield conn


async def _scalar(conn: Conn, query: Any, params: Any = None) -> Any:
    cur = await conn.execute(query, params)
    row = await cur.fetchone()
    assert row is not None
    return row[0]


async def _add_source(
    conn: Conn, workspace: uuid.UUID, owner: uuid.UUID, kind: str = "git"
) -> uuid.UUID:
    return await _scalar(
        conn,
        "INSERT INTO source (workspace_id, owner_id, name, kind) VALUES (%s, %s, %s, %s)"
        " RETURNING id",
        (workspace, owner, f"src-{uuid.uuid4().hex[:8]}", kind),
    )


async def _add_stream(
    conn: Conn,
    workspace: uuid.UUID,
    owner: uuid.UUID,
    source: uuid.UUID,
    schedule: timedelta | None = None,
) -> uuid.UUID:
    return await _scalar(
        conn,
        "INSERT INTO stream (workspace_id, owner_id, source_id, key, schedule)"
        " VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (workspace, owner, source, f"refs/heads/{uuid.uuid4().hex[:8]}", schedule),
    )


@pytest.fixture
async def seed(db: Conn) -> Seed:
    owner = await _scalar(db, "INSERT INTO principal (name) VALUES ('me') RETURNING id")
    workspace = await _scalar(
        db, "INSERT INTO workspace (owner_id, name) VALUES (%s, 'alpha') RETURNING id", (owner,)
    )
    source_a = await _add_source(db, workspace, owner)
    source_b = await _add_source(db, workspace, owner)
    stream_a = await _add_stream(db, workspace, owner, source_a)
    stream_b = await _add_stream(db, workspace, owner, source_b)
    return Seed(owner, workspace, source_a, source_b, stream_a, stream_b)


async def add_job(
    pool: Pool, seed: Seed, source: uuid.UUID | None, *, kind: str = SYNC, **options: Any
) -> uuid.UUID:
    async with pool.connection() as conn:
        enqueued = await enqueue(
            conn,
            workspace_id=seed.workspace,
            owner_id=seed.owner,
            kind=kind,
            source_id=source,
            **options,
        )
    return enqueued.id


async def job_row(pool: Pool, job_id: uuid.UUID) -> dict[str, Any]:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM job WHERE id = %s", (job_id,))
        row = await cur.fetchone()
    assert row is not None
    return row


async def status_of(pool: Pool, job_id: uuid.UUID) -> str:
    return str((await job_row(pool, job_id))["status"])


async def _is(pool: Pool, job_id: uuid.UUID, status: str) -> bool:
    return await status_of(pool, job_id) == status


async def lock_holder(pool: Pool, source: uuid.UUID) -> int | None:
    """The backend that holds the advisory lock of `source`, if any."""
    namespace, key = source_lock_key(source)
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT pid FROM pg_locks"
            " WHERE locktype = 'advisory' AND granted AND objsubid = 2"
            "  AND classid = %s::bigint::oid AND objid = %s::bigint::oid"
            "  AND database = (SELECT oid FROM pg_database WHERE datname = current_database())",
            (namespace, key & 0xFFFFFFFF),
        )
        row = await cur.fetchone()
    return None if row is None else int(row[0])


async def eventually(check: Callable[[], Awaitable[bool]], timeout: float = 5.0) -> None:
    with anyio.fail_after(timeout):
        while not await check():
            await anyio.sleep(0.02)


@asynccontextmanager
async def running(*services: Runnable, timeout: float = 15.0) -> AsyncGenerator[None]:
    """Run workers and schedulers for the block; stop them at its end and wait for them."""
    with anyio.fail_after(timeout):
        async with anyio.create_task_group() as group:
            for service in services:
                group.start_soon(service.run)
            try:
                yield
            finally:
                for service in services:
                    service.stop()


def worker(pool: Pool, handler: Callable[[Job], Awaitable[None]], **options: Any) -> Worker:
    settings: dict[str, Any] = {
        "lease": timedelta(seconds=5),
        "poll_interval": FAST_POLL,
        "grace": timedelta(seconds=5),
        "retry": RetryPolicy(base=timedelta(milliseconds=50)),
    }
    return Worker(pool, {SYNC: handler}, **(settings | options))


# --- Running a job ------------------------------------------------------------------------------


async def test_a_worker_runs_a_job_holding_the_lock_of_its_source(pool: Pool, seed: Seed) -> None:
    job_id = await add_job(
        pool, seed, seed.source_a, stream_id=seed.stream_a, args={"ref": "refs/heads/main"}
    )
    seen: list[Job] = []
    holders: list[int | None] = []

    async def handler(job: Job) -> None:
        seen.append(job)
        holders.append(await lock_holder(pool, seed.source_a))

    async with running(w := worker(pool, handler, name="w1")):
        await eventually(lambda: _is(pool, job_id, "succeeded"))

    [job] = seen
    assert (job.id, job.kind, job.attempt, job.max_attempts) == (job_id, SYNC, 1, 5)
    assert (job.workspace_id, job.owner_id) == (seed.workspace, seed.owner)
    assert (job.source_id, job.stream_id) == (seed.source_a, seed.stream_a)
    assert job.args == {"ref": "refs/heads/main"}
    assert (job.worker, w.name) == ("w1/0", "w1")
    assert holders[0] is not None
    row = await job_row(pool, job_id)
    assert row["attempts"] == 1
    assert row["lease_until"] is None
    assert row["finished_at"] is not None
    assert await lock_holder(pool, seed.source_a) is None


async def test_a_worker_runs_only_kinds_it_has_handlers_for(pool: Pool, seed: Seed) -> None:
    other = await add_job(pool, seed, seed.source_a, kind="reindex")
    mine = await add_job(pool, seed, seed.source_b)

    async def handler(job: Job) -> None:
        pass

    async with running(worker(pool, handler)):
        await eventually(lambda: _is(pool, mine, "succeeded"))

    assert await status_of(pool, other) == "queued"


# --- Enqueue ------------------------------------------------------------------------------------


async def test_enqueue_merges_jobs_with_the_same_dedupe_key(
    pool: Pool, db: Conn, seed: Seed
) -> None:
    async def put(key: str | None, kind: str = SYNC) -> tuple[uuid.UUID, bool]:
        async with pool.connection() as conn:
            enqueued = await enqueue(
                conn,
                workspace_id=seed.workspace,
                owner_id=seed.owner,
                kind=kind,
                source_id=seed.source_a,
                dedupe_key=key,
            )
        return enqueued.id, enqueued.created

    first, created = await put("k")
    assert created
    assert await put("k") == (first, False)
    assert (await put("k", kind="reindex"))[1]
    assert (await put(None))[1]
    assert (await put(None))[1]

    # A running job still counts; a finished one does not.
    job = await claim(db, kinds=[SYNC], worker="t/0", lease=timedelta(seconds=30))
    assert job is not None
    assert job.id == first
    assert await put("k") == (first, False)
    assert await complete(db, job)
    assert await unlock_source(db, seed.source_a)
    second, created = await put("k")
    assert created
    assert second != first

    # Enqueues that race each other still make one job.
    results: list[tuple[uuid.UUID, bool]] = []

    async def racer() -> None:
        results.append(await put("race"))

    async with anyio.create_task_group() as group:
        for _ in range(8):
            group.start_soon(racer)
    assert len({job_id for job_id, _ in results}) == 1
    assert sum(created for _, created in results) == 1


async def test_enqueue_refuses_a_bad_kind(pool: Pool, seed: Seed) -> None:
    with pytest.raises(ValueError, match="job kind"):
        await add_job(pool, seed, None, kind="Git sync")


async def test_enqueue_can_delay_a_job(pool: Pool, db: Conn, seed: Seed) -> None:
    job_id = await add_job(pool, seed, seed.source_a, delay=timedelta(hours=1))

    assert await claim(db, kinds=[SYNC], worker="t/0", lease=timedelta(seconds=30)) is None
    delay = await _scalar(db, "SELECT run_after - created_at FROM job WHERE id = %s", (job_id,))
    assert delay == timedelta(hours=1)


@pytest.mark.parametrize(
    ("columns", "constraint"),
    [
        ({"kind": "Git sync"}, "job_kind_check"),
        ({"status": "done"}, "job_status_check"),
        ({"args": "[]"}, "job_args_check"),
        ({"dedupe_key": ""}, "job_dedupe_key_check"),
        ({"attempts": 6}, "job_attempts_check"),
        ({"max_attempts": 0}, "job_max_attempts_check"),
        ({"status": "running"}, "job_lease_until_check"),
        ({"status": "succeeded"}, "job_finished_at_check"),
        ({"source_id": None}, "job_stream_id_check"),
        ({"stream_id": "<stream b>"}, "job_source_id_stream_id_fkey"),
        ({"workspace_id": "<workspace beta>"}, "job_workspace_id_source_id_fkey"),
    ],
)
async def test_the_job_table_refuses_bad_rows(
    db: Conn, seed: Seed, columns: dict[str, Any], constraint: str
) -> None:
    beta = await _scalar(
        db, "INSERT INTO workspace (owner_id, name) VALUES (%s, 'beta') RETURNING id", (seed.owner,)
    )
    stand_ins: dict[Any, Any] = {"<stream b>": seed.stream_b, "<workspace beta>": beta}
    row: dict[str, Any] = {
        "workspace_id": seed.workspace,
        "owner_id": seed.owner,
        "source_id": seed.source_a,
        "stream_id": seed.stream_a,
        "kind": SYNC,
        "args": "{}",
    } | columns
    values = [stand_ins.get(value, value) for value in row.values()]
    query = sql.SQL("INSERT INTO job ({columns}) VALUES ({values})").format(
        columns=sql.SQL(", ").join(sql.Identifier(column) for column in row),
        values=sql.SQL(", ").join(sql.Placeholder() for _ in row),
    )

    with pytest.raises((errors.CheckViolation, errors.ForeignKeyViolation)) as refused:
        await db.execute(query, values)

    assert refused.value.diag.constraint_name == constraint


# --- Failure: retry with backoff, quarantine ----------------------------------------------------


async def test_a_failed_job_is_retried_with_backoff(pool: Pool, seed: Seed) -> None:
    job_id = await add_job(pool, seed, seed.source_a)
    calls: list[float] = []

    async def handler(job: Job) -> None:
        calls.append(time.monotonic())
        if job.attempt < 3:
            raise RuntimeError("synthetic fetch of https://user:hunter2@git.example.test/r failed")

    retry = RetryPolicy(base=timedelta(milliseconds=150), factor=2)
    async with running(worker(pool, handler, retry=retry)):
        await eventually(lambda: _is(pool, job_id, "succeeded"))

    assert len(calls) == 3
    # The database times the backoff and this process measures it; allow for a little skew.
    assert calls[1] - calls[0] >= 0.15 * 0.9
    assert calls[2] - calls[1] >= 0.30 * 0.9
    row = await job_row(pool, job_id)
    assert row["attempts"] == 3
    assert row["last_error"] == (
        "RuntimeError: synthetic fetch of https://***@git.example.test/r failed"
    )


async def test_backoff_and_quarantine_in_the_queue(pool: Pool, db: Conn, seed: Seed) -> None:
    """The queue functions alone: exact backoff in database time, then quarantine."""
    job_id = await add_job(pool, seed, None, max_attempts=2)
    lease = timedelta(seconds=30)

    job = await claim(db, kinds=[SYNC], worker="t/0", lease=lease)
    assert job is not None
    assert job.attempt == 1
    assert await fail(db, job, error="boom", retry_in=timedelta(minutes=10)) == "queued"
    backoff = await _scalar(db, "SELECT run_after - updated_at FROM job WHERE id = %s", (job_id,))
    assert backoff == timedelta(minutes=10)
    assert await claim(db, kinds=[SYNC], worker="t/0", lease=lease) is None  # not due yet

    await db.execute("UPDATE job SET run_after = now() WHERE id = %s", (job_id,))
    job = await claim(db, kinds=[SYNC], worker="t/0", lease=lease)
    assert job is not None
    assert job.attempt == 2
    # A claim that is not the caller's changes nothing.
    stale = dataclasses.replace(job, worker="t/1")
    assert await fail(db, stale, error="late", retry_in=timedelta(0)) is None
    assert await fail(db, job, error="boom again", retry_in=timedelta(0)) == "quarantined"

    await db.execute(
        "UPDATE job SET run_after = now() - interval '1 hour' WHERE id = %s", (job_id,)
    )
    assert await claim(db, kinds=[SYNC], worker="t/0", lease=lease) is None

    row = await job_row(pool, job_id)
    assert (row["status"], row["attempts"], row["last_error"]) == ("quarantined", 2, "boom again")
    assert row["finished_at"] is not None
    assert row["lease_until"] is None


async def test_a_job_is_quarantined_after_repeated_failure(pool: Pool, seed: Seed) -> None:
    job_id = await add_job(pool, seed, seed.source_a, max_attempts=3)
    attempts: list[int] = []

    async def handler(job: Job) -> None:
        attempts.append(job.attempt)
        raise ValueError("synthetic parse error")

    async with running(worker(pool, handler)):
        await eventually(lambda: _is(pool, job_id, "quarantined"))
        await anyio.sleep(0.2)  # a quarantined job is not claimed again

    assert attempts == [1, 2, 3]
    row = await job_row(pool, job_id)
    assert (row["attempts"], row["last_error"]) == (3, "ValueError: synthetic parse error")
    assert await lock_holder(pool, seed.source_a) is None


# --- A worker dies or hangs ---------------------------------------------------------------------


async def test_a_job_whose_worker_died_is_picked_up_again(pool: Pool, db: Conn, seed: Seed) -> None:
    job_id = await add_job(pool, seed, seed.source_a)
    started = anyio.Event()
    calls: list[tuple[str, int]] = []

    async def stuck(job: Job) -> None:
        calls.append((job.worker, job.attempt))
        started.set()
        await anyio.sleep_forever()

    async def done(job: Job) -> None:
        calls.append((job.worker, job.attempt))

    # The first worker claims the job. Then the connection that holds its claim and the lock of
    # the source is killed, as when the worker's process dies.
    first = worker(pool, stuck, name="first", lease=timedelta(seconds=1))
    async with running(first):
        await started.wait()
        pid = await lock_holder(pool, seed.source_a)
        assert pid is not None
        assert await _scalar(db, "SELECT pg_terminate_backend(%s)", (pid,))
    # The lock of the source went with the connection; the row still says running.
    assert await lock_holder(pool, seed.source_a) is None
    assert await status_of(pool, job_id) == "running"

    # Another worker puts the job back once the lease has run out, and runs it.
    async with running(worker(pool, done, name="second")):
        await eventually(lambda: _is(pool, job_id, "succeeded"))

    assert calls == [("first/0", 1), ("second/0", 2)]
    row = await job_row(pool, job_id)
    assert row["attempts"] == 2
    assert row["last_error"] == "lease of first/0 expired; it died or hung"


async def test_a_hung_worker_whose_lease_ran_out_is_not_doubled(
    pool: Pool, db: Conn, seed: Seed
) -> None:
    job_id = await add_job(pool, seed, seed.source_a)
    started = anyio.Event()
    events: list[str] = []

    async def handler(job: Job) -> None:
        if job.attempt > 1:
            events.append(f"attempt {job.attempt} ran")
            return
        started.set()
        try:
            await anyio.sleep_forever()
        finally:
            events.append("hung handler stopped")

    # The worker renews its lease every second. Before it next does, the lease is made to run
    # out, as if the worker had hung.
    hung = worker(pool, handler, lease=timedelta(seconds=30), heartbeat=timedelta(seconds=1))
    async with running(hung):
        await started.wait()
        await db.execute(
            "UPDATE job SET lease_until = now() - interval '1 second' WHERE id = %s", (job_id,)
        )
        # Another claim puts the job back but cannot take it: the hung worker still holds the
        # lock of the source.
        assert await claim(db, kinds=[SYNC], worker="other/0", lease=timedelta(seconds=30)) is None
        assert await status_of(pool, job_id) == "queued"
        # At its next renewal the worker finds its lease gone, stops the handler and lets go of
        # the source. Then the job runs again.
        await eventually(lambda: _is(pool, job_id, "succeeded"))

    assert events == ["hung handler stopped", "attempt 2 ran"]


# --- One job per source at a time ---------------------------------------------------------------


async def test_two_workers_never_run_the_same_source_at_once(pool: Pool, seed: Seed) -> None:
    jobs = [
        await add_job(pool, seed, source)
        for _ in range(5)
        for source in (seed.source_a, seed.source_b)
    ]
    active: dict[uuid.UUID | None, int] = {}
    peak: dict[uuid.UUID | None, int] = {}
    together: list[int] = []

    async def handler(job: Job) -> None:
        active[job.source_id] = active.get(job.source_id, 0) + 1
        peak[job.source_id] = max(peak.get(job.source_id, 0), active[job.source_id])
        together.append(sum(active.values()))
        await anyio.sleep(0.05)
        active[job.source_id] -= 1

    async def all_done() -> bool:
        return all([await _is(pool, job_id, "succeeded") for job_id in jobs])

    async with running(
        worker(pool, handler, name="one", concurrency=2),
        worker(pool, handler, name="two", concurrency=2),
    ):
        await eventually(all_done, timeout=10)

    assert peak == {seed.source_a: 1, seed.source_b: 1}
    assert max(together) == 2  # the two sources did run side by side


async def test_a_job_of_one_source_does_not_block_another(pool: Pool, seed: Seed) -> None:
    first_a = await add_job(pool, seed, seed.source_a)
    second_a = await add_job(pool, seed, seed.source_a)
    job_b = await add_job(pool, seed, seed.source_b)
    a_started = anyio.Event()
    a_release = anyio.Event()

    async def handler(job: Job) -> None:
        if job.source_id == seed.source_a:
            a_started.set()
            await a_release.wait()

    async with running(worker(pool, handler, concurrency=2)):
        await a_started.wait()
        await eventually(lambda: _is(pool, job_b, "succeeded"))
        # The free slot passed over the second job of a, which waits for the lock of a.
        assert await status_of(pool, first_a) == "running"
        assert await status_of(pool, second_a) == "queued"
        a_release.set()
        await eventually(lambda: _is(pool, second_a, "succeeded"))


# --- Shutdown -----------------------------------------------------------------------------------


async def test_stop_lets_a_running_job_finish(pool: Pool, seed: Seed) -> None:
    running_job = await add_job(pool, seed, seed.source_a)
    waiting_job = await add_job(pool, seed, seed.source_b)
    started = anyio.Event()
    finish = anyio.Event()

    async def handler(job: Job) -> None:
        started.set()
        await finish.wait()

    # One slot: it takes the older job, and the other one waits for it.
    w = worker(pool, handler, grace=timedelta(seconds=5))
    with anyio.fail_after(5):
        async with anyio.create_task_group() as group:
            group.start_soon(w.run)
            await started.wait()
            w.stop()
            await anyio.sleep(0.1)  # longer than the poll interval: a stopped worker claims nothing
            finish.set()

    assert await status_of(pool, running_job) == "succeeded"
    row = await job_row(pool, waiting_job)
    assert (row["status"], row["attempts"]) == ("queued", 0)


async def test_stop_returns_a_job_that_outlives_the_grace(pool: Pool, seed: Seed) -> None:
    job_id = await add_job(pool, seed, seed.source_a)
    started = anyio.Event()
    cancelled: list[bool] = []

    async def handler(job: Job) -> None:
        started.set()
        try:
            await anyio.sleep_forever()
        except anyio.get_cancelled_exc_class():
            cancelled.append(True)
            raise

    w = worker(pool, handler, grace=timedelta(milliseconds=100))
    with anyio.fail_after(5):
        async with anyio.create_task_group() as group:
            group.start_soon(w.run)
            await started.wait()
            w.stop()

    assert cancelled == [True]
    row = await job_row(pool, job_id)
    assert (row["status"], row["attempts"], row["lease_until"]) == ("queued", 0, None)
    assert await lock_holder(pool, seed.source_a) is None

    # The attempt was given back: the next run is attempt 1 again.
    attempts: list[int] = []

    async def done(job: Job) -> None:
        attempts.append(job.attempt)

    async with running(worker(pool, done)):
        await eventually(lambda: _is(pool, job_id, "succeeded"))
    assert attempts == [1]


async def test_cancelling_a_worker_returns_its_job(pool: Pool, seed: Seed) -> None:
    job_id = await add_job(pool, seed, seed.source_a)
    started = anyio.Event()

    async def handler(job: Job) -> None:
        started.set()
        await anyio.sleep_forever()

    w = worker(pool, handler, grace=timedelta(seconds=30))
    with anyio.fail_after(5):
        async with anyio.create_task_group() as group:
            group.start_soon(w.run)
            await started.wait()
            group.cancel_scope.cancel()

    row = await job_row(pool, job_id)
    assert (row["status"], row["attempts"]) == ("queued", 0)
    assert await lock_holder(pool, seed.source_a) is None


# --- Scheduler ----------------------------------------------------------------------------------


async def _set_schedule(db: Conn, stream: uuid.UUID, schedule: timedelta | None) -> None:
    await db.execute(
        "UPDATE stream SET schedule = %s, next_run_at = NULL WHERE id = %s", (schedule, stream)
    )


async def _time_passes(db: Conn, stream: uuid.UUID) -> None:
    """Make a stream due, as if its schedule interval had passed."""
    await db.execute(
        "UPDATE stream SET next_run_at = now() - interval '1 second' WHERE id = %s", (stream,)
    )


async def _stream_jobs(db: Conn, stream: uuid.UUID) -> list[dict[str, Any]]:
    async with db.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM job WHERE stream_id = %s ORDER BY created_at", (stream,))
        return await cur.fetchall()


async def test_a_schedule_fires(db: Conn, seed: Seed) -> None:
    await _set_schedule(db, seed.stream_a, timedelta(hours=1))
    # Not scheduled: a stream without a schedule, and a scheduled stream of a push source.
    push = await _add_source(db, seed.workspace, seed.owner, "push")
    await _add_stream(db, seed.workspace, seed.owner, push, timedelta(minutes=5))

    # A new scheduled stream is due at once.
    assert await enqueue_due_streams(db) == (1, 1)
    [job] = await _stream_jobs(db, seed.stream_a)
    assert (job["kind"], job["source_id"], job["status"]) == (SYNC, seed.source_a, "queued")
    assert job["dedupe_key"] == stream_dedupe_key(seed.stream_a)
    next_run = await _scalar(db, "SELECT next_run_at FROM stream WHERE id = %s", (seed.stream_a,))
    assert next_run - job["created_at"] == timedelta(hours=1)

    # Not due again before the hour is up.
    assert await enqueue_due_streams(db) == (0, 0)

    # Due again while the first sync still waits: no second job, and the turn passes.
    await _time_passes(db, seed.stream_a)
    assert await enqueue_due_streams(db) == (1, 0)
    assert await enqueue_due_streams(db) == (0, 0)

    # Once the first sync has run, the next due time brings a new job.
    claimed = await claim(db, kinds=[SYNC], worker="t/0", lease=timedelta(seconds=30))
    assert claimed is not None
    assert await complete(db, claimed)
    assert await unlock_source(db, seed.source_a)
    await _time_passes(db, seed.stream_a)
    assert await enqueue_due_streams(db) == (1, 1)

    assert [j["status"] for j in await _stream_jobs(db, seed.stream_a)] == ["succeeded", "queued"]
    assert await _stream_jobs(db, seed.stream_b) == []


async def test_a_scheduled_stream_reaches_its_handler(pool: Pool, db: Conn, seed: Seed) -> None:
    await _set_schedule(db, seed.stream_b, timedelta(minutes=15))
    synced = anyio.Event()
    seen: list[tuple[uuid.UUID | None, uuid.UUID | None]] = []

    async def handler(job: Job) -> None:
        seen.append((job.source_id, job.stream_id))
        synced.set()

    async with running(Scheduler(pool, interval=FAST_POLL), worker(pool, handler)):
        await synced.wait()

    assert seen == [(seed.source_b, seed.stream_b)]


async def test_two_schedulers_enqueue_each_due_stream_once(
    pool: Pool, db: Conn, seed: Seed
) -> None:
    streams = {
        await _add_stream(db, seed.workspace, seed.owner, source, timedelta(minutes=1))
        for _ in range(20)
        for source in (seed.source_a, seed.source_b)
    }
    created: list[int] = []

    async def scheduler() -> None:
        created.append(await Scheduler(pool, batch=3).tick())

    for _ in range(3):  # three rounds, each a race of two schedulers
        async with anyio.create_task_group() as group:
            group.start_soon(scheduler)
            group.start_soon(scheduler)
        await db.execute("UPDATE job SET status = 'succeeded', finished_at = now()")
        await db.execute("UPDATE stream SET next_run_at = now() - interval '1 second'")

    cur = await db.execute("SELECT stream_id, count(*) FROM job GROUP BY stream_id")
    assert dict(await cur.fetchall()) == dict.fromkeys(streams, 3)
    assert sum(created) == 3 * len(streams)


# --- Run reports --------------------------------------------------------------------------------


async def runs_of(pool: Pool, seed: Seed) -> list[dict[str, Any]]:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM run WHERE workspace_id = %s ORDER BY started_at", (seed.workspace,)
        )
        return await cur.fetchall()


async def test_every_attempt_writes_a_run_report(pool: Pool, seed: Seed) -> None:
    async def handler(job: Job) -> None:
        run = current_run()
        assert run is not None
        async with run.step("fetch"):
            run.count("records_stored", 3)
        run.llm_tokens(model="local-test-model", input=10, output=2)
        if job.args.get("fail_first") and job.attempt == 1:
            raise RuntimeError("synthetic failure on the first attempt")

    ok = await add_job(pool, seed, seed.source_a, stream_id=seed.stream_a)
    flaky = await add_job(
        pool, seed, seed.source_b, stream_id=seed.stream_b, args={"fail_first": True}
    )

    async def both_succeeded() -> bool:
        return await _is(pool, ok, "succeeded") and await _is(pool, flaky, "succeeded")

    async with running(worker(pool, handler)):
        await eventually(both_succeeded)

    runs = await runs_of(pool, seed)
    # One run per attempt: one for the job that succeeded at once, two for the flaky one.
    assert sorted((run["source_id"], run["status"]) for run in runs) == sorted(
        [
            (seed.source_a, "succeeded"),
            (seed.source_b, "failed"),
            (seed.source_b, "succeeded"),
        ]
    )
    streams = {seed.source_a: seed.stream_a, seed.source_b: seed.stream_b}
    for run in runs:
        assert run["kind"] == SYNC
        assert run["finished_at"] is not None
        assert run["stream_id"] == streams[run["source_id"]]
        report = run["report"]
        assert report["version"] == 1
        assert [step["name"] for step in report["steps"]] == ["fetch"]
        assert report["counts"] == {"records_stored": 3}
        assert report["llm"]["input_tokens"] == 10
    failed = next(run for run in runs if run["status"] == "failed")
    assert "synthetic failure" in failed["error"]
    assert current_run() is None
