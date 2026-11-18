"""The intake planner against a real Postgres: the history frontier that ingest moves, the stream
state the planner reads, the time pinned per job, and runs of a scripted executor through the
worker and the real ingest path. Acceptance of memini-69b: a failed stream gets the same window
again; history backfill stops at the retention start. Every value is synthetic."""

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, LiteralString

import anyio
import pytest
from psycopg import AsyncConnection, errors
from psycopg.rows import TupleRow

from ingest_helpers import DAY, HOUR, T0, thread
from memini.auth import WriteAccess
from memini.db import Pool, connect, open_pool
from memini.ingest import Batch, IngestResult, RecordIn, WindowClaim, ingest
from memini.intake import (
    AS_OF_ARG,
    Plan,
    PlannedWindow,
    StreamState,
    WindowSettings,
    load_stream_state,
    pin_as_of,
    plan_stream,
    plan_windows,
    record_plan,
    resolve_settings,
)
from memini.jobs import Job, RetryPolicy, Worker, enqueue, stream_dedupe_key
from memini.recipes import load_recipe
from memini.runs import current_run
from memini.workspaces import create_workspace, ensure_principal

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

type Conn = AsyncConnection[TupleRow]

RECIPES = Path(__file__).resolve().parent / "fixtures" / "recipes"
SYNC = "mcp_sync"
CHANNEL = "C0042"
RETENTION_DAYS = 120


@dataclass(frozen=True)
class Env:
    pool: Pool
    db: Conn
    owner: uuid.UUID
    access: WriteAccess
    workspace: uuid.UUID
    source: uuid.UUID
    """An mcp source, "hearth", with a retention of RETENTION_DAYS."""
    settings: WindowSettings
    """The Hearth recipe's window: overlap 2d, initial 14d, backfill 30d, max_span 7d."""


@pytest.fixture
async def env(migrated_database_url: str) -> AsyncIterator[Env]:
    async with (
        open_pool(migrated_database_url, min_size=2, max_size=10, timeout=5) as pool,
        connect(migrated_database_url, autocommit=True) as db,
    ):
        owner = await ensure_principal(db, "owner")
        workspace = await create_workspace(db, owner_id=owner, name="acme")
        source = await scalar(
            db,
            "INSERT INTO source (workspace_id, owner_id, name, kind, retention_days)"
            " VALUES (%s, %s, 'hearth', 'mcp', %s) RETURNING id",
            workspace.id,
            owner,
            RETENTION_DAYS,
        )
        settings = resolve_settings(load_recipe(RECIPES / "hearth.toml").window)
        yield Env(pool, db, owner, WriteAccess.for_owner(owner), workspace.id, source, settings)


async def scalar(db: Conn, query: LiteralString, *params: Any) -> Any:
    cur = await db.execute(query, params)
    rows = await cur.fetchall()
    assert len(rows) == 1
    return rows[0][0]


async def send(
    env: Env, window: WindowClaim, records: list[RecordIn] | None = None, **kwargs: Any
) -> IngestResult:
    async with env.pool.connection() as conn:
        return await ingest(
            conn,
            env.access,
            "acme",
            "hearth",
            Batch(records=records or [], stream=CHANNEL, window=window),
            **kwargs,
        )


async def stream_id(env: Env) -> uuid.UUID:
    return await scalar(
        env.db, "SELECT id FROM stream WHERE source_id = %s AND key = %s", env.source, CHANNEL
    )


async def checked(env: Env) -> tuple[datetime | None, datetime | None, Any]:
    """The stream's checked range and cursor: (checked_from, checked_until, cursor)."""
    cur = await env.db.execute(
        "SELECT checked_from, checked_until, cursor FROM stream WHERE source_id = %s AND key = %s",
        (env.source, CHANNEL),
    )
    row = await cur.fetchone()
    assert row is not None
    return row[0], row[1], row[2]


def claim(start: datetime, end: datetime, *, complete: bool = True, **kwargs: Any) -> WindowClaim:
    return WindowClaim(start=start, end=end, complete=complete, **kwargs)


# --- Ingest moves the frontier -------------------------------------------------------------------


