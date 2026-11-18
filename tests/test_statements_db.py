"""The slow-statement log on a real Postgres: what is logged, and what never is.

Run with `make test-integration`. Every value is synthetic.
"""

import io
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from psycopg import errors, sql

from memini.db import Pool, SessionLimits, open_pool, session_limits
from memini.logs import run_id_var
from memini.metrics import DB_SLOW_STATEMENTS, DB_STATEMENT_DURATION

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

PARAMETER = "synthetic-parameter-value"
LITERAL = "synthetic-literal-value"
SLOW = 0.02
"""Seconds: the threshold of the pool in these tests."""


@pytest.fixture
async def pool(migrated_database_url: str) -> AsyncIterator[Pool]:
    async with open_pool(
        migrated_database_url, min_size=1, max_size=2, timeout=5.0, slow_statement_threshold=SLOW
    ) as pool:
        yield pool


def slow_lines(output: io.StringIO) -> list[dict[str, Any]]:
    lines = [json.loads(line) for line in output.getvalue().splitlines()]
    return [line for line in lines if line["logger"] == "memini.db.statements"]


async def test_a_slow_statement_is_logged_with_its_fingerprint_and_never_its_values(
    pool: Pool, log_output: io.StringIO
) -> None:
    slow = DB_SLOW_STATEMENTS.value()
    timed = DB_STATEMENT_DURATION.count()
    run_id = uuid.uuid4()

    token = run_id_var.set(run_id)
    try:
        async with pool.connection() as conn:
            await conn.execute("SELECT pg_sleep(0.05), %s::text", (PARAMETER,))
            await conn.execute(
                sql.SQL("SELECT pg_sleep(0.05), {}::text").format(sql.Literal(LITERAL))
            )
            await conn.execute("SELECT %s::text", (PARAMETER,))  # fast: not logged
    finally:
        run_id_var.reset(token)

    first, second = slow_lines(log_output)
    assert first["level"] == "WARNING"
    assert first["run_id"] == str(run_id)
    assert first["message"].startswith("slow statement: ")
    extra = first["extra"]
    assert extra["statement"] == "SELECT pg_sleep(?), %s::text"
    assert extra["duration_ms"] >= 50
    assert extra["failed"] is False
    assert extra["call"] == "execute"
    assert len(extra["fingerprint"]) == 16
    assert second["extra"]["statement"] == "SELECT pg_sleep(?), ?::text"
    assert second["extra"]["fingerprint"] != extra["fingerprint"]
    assert PARAMETER not in log_output.getvalue()
    assert LITERAL not in log_output.getvalue()
    assert DB_SLOW_STATEMENTS.value() == slow + 2
    assert DB_STATEMENT_DURATION.count() >= timed + 3


async def test_a_statement_stopped_by_its_time_limit_is_logged_as_failed(
    pool: Pool, log_output: io.StringIO
) -> None:
    with session_limits(SessionLimits(statement_timeout=0.1, idle_in_transaction_timeout=0)):
        async with pool.connection() as conn:
            with pytest.raises(errors.QueryCanceled):
                await conn.execute("SELECT pg_sleep(5), %s::text", (PARAMETER,))

    (line,) = slow_lines(log_output)
    assert line["extra"]["failed"] is True
    assert line["extra"]["statement"] == "SELECT pg_sleep(?), %s::text"
    assert PARAMETER not in log_output.getvalue()


async def test_executemany_is_timed_as_one_call(pool: Pool, log_output: io.StringIO) -> None:
    async with pool.connection() as conn:
        await conn.execute("CREATE TEMP TABLE synthetic_rows (value text)")
        async with conn.cursor() as cur:
            await cur.executemany(
                "INSERT INTO synthetic_rows SELECT %s FROM pg_sleep(0.01)",
                [(f"{PARAMETER}-{n}",) for n in range(4)],
            )

    (line,) = slow_lines(log_output)
    assert line["extra"]["call"] == "executemany"
    assert line["extra"]["statement"] == "INSERT INTO synthetic_rows SELECT %s FROM pg_sleep(?)"
    assert PARAMETER not in log_output.getvalue()


async def test_a_threshold_of_zero_logs_nothing(
    migrated_database_url: str, log_output: io.StringIO
) -> None:
    timed = DB_STATEMENT_DURATION.count()
    async with (
        open_pool(migrated_database_url, min_size=1, max_size=1, timeout=5.0) as pool,
        pool.connection() as conn,
    ):
        await conn.execute("SELECT pg_sleep(0.05)")

    assert slow_lines(log_output) == []
    assert DB_STATEMENT_DURATION.count() > timed  # still timed
