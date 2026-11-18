"""Run reports against a real Postgres: the run row, its report and status. Synthetic values."""

import asyncio
import io
import json
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, LiteralString

import anyio
import pytest
from psycopg import AsyncConnection

from memini import runs
from memini.db import Pool, connect, open_pool
from memini.logs import REDACTED, current_run_id
from memini.runs import RunError, record_run, wait_for_run_writes

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

CANARY = "fake-run-password-0001"


@dataclass(frozen=True)
class Scope:
    owner: uuid.UUID
    workspace: uuid.UUID
    source: uuid.UUID
    stream: uuid.UUID
    other_source: uuid.UUID


@pytest.fixture
async def pool(migrated_database_url: str) -> AsyncIterator[Pool]:
    async with open_pool(migrated_database_url, min_size=1, max_size=4) as pool:
        yield pool
        await wait_for_run_writes()


async def _insert(pool: Pool, query: LiteralString, *params: object) -> uuid.UUID:
    async with pool.connection() as conn:
        cur = await conn.execute(query, params)
        row = await cur.fetchone()
    assert row is not None
    return row[0]


@pytest.fixture
async def scope(pool: Pool) -> Scope:
    owner = await _insert(pool, "INSERT INTO principal (name) VALUES ('me') RETURNING id")
    workspace = await _insert(
        pool, "INSERT INTO workspace (owner_id, name) VALUES (%s, 'alpha') RETURNING id", owner
    )
    add_source = (
        "INSERT INTO source (workspace_id, owner_id, name, kind) VALUES (%s, %s, %s, 'push')"
        " RETURNING id"
    )
    source = await _insert(pool, add_source, workspace, owner, "chat")
    other_source = await _insert(pool, add_source, workspace, owner, "notes")
    stream = await _insert(
        pool,
        "INSERT INTO stream (workspace_id, owner_id, source_id, key)"
        " VALUES (%s, %s, %s, 'general') RETURNING id",
        workspace,
        owner,
        source,
    )
    return Scope(owner, workspace, source, stream, other_source)


async def run_row(pool: Pool, run_id: uuid.UUID) -> dict[str, Any]:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT status, started_at, finished_at, report, error, kind, source_id, stream_id"
            " FROM run WHERE id = %s",
            (run_id,),
        )
        row = await cur.fetchone()
    assert row is not None
    names = ("status", "started_at", "finished_at", "report", "error", "kind", "source", "stream")
    return dict(zip(names, row, strict=True))


async def run_count(pool: Pool) -> int:
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT count(*) FROM run")
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def test_a_successful_run_writes_steps_durations_counts_and_tokens(
    pool: Pool, scope: Scope
) -> None:
    async with record_run(
        pool,
        workspace_id=scope.workspace,
        owner_id=scope.owner,
        kind="sync",
        source_id=scope.source,
        stream_id=scope.stream,
        progress_interval=0,
    ) as run:
        # Committed at once: another connection sees the run while it works.
        running = await run_row(pool, run.id)
        assert running["status"] == "running"
        assert running["finished_at"] is None
        assert running["report"]["version"] == 1
        assert running["report"]["steps"] == []

        async with run.step("fetch"):
            await anyio.sleep(0.02)
        run.count("records_fetched", 3)

        # The report so far is saved at the end of a step.
        progress = await run_row(pool, run.id)
        assert [step["name"] for step in progress["report"]["steps"]] == ["fetch"]
        assert progress["report"]["steps"][0]["status"] == "succeeded"
        assert progress["report"]["duration_ms"] is None

        async with run.step("store"):
            run.count("records_stored", 2)
            run.count("records_stored", 2)
        run.count("records_skipped", 0)
        run.llm_tokens(model="local-model", input=1000, output=50)
        run.llm_tokens(model="local-model", input=200, output=30)
        run.llm_tokens(model="other-model", input=10, output=1)

    row = await run_row(pool, run.id)
    assert row["status"] == "succeeded"
    assert row["error"] is None
    assert row["finished_at"] >= row["started_at"]
    assert row["started_at"] == run.started_at
    assert (row["kind"], row["source"], row["stream"]) == ("sync", scope.source, scope.stream)

    report = row["report"]
    assert report["version"] == 1
    assert report["duration_ms"] >= 20
    fetch, store = report["steps"]
    assert (fetch["name"], fetch["status"]) == ("fetch", "succeeded")
    assert fetch["duration_ms"] >= 20
    assert fetch["started_at"].endswith("Z")
    assert (store["name"], store["status"]) == ("store", "succeeded")
    assert store["duration_ms"] >= 0
    assert report["steps_omitted"] == 0
    assert report["counts"] == {"records_fetched": 3, "records_stored": 4, "records_skipped": 0}
    assert report["llm"] == {
        "calls": 3,
        "input_tokens": 1210,
        "output_tokens": 81,
        "by_model": {
            "local-model": {"calls": 2, "input_tokens": 1200, "output_tokens": 80},
            "other-model": {"calls": 1, "input_tokens": 10, "output_tokens": 1},
        },
    }
    assert report == run.report() | {"duration_ms": report["duration_ms"]}


