"""Tokens, authentication and the authorization filter against a real Postgres.

Run with `make test-integration`. Names and tokens are synthetic; tokens are generated per test.
"""

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx2
import pytest
from psycopg import AsyncConnection, errors
from psycopg.rows import TupleRow

from memini.api import create_app
from memini.auth import (
    LAST_USED_RESOLUTION,
    AuthenticationError,
    NewToken,
    Principal,
    ReadFilter,
    Scope,
    TokenNotFoundError,
    authenticate,
    authorization,
    create_token,
    generate_token,
    hash_token,
    list_tokens,
    pool_authenticator,
    revoke_token,
)
from memini.config import Settings
from memini.db import Pool, connect, open_pool
from memini.workspaces import create_workspace, ensure_principal, list_workspaces

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

type Conn = AsyncConnection[TupleRow]

SOME_TOKEN_ID = UUID("00000000-0000-7000-8000-0000000000e0")


@dataclass(frozen=True)
class World:
    owner: UUID
    alpha: UUID
    beta: UUID


@pytest.fixture
async def db(migrated_database_url: str) -> AsyncIterator[Conn]:
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
async def pool(migrated_database_url: str) -> AsyncIterator[Pool]:
    async with open_pool(migrated_database_url, min_size=1, max_size=4, timeout=5.0) as pool:
        yield pool


@pytest.fixture
async def client(settings: Settings, pool: Pool) -> AsyncIterator[httpx2.AsyncClient]:
    transport = httpx2.ASGITransport(app=create_app(settings, pool=pool))
    async with httpx2.AsyncClient(transport=transport, base_url="http://memini.test") as client:
        yield client


async def new_token(
    db: Conn, world: World, *scopes: Scope, workspace_id: UUID | None = None, **kwargs: Any
) -> NewToken:
    return await create_token(
        db,
        owner_id=world.owner,
        name="test agent",
        scopes=scopes or ("read",),
        workspace_id=workspace_id,
        **kwargs,
    )


def bearer(token: NewToken | str) -> dict[str, str]:
    secret = token if isinstance(token, str) else token.secret.get_secret_value()
    return {"Authorization": f"Bearer {secret}"}


async def last_used(db: Conn, token_id: UUID) -> datetime | None:
    cur = await db.execute("SELECT last_used_at FROM token WHERE id = %s", (token_id,))
    row = await cur.fetchone()
    assert row is not None
    value: datetime | None = row[0]
    return value


def refused_with(response: httpx2.Response, status_code: int, code: str) -> None:
    assert response.status_code == status_code
    assert response.json()["error"]["code"] == code
    assert response.headers["WWW-Authenticate"].startswith('Bearer realm="memini"')


# --- Creating, listing and revoking ------------------------------------------------------------


async def test_create_shows_the_token_once_and_stores_only_its_digest(
    db: Conn, world: World
) -> None:
    new = await new_token(db, world, "admin", "read", "read")
    secret = new.secret.get_secret_value()

    assert secret.startswith("memini_")
    assert new.info.prefix == secret[:15]
    assert new.info.scopes == ("read", "admin")  # canonical order, no duplicates
    assert new.info.owner_id == world.owner
    assert new.info.workspace_id is None
    assert new.info.last_used_at is None

    cur = await db.execute(
        "SELECT hash, prefix, t::text FROM token t WHERE id = %s", (new.info.id,)
    )
    row = await cur.fetchone()
    assert row is not None
    digest, prefix, whole_row = row
    assert bytes(digest) == hash_token(secret)
    assert prefix == secret[:15]
    assert secret not in whole_row
    assert secret[15:] not in whole_row

    # Nothing returned later carries the secret or the digest.
    (listed,) = await list_tokens(db, owner_id=world.owner)
    assert listed == new.info
    assert not hasattr(listed, "hash")
    assert secret not in repr(listed)


async def test_list_tokens_is_per_owner(db: Conn, world: World) -> None:
    other = await ensure_principal(db, "test-other")
    mine = await new_token(db, world)
    await create_token(db, owner_id=other, name="other agent", scopes=["read"])

    assert [t.id for t in await list_tokens(db, owner_id=world.owner)] == [mine.info.id]


