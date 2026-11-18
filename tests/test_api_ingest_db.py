"""The push API against a real Postgres: push sources, batches over HTTP, scopes, the workspace
limit of a token, size limits and the error shape. The library's semantics are tested in
tests/test_ingest_db.py. Every value is synthetic."""

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any, LiteralString

import httpx2
import pytest
from psycopg import AsyncConnection
from psycopg.rows import TupleRow
from pydantic import SecretStr

from ingest_helpers import DAY, HOUR, T0, iso, thread_payload
from memini.api import create_app
from memini.auth import Scope, create_token
from memini.config import Settings
from memini.db import Pool, connect, open_pool
from memini.workspaces import ensure_principal

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

type Conn = AsyncConnection[TupleRow]
type Json = dict[str, Any]

RECORDS = "/v1/workspaces/alpha/sources/chat/records"


@pytest.fixture
async def db(migrated_database_url: str) -> AsyncIterator[Conn]:
    async with connect(migrated_database_url, autocommit=True) as conn:
        yield conn


@pytest.fixture
async def owner(db: Conn) -> uuid.UUID:
    return await ensure_principal(db, "test-owner")


@pytest.fixture
async def pool(migrated_database_url: str) -> AsyncIterator[Pool]:
    async with open_pool(migrated_database_url, min_size=1, max_size=4, timeout=5.0) as pool:
        yield pool


@pytest.fixture
def settings(migrated_database_url: str) -> Settings:
    return Settings(
        database_url=SecretStr(migrated_database_url),
        ingest_max_body_size=64 * 1024,
        ingest_max_records=5,
        ingest_max_payload_size=4096,
    )


@pytest.fixture
async def client(settings: Settings, pool: Pool) -> AsyncIterator[httpx2.AsyncClient]:
    transport = httpx2.ASGITransport(app=create_app(settings, pool=pool))
    async with httpx2.AsyncClient(transport=transport, base_url="http://memini.test") as client:
        yield client


async def bearer(
    db: Conn, owner: uuid.UUID, *scopes: Scope, workspace_id: uuid.UUID | None = None
) -> dict[str, str]:
    new = await create_token(
        db, owner_id=owner, name="test client", scopes=scopes, workspace_id=workspace_id
    )
    return {"Authorization": f"Bearer {new.secret.get_secret_value()}"}


@pytest.fixture
async def admin(db: Conn, owner: uuid.UUID) -> dict[str, str]:
    return await bearer(db, owner, "read", "admin")


@pytest.fixture
async def pusher(db: Conn, owner: uuid.UUID) -> dict[str, str]:
    return await bearer(db, owner, "ingest")


def ok(response: httpx2.Response, status_code: int = 200) -> Json:
    assert response.status_code == status_code, response.text
    body: Json = response.json()
    return body


def refused(response: httpx2.Response, status_code: int, code: str) -> Json:
    assert response.status_code == status_code, response.text
    error: Json = response.json()["error"]
    assert error["code"] == code, error
    return error


async def scalar(db: Conn, query: LiteralString, *params: Any) -> Any:
    cur = await db.execute(query, params)
    row = await cur.fetchone()
    assert row is not None
    return row[0]


async def setup(client: httpx2.AsyncClient, admin: dict[str, str]) -> Json:
    """Workspaces alpha and beta, a push source chat in each and a git source repo in alpha."""
    workspaces: Json = {}
    for name in ("alpha", "beta"):
        workspaces[name] = ok(
            await client.post("/v1/workspaces", json={"name": name}, headers=admin), 201
        )
        ok(
            await client.post(
                f"/v1/workspaces/{name}/sources",
                json={"kind": "push", "name": "chat", "retention_days": 90},
                headers=admin,
            ),
            201,
        )
    ok(
        await client.post(
            "/v1/workspaces/alpha/sources",
            json={
                "kind": "git",
                "name": "repo",
                "location": "https://git.example.invalid/team/repo.git",
                "refs": ["main"],
            },
            headers=admin,
        ),
        201,
    )
    return workspaces


def thread(external_id: str, hours: int = 9) -> Json:
    at = T0 + hours * HOUR
    return {
        "kind": "thread",
        "external_id": external_id,
        "occurred_at": iso(at),
        "external_url": f"https://chat.example.invalid/archives/{external_id}",
        "payload": thread_payload(at),
    }


def day_batch(*records: Json) -> Json:
    return {
        "stream": "chat/general",
        "window": {"from": iso(T0), "to": iso(T0 + DAY), "complete": True},
        "records": list(records),
    }


# --- A push source and its batches ---------------------------------------------------------------