async def test_a_failing_job_is_failed_with_a_redacted_error_and_reraises(
    pool: Pool, scope: Scope
) -> None:
    run_ids: list[uuid.UUID] = []

    async def job() -> None:
        async with record_run(
            pool, workspace_id=scope.workspace, owner_id=scope.owner, kind="sync"
        ) as run:
            run_ids.append(run.id)
            run.count("records_fetched", 7)
            async with run.step("fetch"):
                raise ValueError(f"cannot reach postgresql://memini:{CANARY}@db.invalid/x")

    with pytest.raises(ValueError, match="cannot reach"):
        await job()

    row = await run_row(pool, run_ids[0])
    assert row["status"] == "failed"
    assert row["finished_at"] is not None
    assert row["error"] == f"ValueError: cannot reach postgresql://memini:{REDACTED}@db.invalid/x"
    assert CANARY not in json.dumps(row["report"])
    (step,) = row["report"]["steps"]
    assert (step["status"], step["error"]) == ("failed", "ValueError")
    assert step["duration_ms"] is not None
    assert row["report"]["counts"] == {"records_fetched": 7}


async def test_a_long_error_is_cut(pool: Pool, scope: Scope) -> None:
    run_ids: list[uuid.UUID] = []

    async def job() -> None:
        async with record_run(
            pool, workspace_id=scope.workspace, owner_id=scope.owner, kind="sync"
        ) as run:
            run_ids.append(run.id)
            raise RuntimeError("x" * 5000)

    with pytest.raises(RuntimeError, match="xxx"):
        await job()

    error = (await run_row(pool, run_ids[0]))["error"]
    assert len(error) == runs.MAX_ERROR_LENGTH
    assert error.endswith("…")


async def test_a_cancelled_task_gives_a_cancelled_run(pool: Pool, scope: Scope) -> None:
    started = asyncio.Event()
    run_ids: list[uuid.UUID] = []

    async def job() -> None:
        async with record_run(
            pool, workspace_id=scope.workspace, owner_id=scope.owner, kind="sync"
        ) as run:
            run_ids.append(run.id)
            async with run.step("wait"):
                started.set()
                await asyncio.Event().wait()

    task = asyncio.create_task(job())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await wait_for_run_writes()

    row = await run_row(pool, run_ids[0])
    assert row["status"] == "cancelled"
    assert row["finished_at"] is not None
    assert row["error"] == "CancelledError"
    assert row["report"]["steps"][0]["status"] == "cancelled"


async def test_a_cancel_scope_gives_a_cancelled_run(pool: Pool, scope: Scope) -> None:
    """A cancel scope cancels every await inside it, the final write's too; it still lands."""
    run_ids: list[uuid.UUID] = []

    with anyio.CancelScope() as cancel_scope:
        async with record_run(
            pool, workspace_id=scope.workspace, owner_id=scope.owner, kind="sync"
        ) as run:
            run_ids.append(run.id)
            cancel_scope.cancel()
            await anyio.sleep(10)
    assert cancel_scope.cancelled_caught
    await wait_for_run_writes()

    assert (await run_row(pool, run_ids[0]))["status"] == "cancelled"