async def test_ingest_moves_the_frontier_back_with_a_complete_window_of_history(env: Env) -> None:
    first = await send(env, claim(T0 + 10 * DAY, T0 + 14 * DAY, cursor={"page": "a"}))
    assert first.checkpoint is not None
    assert first.checkpoint.checked_from == T0 + 10 * DAY
    assert await checked(env) == (T0 + 10 * DAY, T0 + 14 * DAY, {"page": "a"})

    # A step of history that reaches the frontier: stored, and the frontier moves back; the
    # checkpoint and its cursor stay.
    history = await send(
        env,
        claim(T0 + 6 * DAY, T0 + 10 * DAY, cursor={"page": "ignored"}),
        [thread("c0042/0007", T0 + 7 * DAY)],
    )
    assert (history.inserted, history.checkpoint_moved) == (1, False)
    assert history.checkpoint is not None
    assert history.checkpoint.checked_from == T0 + 6 * DAY
    assert await checked(env) == (T0 + 6 * DAY, T0 + 14 * DAY, {"page": "a"})

    # Incomplete: stored, the frontier stays.
    partial = await send(
        env, claim(T0 + 2 * DAY, T0 + 6 * DAY, complete=False), [thread("c0042/0003", T0 + 3 * DAY)]
    )
    assert partial.inserted == 1
    assert await checked(env) == (T0 + 6 * DAY, T0 + 14 * DAY, {"page": "a"})

    # Complete but with a gap below the frontier: stored, the frontier stays.
    gap = await send(env, claim(T0, T0 + DAY), [thread("c0042/0001", T0 + HOUR)])
    assert gap.inserted == 1
    assert await checked(env) == (T0 + 6 * DAY, T0 + 14 * DAY, {"page": "a"})

    # A forward window whose overlap reaches below the frontier moves both.
    both = await send(env, claim(T0 + 5 * DAY, T0 + 15 * DAY, cursor={"page": "b"}))
    assert both.checkpoint_moved
    assert await checked(env) == (T0 + 5 * DAY, T0 + 15 * DAY, {"page": "b"})


async def test_a_failed_batch_leaves_the_frontier_where_it_was(env: Env) -> None:
    await send(env, claim(T0 + 10 * DAY, T0 + 14 * DAY))

    async def failing(conn: Any, records: Any) -> None:
        raise RuntimeError("synthetic indexer failure")

    with pytest.raises(RuntimeError, match="synthetic"):
        await send(
            env,
            claim(T0 + 6 * DAY, T0 + 10 * DAY),
            [thread("c0042/0007", T0 + 7 * DAY)],
            indexer=failing,
        )
    assert await checked(env) == (T0 + 10 * DAY, T0 + 14 * DAY, None)
    assert await scalar(env.db, "SELECT count(*) FROM record") == 0


async def test_a_range_whose_start_was_never_recorded_gets_one_from_its_next_window(
    env: Env,
) -> None:
    await send(env, claim(T0 + 10 * DAY, T0 + 14 * DAY))
    # As a checkpoint set before the frontier existed.
    await env.db.execute("UPDATE stream SET checked_from = NULL")
    assert (await load_stream_state(env.db, await stream_id(env))).checked_from is None

    await send(env, claim(T0 + 12 * DAY, T0 + 16 * DAY))
    assert await checked(env) == (T0 + 12 * DAY, T0 + 16 * DAY, None)


async def test_the_schema_keeps_the_frontier_inside_the_range(env: Env) -> None:
    await send(env, claim(T0 + 10 * DAY, T0 + 14 * DAY))
    with pytest.raises(errors.CheckViolation, match="stream_checked_from_check"):
        await env.db.execute("UPDATE stream SET checked_from = %s", (T0 + 15 * DAY,))
    with pytest.raises(errors.CheckViolation, match="stream_checked_from_check"):
        await env.db.execute("UPDATE stream SET checked_until = NULL")


# --- What the planner reads ----------------------------------------------------------------------


async def test_the_planner_reads_the_range_and_the_sources_retention(env: Env) -> None:
    await send(env, claim(T0 + 10 * DAY, T0 + 14 * DAY))
    stream = await stream_id(env)

    state = await load_stream_state(env.db, stream)
    assert state == StreamState(
        checked_until=T0 + 14 * DAY,
        checked_from=T0 + 10 * DAY,
        retention=timedelta(days=RETENTION_DAYS),
    )
    plan = await plan_stream(env.db, stream, env.settings, T0 + 15 * DAY)
    assert plan == plan_windows(state, env.settings, T0 + 15 * DAY)

    await env.db.execute("UPDATE source SET retention_days = NULL")
    assert (await load_stream_state(env.db, stream)).retention is None
    with pytest.raises(LookupError):
        await load_stream_state(env.db, uuid.uuid4())