async def test_a_push_source_takes_a_batch_and_the_same_batch_again_changes_nothing(
    client: httpx2.AsyncClient, db: Conn, admin: dict[str, str], pusher: dict[str, str]
) -> None:
    await setup(client, admin)
    shown = ok(await client.get("/v1/workspaces/alpha/sources/chat", headers=admin))
    assert (shown["kind"], shown["location"], shown["refs"], shown["retention_days"]) == (
        "push",
        None,
        [],
        90,
    )

    batch = day_batch(
        thread("general/0001"),
        thread("general/0002", 11),
        {
            "kind": "meeting",
            "external_id": "m-1",
            "occurred_at": iso(T0 + 14 * HOUR),
            "payload": {"title": "Release planning", "started_at": iso(T0 + 14 * HOUR)},
        },
    )
    first = ok(await client.post(RECORDS, json=batch, headers=pusher))
    assert first["counts"] == {
        "inserted": 3,
        "updated": 0,
        "revived": 0,
        "deleted": 0,
        "unchanged": 0,
        "reconciled": 0,
    }
    assert first["stream"] == "chat/general"
    assert first["window_complete"] is True
    assert first["checkpoint"] == {
        "checked_until": "2026-03-02T00:00:00Z",
        "cursor": None,
        "moved": True,
    }

    xmin = await scalar(db, "SELECT string_agg(xmin::text, ',' ORDER BY id) FROM record")
    # The same batch, with other spacing and key order: the same content.
    body = json.dumps(dict(reversed(list(batch.items()))), indent=2).encode()
    again = ok(
        await client.post(
            RECORDS, content=body, headers={**pusher, "Content-Type": "application/json"}
        )
    )
    assert again["counts"]["unchanged"] == 3
    assert again["counts"]["inserted"] == 0
    assert again["checkpoint"]["moved"] is False
    assert await scalar(db, "SELECT string_agg(xmin::text, ',' ORDER BY id) FROM record") == xmin

    streams = ok(await client.get("/v1/workspaces/alpha/sources/chat/streams", headers=admin))
    (stream,) = streams["streams"]
    assert stream["key"] == "chat/general"
    assert stream["checked_until"] == "2026-03-02T00:00:00Z"
    assert stream["last_run"]["kind"] == "ingest"
    assert stream["last_run"]["status"] == "succeeded"
    run = ok(await client.get(f"/v1/runs/{again['run_id']}", headers=admin))
    assert run["report"]["counts"]["records_unchanged"] == 3
    assert (run["source"], run["stream"]) == ("chat", "chat/general")

    # A tombstone, and an incomplete window: the checkpoint stays, the run is partial.
    later = ok(
        await client.post(
            RECORDS,
            json={
                "stream": "chat/general",
                "window": {"from": iso(T0 + DAY), "to": iso(T0 + 2 * DAY), "complete": False},
                "records": [{"kind": "thread", "external_id": "general/0002", "deleted": True}],
            },
            headers=pusher,
        )
    )
    assert later["counts"]["deleted"] == 1
    assert later["checkpoint"]["checked_until"] == "2026-03-02T00:00:00Z"
    run = ok(await client.get(f"/v1/runs/{later['run_id']}", headers=admin))
    assert run["status"] == "partial"


async def test_a_push_source_has_no_schedule(
    client: httpx2.AsyncClient, admin: dict[str, str]
) -> None:
    await setup(client, admin)
    path = "/v1/workspaces/alpha/sources/chat"
    error = refused(
        await client.patch(path, json={"schedule_seconds": 3600}, headers=admin),
        422,
        "invalid_source",
    )
    assert "schedule" in error["message"]
    refused(await client.post(f"{path}/sync", headers=admin), 409, "not_syncable")
    refused(
        await client.post(
            "/v1/workspaces/alpha/sources",
            json={"kind": "push", "name": "chat"},
            headers=admin,
        ),
        409,
        "conflict",
    )
    refused(
        await client.post(
            "/v1/workspaces/alpha/sources",
            json={"kind": "push", "name": "inbox", "location": "/srv/inbox"},
            headers=admin,
        ),
        422,
        "validation_error",
    )


# --- Who may send --------------------------------------------------------------------------------


async def test_a_batch_needs_the_ingest_scope_and_stays_in_the_tokens_workspace(
    client: httpx2.AsyncClient, db: Conn, owner: uuid.UUID, admin: dict[str, str]
) -> None:
    workspaces = await setup(client, admin)
    batch = day_batch(thread("general/0001"))

    error = refused(
        await client.post(RECORDS, json=batch, headers=admin), 403, "insufficient_scope"
    )
    assert "ingest" in error["message"]
    reader = await bearer(db, owner, "read")
    refused(await client.post(RECORDS, json=batch, headers=reader), 403, "insufficient_scope")

    limited = await bearer(db, owner, "ingest", workspace_id=uuid.UUID(workspaces["beta"]["id"]))
    refused(await client.post(RECORDS, json=batch, headers=limited), 404, "not_found")
    assert await scalar(db, "SELECT count(*) FROM record") == 0
    assert await scalar(db, "SELECT count(*) FROM stream WHERE key = 'chat/general'") == 0
    ok(await client.post("/v1/workspaces/beta/sources/chat/records", json=batch, headers=limited))

    pusher = await bearer(db, owner, "ingest")
    refused(
        await client.post(
            "/v1/workspaces/alpha/sources/nowhere/records", json=batch, headers=pusher
        ),
        404,
        "not_found",
    )
    error = refused(
        await client.post("/v1/workspaces/alpha/sources/repo/records", json=batch, headers=pusher),
        409,
        "not_ingestible",
    )
    assert "repository" in error["message"]