async def test_revoke_is_idempotent_and_per_owner(db: Conn, world: World) -> None:
    new = await new_token(db, world)
    first = await revoke_token(db, owner_id=world.owner, token_id=new.info.id)
    assert first.revoked_at is not None
    again = await revoke_token(db, owner_id=world.owner, token_id=new.info.id)
    assert again.revoked_at == first.revoked_at

    other = await ensure_principal(db, "test-other")
    with pytest.raises(TokenNotFoundError):
        await revoke_token(db, owner_id=other, token_id=new.info.id)
    with pytest.raises(TokenNotFoundError):
        await revoke_token(db, owner_id=world.owner, token_id=world.alpha)


async def test_expiry_must_be_in_the_future(db: Conn, world: World) -> None:
    with pytest.raises(errors.CheckViolation):
        await new_token(db, world, expires_at=datetime(2000, 1, 1, tzinfo=UTC))


async def test_ensure_principal_is_idempotent(db: Conn) -> None:
    first = await ensure_principal(db, "test-owner")
    assert await ensure_principal(db, "test-owner") == first
    assert await ensure_principal(db, "test-second") != first


# --- Authentication ----------------------------------------------------------------------------


async def test_a_token_resolves_to_its_principal(db: Conn, world: World) -> None:
    new = await new_token(db, world, "read", "ingest", workspace_id=world.alpha)

    principal = await authenticate(db, new.secret.get_secret_value())

    assert principal == Principal(
        principal_id=world.owner,
        token_id=new.info.id,
        scopes=frozenset({"read", "ingest"}),
        workspace_id=world.alpha,
    )


async def test_unknown_revoked_and_expired_tokens_are_refused(db: Conn, world: World) -> None:
    with pytest.raises(AuthenticationError) as refused:
        await authenticate(db, generate_token())
    assert refused.value.reason == "unknown"

    with pytest.raises(AuthenticationError) as refused:
        await authenticate(db, "memini_not-a-token")
    assert refused.value.reason == "malformed"

    revoked = await new_token(db, world)
    await revoke_token(db, owner_id=world.owner, token_id=revoked.info.id)
    with pytest.raises(AuthenticationError) as refused:
        await authenticate(db, revoked.secret.get_secret_value())
    assert (refused.value.reason, refused.value.token_id) == ("revoked", revoked.info.id)

    expired = await expired_token(db, world)
    with pytest.raises(AuthenticationError) as refused:
        await authenticate(db, expired.secret.get_secret_value())
    assert (refused.value.reason, refused.value.token_id) == ("expired", expired.info.id)


async def expired_token(db: Conn, world: World) -> NewToken:
    new = await new_token(db, world, expires_at=datetime.now(UTC) + timedelta(hours=1))
    await db.execute(
        "UPDATE token SET created_at = now() - interval '2 hours',"
        " expires_at = now() - interval '1 hour' WHERE id = %s",
        (new.info.id,),
    )
    return new


async def test_last_used_is_written_at_most_once_a_minute(
    db: Conn, world: World, pool: Pool
) -> None:
    assert timedelta(minutes=1) == LAST_USED_RESOLUTION
    new = await new_token(db, world)
    secret = new.secret.get_secret_value()
    authenticator = pool_authenticator(pool)

    await authenticator(secret)
    first = await last_used(db, new.info.id)
    assert first is not None

    await authenticator(secret)
    assert await last_used(db, new.info.id) == first  # within the minute: no write

    await db.execute(
        "UPDATE token SET last_used_at = now() - interval '2 minutes' WHERE id = %s",
        (new.info.id,),
    )
    stale = await last_used(db, new.info.id)
    await authenticator(secret)
    latest = await last_used(db, new.info.id)
    assert stale is not None
    assert latest is not None
    assert latest > stale


async def test_a_refused_token_is_not_marked_used(db: Conn, world: World, pool: Pool) -> None:
    new = await new_token(db, world)
    await revoke_token(db, owner_id=world.owner, token_id=new.info.id)
    with pytest.raises(AuthenticationError):
        await pool_authenticator(pool)(new.secret.get_secret_value())
    assert await last_used(db, new.info.id) is None