async def add_job(env: Env, stream: uuid.UUID, args: dict[str, object] | None = None) -> uuid.UUID:
    enqueued = await enqueue(
        env.db,
        workspace_id=env.workspace,
        owner_id=env.owner,
        kind=SYNC,
        source_id=env.source,
        stream_id=stream,
        args=args,
        dedupe_key=stream_dedupe_key(stream),
    )
    return enqueued.id


async def test_a_job_pins_the_database_time_once_for_all_its_attempts(env: Env) -> None:
    await send(env, claim(T0, T0 + DAY))
    stream = await stream_id(env)
    job = await add_job(env, stream, {"note": "kept"})

    before = await scalar(env.db, "SELECT now()")
    pinned = await pin_as_of(env.db, job)
    await anyio.sleep(0.01)
    assert await pin_as_of(env.db, job) == pinned
    assert pinned >= before
    args = await scalar(env.db, "SELECT args FROM job WHERE id = %s", job)
    assert args["note"] == "kept"
    assert datetime.fromisoformat(args[AS_OF_ARG]) == pinned

    # Inside the caller's transaction, in a savepoint.
    async with env.pool.connection() as conn, conn.transaction():
        assert await pin_as_of(conn, job) == pinned

    # A job enqueued with its time keeps it.
    await env.db.execute("UPDATE job SET status = 'succeeded', finished_at = now()")
    given = await add_job(env, stream, {AS_OF_ARG: "2026-03-05T06:07:08.000009+00:00"})
    assert (await pin_as_of(env.db, given)).isoformat() == "2026-03-05T06:07:08.000009+00:00"

    await env.db.execute("UPDATE job SET args = '{\"as_of\": null}' WHERE id = %s", (given,))
    with pytest.raises(ValueError, match="as_of is not a time"):
        await pin_as_of(env.db, given)
    await env.db.execute(
        'UPDATE job SET args = \'{"as_of": "2026-03-05"}\' WHERE id = %s', (given,)
    )
    with pytest.raises(ValueError, match="as_of has no time zone"):
        await pin_as_of(env.db, given)
    with pytest.raises(LookupError):
        await pin_as_of(env.db, uuid.uuid4())


# --- Runs of a scripted executor -----------------------------------------------------------------


def records_of(window: PlannedWindow) -> list[RecordIn]:
    """What the invented server holds in a window: one thread at its start."""
    return [thread(f"c0042/{int(window.start.timestamp())}", window.start)]


type Outcome = Callable[[int, PlannedWindow], Awaitable[bool]]
"""Whether the window at this position of the run comes back complete."""


async def always_complete(position: int, window: PlannedWindow) -> bool:
    return True


async def execute(env: Env, stream: uuid.UUID, plan: Plan, outcome: Outcome) -> list[bool]:
    """Run a plan as an executor does: one ingest batch per window, each chain stopping at its
    first incomplete window. Returns whether each window that ran was complete."""
    results: list[bool] = []
    run = current_run()
    if run is not None:
        record_plan(run, plan)
    for chain in (plan.forward, plan.backfill):
        for window in chain:
            complete = await outcome(len(results), window)
            results.append(complete)
            await send(env, window.claim(complete=complete), records_of(window))
            if not complete:
                break
    return results


