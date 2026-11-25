"""The person routes against a real Postgres: scopes, the workspace limit of a token, the error
shape, and the acceptance flow of memini-yq0 over HTTP. The library is tested in
tests/test_persons_db.py. Every name, address and id here is made up."""

import uuid
from collections.abc import AsyncIterator
from typing import Any

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
from memini.workspaces import ensure_principal

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

type Conn = AsyncConnection[TupleRow]
type Json = dict[str, Any]

PERSONS = "/v1/workspaces/alpha/persons"


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


def ok(response: httpx2.Response, status_code: int = 200) -> Json:
    assert response.status_code == status_code, response.text
    body: Json = response.json()
    return body


def refused(response: httpx2.Response, status_code: int, code: str) -> Json:
    assert response.status_code == status_code, response.text
    error: Json = response.json()["error"]
    assert error["code"] == code, error
    return error


def message(external_id: str, author: str, hours: int, people: list[Json] | None = None) -> Json:
    at = T0 + hours * HOUR
    payload: Json = {
        "messages": [{"id": "m1", "author": author, "occurred_at": iso(at), "text": "Ready."}]
    }
    if people is not None:
        payload["people"] = people
    return {
        "kind": "thread",
        "external_id": external_id,
        "occurred_at": iso(at),
        "payload": payload,
    }


@pytest.fixture
async def world(db: Conn, owner: uuid.UUID, client: httpx2.AsyncClient) -> dict[str, Any]:
    """Workspaces alpha and beta with a push source each; in alpha, Ann by mail and by chat."""
    admin = await bearer(db, owner, "read", "admin")
    found: dict[str, Any] = {"admin": admin}
    for name in ("alpha", "beta"):
        workspace = ok(await client.post("/v1/workspaces", json={"name": name}, headers=admin), 201)
        found[name] = uuid.UUID(workspace["id"])
        ok(
            await client.post(
                f"/v1/workspaces/{name}/sources",
                json={"kind": "push", "name": "inbox"},
                headers=admin,
            ),
            201,
        )
    pusher = await bearer(db, owner, "ingest")
    batch = {
        "records": [
            message("mail-1", "Ann Example <ann@example.test>", 9),
            message(
                "chat-1", "Ann Example", 10, [{"name": "Ann Example", "chat_user_id": "U-ANN"}]
            ),
        ]
    }
    ok(await client.post("/v1/workspaces/alpha/sources/inbox/records", json=batch, headers=pusher))
    ok(await client.post("/v1/workspaces/beta/sources/inbox/records", json=batch, headers=pusher))
    found["reader"] = await bearer(db, owner, "read")
    found["beta_admin"] = await bearer(db, owner, "read", "admin", workspace_id=found["beta"])
    return found


async def lookup(
    client: httpx2.AsyncClient, headers: dict[str, str], kind: str, value: str
) -> Json:
    return ok(
        await client.get(
            f"{PERSONS}/lookup", params={"kind": kind, "value": value}, headers=headers
        )
    )


async def test_mail_and_chat_become_one_person_and_split_again(
    client: httpx2.AsyncClient, world: dict[str, Any]
) -> None:
    admin, reader = world["admin"], world["reader"]
    listed = ok(await client.get(PERSONS, headers=reader))
    assert [p["display_name"] for p in listed["persons"]] == ["Ann Example", "Ann Example"]
    by_mail = await lookup(client, reader, "email", "ANN@example.test")
    by_chat = await lookup(client, reader, "chat_user_id", "U-ANN")
    assert by_mail["id"] != by_chat["id"]

    added = ok(
        await client.post(
            f"{PERSONS}/{by_mail['id']}/aliases",
            json={"kind": "chat_user_id", "value": "U-ANN"},
            headers=admin,
        )
    )

    assert added["created"] is False
    assert added["merged"] == by_chat["id"]
    assert added["person"]["members"] == [by_chat["id"]]
    assert {(a["kind"], a["normalized_value"]) for a in added["person"]["aliases"]} == {
        ("email", "ann@example.test"),
        ("chat_user_id", "U-ANN"),
    }
    assert (await lookup(client, reader, "chat_user_id", "U-ANN"))["id"] == by_mail["id"]
    listed = ok(await client.get(PERSONS, headers=reader))
    assert [p["id"] for p in listed["persons"]] == [by_mail["id"]]
    merged = ok(await client.get(f"{PERSONS}/{by_chat['id']}", headers=reader))
    assert (merged["canonical_id"], merged["merged_into"]) == (by_mail["id"], by_mail["id"])

    split = ok(await client.post(f"{PERSONS}/{by_chat['id']}/split", headers=admin))
    assert (split["canonical_id"], split["merged_into"], split["merged_at"]) == (
        by_chat["id"],
        None,
        None,
    )
    assert (await lookup(client, reader, "chat_user_id", "U-ANN"))["id"] == by_chat["id"]
    refused(await client.post(f"{PERSONS}/{by_chat['id']}/split", headers=admin), 409, "not_merged")

    merged = ok(
        await client.post(
            f"{PERSONS}/{by_chat['id']}/merge", json={"into": by_mail["id"]}, headers=admin
        )
    )
    assert merged["id"] == by_mail["id"]
    refused(
        await client.post(
            f"{PERSONS}/{by_mail['id']}/merge", json={"into": by_chat["id"]}, headers=admin
        ),
        409,
        "same_person",
    )
    refused(
        await client.post(
            f"{PERSONS}/{by_chat['id']}/merge", json={"into": by_mail["id"]}, headers=admin
        ),
        409,
        "already_merged",
    )


