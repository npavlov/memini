"""Threads, meetings and documents through the push API into the index and back out of search,
against a real Postgres. Acceptance of memini-8r2: fixtures of each kind ingest and are found by
search with the right kind, time and locator. Acceptance of memini-q1q: each hit carries its
chunk's anchor, link and time, over the library, HTTP and MCP, and its record_id and anchor are
taken as they are as evidence of a decision. Also: a batch sent again embeds nothing, an edited
message re-embeds its group only, a batch without an embedding provider is stored without chunks
and a rebuild indexes it, a stale index refuses the batch. Every value is synthetic; vectors come
from the FakeEmbedder, so the searches here are lexical and exact."""

import json
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, LiteralString

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp_types import TextContent
from psycopg import AsyncConnection
from psycopg.rows import TupleRow
from pydantic import SecretStr

from memini.api import create_app
from memini.auth import Principal, ReadFilter, Scope, authorize_read, create_token
from memini.chunking import Chunk, chunk_payload
from memini.config import Settings
from memini.db import Pool, connect, open_pool
from memini.embeddings import EmbeddingSpec, FakeEmbedder
from memini.ids import record_id
from memini.index import default_raw_content, reindex
from memini.ingest import canonical_json, normalize
from memini.mcp import MCP_PATH
from memini.search import Hit, Mode, SearchFilters, SearchQuery, search
from memini.workspaces import ensure_principal

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

type Conn = AsyncConnection[TupleRow]
type Json = dict[str, Any]

FIXTURES = Path(__file__).parent / "fixtures" / "chunking"
RECORDS = "/v1/workspaces/acme/sources/chat/records"
OTHER_MODEL = EmbeddingSpec(provider="openai_compatible", model="fake-embedder-2", dimension=12)


def fixture(name: str) -> Json:
    data: Json = json.loads((FIXTURES / f"{name}.json").read_text())
    return data


RU_THREAD: Json = {
    "channel": "теплица",
    "title": "Рассада",
    "messages": [
        {
            "id": "r-1",
            "author": "Аня Пример",
            "sent_at": "2026-03-04T06:00:00Z",
            "text": "Рассаду томатов пора пикировать.",
            "url": "https://chat.example.invalid/archives/teplica/r-1",
        },
        {
            "id": "r-2",
            "author": "Борис Образец",
            "sent_at": "2026-03-04T06:20:00Z",
            "text": "Пикирую в субботу, горшки уже купил.",
            "recipients": ["Аня Пример"],
        },
    ],
}

BATCH: list[Json] = [
    {
        "kind": "thread",
        "external_id": "greenhouse/1772355600.000100",
        "occurred_at": "2026-03-03T18:01:00Z",
        "external_url": "https://chat.example.invalid/archives/greenhouse/p1772355600000100",
        "payload": fixture("thread_greenhouse"),
    },
    {
        "kind": "thread",
        "external_id": "teplica/r-1",
        "occurred_at": "2026-03-04T06:20:00Z",
        "payload": RU_THREAD,
    },
    {
        "kind": "meeting",
        "external_id": "meeting-2026-03-03",
        "occurred_at": "2026-03-03T07:00:00Z",
        "payload": fixture("meeting_planning"),
    },
    {
        "kind": "document",
        "external_id": "handbook",
        "occurred_at": "2026-02-20T10:00:00Z",
        "external_url": "https://notes.example.invalid/handbook",
        "payload": fixture("document_handbook"),
    },
]


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
    return Settings(database_url=SecretStr(migrated_database_url))


@pytest.fixture
def fake() -> FakeEmbedder:
    return FakeEmbedder()


@asynccontextmanager
async def client_of(
    settings: Settings, pool: Pool, embedder: FakeEmbedder | None
) -> AsyncGenerator[httpx2.AsyncClient]:
    """A client of the application; ``embedder`` is the service's, None for no provider (the
    application's lifespan does not run, so it creates none of its own)."""
    transport = httpx2.ASGITransport(app=create_app(settings, pool=pool, embedder=embedder))
    async with httpx2.AsyncClient(transport=transport, base_url="http://memini.test") as client:
        yield client


async def bearer(db: Conn, owner: uuid.UUID, *scopes: Scope) -> dict[str, str]:
    new = await create_token(db, owner_id=owner, name="test client", scopes=scopes)
    return {"Authorization": f"Bearer {new.secret.get_secret_value()}"}


def ok(response: httpx2.Response, status_code: int = 200) -> Json:
    assert response.status_code == status_code, response.text
    body: Json = response.json()
    return body


