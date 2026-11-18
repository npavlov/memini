"""Source filters and prune through the HTTP API, against a real Postgres: filters set on a push
source when it is added and changed, refused when not valid or on a git source, applied to the
batches sent, and prune of what was stored before. The library's semantics are tested in
tests/test_prune_db.py. Every value is synthetic: names are made up, addresses are under
example.test."""

import uuid
from collections.abc import AsyncIterator
from typing import Any, LiteralString

import httpx2
import pytest
from psycopg import AsyncConnection
from psycopg.rows import TupleRow
from pydantic import SecretStr

from ingest_helpers import HOUR, T0, iso
from memini.api import create_app
from memini.auth import Scope, create_token
from memini.config import Settings
from memini.db import Pool, connect, open_pool
from memini.embeddings import FakeEmbedder
from memini.workspaces import ensure_principal

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

type Conn = AsyncConnection[TupleRow]
type Json = dict[str, Any]

SOURCES = "/v1/workspaces/alpha/sources"
BOT = "Deploy Bot <deploy-bot@example.test>"
ANN = "Ann Example <ann@example.test>"
BOT_TEXT = "Deployed build 4711 to staging."


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
async def client(migrated_database_url: str, pool: Pool) -> AsyncIterator[httpx2.AsyncClient]:
    settings = Settings(database_url=SecretStr(migrated_database_url))
    app = create_app(settings, pool=pool, embedder=FakeEmbedder())
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://memini.test") as client:
        yield client


async def bearer(db: Conn, owner: uuid.UUID, *scopes: Scope) -> dict[str, str]:
    new = await create_token(db, owner_id=owner, name="test client", scopes=scopes)
    return {"Authorization": f"Bearer {new.secret.get_secret_value()}"}


@pytest.fixture
async def admin(db: Conn, owner: uuid.UUID) -> dict[str, str]:
    headers = await bearer(db, owner, "read", "admin", "ingest")
    return headers


def ok(response: httpx2.Response, status_code: int = 200) -> Json:
    assert response.status_code == status_code, response.text
    body: Json = response.json()
    return body


def refused(response: httpx2.Response, status_code: int, code: str) -> Json:
    assert response.status_code == status_code, response.text
    error: Json = response.json()["error"]
    assert error["code"] == code, error
    return error


async def rows(db: Conn, query: LiteralString, *params: Any) -> list[tuple[Any, ...]]:
    cur = await db.execute(query, params)
    return [tuple(row) for row in await cur.fetchall()]


