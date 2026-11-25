"""Records marked sensitive (memini.sensitivity) against a real Postgres: marked at push or by an
admin, kept and indexed, left out of search, threads, decisions and the export unless a reader
asks with the ``sensitive`` scope, and never queued for a model. Scenario S19.

Every value is synthetic (tests/ingest_helpers.py).
"""

import json
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, LiteralString

import httpx2
import pytest
from fastapi import FastAPI
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp_types import CallToolResult, TextContent
from psycopg import AsyncConnection
from psycopg.rows import TupleRow
from pydantic import SecretStr

from ingest_helpers import T0, thread
from memini.api import create_app
from memini.auth import Scope, WriteAccess, create_token
from memini.cli.export import export_workspace
from memini.config import Settings
from memini.db import Pool, connect, open_pool
from memini.embeddings import FakeEmbedder
from memini.ids import record_id
from memini.ingest import Batch, IngestResult, PayloadIndexer, RecordIn, ingest
from memini.jobs.extraction import EXTRACT_KIND
from memini.knowledge import EvidenceRef, NewItem, record_item
from memini.mcp import MCP_PATH
from memini.removal import ForgetTarget
from memini.runs import wait_for_run_writes
from memini.sensitivity import list_sensitive, mark_record
from memini.sources import NewPushSource, create_push_source
from memini.workspaces import create_workspace, ensure_principal

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

type Conn = AsyncConnection[TupleRow]
type Json = dict[str, Any]

BASE_URL = "http://memini.test"
SALARY = "The final settlement is zq4410 crowns, paid on the last day."
"""The text of the sensitive thread; ``zq4410`` is in nothing else."""
NEEDLE = "zq4410"
PLAIN = "The greenhouse opens at seven, zq4410 is not in this one."


@dataclass(frozen=True)
class Env:
    url: str
    db: Conn
    pool: Pool
    owner: uuid.UUID
    access: WriteAccess
    chat: uuid.UUID
    embedder: FakeEmbedder

    def app(self) -> FastAPI:
        return create_app(
            Settings(database_url=SecretStr(self.url)), pool=self.pool, embedder=self.embedder
        )

    async def token(self, *scopes: Scope) -> str:
        new = await create_token(self.db, owner_id=self.owner, name="test agent", scopes=scopes)
        return new.secret.get_secret_value()

    def rid(self, external_id: str) -> str:
        return record_id(self.chat, "thread", external_id)


@pytest.fixture
async def env(migrated_database_url: str) -> AsyncIterator[Env]:
    async with (
        connect(migrated_database_url, autocommit=True) as db,
        open_pool(migrated_database_url, min_size=1, max_size=6, timeout=5) as pool,
    ):
        owner = await ensure_principal(db, "owner")
        await create_workspace(db, owner_id=owner, name="acme")
        access = WriteAccess.for_owner(owner)
        chat = await create_push_source(db, access, "acme", NewPushSource(name="chat"))
        yield Env(migrated_database_url, db, pool, owner, access, chat.id, FakeEmbedder())


async def send(env: Env, *records: RecordIn, extract: bool = False) -> IngestResult:
    result = await ingest(
        env.db,
        env.access,
        "acme",
        "chat",
        Batch(records=records),
        runs=env.pool,
        indexer=PayloadIndexer(env.embedder),
        extract=extract,
    )
    await wait_for_run_writes()
    return result


def sensitive_thread(external_id: str, text: str = SALARY) -> RecordIn:
    plain = thread(external_id, text=text)
    return RecordIn(
        kind=plain.kind,
        external_id=plain.external_id,
        occurred_at=plain.occurred_at,
        payload=plain.payload,
        external_url=plain.external_url,
        sensitive=True,
    )


async def rows(db: Conn, query: LiteralString, *params: Any) -> list[tuple[Any, ...]]:
    cur = await db.execute(query, params)
    return [tuple(row) for row in await cur.fetchall()]


@asynccontextmanager
async def http_client(app: FastAPI, token: str) -> AsyncGenerator[httpx2.AsyncClient]:
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {token}"},
        ) as client,
    ):
        yield client


@asynccontextmanager
async def mcp_client(app: FastAPI, token: str) -> AsyncGenerator[Client]:
    async with http_client(app, token) as http:
        transport = streamable_http_client(f"{BASE_URL}{MCP_PATH}", http_client=http)
        async with Client(transport, mode="legacy") as client:
            yield client


def tool_text(result: CallToolResult) -> str:
    (content,) = result.content
    assert isinstance(content, TextContent)
    return content.text


def hit_ids(body: Mapping[str, Any]) -> set[str]:
    return {str(hit["record_id"]) for hit in body["items"]}


# --- Marking -------------------------------------------------------------------------------------