async def rows(db: Conn, query: LiteralString, *params: Any) -> list[tuple[Any, ...]]:
    cur = await db.execute(query, params)
    return [tuple(row) for row in await cur.fetchall()]


async def scalar(db: Conn, query: LiteralString, *params: Any) -> Any:
    (row,) = await rows(db, query, *params)
    return row[0]


async def setup(client: httpx2.AsyncClient, admin: dict[str, str]) -> uuid.UUID:
    """Workspace acme with a push source chat; returns the source's id."""
    ok(await client.post("/v1/workspaces", json={"name": "acme"}, headers=admin), 201)
    created = ok(
        await client.post(
            "/v1/workspaces/acme/sources", json={"kind": "push", "name": "chat"}, headers=admin
        ),
        201,
    )
    return uuid.UUID(created["id"])


def expected_chunks(source: uuid.UUID, item: Json) -> tuple[str, list[Chunk]]:
    """The record id of a batch item and the chunks its payload makes."""
    rid = record_id(source, item["kind"], item["external_id"])
    payload = canonical_json(item["payload"])
    return rid, list(chunk_payload(rid, item["kind"], payload).chunks)


def reader(owner: uuid.UUID) -> ReadFilter:
    principal = Principal(
        principal_id=owner, token_id=uuid.uuid4(), scopes=frozenset({"read"}), workspace_id=None
    )
    return authorize_read(principal)


async def find(
    db: Conn, embedder: FakeEmbedder, owner: uuid.UUID, text: str, mode: Mode, **filters: Any
) -> list[Hit]:
    query = SearchQuery(text=text, mode=mode, filters=SearchFilters(**filters), limit=20)
    return await search(db, embedder, reader(owner), query)


async def chunk_places(db: Conn) -> dict[str, tuple[Any, ...]]:
    """Every chunk by id: record, kind, text hash, anchor, URL, time."""
    found = await rows(
        db, "SELECT id, record_id, kind, text_hash, anchor, url, occurred_at FROM chunk"
    )
    return {row[0]: row[1:] for row in found}


# --- Ingest, then search -------------------------------------------------------------------------