async def test_a_job_may_end_its_run_as_partial(pool: Pool, scope: Scope) -> None:
    async with record_run(
        pool, workspace_id=scope.workspace, owner_id=scope.owner, kind="sync"
    ) as run:
        run.set_outcome("partial", f"2 calls missing; token memini_{'F' * 24}")

    row = await run_row(pool, run.id)
    assert row["status"] == "partial"
    assert row["error"] == f"2 calls missing; token {REDACTED}"
    assert row["finished_at"] is not None


async def test_an_exception_overrides_the_chosen_outcome(pool: Pool, scope: Scope) -> None:
    run_ids: list[uuid.UUID] = []

    async def job() -> None:
        async with record_run(
            pool, workspace_id=scope.workspace, owner_id=scope.owner, kind="sync"
        ) as run:
            run_ids.append(run.id)
            run.set_outcome("partial", "window incomplete")
            raise KeyError("cursor")

    with pytest.raises(KeyError, match="cursor"):
        await job()

    row = await run_row(pool, run_ids[0])
    assert (row["status"], row["error"]) == ("failed", "KeyError: 'cursor'")


async def test_log_lines_inside_a_run_carry_its_id(
    pool: Pool, scope: Scope, log_output: io.StringIO
) -> None:
    async with record_run(
        pool, workspace_id=scope.workspace, owner_id=scope.owner, kind="retention"
    ) as run:
        assert current_run_id() == run.id
        logging.getLogger("memini.test").info("working")
    assert current_run_id() is None
    logging.getLogger("memini.test").info("after")

    entries = [json.loads(line) for line in log_output.getvalue().splitlines()]
    by_message = {entry["message"]: entry for entry in entries}
    for message in ("run started: retention", "working", "run finished: succeeded"):
        assert by_message[message]["run_id"] == str(run.id)
    assert "run_id" not in by_message["after"]
    finished = by_message["run finished: succeeded"]
    assert finished["extra"]["kind"] == "retention"
    assert finished["extra"]["status"] == "succeeded"


async def test_the_failure_is_logged_once_with_its_traceback(
    pool: Pool, scope: Scope, log_output: io.StringIO
) -> None:
    async def job() -> None:
        async with record_run(
            pool, workspace_id=scope.workspace, owner_id=scope.owner, kind="sync"
        ):
            raise ValueError(f"password={CANARY}")

    with pytest.raises(ValueError, match="password="):
        await job()

    text = log_output.getvalue()
    assert CANARY not in text
    entries = [json.loads(line) for line in text.splitlines()]
    (finished,) = [entry for entry in entries if entry["message"].startswith("run finished")]
    assert finished["level"] == "ERROR"
    assert finished["exception"]["type"] == "ValueError"
    assert finished["extra"]["error"] == f"ValueError: password={REDACTED}"


async def test_a_stream_without_its_source_is_refused(pool: Pool, scope: Scope) -> None:
    with pytest.raises(ValueError, match="names its source"):
        async with record_run(
            pool,
            workspace_id=scope.workspace,
            owner_id=scope.owner,
            kind="sync",
            stream_id=scope.stream,
        ):
            pytest.fail("the job must not run")

    assert await run_count(pool) == 0


async def test_a_stream_of_another_source_is_refused_by_the_database(
    pool: Pool, scope: Scope
) -> None:
    with pytest.raises(RunError, match=r"refused the run \(run_source_id_stream_id_fkey\)"):
        async with record_run(
            pool,
            workspace_id=scope.workspace,
            owner_id=scope.owner,
            kind="sync",
            source_id=scope.other_source,
            stream_id=scope.stream,
        ):
            pytest.fail("the job must not run")

    assert await run_count(pool) == 0


@pytest.mark.parametrize("kind", ["", "Sync", "sync now", "1sync"])
async def test_a_bad_kind_is_refused(pool: Pool, scope: Scope, kind: str) -> None:
    with pytest.raises(ValueError, match="run kind"):
        record_run(pool, workspace_id=scope.workspace, owner_id=scope.owner, kind=kind)


async def test_bad_counts_and_steps_are_refused(pool: Pool, scope: Scope) -> None:
    async with record_run(
        pool, workspace_id=scope.workspace, owner_id=scope.owner, kind="sync"
    ) as run:
        with pytest.raises(ValueError, match="count name"):
            run.count("Records Stored")
        with pytest.raises(ValueError, match="at least 0"):
            run.count("records_stored", -1)
        with pytest.raises(ValueError, match="input tokens"):
            run.llm_tokens(model="local-model", input=-1, output=0)
        with pytest.raises(ValueError, match="step name"):
            async with run.step(""):
                pass
        with pytest.raises(ValueError, match="outcome"):
            run.set_outcome("succeeded", "fine")  # pyright: ignore[reportArgumentType]