# --- The HTTP API ------------------------------------------------------------------------------


async def test_http_missing_malformed_and_unknown_tokens_are_401(
    client: httpx2.AsyncClient, world: World
) -> None:
    refused_with(await client.get("/v1/workspaces"), 401, "unauthorized")
    for header in ("Basic dXNlcjpwYXNz", "Bearer", "Bearer memini_short"):
        response = await client.get("/v1/workspaces", headers={"Authorization": header})
        expected = "unauthorized" if header.startswith("Basic") else "invalid_token"
        refused_with(response, 401, expected)
    refused_with(
        await client.get("/v1/workspaces", headers=bearer(generate_token())), 401, "invalid_token"
    )


async def test_http_revoked_and_expired_tokens_are_401(
    client: httpx2.AsyncClient, db: Conn, world: World
) -> None:
    revoked = await new_token(db, world)
    assert (await client.get("/v1/workspaces", headers=bearer(revoked))).status_code == 200
    await revoke_token(db, owner_id=world.owner, token_id=revoked.info.id)
    response = await client.get("/v1/workspaces", headers=bearer(revoked))
    refused_with(response, 401, "invalid_token")

    expired = await expired_token(db, world)
    response = await client.get("/v1/workspaces", headers=bearer(expired))
    refused_with(response, 401, "invalid_token")


async def test_http_scopes(client: httpx2.AsyncClient, db: Conn, world: World) -> None:
    writer = await new_token(db, world, "ingest", "admin")
    response = await client.get("/v1/workspaces", headers=bearer(writer))
    refused_with(response, 403, "insufficient_scope")
    assert 'scope="read"' in response.headers["WWW-Authenticate"]

    reader = await new_token(db, world, "read")
    response = await client.get("/v1/workspaces", headers=bearer(reader))
    assert response.status_code == 200
    assert [w["name"] for w in response.json()["workspaces"]] == ["alpha", "beta"]


async def test_a_token_limited_to_a_workspace_cannot_read_another(
    client: httpx2.AsyncClient, db: Conn, world: World
) -> None:
    limited = await new_token(db, world, "read", workspace_id=world.alpha)

    response = await client.get("/v1/workspaces", headers=bearer(limited))
    assert response.status_code == 200
    assert [(w["id"], w["name"]) for w in response.json()["workspaces"]] == [
        (str(world.alpha), "alpha")
    ]

    assert (await client.get("/v1/workspaces/alpha", headers=bearer(limited))).status_code == 200
    other = await client.get("/v1/workspaces/beta", headers=bearer(limited))
    missing = await client.get("/v1/workspaces/gamma", headers=bearer(limited))
    # Another workspace looks exactly like one that does not exist.
    assert other.status_code == missing.status_code == 404
    assert other.json()["error"]["code"] == missing.json()["error"]["code"] == "not_found"

    unlimited = await new_token(db, world, "read")
    response = await client.get("/v1/workspaces/beta", headers=bearer(unlimited))
    assert response.status_code == 200
    assert response.json()["id"] == str(world.beta)


async def test_the_read_path_calls_the_filter(
    client: httpx2.AsyncClient, db: Conn, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Principal] = []
    real = authorization.authorize_read

    def spy(principal: Principal, **kwargs: Any) -> ReadFilter:
        calls.append(principal)
        return real(principal, **kwargs)

    monkeypatch.setattr(authorization, "authorize_read", spy)
    new = await new_token(db, world, "read", workspace_id=world.beta)

    assert (await client.get("/v1/workspaces", headers=bearer(new))).status_code == 200
    assert (await client.get("/v1/workspaces/beta", headers=bearer(new))).status_code == 200

    expected = Principal(
        principal_id=world.owner,
        token_id=new.info.id,
        scopes=frozenset({"read"}),
        workspace_id=world.beta,
    )
    assert calls == [expected, expected]