async def test_each_kind_is_indexed_at_ingest_and_found_with_its_kind_time_and_place(
    db: Conn, owner: uuid.UUID, pool: Pool, settings: Settings, fake: FakeEmbedder
) -> None:
    admin = await bearer(db, owner, "read", "admin")
    pusher = await bearer(db, owner, "ingest")
    async with client_of(settings, pool, fake) as client:
        source = await setup(client, admin)
        answer = ok(await client.post(RECORDS, json={"records": BATCH}, headers=pusher))
        run = ok(await client.get(f"/v1/runs/{answer['run_id']}", headers=admin))

    expected = dict(expected_chunks(source, item) for item in BATCH)
    total = sum(len(chunks) for chunks in expected.values())
    assert answer["counts"]["inserted"] == 4
    index = answer["index"]
    assert index["indexed"] is True
    assert index["note"] is None
    assert (index["records_not_indexed"], index["chunks_inserted"]) == (0, total)
    assert 0 < index["texts_embedded"] <= total
    assert run["report"]["counts"]["chunks_inserted"] == total
    assert run["report"]["counts"]["records_not_indexed"] == 0

    # The stored chunks are the chunkers' output, and each record keeps its normalized text.
    stored = await chunk_places(db)
    for rid, chunks in expected.items():
        for c in chunks:
            assert stored[c.id][:5] == (rid, c.kind, c.content_hash, c.anchor, c.url)
            assert stored[c.id][5] == c.occurred_at
    for item in BATCH:
        rid = record_id(source, item["kind"], item["external_id"])
        text = await scalar(db, "SELECT normalized_text FROM record WHERE id = %s", rid)
        assert text == normalize(item["kind"], item["payload"])

    # A thread: its kind, its time, its URL; the chunk names the message it starts with.
    (hit,) = await find(db, fake, owner, "Bought the hose", "exact")
    thread = BATCH[0]
    assert (hit.record_kind, hit.source_kind, hit.source) == ("thread", "push", "chat")
    assert hit.occurred_at == datetime(2026, 3, 3, 18, 1, tzinfo=UTC)
    assert hit.external_id == thread["external_id"]
    assert hit.path is None
    assert hit.header.startswith("greenhouse · Watering schedule · ")
    anchor = stored[hit.chunk_id][3]
    assert anchor.startswith("msg-")
    assert hit.anchor == anchor
    assert hit.locator == f"chat:{thread['external_id']}#{anchor}"
    # The group starts with the hose message, which has no URL: the link is the thread's.
    assert hit.chunk_occurred_at == datetime(2026, 3, 2, 12, 10, tzinfo=UTC)
    assert (hit.chunk_url, hit.link) == (None, thread["external_url"])
    assert hit.start_line is not None

    # Russian words in a Russian thread, lexically; its first message's URL is on the chunk.
    hits = await find(db, fake, owner, "пикировать рассаду", "lexical", kinds=("thread",))
    assert hits
    assert hits[0].external_id == "teplica/r-1"
    assert hits[0].occurred_at == datetime(2026, 3, 4, 6, 20, tzinfo=UTC)
    assert hits[0].locator == "chat:teplica/r-1#msg-r-1"
    assert stored[hits[0].chunk_id][3:5] == (
        "msg-r-1",
        "https://chat.example.invalid/archives/teplica/r-1",
    )
    # The record has no URL; the link is the first message's, the time that message's.
    assert hits[0].external_url is None
    assert hits[0].link == "https://chat.example.invalid/archives/teplica/r-1"
    assert hits[0].chunk_occurred_at == datetime(2026, 3, 4, 6, tzinfo=UTC)

    # A meeting: its summary, action items and transcript are found under its kind and start.
    hits = await find(db, fake, owner, "шланг", "lexical", kinds=("meeting",))
    assert {h.record_kind for h in hits} == {"meeting"}
    assert {h.occurred_at for h in hits} == {datetime(2026, 3, 3, 7, tzinfo=UTC)}
    assert {h.anchor for h in hits} == {stored[h.chunk_id][3] for h in hits}
    assert {h.locator for h in hits} == {f"chat:meeting-2026-03-03#{h.anchor}" for h in hits}
    assert {h.link for h in hits} == {None}
    anchors = {h.anchor for h in hits}
    assert {"summary", "action-items"} <= anchors
    assert any(anchor.startswith("seg-") for anchor in anchors if anchor)
    (spoken,) = await find(db, fake, owner, "Шланг куплю в субботу", "exact")
    assert spoken.chunk_kind == "transcript"
    assert "Transcript 00:04 to 03:10" in spoken.header
    assert stored[spoken.chunk_id][5] == datetime(2026, 3, 3, 7, 0, 4, tzinfo=UTC)
    assert spoken.anchor is not None
    assert spoken.anchor.startswith("seg-")
    assert spoken.locator == f"chat:meeting-2026-03-03#{spoken.anchor}"
    assert spoken.occurred_at == datetime(2026, 3, 3, 7, tzinfo=UTC)
    assert spoken.chunk_occurred_at == datetime(2026, 3, 3, 7, 0, 4, tzinfo=UTC)

    # A document: found by a word under its heading, with its trail as the anchor.
    hits = await find(db, fake, owner, "journal", "lexical", kinds=("document",))
    assert [h.record_kind for h in hits] == ["document"]
    assert hits[0].locator == "chat:handbook#Journal"
    assert hits[0].link == "https://notes.example.invalid/handbook"
    assert hits[0].occurred_at == datetime(2026, 2, 20, 10, tzinfo=UTC)
    assert (hits[0].anchor, hits[0].chunk_occurred_at) == ("Journal", None)
    assert stored[hits[0].chunk_id][3] == "Journal"
    assert hits[0].header == "Greenhouse handbook: Journal"
    (winter,) = await find(db, fake, owner, "тёплой водой", "exact")
    assert winter.record_kind == "document"
    # The text before the first heading has no anchor: the document is the place.
    (opening,) = await find(db, fake, owner, "does not exist", "exact")
    assert (opening.anchor, opening.locator) == (None, "chat:handbook")

    # The time filter takes the record's time, not the chunk's: every group of the thread that
    # holds the word starts before the bound, its last message is after it.
    since = datetime(2026, 3, 3, 8, tzinfo=UTC)
    hits = await find(db, fake, owner, "шланг", "lexical", since=since)
    assert {h.record_kind for h in hits} == {"thread"}
    assert all(h.chunk_occurred_at is not None and h.chunk_occurred_at < since for h in hits)
    assert all(h.occurred_at >= since for h in hits)
    hits = await find(db, fake, owner, "шланг", "lexical", until=since)
    assert {h.record_kind for h in hits} == {"meeting"}


# --- Hits over HTTP and MCP, cited as evidence --------------------------------------------------