async def setup(client: httpx2.AsyncClient, admin: dict[str, str], **filters: Any) -> Json:
    ok(await client.post("/v1/workspaces", json={"name": "alpha"}, headers=admin), 201)
    ok(
        await client.post(
            SOURCES,
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
    body: Json = {"kind": "push", "name": "chat"}
    if filters:
        body["filters"] = filters
    return ok(await client.post(SOURCES, json=body, headers=admin), 201)


def thread(external_id: str, *authors: str) -> Json:
    return {
        "kind": "thread",
        "external_id": external_id,
        "occurred_at": iso(T0 + HOUR),
        "payload": {
            "channel": "release",
            "messages": [
                {
                    "author": author,
                    "sent_at": iso(T0 + minute * HOUR / 60),
                    "text": BOT_TEXT if "Bot" in author else f"Message {minute} of the thread.",
                }
                for minute, author in enumerate(authors, start=1)
            ],
        },
    }


async def send(client: httpx2.AsyncClient, admin: dict[str, str], *records: Json) -> Json:
    return ok(
        await client.post(f"{SOURCES}/chat/records", json={"records": list(records)}, headers=admin)
    )


async def test_filters_are_set_shown_and_changed_with_a_push_source(
    client: httpx2.AsyncClient, admin: dict[str, str]
) -> None:
    created = await setup(
        client,
        admin,
        allow_domains=["Example.TEST", "example.test"],
        exclude_authors=["*bot*"],
    )

    assert created["filters"] == {
        "allow_channels": [],
        "allow_domains": ["example.test"],
        "exclude_authors": ["*bot*"],
        "exclude_titles": [],
        "exclude_automated": False,
    }
    assert ok(await client.get(f"{SOURCES}/repo", headers=admin))["filters"] is None
    changed = ok(
        await client.patch(
            f"{SOURCES}/chat",
            json={"filters": {"exclude_titles": ["Daily*"], "exclude_automated": True}},
            headers=admin,
        )
    )
    assert changed["filters"]["exclude_titles"] == ["Daily*"]
    assert changed["filters"]["exclude_authors"] == []  # every rule at once
    assert changed["filters"]["exclude_automated"] is True
    cleared = ok(await client.patch(f"{SOURCES}/chat", json={"filters": {}}, headers=admin))
    assert cleared["filters"]["exclude_automated"] is False
    listed = ok(await client.get(SOURCES, headers=admin))["sources"]
    assert {source["name"]: source["filters"] is None for source in listed} == {
        "chat": False,
        "repo": True,
    }


@pytest.mark.parametrize(
    ("filters", "status", "code"),
    [
        ({"allow_domains": ["not a domain"]}, 422, "invalid_source"),
        ({"exclude_authors": ["[z-a]"]}, 422, "invalid_source"),
        ({"exclude_authors": [""]}, 422, "validation_error"),
        ({"exclude_authors": "*bot*"}, 422, "validation_error"),
        ({"exclude_bots": True}, 422, "validation_error"),
        ({"exclude_automated": "yes"}, 422, "validation_error"),
        ({"allow_channels": [f"c-{n}" for n in range(101)]}, 422, "validation_error"),
    ],
)
async def test_filters_that_are_not_valid_are_refused(
    client: httpx2.AsyncClient,
    admin: dict[str, str],
    filters: Json,
    status: int,
    code: str,
) -> None:
    await setup(client, admin)

    error = refused(
        await client.patch(f"{SOURCES}/chat", json={"filters": filters}, headers=admin),
        status,
        code,
    )
    if code == "invalid_source":
        assert "filters." in error["message"]
        assert "not a domain" not in error["message"]
    refused(
        await client.post(
            SOURCES, json={"kind": "push", "name": "other", "filters": filters}, headers=admin
        ),
        status,
        code,
    )


async def test_filters_are_refused_on_a_git_source_and_null_is_refused(
    client: httpx2.AsyncClient, admin: dict[str, str]
) -> None:
    await setup(client, admin)

    refused(
        await client.patch(
            f"{SOURCES}/repo", json={"filters": {"exclude_authors": ["*bot*"]}}, headers=admin
        ),
        422,
        "invalid_source",
    )
    refused(
        await client.patch(f"{SOURCES}/chat", json={"filters": None}, headers=admin),
        422,
        "validation_error",
    )


async def test_prune_applies_a_new_rule_to_the_stored_records(
    client: httpx2.AsyncClient, admin: dict[str, str], db: Conn, owner: uuid.UUID
) -> None:
    await setup(client, admin)
    await send(
        client,
        admin,
        thread("t-mixed", ANN, BOT, ANN),
        thread("t-bot", BOT),
        thread("t-human", ANN),
    )
    ok(
        await client.patch(
            f"{SOURCES}/chat", json={"filters": {"exclude_authors": ["*bot*"]}}, headers=admin
        )
    )

    pruned = ok(await client.post(f"{SOURCES}/chat/prune", headers=admin))

    assert pruned["rules"] is True
    assert pruned["counts"] == {
        "scanned": 3,
        "deleted": 1,
        "trimmed": 1,
        "messages_removed": 2,
        "unchanged": 1,
        "unreadable": 0,
    }
    assert pruned["chunks_deleted"] > 0
    assert pruned["records_not_indexed"] == 0
    stored = await rows(db, "SELECT external_id FROM record ORDER BY external_id")
    assert stored == [("t-human",), ("t-mixed",)]
    bot_chunks = await rows(db, "SELECT count(*) FROM chunk WHERE content LIKE %s", f"%{BOT_TEXT}%")
    assert bot_chunks == [(0,)]
    run = ok(await client.get(f"/v1/runs/{pruned['run_id']}", headers=admin))
    assert (run["kind"], run["status"]) == ("prune", "succeeded")
    assert run["report"]["counts"]["records_pruned"] == 1

    again = ok(await client.post(f"{SOURCES}/chat/prune", headers=admin))

    assert (again["counts"]["deleted"], again["counts"]["trimmed"]) == (0, 0)
    assert again["counts"]["unchanged"] == 2

    # A batch sent under the rule stores the bot's messages nowhere.
    sent = await send(client, admin, thread("t-new", BOT, ANN))
    assert sent["counts"]["inserted"] == 1
    new_run = ok(await client.get(f"/v1/runs/{sent['run_id']}", headers=admin))
    assert new_run["report"]["counts"]["messages_filtered"] == 1
    bot_chunks = await rows(db, "SELECT count(*) FROM chunk WHERE content LIKE %s", f"%{BOT_TEXT}%")
    assert bot_chunks == [(0,)]

    reader = await bearer(db, owner, "read")
    refused(await client.post(f"{SOURCES}/chat/prune", headers=reader), 403, "insufficient_scope")
    refused(await client.post(f"{SOURCES}/repo/prune", headers=admin), 409, "not_prunable")
    refused(await client.post(f"{SOURCES}/none/prune", headers=admin), 404, "not_found")