async def test_a_push_marks_a_record_and_never_clears_the_mark(env: Env) -> None:
    await send(env, sensitive_thread("pay/1"), thread("garden/1", text=PLAIN))

    marked = await rows(
        env.db, "SELECT id, sensitive, sensitive_at IS NOT NULL FROM record ORDER BY external_id"
    )
    assert marked == [(env.rid("garden/1"), False, False), (env.rid("pay/1"), True, True)]

    # The same thread sent again without the mark keeps it; the content and index are kept too.
    again = await send(env, thread("pay/1", text=SALARY))
    assert again.unchanged == 1
    assert await rows(env.db, "SELECT sensitive FROM record WHERE id = %s", env.rid("pay/1")) == [
        (True,)
    ]
    chunks = await rows(env.db, "SELECT count(*) FROM chunk WHERE record_id = %s", env.rid("pay/1"))
    assert chunks[0][0] > 0


async def test_an_admin_marks_and_clears_with_a_reason_and_lists_without_content(env: Env) -> None:
    await send(env, thread("pay/1", text=SALARY), thread("garden/1", text=PLAIN))

    marked = await mark_record(
        env.db,
        env.access,
        "acme",
        ForgetTarget(source="chat", external_id="pay/1"),
        sensitive=True,
        reason="  Salary figures  ",
    )
    assert (marked.record_id, marked.sensitive, marked.reason, marked.changed) == (
        env.rid("pay/1"),
        True,
        "Salary figures",
        True,
    )
    same = await mark_record(
        env.db,
        env.access,
        "acme",
        ForgetTarget(record_id=env.rid("pay/1")),
        sensitive=True,
        reason="Salary figures",
    )
    assert not same.changed

    _, listed = await list_sensitive(env.db, env.access, "acme")
    assert [(each.record_id, each.external_id, each.reason) for each in listed] == [
        (env.rid("pay/1"), "pay/1", "Salary figures")
    ]

    cleared = await mark_record(
        env.db, env.access, "acme", ForgetTarget(record_id=env.rid("pay/1")), sensitive=False
    )
    assert (cleared.sensitive, cleared.reason, cleared.sensitive_at) == (False, None, None)
    assert (await list_sensitive(env.db, env.access, "acme"))[1] == []


async def test_the_routes_need_admin_and_answer_as_the_cli_reads_them(env: Env) -> None:
    await send(env, thread("pay/1", text=SALARY))
    admin = await env.token("admin")
    reader = await env.token("read")
    body = {"source": "chat", "external_id": "pay/1", "sensitive": True, "reason": "salary"}
    async with http_client(env.app(), reader) as client:
        denied = await client.put("/v1/workspaces/acme/records/sensitive", json=body)
    async with http_client(env.app(), admin) as client:
        done = await client.put("/v1/workspaces/acme/records/sensitive", json=body)
        missing = await client.put(
            "/v1/workspaces/acme/records/sensitive",
            json={"source": "chat", "external_id": "nothing", "sensitive": True},
        )
        listed = await client.get("/v1/workspaces/acme/records/sensitive")

    assert (denied.status_code, denied.json()["error"]["code"]) == (403, "insufficient_scope")
    assert (missing.status_code, missing.json()["error"]["code"]) == (404, "not_found")
    assert done.status_code == 200, done.text
    assert {key: done.json()[key] for key in ("record_id", "sensitive", "reason", "changed")} == {
        "record_id": env.rid("pay/1"),
        "sensitive": True,
        "reason": "salary",
        "changed": True,
    }
    assert listed.status_code == 200, listed.text
    assert [each["external_id"] for each in listed.json()["records"]] == ["pay/1"]
    assert NEEDLE not in listed.text


# --- Reading -------------------------------------------------------------------------------------


async def test_search_leaves_a_sensitive_record_out_unless_asked_with_the_scope(env: Env) -> None:
    await send(env, sensitive_thread("pay/1"), thread("garden/1", text=PLAIN))
    plain = await env.token("read")
    trusted = await env.token("read", "sensitive")
    query = {"query": NEEDLE, "mode": "lexical"}

    async with http_client(env.app(), plain) as client:
        default = await client.get("/v1/search", params=query)
        refused = await client.get("/v1/search", params=query | {"include_sensitive": "true"})
    async with http_client(env.app(), trusted) as client:
        scoped = await client.get("/v1/search", params=query)
        asked = await client.get("/v1/search", params=query | {"include_sensitive": "true"})

    assert default.status_code == 200, default.text
    assert hit_ids(default.json()) == {env.rid("garden/1")}
    assert "crowns" not in default.text
    assert (refused.status_code, refused.json()["error"]["code"]) == (403, "insufficient_scope")
    # The scope alone asks for nothing.
    assert hit_ids(scoped.json()) == {env.rid("garden/1")}
    assert hit_ids(asked.json()) == {env.rid("garden/1"), env.rid("pay/1")}