@asynccontextmanager
async def mcp_client(
    settings: Settings, pool: Pool, embedder: FakeEmbedder, headers: dict[str, str]
) -> AsyncGenerator[Client]:
    """An MCP client of the application, its lifespan running, as an agent connects."""
    app = create_app(settings, pool=pool, embedder=embedder)
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://memini.test", headers=headers
        ) as http,
    ):
        transport = streamable_http_client(f"http://memini.test{MCP_PATH}", http_client=http)
        async with Client(transport, mode="legacy") as client:
            yield client


def instant(value: str) -> datetime:
    return datetime.fromisoformat(value)


async def test_a_hit_carries_its_anchor_link_and_time_over_http_and_mcp_and_cites_as_evidence(
    db: Conn, owner: uuid.UUID, pool: Pool, settings: Settings, fake: FakeEmbedder
) -> None:
    admin = await bearer(db, owner, "read", "admin")
    pusher = await bearer(db, owner, "ingest")
    agent = await bearer(db, owner, "read", "ingest")
    query = {"query": "пикировать рассаду", "mode": "lexical", "kinds": ["thread"]}
    async with client_of(settings, pool, fake) as client:
        source = await setup(client, admin)
        ok(await client.post(RECORDS, json={"records": BATCH}, headers=pusher))
        found = ok(await client.get("/v1/search", params=query, headers=agent))
        sections = ok(
            await client.get(
                "/v1/search",
                params={"query": "journal", "mode": "lexical", "kinds": ["document"]},
                headers=agent,
            )
        )

    # HTTP: the record's place and time, and the chunk's.
    hit = found["items"][0]
    rid = record_id(source, "thread", "teplica/r-1")
    assert hit["record_id"] == rid
    assert (hit["locator"], hit["anchor"]) == ("chat:teplica/r-1#msg-r-1", "msg-r-1")
    assert hit["link"] == "https://chat.example.invalid/archives/teplica/r-1"
    assert hit["url"] is None
    assert instant(hit["occurred_at"]) == datetime(2026, 3, 4, 6, 20, tzinfo=UTC)
    assert instant(hit["chunk_occurred_at"]) == datetime(2026, 3, 4, 6, tzinfo=UTC)
    journal = sections["items"][0]
    assert (journal["locator"], journal["anchor"]) == ("chat:handbook#Journal", "Journal")
    assert journal["link"] == journal["url"] == "https://notes.example.invalid/handbook"
    assert journal["chunk_occurred_at"] == journal["occurred_at"]

    # MCP: the same hit, its output schema, and the text for clients that read only text.
    async with mcp_client(settings, pool, fake, agent) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        result = await client.call_tool("search", query)
    schema = tools["search"].output_schema
    assert schema is not None
    assert {"anchor", "link", "at"} <= set(schema["$defs"]["SearchHit"]["properties"])
    assert result.is_error is False
    assert result.structured_content == found
    (content,) = result.content
    assert isinstance(content, TextContent)
    assert "1. chat:teplica/r-1#msg-r-1\n" in content.text
    assert "chunk at 2026-03-04T06:00:00+00:00" in content.text
    assert "link: https://chat.example.invalid/archives/teplica/r-1\n" in content.text
    assert f"evidence: record_id {rid}, anchor msg-r-1\n" in content.text

    # record_id and anchor are evidence as they are.
    decision = {
        "statement": "Seedlings are pricked out on Saturday.",
        "decided_at": "2026-03-04T06:20:00Z",
        "evidence": [
            {"record_id": hit["record_id"], "anchor": hit["anchor"]},
            {"record_id": journal["record_id"], "anchor": journal["anchor"]},
        ],
    }
    async with client_of(settings, pool, fake) as client:
        item = ok(
            await client.post("/v1/workspaces/acme/decisions", json=decision, headers=pusher),
            201,
        )
    assert [(e["record_id"], e["anchor"]) for e in item["evidence"]] == [
        (rid, "msg-r-1"),
        (journal["record_id"], "Journal"),
    ]


# --- Changes -------------------------------------------------------------------------------------