async def test_runs_load_the_recent_window_then_history_down_to_the_retention_start(
    env: Env,
) -> None:
    # The first sync creates the stream; its plan is the initial load.
    as_of = T0 + 200 * DAY
    plan = plan_windows(StreamState(retention=timedelta(days=RETENTION_DAYS)), env.settings, as_of)
    assert [w.kind for w in plan.windows] == ["initial", "initial"]
    for window in plan.windows:
        await send(env, window.claim(complete=True), records_of(window))
    stream = await stream_id(env)
    assert await checked(env) == (as_of - 14 * DAY, as_of, None)

    frontiers: list[datetime] = []
    for run in range(1, 10):
        as_of = T0 + 200 * DAY + run * DAY
        plan = await plan_stream(env.db, stream, env.settings, as_of)
        if run == 2:
            # This run fails on its second history window: the frontier stops at the first.
            async def second_fails(position: int, window: PlannedWindow) -> bool:
                return position != 2

            results = await execute(env, stream, plan, second_fails)
            assert results == [True, True, False]
            frontier, _, _ = await checked(env)
            assert frontier == plan.backfill[0].start
            # The retry, with the same as_of, plans the failed window again, the same window.
            retry = await plan_stream(env.db, stream, env.settings, as_of)
            assert retry.backfill[0] == plan.backfill[1]
            assert retry.forward_skipped == "caught_up"
            plan = retry
        if not plan.backfill:
            assert plan.backfill_skipped == "history_loaded"
            break
        await execute(env, stream, plan, always_complete)
        frontier, until, _ = await checked(env)
        assert until == as_of
        assert frontier is not None
        frontiers.append(frontier)
    else:
        pytest.fail("history never ended")

    # 30 days a run; the second run 7 days and then, on its retry, 30 more; the last step cut
    # at the retention start of its run, exactly.
    start = T0 + 200 * DAY - 14 * DAY
    last_as_of = T0 + 200 * DAY + len(frontiers) * DAY
    assert frontiers == [
        start - 30 * DAY,
        start - 67 * DAY,
        start - 97 * DAY,
        last_as_of - timedelta(days=RETENTION_DAYS),
    ]
    oldest = await scalar(env.db, "SELECT min(occurred_at) FROM record")
    assert oldest == frontiers[-1]


async def test_a_failed_sync_job_plans_the_same_windows_on_its_retry(env: Env) -> None:
    attempts: list[tuple[int, datetime, Plan]] = []

    async def mcp_sync(job: Job) -> None:
        assert job.stream_id is not None
        async with env.pool.connection() as conn:
            as_of = await pin_as_of(conn, job.id)
            plan = await plan_stream(conn, job.stream_id, env.settings, as_of)
        attempts.append((job.attempt, as_of, plan))
        run = current_run()
        assert run is not None
        record_plan(run, plan)
        if job.attempt == 1:
            raise RuntimeError("synthetic: the server timed out before the first window")
        for chain in (plan.forward, plan.backfill):
            for window in chain:
                async with run.step(window.label):
                    await send(env, window.claim(complete=True), records_of(window))

    # A stream with a checkpoint an hour ago and history to load.
    now: datetime = await scalar(env.db, "SELECT now()")
    await send(env, claim(now - 3 * HOUR - 14 * DAY, now - HOUR))
    stream = await stream_id(env)
    job = await add_job(env, stream)

    worker = Worker(
        env.pool,
        {SYNC: mcp_sync},
        lease=timedelta(seconds=5),
        poll_interval=timedelta(milliseconds=20),
        retry=RetryPolicy(base=timedelta(milliseconds=50)),
    )
    with anyio.fail_after(15):
        async with anyio.create_task_group() as group:
            group.start_soon(worker.run)
            while await scalar(env.db, "SELECT status FROM job WHERE id = %s", job) != "succeeded":
                await anyio.sleep(0.02)
            worker.stop()

    (first, first_as_of, first_plan), (second, second_as_of, second_plan) = attempts
    assert (first, second) == (1, 2)
    assert second_as_of == first_as_of
    assert second_plan == first_plan
    assert [w.kind for w in first_plan.windows] == ["forward", *["backfill"] * 5]

    cur = await env.db.execute(
        "SELECT status, report FROM run WHERE kind = %s ORDER BY started_at", (SYNC,)
    )
    runs = await cur.fetchall()
    assert [status for status, _ in runs] == ["failed", "succeeded"]
    assert runs[0][1]["details"]["plan"] == runs[1][1]["details"]["plan"] == first_plan.to_json()
    assert [step["name"] for step in runs[1][1]["steps"]] == [w.label for w in first_plan.windows]
    assert runs[1][1]["counts"]["records_inserted"] == len(first_plan.windows)

    frontier, until, _ = await checked(env)
    assert until == first_as_of
    assert frontier == first_plan.backfill[-1].start