async def test_the_search_tool_takes_include_sensitive(env: Env) -> None:
    await send(env, sensitive_thread("pay/1"), thread("garden/1", text=PLAIN))
    arguments = {"query": NEEDLE, "mode": "lexical"}

    async with mcp_client(env.app(), await env.token("read")) as client:
        default = await client.call_tool("search", arguments)
        refused = await client.call_tool("search", arguments | {"include_sensitive": True})
    async with mcp_client(env.app(), await env.token("read", "sensitive")) as client:
        asked = await client.call_tool("search", arguments | {"include_sensitive": True})

    assert default.is_error is False
    assert hit_ids(default.structured_content) == {env.rid("garden/1")}
    assert refused.is_error is True
    assert "sensitive scope" in tool_text(refused)
    assert hit_ids(asked.structured_content) == {env.rid("garden/1"), env.rid("pay/1")}


async def test_a_sensitive_thread_is_named_as_such_and_read_only_when_asked(env: Env) -> None:
    await send(env, sensitive_thread("pay/1"))
    path = f"/v1/threads/{env.rid('pay/1')}"

    async with http_client(env.app(), await env.token("read")) as client:
        hidden = await client.get(path)
        gone = await client.get(f"/v1/threads/{'0' * 64}")
    async with http_client(env.app(), await env.token("read", "sensitive")) as client:
        read = await client.get(path, params={"include_sensitive": "true"})

    assert (hidden.status_code, hidden.json()["error"]["code"]) == (404, "sensitive_record")
    assert NEEDLE not in hidden.text
    assert (gone.status_code, gone.json()["error"]["code"]) == (404, "not_found")
    assert read.status_code == 200, read.text
    assert NEEDLE in read.text


async def test_a_decision_only_a_sensitive_record_supports_is_left_out(env: Env) -> None:
    await send(env, sensitive_thread("pay/1"), thread("garden/1", text=PLAIN))

    async def cite(statement: str, *external_ids: str) -> uuid.UUID:
        item = await record_item(
            env.db,
            env.access,
            "acme",
            NewItem(
                kind="decision",
                statement=statement,
                decided_at=T0,
                evidence=[
                    EvidenceRef(record_id=env.rid(each), anchor="msg-1") for each in external_ids
                ],
            ),
        )
        return item.id

    only = await cite("The settlement is paid on the last day.", "pay/1")
    both = await cite("The greenhouse opens at seven.", "pay/1", "garden/1")

    async with http_client(env.app(), await env.token("read")) as client:
        listed = await client.get("/v1/decisions")
        one = await client.get(f"/v1/decisions/{only}")
    async with http_client(env.app(), await env.token("read", "sensitive")) as client:
        asked = await client.get("/v1/decisions", params={"include_sensitive": "true"})

    assert listed.status_code == 200, listed.text
    items = {uuid.UUID(item["id"]): item for item in listed.json()["items"]}
    assert set(items) == {both}
    states = {each["external_id"]: each["record_state"] for each in items[both]["evidence"]}
    assert states == {"pay/1": "sensitive", "garden/1": "stored"}
    assert one.status_code == 404
    assert {uuid.UUID(item["id"]) for item in asked.json()["items"]} == {only, both}


async def test_no_model_step_is_queued_for_a_sensitive_record(env: Env) -> None:
    await send(env, sensitive_thread("pay/1"), thread("garden/1", text=PLAIN), extract=True)

    queued = await rows(
        env.db, "SELECT dedupe_key FROM job WHERE kind = %s ORDER BY dedupe_key", EXTRACT_KIND
    )
    assert len(queued) == 1
    assert env.rid("garden/1") in queued[0][0]
    assert env.rid("pay/1") not in queued[0][0]


async def test_the_export_leaves_sensitive_records_out_unless_asked(env: Env) -> None:
    await send(env, sensitive_thread("pay/1"), thread("garden/1", text=PLAIN))

    async def exported(*, include_sensitive: bool = False) -> list[Json]:
        # The push source is private, as a new source is; its records need include_private.
        lines = [
            json.loads(line)
            async for line in export_workspace(
                env.db, "owner", "acme", include_private=True, include_sensitive=include_sensitive
            )
        ]
        return [line for line in lines if line["type"] == "record"]

    default = await exported()
    assert [line["external_id"] for line in default] == ["garden/1"]
    assert "sensitive" not in default[0]
    included = await exported(include_sensitive=True)
    marks = {line["external_id"]: line.get("sensitive", False) for line in included}
    assert marks == {"garden/1": False, "pay/1": True}