async def test_a_batch_sent_again_embeds_nothing_and_an_edit_reembeds_its_group_only(
    db: Conn, owner: uuid.UUID, pool: Pool, settings: Settings, fake: FakeEmbedder
) -> None:
    admin = await bearer(db, owner, "read", "admin")
    pusher = await bearer(db, owner, "ingest")
    async with client_of(settings, pool, fake) as client:
        await setup(client, admin)
        ok(await client.post(RECORDS, json={"records": BATCH}, headers=pusher))
        before = await chunk_places(db)
        xmin = await rows(db, "SELECT id, xmin::text FROM chunk ORDER BY id")
        embedded = len(fake.texts)

        again = ok(await client.post(RECORDS, json={"records": BATCH}, headers=pusher))
        assert again["counts"]["unchanged"] == 4
        assert again["index"]["chunks_inserted"] == 0
        assert again["index"]["texts_embedded"] == 0
        assert await rows(db, "SELECT id, xmin::text FROM chunk ORDER BY id") == xmin

        thread = json.loads(json.dumps(BATCH[0]))
        messages = thread["payload"]["items"]
        index = next(i for i, m in enumerate(messages) if m["text"].startswith("Bought the hose"))
        messages[index]["text"] = "Bought the hose: ten metres, olive."  # as long as before
        edited = ok(await client.post(RECORDS, json={"records": [thread]}, headers=pusher))

    assert edited["counts"]["updated"] == 1
    assert edited["index"]["texts_embedded"] == 1
    assert len(fake.texts) == embedded + 1
    after = await chunk_places(db)
    assert after.keys() == before.keys()
    changed = [cid for cid in after if after[cid][2] != before[cid][2]]
    assert len(changed) == 1
    assert edited["index"]["chunks_updated"] == 1
    (hit,) = await find(db, fake, owner, "ten metres, olive", "exact")
    assert hit.chunk_id == changed[0]


# --- Without embeddings, and a stale index -------------------------------------------------------


async def test_without_an_embedding_provider_records_are_stored_and_a_rebuild_indexes_them(
    db: Conn, owner: uuid.UUID, pool: Pool, settings: Settings, fake: FakeEmbedder
) -> None:
    admin = await bearer(db, owner, "read", "admin")
    pusher = await bearer(db, owner, "ingest")
    async with client_of(settings, pool, None) as client:
        source = await setup(client, admin)
        answer = ok(await client.post(RECORDS, json={"records": BATCH}, headers=pusher))
        run = ok(await client.get(f"/v1/runs/{answer['run_id']}", headers=admin))

    assert answer["counts"]["inserted"] == 4
    assert answer["index"]["indexed"] is False
    assert answer["index"]["records_not_indexed"] == 4
    assert "memini reindex" in answer["index"]["note"]
    assert run["report"]["counts"]["records_not_indexed"] == 4
    assert await scalar(db, "SELECT count(*) FROM chunk") == 0
    assert await scalar(db, "SELECT count(*) FROM record WHERE normalized_text IS NULL") == 0

    workspace = await scalar(db, "SELECT id FROM workspace WHERE name = 'acme'")
    result = await reindex(db, fake, workspace_id=workspace, raw=default_raw_content(db))

    expected = dict(expected_chunks(source, item) for item in BATCH)
    assert result.records_indexed == 4
    stored = await chunk_places(db)
    assert stored == {
        c.id: (rid, c.kind, c.content_hash, c.anchor, c.url, c.occurred_at)
        for rid, chunks in expected.items()
        for c in chunks
    }
    (hit,) = await find(db, fake, owner, "Bought the hose", "exact")
    assert hit.record_kind == "thread"

    # A second rebuild finds every chunk as it is, anchor, URL and time included.
    xmin = await rows(db, "SELECT id, xmin::text FROM chunk ORDER BY id")
    again = await reindex(db, fake, workspace_id=workspace, raw=default_raw_content(db))
    assert (again.counts.unchanged, again.counts.updated, again.counts.embedded) == (
        len(stored),
        0,
        0,
    )
    assert await rows(db, "SELECT id, xmin::text FROM chunk ORDER BY id") == xmin

    # Now indexed at ingest: the same batch writes and embeds nothing.
    async with client_of(settings, pool, fake) as client:
        resent = ok(await client.post(RECORDS, json={"records": BATCH}, headers=pusher))
    assert resent["counts"]["unchanged"] == 4
    assert await chunk_places(db) == stored


async def test_a_stale_index_refuses_the_batch_and_stores_nothing(
    db: Conn, owner: uuid.UUID, pool: Pool, settings: Settings, fake: FakeEmbedder
) -> None:
    admin = await bearer(db, owner, "read", "admin")
    pusher = await bearer(db, owner, "ingest")
    async with client_of(settings, pool, fake) as client:
        await setup(client, admin)
        ok(await client.post(RECORDS, json={"records": BATCH[:1]}, headers=pusher))
    async with client_of(settings, pool, FakeEmbedder(OTHER_MODEL)) as client:
        response = await client.post(RECORDS, json={"records": BATCH[1:]}, headers=pusher)

    assert response.status_code == 503, response.text
    error = response.json()["error"]
    assert error["code"] == "index_mismatch"
    assert "memini reindex --workspace acme" in error["message"]
    assert await scalar(db, "SELECT count(*) FROM record") == 1