async def test_me_is_set_read_and_unset(client: httpx2.AsyncClient, world: dict[str, Any]) -> None:
    admin, reader = world["admin"], world["reader"]
    refused(await client.get(f"{PERSONS}/me", headers=reader), 404, "no_me")
    ann = await lookup(client, reader, "email", "ann@example.test")

    marked = ok(await client.put(f"{PERSONS}/me", json={"person_id": ann["id"]}, headers=admin))
    assert marked["is_me"] is True
    me = ok(await client.get(f"{PERSONS}/me", headers=reader))
    assert me["id"] == ann["id"]
    refused(
        await client.put(f"{PERSONS}/me", json={"person_id": str(uuid.uuid4())}, headers=admin),
        404,
        "not_found",
    )

    response = await client.delete(f"{PERSONS}/me", headers=admin)
    assert response.status_code == 204
    refused(await client.get(f"{PERSONS}/me", headers=reader), 404, "no_me")


async def test_persons_are_made_renamed_and_their_aliases_removed(
    client: httpx2.AsyncClient, world: dict[str, Any]
) -> None:
    admin = world["admin"]
    made = ok(
        await client.post(
            PERSONS,
            json={
                "display_name": "Bo  Sample",
                "aliases": [{"kind": "email", "value": "Bo@Example.test"}],
            },
            headers=admin,
        ),
        201,
    )
    assert made["display_name"] == "Bo Sample"
    assert [(a["kind"], a["value"], a["normalized_value"]) for a in made["aliases"]] == [
        ("email", "Bo@Example.test", "bo@example.test")
    ]
    held = refused(
        await client.post(
            PERSONS,
            json={"display_name": "Bo", "aliases": [{"kind": "email", "value": "bo@example.test"}]},
            headers=admin,
        ),
        409,
        "alias_held",
    )
    assert made["id"] in held["message"]
    invalid = refused(
        await client.post(
            f"{PERSONS}/{made['id']}/aliases",
            json={"kind": "email", "value": "secret-looking value"},
            headers=admin,
        ),
        422,
        "invalid_alias",
    )
    assert "secret-looking" not in invalid["message"]

    renamed = ok(
        await client.patch(
            f"{PERSONS}/{made['id']}", json={"display_name": "B. Sample"}, headers=admin
        )
    )
    assert renamed["display_name"] == "B. Sample"
    alias_id = made["aliases"][0]["id"]
    left = ok(await client.delete(f"{PERSONS}/{made['id']}/aliases/{alias_id}", headers=admin))
    assert left["aliases"] == []
    refused(
        await client.delete(f"{PERSONS}/{made['id']}/aliases/{alias_id}", headers=admin),
        404,
        "not_found",
    )


async def test_a_listing_pages_and_searches(
    client: httpx2.AsyncClient, world: dict[str, Any]
) -> None:
    reader = world["reader"]
    first = ok(await client.get(PERSONS, params={"limit": 1}, headers=reader))
    assert len(first["persons"]) == 1
    second = ok(
        await client.get(PERSONS, params={"limit": 1, "after": first["next_after"]}, headers=reader)
    )
    assert len(second["persons"]) == 1
    assert second["next_after"] is None
    assert first["persons"][0]["id"] != second["persons"][0]["id"]
    found = ok(await client.get(PERSONS, params={"q": "u-ann"}, headers=reader))
    assert [a["normalized_value"] for a in found["persons"][0]["aliases"]] == ["U-ANN"]
    refused(
        await client.get(PERSONS, params={"after": "not a cursor"}, headers=reader),
        422,
        "invalid_cursor",
    )
    refused(
        await client.get(
            f"{PERSONS}/lookup", params={"kind": "phone", "value": "1"}, headers=reader
        ),
        422,
        "validation_error",
    )
    refused(await client.get(f"{PERSONS}/{uuid.uuid4()}", headers=reader), 404, "not_found")


async def test_scopes_and_the_workspace_limit_hold(
    client: httpx2.AsyncClient, world: dict[str, Any]
) -> None:
    reader, beta_admin = world["reader"], world["beta_admin"]
    ann = await lookup(client, reader, "email", "ann@example.test")
    alias = {"kind": "display_name", "value": "Ann"}

    refused(
        await client.post(f"{PERSONS}/{ann['id']}/aliases", json=alias, headers=reader),
        403,
        "insufficient_scope",
    )
    refused(
        await client.put(f"{PERSONS}/me", json={"person_id": ann["id"]}, headers=reader),
        403,
        "insufficient_scope",
    )
    refused(await client.get(PERSONS), 401, "unauthorized")

    # A token limited to beta sees nothing of alpha, and cannot reach alpha's persons from beta.
    refused(await client.get(PERSONS, headers=beta_admin), 404, "not_found")
    refused(await client.get(f"{PERSONS}/{ann['id']}", headers=beta_admin), 404, "not_found")
    refused(
        await client.post(f"{PERSONS}/{ann['id']}/aliases", json=alias, headers=beta_admin),
        404,
        "not_found",
    )
    refused(
        await client.get(f"/v1/workspaces/beta/persons/{ann['id']}", headers=beta_admin),
        404,
        "not_found",
    )
    beta = ok(await client.get("/v1/workspaces/beta/persons", headers=beta_admin))
    assert len(beta["persons"]) == 2
    assert ann["id"] not in {person["id"] for person in beta["persons"]}
