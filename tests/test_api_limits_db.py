"""Database time limits of requests against a real Postgres: what a borrowed connection gets, a
slow statement of a route cut by it, the MCP tools under it, and connections outside requests.

Run with `make test-integration`. Names and tokens are synthetic.
"""

import time
from collections.abc import AsyncIterator
from typing import Any, LiteralString

import anyio
import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from psycopg import AsyncConnection
from psycopg.rows import TupleRow
from pydantic import SecretStr

from memini import status
from memini.api import create_app
from memini.auth import ReadFilter, Scope, create_token
from memini.config import Settings
from memini.db import TIMEOUT_ERRORS, Pool, SessionLimits, connect, open_pool, session_limits
from memini.index import IndexFingerprint
from memini.mcp import MCP_PATH
from memini.workspaces import ensure_principal

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

type Conn = AsyncConnection[TupleRow]

BASE_URL = "http://memini.test"
SLOW_TRIGGER = """
CREATE FUNCTION slow_source_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_sleep(1);
    RETURN COALESCE(NEW, OLD);
END $$;
CREATE TRIGGER slow_update BEFORE UPDATE ON source
    FOR EACH ROW EXECUTE FUNCTION slow_source_change();
CREATE TRIGGER slow_delete BEFORE DELETE ON source
    FOR EACH ROW EXECUTE FUNCTION slow_source_change();
"""


@pytest.fixture
async def db(migrated_database_url: str) -> AsyncIterator[Conn]:
    """A connection outside the pool, in autocommit: the database's own defaults."""
    async with connect(migrated_database_url, autocommit=True) as conn:
        yield conn


@pytest.fixture
async def pool(migrated_database_url: str) -> AsyncIterator[Pool]:
    # One connection: every borrow below gets the same session.
    async with open_pool(migrated_database_url, min_size=1, max_size=1, timeout=5.0) as pool:
        yield pool


async def scalar(conn: Conn, query: LiteralString) -> Any:
    cur = await conn.execute(query)
    row = await cur.fetchone()
    assert row is not None
    return row[0]


async def limits_of(conn: Conn) -> tuple[str, str]:
    return (
        await scalar(conn, "SHOW statement_timeout"),
        await scalar(conn, "SHOW idle_in_transaction_session_timeout"),
    )


def settings_for(url: str, **values: Any) -> Settings:
    return Settings(database_url=SecretStr(url), **values)


async def bearer(db: Conn, *scopes: Scope) -> dict[str, str]:
    owner = await ensure_principal(db, "test-owner")
    token = await create_token(db, owner_id=owner, name="test agent", scopes=scopes)
    return {"Authorization": f"Bearer {token.secret.get_secret_value()}"}


# --- Connections ---------------------------------------------------------------------------------


async def test_a_connection_lent_in_a_request_has_its_limits_and_outside_the_defaults(
    pool: Pool, db: Conn
) -> None:
    defaults = await limits_of(db)
    with session_limits(SessionLimits(statement_timeout=0.25, idle_in_transaction_timeout=2)):
        async with pool.connection() as conn:
            session = await scalar(conn, "SELECT pg_backend_pid()")
            assert await limits_of(conn) == ("250ms", "2s")
        # 0: the database's default for that one.
        with session_limits(SessionLimits(statement_timeout=0, idle_in_transaction_timeout=60)):
            async with pool.connection() as conn:
                assert await limits_of(conn) == (defaults[0], "1min")

    # The same session lent outside a request, as the job worker borrows it: the defaults again.
    async with pool.connection() as conn:
        assert await scalar(conn, "SELECT pg_backend_pid()") == session
        assert await limits_of(conn) == defaults
    conn = await pool.getconn()
    try:
        assert await scalar(conn, "SELECT pg_backend_pid()") == session
        assert await limits_of(conn) == defaults
    finally:
        await pool.putconn(conn)


async def test_a_statement_past_the_limit_is_stopped(pool: Pool) -> None:
    with session_limits(SessionLimits(statement_timeout=0.1, idle_in_transaction_timeout=0)):
        async with pool.connection() as conn:
            with pytest.raises(TIMEOUT_ERRORS):
                await conn.execute("SELECT pg_sleep(2)")


async def test_a_transaction_left_idle_past_the_limit_ends_its_session(pool: Pool) -> None:
    with session_limits(SessionLimits(statement_timeout=0, idle_in_transaction_timeout=0.2)):
        async with pool.connection() as conn:
            await conn.execute("SELECT 1")  # opens the transaction
            await anyio.sleep(0.8)
            with pytest.raises(TIMEOUT_ERRORS):
                await conn.execute("SELECT 1")
    # The pool replaces the closed session.
    async with pool.connection() as conn:
        assert await scalar(conn, "SELECT 1") == 1


# --- Through the application ---------------------------------------------------------------------


@pytest.fixture
async def client(migrated_database_url: str, pool: Pool) -> AsyncIterator[httpx2.AsyncClient]:
    app = create_app(settings_for(migrated_database_url, http_statement_timeout=0.2), pool=pool)
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=BASE_URL) as client,
    ):
        yield client


async def test_a_slow_statement_of_a_route_answers_503_and_changes_nothing(
    client: httpx2.AsyncClient, db: Conn
) -> None:
    admin = await bearer(db, "read", "admin")
    created = await client.post("/v1/workspaces", json={"name": "alpha"}, headers=admin)
    assert created.status_code == 201, created.text
    path = "/v1/workspaces/alpha/sources"
    added = await client.post(path, json={"kind": "push", "name": "notes"}, headers=admin)
    assert added.status_code == 201, added.text
    await db.execute(SLOW_TRIGGER)

    started = time.monotonic()
    response = await client.patch(f"{path}/notes", json={"name": "memos"}, headers=admin)
    assert time.monotonic() - started < 1  # cut before the trigger's second was up
    assert response.status_code == 503, response.text
    error = response.json()["error"]
    assert error["code"] == "query_timeout"
    assert error["request_id"] == response.headers["X-Request-ID"]
    assert await scalar(db, "SELECT name FROM source") == "notes"

    # Deleting a source is exempt: its cascade grows with the source.
    deleted = await client.delete(f"{path}/notes", headers=admin)
    assert deleted.status_code == 204, deleted.text
    assert await scalar(db, "SELECT count(*) FROM source") == 0


async def test_mcp_tools_run_under_the_request_limits(
    migrated_database_url: str, pool: Pool, db: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, str]] = []
    read = status._read  # pyright: ignore[reportPrivateUsage]

    async def watched_read(
        conn: Conn, access: ReadFilter, fingerprint: IndexFingerprint | None
    ) -> status.ServiceStatus:
        seen.append(await limits_of(conn))
        return await read(conn, access, fingerprint)

    monkeypatch.setattr(status, "_read", watched_read)
    settings = settings_for(
        migrated_database_url, http_statement_timeout=12.5, http_idle_in_transaction_timeout=90
    )
    app = create_app(settings, pool=pool)
    headers = await bearer(db, "read")
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url=BASE_URL, headers=headers
        ) as http,
        Client(streamable_http_client(f"{BASE_URL}{MCP_PATH}", http_client=http)) as mcp,
    ):
        result = await mcp.call_tool("status", {})
    assert result.is_error is False
    # The tool runs in a task of the MCP server, not the request's; the limits went with it.
    assert seen == [("12500ms", "90s")]