# --- What a batch may hold -----------------------------------------------------------------------


async def test_a_batch_that_is_not_valid_is_refused_without_its_values(
    client: httpx2.AsyncClient, db: Conn, admin: dict[str, str], pusher: dict[str, str]
) -> None:
    await setup(client, admin)
    private = "private-text-that-must-not-echo-7f3a"

    no_author = thread("general/0001")
    no_author["payload"]["messages"][0] = {"sent_at": iso(T0), "text": private}
    error = refused(
        await client.post(RECORDS, json=day_batch(no_author), headers=pusher), 422, "invalid_batch"
    )
    assert "records[0].payload.messages[0].author" in error["message"]
    assert private not in json.dumps(error)

    naive = thread("general/0001")
    naive["occurred_at"] = "2026-03-01T09:00:00"
    error = refused(
        await client.post(RECORDS, json=day_batch(naive), headers=pusher), 422, "validation_error"
    )
    assert {"location": ["body", "records", 0, "occurred_at"], "type": "timezone_aware"} in error[
        "details"
    ]

    code = {"kind": "code_file", "external_id": "main:src/app.py", "deleted": True}
    refused(
        await client.post(RECORDS, json=day_batch(code), headers=pusher), 422, "validation_error"
    )
    unknown = {**thread("general/0001"), "author": private}
    error = refused(
        await client.post(RECORDS, json=day_batch(unknown), headers=pusher), 422, "validation_error"
    )
    assert private not in json.dumps(error)

    twice = day_batch(thread("general/0001"), thread("general/0001"))
    error = refused(await client.post(RECORDS, json=twice, headers=pusher), 422, "invalid_batch")
    assert "records[0]" in error["message"]

    dup = (
        b'{"stream": "chat/general", "records": [{"kind": "thread", "external_id": "x",'
        b' "external_id": "y", "deleted": true}]}'
    )
    refused(
        await client.post(
            RECORDS, content=dup, headers={**pusher, "Content-Type": "application/json"}
        ),
        422,
        "duplicate_key",
    )
    refused(
        await client.post(
            RECORDS, content=b"{not json", headers={**pusher, "Content-Type": "application/json"}
        ),
        422,
        "validation_error",
    )
    assert await scalar(db, "SELECT count(*) FROM record") == 0


async def test_the_limits_of_a_batch_answer_413(
    client: httpx2.AsyncClient, db: Conn, admin: dict[str, str], pusher: dict[str, str]
) -> None:
    await setup(client, admin)

    big = day_batch(thread("general/0001"))
    big["records"][0]["payload"]["padding"] = "x" * (64 * 1024)
    error = refused(await client.post(RECORDS, json=big, headers=pusher), 413, "content_too_large")
    assert error["request_id"]

    async def chunks() -> AsyncIterator[bytes]:  # no Content-Length: counted as it arrives
        body = json.dumps(big).encode()
        for start in range(0, len(body), 4096):
            yield body[start : start + 4096]

    refused(
        await client.post(
            RECORDS, content=chunks(), headers={**pusher, "Content-Type": "application/json"}
        ),
        413,
        "content_too_large",
    )

    many = day_batch(*(thread(f"general/{n:04}") for n in range(6)))
    error = refused(await client.post(RECORDS, json=many, headers=pusher), 413, "too_many_records")
    assert "at most 5 records" in error["message"]

    heavy = day_batch(thread("general/0001"))
    heavy["records"][0]["payload"]["padding"] = "x" * 4096
    error = refused(
        await client.post(RECORDS, json=heavy, headers=pusher), 413, "payload_too_large"
    )
    assert "records[0].payload" in error["message"]
    assert await scalar(db, "SELECT count(*) FROM record") == 0


async def test_a_window_with_a_gap_answers_409(
    client: httpx2.AsyncClient, db: Conn, admin: dict[str, str], pusher: dict[str, str]
) -> None:
    await setup(client, admin)
    ok(await client.post(RECORDS, json=day_batch(), headers=pusher))
    gap = {
        "stream": "chat/general",
        "window": {"from": iso(T0 + 2 * DAY), "to": iso(T0 + 3 * DAY), "complete": True},
        "records": [thread("general/0003", 50)],
    }
    error = refused(await client.post(RECORDS, json=gap, headers=pusher), 409, "window_gap")
    assert "2026-03-02T00:00:00+00:00" in error["message"]
    assert await scalar(db, "SELECT count(*) FROM record") == 0
