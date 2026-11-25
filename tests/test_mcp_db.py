"""The MCP endpoint against a real Postgres: database tokens, the status tool, the filter.

Run with `make test-integration`. Names and keys are synthetic; tokens are generated per test. A
real MCP client (the SDK's streamable HTTP client) talks to the application in-process.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx2
import pytest
from fastapi import FastAPI
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from psycopg import AsyncConnection
from psycopg.rows import TupleRow

from memini.api import create_app
from memini.auth import (
    NewToken,
    Principal,
    ReadFilter,
    Scope,
    authorization,
    create_token,
    revoke_token,
)
from memini.config import Settings
from memini.db import Pool, connect, open_pool
from memini.embeddings import FakeEmbedder
from memini.mcp import MCP_PATH
from memini.status import expected_schema_version
from memini.workspaces import create_workspace, ensure_principal

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

type Conn = AsyncConnection[TupleRow]

BASE_URL = "http://memini.test"


@dataclass(frozen=True)
class World:
    owner: UUID
    alpha: UUID
    beta: UUID


@pytest.fixture
async def db(migrated_database_url: str) -> AsyncGenerator[Conn]:
    """A connection to a migrated database, in autocommit: each statement is committed."""
    async with connect(migrated_database_url, autocommit=True) as conn:
        yield conn


@pytest.fixture
async def world(db: Conn) -> World:
    owner = await ensure_principal(db, "test-owner")
    alpha = await create_workspace(db, owner_id=owner, name="alpha")
    beta = await create_workspace(db, owner_id=owner, name="beta")
    return World(owner=owner, alpha=alpha.id, beta=beta.id)


@pytest.fixture
async def pool(migrated_database_url: str) -> AsyncGenerator[Pool]:
    async with open_pool(migrated_database_url, min_size=1, max_size=4, timeout=5.0) as pool:
        yield pool


@pytest.fixture
def app(settings: Settings, pool: Pool) -> FastAPI:
    return create_app(settings, pool=pool)


async def new_token(
    db: Conn, world: World, *scopes: Scope, workspace_id: UUID | None = None
) -> str:
    token = await create_token(
        db,
        owner_id=world.owner,
        name="test agent",
        scopes=scopes or ("read",),
        workspace_id=workspace_id,
    )
    return secret(token)


def secret(token: NewToken) -> str:
    return token.secret.get_secret_value()


@asynccontextmanager
async def http_client(app: FastAPI, token: str | None) -> AsyncGenerator[httpx2.AsyncClient]:
    headers = {"Authorization": f"Bearer {token}"} if token else None
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url=BASE_URL, headers=headers
        ) as client,
    ):
        yield client


@asynccontextmanager
async def mcp_client(
    app: FastAPI, token: str | None, *, mode: str = "legacy"
) -> AsyncGenerator[Client]:
    async with http_client(app, token) as http:
        transport = streamable_http_client(f"{BASE_URL}{MCP_PATH}", http_client=http)
        async with Client(transport, mode=mode) as client:
            yield client


async def call_status(app: FastAPI, token: str, *, mode: str = "legacy") -> dict[str, Any]:
    async with mcp_client(app, token, mode=mode) as client:
        result = await client.call_tool("status", {})
    assert result.is_error is False
    structured: dict[str, Any] = result.structured_content
    return structured


async def insert_source(db: Conn, world: World, workspace_id: UUID, name: str, kind: str) -> UUID:
    cur = await db.execute(
        "INSERT INTO source (workspace_id, owner_id, name, kind) VALUES (%s, %s, %s, %s)"
        " RETURNING id",
        (workspace_id, world.owner, name, kind),
    )
    row = await cur.fetchone()
    assert row is not None
    source_id: UUID = row[0]
    return source_id


async def insert_stream(
    db: Conn,
    world: World,
    workspace_id: UUID,
    source_id: UUID,
    key: str,
    *,
    checked_ago: str | None,
    schedule: str | None,
) -> UUID:
    """A stream checked ``checked_ago`` (an interval) before now; ``None``: never checked."""
    cur = await db.execute(
        "INSERT INTO stream (workspace_id, owner_id, source_id, key, checked_until, schedule)"
        " VALUES (%s, %s, %s, %s, now() - %s::interval, %s::interval) RETURNING id",
        (workspace_id, world.owner, source_id, key, checked_ago, schedule),
    )
    row = await cur.fetchone()
    assert row is not None
    stream_id: UUID = row[0]
    return stream_id


async def insert_run(
    db: Conn,
    world: World,
    workspace_id: UUID,
    source_id: UUID,
    stream_id: UUID,
    *,
    status: str,
    started_ago: str,
    finished_ago: str | None,
) -> None:
    await db.execute(
        "INSERT INTO run (workspace_id, owner_id, source_id, stream_id, kind, status,"
        " started_at, finished_at)"
        " VALUES (%s, %s, %s, %s, 'sync', %s, now() - %s::interval, now() - %s::interval)",
        (workspace_id, world.owner, source_id, stream_id, status, started_ago, finished_ago),
    )


@pytest.fixture
async def streams(db: Conn, world: World) -> None:
    """alpha: one current git stream, one never checked. beta: a push stream far behind."""
    code = await insert_source(db, world, world.alpha, "code", "git")
    main = await insert_stream(
        db,
        world,
        world.alpha,
        code,
        "refs/heads/main",
        checked_ago="5 minutes",
        schedule="15 minutes",
    )
    await insert_run(
        db,
        world,
        world.alpha,
        code,
        main,
        status="failed",
        started_ago="1 hour",
        finished_ago="59 minutes",
    )
    await insert_run(
        db,
        world,
        world.alpha,
        code,
        main,
        status="succeeded",
        started_ago="6 minutes",
        finished_ago="5 minutes",
    )
    await insert_stream(
        db, world, world.alpha, code, "refs/heads/dev", checked_ago=None, schedule="1 hour"
    )
    notes = await insert_source(db, world, world.beta, "notes", "push")
    inbox = await insert_stream(
        db, world, world.beta, notes, "inbox", checked_ago="3 days", schedule="1 hour"
    )
    await insert_run(
        db,
        world,
        world.beta,
        notes,
        inbox,
        status="running",
        started_ago="2 minutes",
        finished_ago=None,
    )


# --- status -----------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["legacy", "auto"])
@pytest.mark.usefixtures("streams")
async def test_status_reports_every_stream_the_token_may_read(
    app: FastAPI, db: Conn, world: World, mode: str
) -> None:
    status = await call_status(app, await new_token(db, world, "read"), mode=mode)

    assert status["current"] is False  # dev was never checked, inbox is days behind
    assert status["database"] == {
        "reachable": True,
        "schema_version": expected_schema_version(),
        "expected_schema_version": expected_schema_version(),
    }
    assert [w["name"] for w in status["workspaces"]] == ["alpha", "beta"]
    alpha, beta = status["workspaces"]
    assert alpha["id"] == str(world.alpha)

    dev, main = alpha["streams"]  # by source, then key
    assert main["source"] == "code"
    assert main["source_kind"] == "git"
    assert main["key"] == "refs/heads/main"
    assert main["schedule_seconds"] == 900.0
    assert 300.0 <= main["lag_seconds"] < 360.0
    assert main["checked_until"] is not None
    assert main["current"] is True
    # The newest run, not the failed one before it.
    assert main["last_run"]["status"] == "succeeded"
    assert main["last_run"]["finished_at"] is not None

    assert dev["key"] == "refs/heads/dev"
    assert dev["checked_until"] is None
    assert dev["lag_seconds"] is None
    assert dev["current"] is False
    assert dev["last_run"] is None

    (inbox,) = beta["streams"]
    assert inbox["source_kind"] == "push"
    assert inbox["lag_seconds"] >= 3 * 86400
    assert inbox["current"] is False
    assert inbox["last_run"]["status"] == "running"
    assert inbox["last_run"]["finished_at"] is None
    # The failed run before it is followed by one that succeeded: no failures in a row.
    assert (main["failures"], main["last_failure"]) == (0, None)
    assert main["behind"] == []
    assert dev["behind"] == ["never_checked"]
    assert inbox["behind"] == ["lag"]
    assert inbox["failures"] == 0

    # The application has no embedding provider: no index, nothing judged by it.
    assert status["index"]["state"] == "off"
    assert [w["index"]["state"] for w in status["workspaces"]] == ["off", "off"]


async def test_the_status_tool_judges_the_index_with_the_service_s_embedder(
    settings: Settings, pool: Pool, db: Conn, world: World
) -> None:
    notes = await insert_source(db, world, world.beta, "notes", "push")
    inbox = await insert_stream(
        db, world, world.beta, notes, "inbox", checked_ago="1 minute", schedule=None
    )
    # A thread stored while no embedding provider was set: a payload, no chunk.
    await db.execute(
        "INSERT INTO record (id, workspace_id, owner_id, source_id, stream_id, kind, external_id,"
        " content_hash, occurred_at, payload, normalized_text)"
        " VALUES (record_id(%s, 'thread', 'inbox/0001'), %s, %s, %s, %s, 'thread', 'inbox/0001',"
        " repeat('0', 64), now(), %s, 'synthetic text')",
        (notes, world.beta, world.owner, notes, inbox, b'{"messages":[]}'),
    )
    app = create_app(settings, pool=pool, embedder=FakeEmbedder())

    status = await call_status(app, await new_token(db, world, "read"))

    assert status["current"] is False
    assert status["index"]["state"] == "behind"
    alpha, beta = status["workspaces"]
    assert alpha["index"]["state"] == "none"
    assert beta["index"]["records_not_indexed"] == 1
    assert beta["index"]["reindex"] == "memini reindex --workspace beta"
    assert beta["streams"][0]["behind"] == ["not_indexed"]
    assert beta["streams"][0]["current"] is False

    async with mcp_client(app, await new_token(db, world, "read")) as client:
        result = await client.call_tool("status", {})
    lines = [
        line
        for content in result.content
        if content.type == "text"
        for line in content.text.splitlines()
    ]
    (inbox_line,) = (line for line in lines if line.startswith("- beta/notes (push) inbox: lag "))
    assert inbox_line.endswith(", no run yet; 1 records not indexed")
    text = "\n".join(lines)
    assert (
        "- beta: index none, 1 records not indexed; run `memini reindex --workspace beta`" in text
    )


@pytest.mark.usefixtures("streams")
async def test_a_workspace_token_sees_only_its_workspace(
    app: FastAPI, db: Conn, world: World
) -> None:
    token = await new_token(db, world, "read", workspace_id=world.beta)
    status = await call_status(app, token)

    assert [w["name"] for w in status["workspaces"]] == ["beta"]
    assert [s["key"] for s in status["workspaces"][0]["streams"]] == ["inbox"]


async def test_with_no_streams_the_base_is_current(app: FastAPI, db: Conn, world: World) -> None:
    status = await call_status(app, await new_token(db, world, "read"))

    assert status["current"] is True
    assert [w["streams"] for w in status["workspaces"]] == [[], []]


async def test_streams_without_a_schedule_do_not_count(
    app: FastAPI, db: Conn, world: World
) -> None:
    code = await insert_source(db, world, world.alpha, "code", "git")
    await insert_stream(
        db, world, world.alpha, code, "refs/heads/main", checked_ago="2 minutes", schedule="1 hour"
    )
    docs = await insert_source(db, world, world.alpha, "docs", "push")
    await insert_stream(
        db, world, world.alpha, docs, "manual", checked_ago="30 days", schedule=None
    )
    await insert_stream(db, world, world.alpha, docs, "never", checked_ago=None, schedule=None)

    status = await call_status(app, await new_token(db, world, "read"))

    assert status["current"] is True
    streams = {s["key"]: s for s in status["workspaces"][0]["streams"]}
    assert streams["manual"]["current"] is None
    assert streams["manual"]["schedule_seconds"] is None
    assert streams["manual"]["lag_seconds"] >= 30 * 86400
    assert streams["never"]["current"] is None


async def test_status_reads_through_the_authorization_filter(
    app: FastAPI, db: Conn, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Principal] = []
    original = authorization.authorize_read

    def watched(principal: Principal, **kwargs: Any) -> ReadFilter:
        calls.append(principal)
        return original(principal, **kwargs)

    monkeypatch.setattr(authorization, "authorize_read", watched)
    token = await new_token(db, world, "read", workspace_id=world.alpha)

    status = await call_status(app, token)

    assert len(calls) == 1
    assert calls[0].workspace_id == world.alpha
    assert [w["name"] for w in status["workspaces"]] == ["alpha"]


# --- Tokens from the database ---------------------------------------------------------------------


async def test_a_database_token_lists_the_tools(app: FastAPI, db: Conn, world: World) -> None:
    async with mcp_client(app, await new_token(db, world, "read")) as client:
        listed = await client.list_tools()
    assert [tool.name for tool in listed.tools] == [
        "search",
        "search_code",
        "read_source",
        "get_symbol",
        "list_commits",
        "get_catalog",
        "get_project",
        "get_domain",
        "catalog_guide",
        "list_meetings",
        "get_meeting",
        "list_messages",
        "get_thread",
        "list_decisions",
        "get_decision",
        "status",
    ]


async def test_the_client_cannot_connect_without_a_token(app: FastAPI) -> None:
    with pytest.raises(ExceptionGroup) as info:
        async with mcp_client(app, None):
            pass
    assert info.group_contains(MCPError)


_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
_ACCEPT = {"Accept": "application/json, text/event-stream"}


async def post_initialize(app: FastAPI, token: str | None) -> httpx2.Response:
    async with http_client(app, token) as http:
        return await http.post(MCP_PATH, json=_INITIALIZE, headers=_ACCEPT)


async def test_no_token_is_refused(app: FastAPI) -> None:
    response = await post_initialize(app, None)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == 'Bearer realm="memini"'


async def test_a_revoked_token_is_refused(app: FastAPI, db: Conn, world: World) -> None:
    token = await create_token(db, owner_id=world.owner, name="test agent", scopes=("read",))
    await revoke_token(db, owner_id=world.owner, token_id=token.info.id)

    response = await post_initialize(app, secret(token))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


async def test_a_token_without_the_read_scope_is_forbidden(
    app: FastAPI, db: Conn, world: World
) -> None:
    response = await post_initialize(app, await new_token(db, world, "ingest", "admin"))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "insufficient_scope"
    assert response.headers["WWW-Authenticate"] == (
        'Bearer realm="memini", error="insufficient_scope", scope="read"'
    )