async def test_the_read_path_returns_only_what_the_filter_allows(
    client: httpx2.AsyncClient, db: Conn, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def deny_everything(principal: Principal, **kwargs: Any) -> ReadFilter:
        return ReadFilter(principal=principal, workspace_ids=frozenset())

    unlimited = await new_token(db, world, "read")
    monkeypatch.setattr(authorization, "authorize_read", deny_everything)

    response = await client.get("/v1/workspaces", headers=bearer(unlimited))
    assert response.status_code == 200
    assert response.json() == {"workspaces": []}
    response = await client.get("/v1/workspaces/alpha", headers=bearer(unlimited))
    assert response.status_code == 404


async def test_read_functions_apply_the_filter(db: Conn, world: World) -> None:
    caller = Principal(principal_id=world.owner, token_id=SOME_TOKEN_ID, scopes=frozenset())
    unlimited = ReadFilter(principal=caller, workspace_ids=None)
    assert [w.name for w in await list_workspaces(db, unlimited)] == ["alpha", "beta"]
    only_beta = ReadFilter(principal=unlimited.principal, workspace_ids=frozenset({world.beta}))
    assert [w.name for w in await list_workspaces(db, only_beta)] == ["beta"]


async def test_the_token_never_reaches_logs_or_error_bodies(
    client: httpx2.AsyncClient, db: Conn, world: World, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    bodies: list[str] = []
    secrets: list[str] = []

    async def call(token: str, path: str = "/v1/workspaces") -> None:
        secrets.append(token)
        response = await client.get(path, headers={"Authorization": f"Bearer {token}"})
        bodies.append(response.text)
        bodies.append(str(response.headers))

    reader = await new_token(db, world, "read")
    writer = await new_token(db, world, "ingest")
    revoked = await new_token(db, world)
    await revoke_token(db, owner_id=world.owner, token_id=revoked.info.id)
    expired = await expired_token(db, world)
    reader_secret = reader.secret.get_secret_value()
    near_miss = reader_secret[:-1] + ("A" if reader_secret[-1] != "A" else "B")

    await call(reader_secret)
    await call(reader_secret, "/v1/workspaces/no-such-workspace")
    await call(writer.secret.get_secret_value())
    await call(revoked.secret.get_secret_value())
    await call(expired.secret.get_secret_value())
    await call(near_miss)
    await call(generate_token())
    await call(reader_secret + "x")

    assert caplog.records, "nothing was logged; the check would prove nothing"
    logged = caplog.text + "".join(str(record.__dict__) for record in caplog.records)
    for secret in secrets:
        assert secret not in logged
        assert secret[15:] not in logged  # not even past the display prefix
        for body in bodies:
            assert secret not in body


async def test_all_http_and_mcp_reads_apply_the_callers_filter(
    settings: Settings, pool: Pool, db: Conn, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise the registered reads and observe the caller's filter reaching SQL construction."""
    from fastapi.routing import iter_route_contexts
    from psycopg import sql
    from pydantic import SecretStr

    from memini.auth import WriteAccess
    from memini.embeddings import FakeEmbedder
    from memini.mcp.server import TOOLS
    from memini.mcp.tools import ToolCall

    token = await new_token(db, world, "read", "admin", "ingest", workspace_id=world.alpha)
    principal = await pool_authenticator(pool)(token.secret.get_secret_value())
    observed: list[str] = []

    class WatchedRead(ReadFilter):
        def where(self, **columns: Any) -> sql.Composable:
            observed.append("read")
            return super().where(**columns)

    class WatchedWrite(WriteAccess):
        def where(self, **columns: Any) -> sql.Composable:
            observed.append("write")
            return super().where(**columns)

    read = authorization.authorize_read
    write = authorization.authorize_write

    def watch_read(caller: Principal, **kwargs: Any) -> ReadFilter:
        assert caller == principal
        access = read(caller, **kwargs)
        return WatchedRead(caller, access.workspace_ids)

    def watch_write(caller: Principal, scope: Scope) -> WriteAccess:
        assert caller == principal
        access = write(caller, scope)
        return WatchedWrite(access.owner_id, access.workspace_ids)

    monkeypatch.setattr(authorization, "authorize_read", watch_read)
    monkeypatch.setattr(authorization, "authorize_write", watch_write)
    app = create_app(
        settings.model_copy(update={"metrics_token": SecretStr(generate_token())}),
        pool=pool,
        embedder=FakeEmbedder(),
    )
    scoped_metadata = {
        "/v1/tokens",
        "/v1/workspaces/{workspace}/sources/{source}/agent-runs/{run_id}",
        "/v1/workspaces/{workspace}/sources/{source}/agent-runs/{run_id}/next",
    }
    path_values = {
        "workspace": "alpha",
        "source": "source",
        "record_id": "0" * 64,
        "path": "file.py",
        "external_id": "external",
        "project": "sample",
        "domain": "sample",
        **dict.fromkeys(("run_id", "person_id", "item_id"), str(SOME_TOKEN_ID)),
    }
    query_values = {
        "/v1/search": {"query": "sample", "mode": "lexical"},
        "/v1/search/code": {"query": "sample", "mode": "exact"},
        "/v1/symbols": {"name": "sample"},
        "/v1/workspaces/{workspace}/persons/lookup": {"kind": "display_name", "value": "Sample"},
    }
    mutations = {
        "create_workspace",
        "create_source",
        "update_source",
        "delete_source",
        "sync_source",
        "prune_source",
        "ingest_records",
        "start_agent_run",
        "store_agent_response",
        "finish_agent_run",
        "create_token",
        "revoke_token",
        "create_person",
        "set_me",
        "unset_me",
        "rename_person",
        "add_alias",
        "remove_alias",
        "merge_person",
        "split_person",
        "add_document",
        "record_decision",
        "supersede_decision",
        "close_decision",
        "retract_decision",
        "extract",
        "forget",
        "put_project",
        "link_decision_domains",
        "unlink_decision_domain",
        "unlink_decision_project",
        "put_source_default",
        "delete_source_default",
        "assign_domains",
    }
    public_or_service = {"/healthz", "/readyz", "/version", "/metrics", "/openapi.json", "/mcp"}
    # The service's own text, the same for every token: it reads no row to filter.
    no_rows = {"/v1/catalog/guide"}
    no_rows_tools = {"catalog_guide"}
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        base_url="http://memini.test",
        headers=bearer(token),
    ) as http:
        for route in iter_route_contexts(app.routes):
            path = route.path_format
            assert path is not None
            if path in public_or_service:
                continue
            if "GET" not in (route.methods or set()):
                assert route.name in mutations, (
                    f"Classify {route.methods} {path} as a read or write"
                )
                continue
            observed.clear()
            response = await http.get(path.format_map(path_values), params=query_values.get(path))
            assert response.status_code in {200, 404}, (path, response.text)
            if path in no_rows:
                assert (response.status_code, observed) == (200, []), path
                continue
            assert ("write" if path in scoped_metadata else "read") in observed, path
            assert str(world.beta) not in response.text, path

    arguments: dict[str, dict[str, Any]] = {
        "status": {},
        "search": query_values["/v1/search"],
        "search_code": query_values["/v1/search/code"],
        "read_source": {"workspace": "alpha", "repository": "source"},
        "get_symbol": {"name": "sample"},
        "list_commits": {},
        "list_meetings": {},
        "list_messages": {},
        "get_meeting": {"record_id": "0" * 64},
        "get_thread": {"record_id": "0" * 64},
        "list_decisions": {},
        "get_decision": {"item_id": str(SOME_TOKEN_ID)},
        "get_catalog": {"workspace": "alpha"},
        "get_project": {"workspace": "alpha", "project": "sample"},
        "get_domain": {"workspace": "alpha", "project": "sample", "domain": "sample"},
        "catalog_guide": {},
    }
    writes = {
        "record_decision",
        "close_decision",
        "retract_decision",
        "supersede_decision",
        "add_document",
        "put_project",
    }
    assert {tool.name for tool in TOOLS if not tool.read_only} == writes
    assert {tool.name for tool in TOOLS if tool.read_only} == set(arguments)
    for tool in TOOLS:
        if tool.name in writes:
            continue
        assert tool.scope == "read"
        observed.clear()
        result = await tool.call(
            ToolCall(principal=principal, pool=pool, services=app.state.services),
            arguments[tool.name],
        )
        if tool.name in no_rows_tools:
            assert (result.is_error, observed) == (False, []), tool.name
            continue
        assert "read" in observed, (tool.name, result.content)
        assert str(world.beta) not in result.model_dump_json(), tool.name
