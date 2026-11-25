"""Token administration through the HTTP API (``/v1/tokens``), against a real Postgres.

Run with `make test-integration`. Names are synthetic; tokens are generated per test.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any

import httpx2
import pytest
from psycopg import AsyncConnection
from psycopg.rows import TupleRow
from pydantic import SecretStr

from memini.api import create_app
from memini.auth import AuthenticationError, Principal, Scope, authenticate, create_token
from memini.config import Settings
from memini.db import Pool, connect, open_pool
from memini.workspaces import create_workspace, ensure_principal

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

type Conn = AsyncConnection[TupleRow]
type Json = dict[str, Any]
type Headers = dict[str, str]


@pytest.fixture
async def db(migrated_database_url: str) -> AsyncIterator[Conn]:
    async with connect(migrated_database_url, autocommit=True) as conn:
        yield conn


@pytest.fixture
async def owner(db: Conn) -> uuid.UUID:
    return await ensure_principal(db, "owner")


@pytest.fixture
async def workspaces(db: Conn, owner: uuid.UUID) -> dict[str, uuid.UUID]:
    found: dict[str, uuid.UUID] = {}
    for name in ("alpha", "beta"):
        found[name] = (await create_workspace(db, owner_id=owner, name=name)).id
    return found


@pytest.fixture
async def pool(migrated_database_url: str) -> AsyncIterator[Pool]:
    async with open_pool(migrated_database_url, min_size=1, max_size=4, timeout=5.0) as pool:
        yield pool


@pytest.fixture
async def client(migrated_database_url: str, pool: Pool) -> AsyncIterator[httpx2.AsyncClient]:
    settings = Settings(database_url=SecretStr(migrated_database_url))
    transport = httpx2.ASGITransport(app=create_app(settings, pool=pool))
    async with httpx2.AsyncClient(transport=transport, base_url="http://memini.test") as client:
        yield client


async def bearer(
    db: Conn, owner: uuid.UUID, *scopes: Scope, workspace_id: uuid.UUID | None = None
) -> Headers:
    new = await create_token(
        db, owner_id=owner, name="test agent", scopes=scopes, workspace_id=workspace_id
    )
    return {"Authorization": f"Bearer {new.secret.get_secret_value()}"}


@pytest.fixture
async def admin(db: Conn, owner: uuid.UUID) -> Headers:
    return await bearer(db, owner, "read", "admin")


def ok(response: httpx2.Response, status_code: int = 200) -> Json:
    assert response.status_code == status_code, response.text
    body: Json = response.json()
    return body


def refused(response: httpx2.Response, status_code: int, code: str) -> Json:
    assert response.status_code == status_code, response.text
    error: Json = response.json()["error"]
    assert error["code"] == code
    return error


async def resolve(db: Conn, token: str) -> Principal:
    return await authenticate(db, token)


async def new_token(client: httpx2.AsyncClient, headers: Headers, **body: Any) -> Json:
    payload: Json = {"scopes": ["read"], "name": "laptop agent", **body}
    return ok(await client.post("/v1/tokens", json=payload, headers=headers), 201)


# --- Create --------------------------------------------------------------------------------------


async def test_an_admin_creates_a_token_shown_once(
    client: httpx2.AsyncClient, db: Conn, owner: uuid.UUID, admin: Headers
) -> None:
    response = await client.post(
        "/v1/tokens", json={"scopes": ["ingest", "read"], "name": "laptop agent"}, headers=admin
    )

    created = ok(response, 201)
    token: str = created["token"]
    assert response.headers["Cache-Control"] == "no-store"
    assert token.startswith("memini_")
    assert created["prefix"] == token[:15]
    assert created["scopes"] == ["read", "ingest"]
    assert created["workspace"] is None
    assert created["status"] == "active"
    assert created["expires_at"] is None
    principal = await resolve(db, token)
    assert principal.principal_id == owner
    assert principal.scopes == frozenset({"read", "ingest"})
    assert principal.workspace_id is None

    listed = ok(await client.get("/v1/tokens", headers=admin))
    assert token not in str(listed)
    assert token[15:] not in str(listed)
    assert "token" not in listed["items"][-1]


async def test_a_token_is_limited_to_a_workspace_and_expires(
    client: httpx2.AsyncClient,
    db: Conn,
    admin: Headers,
    workspaces: dict[str, uuid.UUID],
) -> None:
    created = await new_token(
        client, admin, scopes=["read"], workspace="alpha", expires_in_seconds=3600
    )

    assert created["workspace"] == "alpha"
    lifetime = datetime.fromisoformat(created["expires_at"]) - datetime.fromisoformat(
        created["created_at"]
    )
    assert lifetime == timedelta(hours=1)
    principal = await resolve(db, created["token"])
    assert principal.workspace_id == workspaces["alpha"]


async def test_an_admin_may_give_any_scope(
    client: httpx2.AsyncClient, db: Conn, owner: uuid.UUID
) -> None:
    only_admin = await bearer(db, owner, "admin")

    created = await new_token(client, only_admin, scopes=["read", "ingest", "admin"])

    assert created["scopes"] == ["read", "ingest", "admin"]


@pytest.mark.parametrize(
    "body",
    [
        {"scopes": [], "name": "x"},
        {"scopes": ["write"], "name": "x"},
        {"scopes": ["read"]},
        {"scopes": ["read"], "name": "   "},
        {"scopes": ["read"], "name": "tab\there"},
        {"scopes": ["read"], "name": "x", "workspace": "Not A Handle"},
        {"scopes": ["read"], "name": "x", "expires_in_seconds": 0},
        {"scopes": ["read"], "name": "x", "expires_in_seconds": 36526 * 86400},
        {"scopes": ["read"], "name": "x", "owner": "someone-else"},
    ],
)
async def test_an_invalid_request_is_422(
    client: httpx2.AsyncClient, admin: Headers, body: Json
) -> None:
    refused(await client.post("/v1/tokens", json=body, headers=admin), 422, "validation_error")


async def test_an_unknown_workspace_is_404(client: httpx2.AsyncClient, admin: Headers) -> None:
    response = await client.post(
        "/v1/tokens", json={"scopes": ["read"], "name": "x", "workspace": "gamma"}, headers=admin
    )

    assert refused(response, 404, "not_found")["message"] == "No such workspace."


@pytest.mark.parametrize("scopes", [("read",), ("read", "ingest")])
async def test_a_token_without_admin_manages_no_tokens(
    client: httpx2.AsyncClient, db: Conn, owner: uuid.UUID, scopes: tuple[Scope, ...]
) -> None:
    headers = await bearer(db, owner, *scopes)
    some = (await create_token(db, owner_id=owner, name="x", scopes=["read"])).info.id

    for response in (
        await client.post("/v1/tokens", json={"scopes": ["read"], "name": "x"}, headers=headers),
        await client.get("/v1/tokens", headers=headers),
        await client.delete(f"/v1/tokens/{some}", headers=headers),
    ):
        refused(response, 403, "insufficient_scope")


async def test_without_a_token_it_is_401(client: httpx2.AsyncClient) -> None:
    refused(await client.get("/v1/tokens"), 401, "unauthorized")


# --- A token limited to one workspace ------------------------------------------------------------


async def test_a_workspace_limited_admin_creates_tokens_for_its_workspace_only(
    client: httpx2.AsyncClient,
    db: Conn,
    owner: uuid.UUID,
    workspaces: dict[str, uuid.UUID],
) -> None:
    alpha_admin = await bearer(db, owner, "read", "admin", workspace_id=workspaces["alpha"])

    everywhere = await client.post(
        "/v1/tokens", json={"scopes": ["read"], "name": "x"}, headers=alpha_admin
    )
    other = await client.post(
        "/v1/tokens",
        json={"scopes": ["read"], "name": "x", "workspace": "beta"},
        headers=alpha_admin,
    )
    own = await new_token(client, alpha_admin, scopes=["read", "admin"], workspace="alpha")

    refused(everywhere, 403, "workspace_limited")
    refused(other, 404, "not_found")
    assert own["workspace"] == "alpha"
    assert (await resolve(db, own["token"])).workspace_id == workspaces["alpha"]


async def test_a_workspace_limited_admin_sees_and_revokes_only_its_workspace_tokens(
    client: httpx2.AsyncClient,
    db: Conn,
    owner: uuid.UUID,
    admin: Headers,
    workspaces: dict[str, uuid.UUID],
) -> None:
    alpha_admin = await bearer(db, owner, "admin", workspace_id=workspaces["alpha"])
    in_alpha = await new_token(client, admin, workspace="alpha", name="alpha agent")
    in_beta = await new_token(client, admin, workspace="beta", name="beta agent")
    everywhere = await new_token(client, admin, name="everywhere")

    seen = ok(await client.get("/v1/tokens", headers=alpha_admin))["items"]
    beta = await client.delete(f"/v1/tokens/{in_beta['id']}", headers=alpha_admin)
    every = await client.delete(f"/v1/tokens/{everywhere['id']}", headers=alpha_admin)
    alpha = await client.delete(f"/v1/tokens/{in_alpha['id']}", headers=alpha_admin)

    assert {t["workspace"] for t in seen} == {"alpha"}
    assert {t["name"] for t in seen} == {"test agent", "alpha agent"}
    assert refused(beta, 404, "not_found")["message"] == "No such token."
    refused(every, 404, "not_found")
    assert ok(alpha)["status"] == "revoked"
    await resolve(db, in_beta["token"])
    await resolve(db, everywhere["token"])


async def test_another_principals_tokens_are_not_seen(
    client: httpx2.AsyncClient, db: Conn, admin: Headers
) -> None:
    stranger = await ensure_principal(db, "someone-else")
    theirs = await create_token(db, owner_id=stranger, name="theirs", scopes=["read"])

    listed = ok(await client.get("/v1/tokens", headers=admin))["items"]
    revoked = await client.delete(f"/v1/tokens/{theirs.info.id}", headers=admin)

    assert "theirs" not in {t["name"] for t in listed}
    refused(revoked, 404, "not_found")


# --- List and revoke -----------------------------------------------------------------------------


async def test_list_shows_tokens_oldest_first_with_their_status(
    client: httpx2.AsyncClient, admin: Headers, workspaces: dict[str, uuid.UUID]
) -> None:
    first = await new_token(client, admin, name="first", workspace="beta")
    second = await new_token(client, admin, name="second")
    ok(await client.delete(f"/v1/tokens/{first['id']}", headers=admin))

    tokens = ok(await client.get("/v1/tokens", headers=admin))["items"]

    assert [t["name"] for t in tokens] == ["test agent", "first", "second"]
    assert [t["status"] for t in tokens] == ["active", "revoked", "active"]
    assert tokens[1]["workspace"] == "beta"
    assert tokens[0]["last_used_at"] is not None
    assert set(tokens[2]) == {
        "id",
        "name",
        "prefix",
        "scopes",
        "workspace",
        "status",
        "created_at",
        "expires_at",
        "revoked_at",
        "last_used_at",
    }
    assert second["token"] not in str(tokens)


async def test_revoke_is_idempotent_and_keeps_the_first_time(
    client: httpx2.AsyncClient, db: Conn, admin: Headers
) -> None:
    created = await new_token(client, admin)

    first = ok(await client.delete(f"/v1/tokens/{created['id']}", headers=admin))
    again = ok(await client.delete(f"/v1/tokens/{created['id']}", headers=admin))

    assert first["status"] == again["status"] == "revoked"
    assert first["revoked_at"] == again["revoked_at"]
    assert "token" not in first
    with pytest.raises(AuthenticationError) as refusal:
        await resolve(db, created["token"])
    assert refusal.value.reason == "revoked"


async def test_revoking_an_unknown_token_is_404(client: httpx2.AsyncClient, admin: Headers) -> None:
    unknown = await client.delete(f"/v1/tokens/{uuid.uuid4()}", headers=admin)
    malformed = await client.delete("/v1/tokens/not-a-uuid", headers=admin)

    refused(unknown, 404, "not_found")
    refused(malformed, 422, "validation_error")


async def test_a_token_may_revoke_itself(
    client: httpx2.AsyncClient, db: Conn, owner: uuid.UUID
) -> None:
    new = await create_token(db, owner_id=owner, name="self", scopes=["read", "admin"])
    headers = {"Authorization": f"Bearer {new.secret.get_secret_value()}"}

    revoked = ok(await client.delete(f"/v1/tokens/{new.info.id}", headers=headers))
    after = await client.get("/v1/tokens", headers=headers)

    assert revoked["status"] == "revoked"
    refused(after, 401, "invalid_token")