async def test_steps_past_the_limit_are_counted_not_listed(
    pool: Pool, scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runs, "MAX_STEPS", 2)
    async with record_run(
        pool, workspace_id=scope.workspace, owner_id=scope.owner, kind="sync"
    ) as run:
        for name in ("one", "two", "three"):
            async with run.step(name):
                pass

    report = (await run_row(pool, run.id))["report"]
    assert [step["name"] for step in report["steps"]] == ["one", "two"]
    assert report["steps_omitted"] == 1


async def test_a_dedicated_connection_works_too(
    migrated_database_url: str, pool: Pool, scope: Scope
) -> None:
    async with (
        connect(migrated_database_url, autocommit=True) as conn,
        record_run(conn, workspace_id=scope.workspace, owner_id=scope.owner, kind="sync") as run,
    ):
        assert (await run_row(pool, run.id))["status"] == "running"

    assert (await run_row(pool, run.id))["status"] == "succeeded"


async def test_a_connection_inside_a_transaction_is_refused(
    migrated_database_url: str, scope: Scope
) -> None:
    async with connect(migrated_database_url) as conn:
        await conn.execute("SELECT 1")  # opens a transaction
        with pytest.raises(RunError, match="inside a transaction"):
            async with record_run(
                conn, workspace_id=scope.workspace, owner_id=scope.owner, kind="sync"
            ):
                pytest.fail("the job must not run")


async def test_a_failed_report_write_does_not_mask_the_job_error(
    migrated_database_url: str, pool: Pool, scope: Scope, log_output: io.StringIO
) -> None:
    async def job(conn: AsyncConnection[Any]) -> None:
        async with record_run(
            conn, workspace_id=scope.workspace, owner_id=scope.owner, kind="sync"
        ):
            await conn.close()  # the final write will fail
            raise ValueError("the job's own error")

    async with connect(migrated_database_url, autocommit=True) as conn:
        with pytest.raises(ValueError, match="the job's own error"):
            await job(conn)

    messages = [json.loads(line)["message"] for line in log_output.getvalue().splitlines()]
    assert "could not write the report of the run" in messages
    assert "run finished: failed" in messages


async def test_a_failed_report_write_after_success_is_logged_not_raised(
    migrated_database_url: str, pool: Pool, scope: Scope, log_output: io.StringIO
) -> None:
    async with (
        connect(migrated_database_url, autocommit=True) as conn,
        record_run(conn, workspace_id=scope.workspace, owner_id=scope.owner, kind="sync") as run,
    ):
        await conn.close()

    assert (await run_row(pool, run.id))["status"] == "running"
    entries = [json.loads(line) for line in log_output.getvalue().splitlines()]
    (failure,) = [e for e in entries if e["message"] == "could not write the report of the run"]
    assert failure["level"] == "ERROR"
    assert failure["run_id"] == str(run.id)
    assert failure["exception"]["message"] == "the run's connection is closed"


async def test_details_reach_the_report_only_when_a_job_sets_them(pool: Pool, scope: Scope) -> None:
    async with record_run(
        pool, workspace_id=scope.workspace, owner_id=scope.owner, kind="sync"
    ) as plain:
        pass
    assert "details" not in (await run_row(pool, plain.id))["report"]

    plan: dict[str, Any] = {"windows": [{"kind": "forward", "from": "2026-03-01T00:00:00Z"}]}
    async with record_run(
        pool, workspace_id=scope.workspace, owner_id=scope.owner, kind="sync"
    ) as run:
        run.detail("plan", plan)
        plan["windows"].clear()  # the run keeps a copy
        run.detail("note", "first")
        run.detail("note", "second")  # set again: replaced
        with pytest.raises(ValueError, match="detail name"):
            run.detail("Plan", {})

    report = (await run_row(pool, run.id))["report"]
    assert report["details"] == {
        "plan": {"windows": [{"kind": "forward", "from": "2026-03-01T00:00:00Z"}]},
        "note": "second",
    }
