"""The M2 check (docs/plan.md, M2 "Done when") over the synthetic comms fixtures (``eval/comms``),
through the comms tools of a running application, against a real Postgres. Acceptance of
memini-psr.

The cast of ``people.json`` is made first with its deterministic aliases and "me" marked on mira;
the batches are ingested with the FakeEmbedder; then each Russian display name marked
``needs_merge`` is attached by hand, which merges the person ingest made of it into its cast
member (labels/queries.json: "every alias attached, those marked needs_merge included"). So the
Russian weekly sync, where Mira appears as "Мира Кастелан", is hers only through a merge.

1. The fixtures ingest; a second sync of the sources as they stand changes nothing.
2. Every key of ``meetings_attended`` and ``messages_to_me`` through the MCP tools
   ``list_meetings`` and ``list_messages`` (the label's text as the query, the person "me"), and
   again over HTTP: every ``expect`` record comes back, with the messages the label names, and no
   ``not_expected`` one.
3. Retention (``labels/queries.json``, ``retention``): with 21 days on chat, mail and meetings as
   of 5 October, the expired records go with their chunks and person links, the records at the
   edge stay, and the tools stop returning what expired.

The other comms tools and the persons filter of search are checked on the same data. The
FakeEmbedder's vectors carry no meaning; the comms tools match a topic by words.
"""

import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, LiteralString, cast

import httpx2
import pytest
from fastapi import FastAPI
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp_types import CallToolResult, TextContent
from psycopg import AsyncConnection
from psycopg.rows import TupleRow

from comms_fixtures import (
    WORKSPACE,
    RecordRef,
    create_sources,
    final_records,
    load,
    me,
    people,
    query_labels,
    record_id_of,
    second_sync,
)
from memini.api import create_app
from memini.auth import (
    Principal,
    ReadFilter,
    Scope,
    WriteAccess,
    authorize_read,
    create_token,
)
from memini.config import Settings
from memini.db import Pool, connect, open_pool
from memini.embeddings import FakeEmbedder
from memini.ingest import PayloadIndexer, ingest
from memini.mcp import MCP_PATH
from memini.persons import AliasKind, add_alias, create_person, set_me, unset_me
from memini.removal import expire_source
from memini.search import SearchFilters, SearchQuery, search
from memini.sources import SourceUpdate, update_source
from memini.workspaces import ensure_principal

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

type Conn = AsyncConnection[TupleRow]
type Json = dict[str, Any]

BASE_URL = "http://memini.test"


@dataclass(frozen=True)
class World:
    db: Conn
    pool: Pool
    owner: uuid.UUID
    access: WriteAccess
    sources: dict[str, uuid.UUID]
    persons: dict[str, uuid.UUID]
    """The cast by key, as made before ingest."""
    merged: dict[str, uuid.UUID]
    """The person ingest made of each needs_merge name, merged into its cast member since."""
    embedder: FakeEmbedder
    settings: Settings

    def app(self) -> FastAPI:
        return create_app(self.settings, pool=self.pool, embedder=self.embedder)

    async def token(self, *scopes: Scope) -> str:
        new = await create_token(
            self.db, owner_id=self.owner, name="test agent", scopes=scopes or ("read",)
        )
        return new.secret.get_secret_value()

    def record_id(self, ref: RecordRef) -> str:
        return record_id_of(ref, self.sources)


async def make_cast(db: Conn, access: WriteAccess) -> tuple[dict[str, uuid.UUID], list[Json]]:
    """Every person of people.json with its aliases except those marked needs_merge, which come
    back to be attached by hand; me marked."""
    ids: dict[str, uuid.UUID] = {}
    later: list[Json] = []
    for person in cast("list[Json]", people()["people"]):
        aliases: list[tuple[AliasKind, str]] = []
        for alias in cast("list[Json]", person["aliases"]):
            if alias.get("needs_merge"):
                later.append({"person": person["key"], **alias})
            else:
                aliases.append((cast("AliasKind", alias["kind"]), str(alias["value"])))
        made = await create_person(db, access, WORKSPACE, str(person["name"]), aliases)
        ids[str(person["key"])] = made.id
    await set_me(db, access, WORKSPACE, ids[me()])
    return ids, later


@pytest.fixture
async def world(migrated_database_url: str, settings: Settings) -> AsyncIterator[World]:
    async with (
        connect(migrated_database_url, autocommit=True) as db,
        open_pool(migrated_database_url, min_size=1, max_size=4, timeout=5.0) as pool,
    ):
        owner = await ensure_principal(db, "owner")
        access = WriteAccess.for_owner(owner)
        sources = await create_sources(db, access)
        persons, later = await make_cast(db, access)
        embedder = FakeEmbedder()
        await load(db, access, indexer=PayloadIndexer(embedder))
        merged: dict[str, uuid.UUID] = {}
        for alias in later:
            added = await add_alias(
                db,
                access,
                WORKSPACE,
                persons[alias["person"]],
                cast("AliasKind", alias["kind"]),
                str(alias["value"]),
            )
            assert added.merged is not None, alias  # ingest made a person of the name
            merged[str(alias["person"])] = added.merged
        yield World(db, pool, owner, access, sources, persons, merged, embedder, settings)


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


def text(result: CallToolResult) -> str:
    (content,) = result.content
    assert isinstance(content, TextContent)
    return content.text


def ok(result: CallToolResult) -> Json:
    assert result.is_error is False, text(result)
    structured: Json = result.structured_content
    return structured


def refused(result: CallToolResult) -> str:
    assert result.is_error is True
    return text(result)


def refs(results: Sequence[Json]) -> set[RecordRef]:
    return {RecordRef(r["source"], r["kind"], r["external_id"]) for r in results}


def labelled(entries: Sequence[Json]) -> set[RecordRef]:
    return {RecordRef.of(entry) for entry in entries}


async def rows(db: Conn, query: LiteralString, *params: Any) -> list[tuple[Any, ...]]:
    cur = await db.execute(query, params)
    return [tuple(row) for row in await cur.fetchall()]


# --- The M2 check --------------------------------------------------------------------------------


def check_meetings(key: Json, meetings: list[Json]) -> None:
    found = refs(meetings)
    assert labelled(key["expect"]) <= found, (key["id"], found)
    assert not labelled(key["not_expected"]) & found, (key["id"], found)
    for meeting in meetings:
        assert meeting["kind"] == "meeting"
        assert meeting["anchor"] is not None
        assert meeting["snippet"]
        assert (
            meeting["locator"]
            == f"{meeting['source']}:{meeting['external_id']}#{meeting['anchor']}"
        )


def check_messages(key: Json, messages: list[Json]) -> None:
    found = refs(messages)
    assert labelled(key["expect"]) <= found, (key["id"], found)
    assert not labelled(key["not_expected"]) & found, (key["id"], found)
    for expected in cast("list[Json]", key["expect"]):
        ref = RecordRef.of(expected)
        returned = {
            m["id"] for m in messages if RecordRef(m["source"], m["kind"], m["external_id"]) == ref
        }
        assert set(expected["items"]) <= returned, (key["id"], ref, returned)
    for message in messages:
        assert set(message["roles"]) & {"recipient", "mention"}, message["locator"]
        assert message["anchor"] == f"msg-{message['message_id']}"


async def test_the_m2_check_over_mcp_and_http(world: World) -> None:
    labels = query_labels()
    assert labels["me"] == me()

    # 1. A second sync of the sources as they stand changes nothing.
    for source, batch in second_sync():
        again = await ingest(
            world.db, world.access, WORKSPACE, source, batch, indexer=PayloadIndexer(world.embedder)
        )
        assert (again.inserted, again.updated, again.revived, again.deleted) == (0, 0, 0, 0)
        assert again.reconciled == 0, source

    # 2. Meetings I attended about X and messages to me about X, over MCP.
    app = world.app()
    token = await world.token("read")
    async with mcp_client(app, token) as client:
        for key in cast("list[Json]", labels["meetings_attended"]):
            result = ok(await client.call_tool("list_meetings", {"query": key["text"]}))
            assert [p["person_id"] for p in result["persons"]] == [str(world.persons[me()])]
            assert result["persons"][0]["is_me"] is True
            check_meetings(key, result["items"])
        for key in cast("list[Json]", labels["messages_to_me"]):
            result = ok(await client.call_tool("list_messages", {"query": key["text"]}))
            check_messages(key, result["items"])

    # ... and over HTTP.
    async with http_client(app, token) as http:
        for key in cast("list[Json]", labels["meetings_attended"]):
            response = await http.get("/v1/meetings", params={"query": key["text"]})
            assert response.status_code == 200, response.text
            check_meetings(key, response.json()["items"])
        for key in cast("list[Json]", labels["messages_to_me"]):
            response = await http.get("/v1/messages", params={"query": key["text"]})
            assert response.status_code == 200, response.text
            check_messages(key, response.json()["items"])


async def test_records_past_retention_go_and_the_tools_stop_returning_them(world: World) -> None:
    rule = cast("Json", query_labels()["retention"])
    expired = labelled(rule["expired"])
    kept = labelled(rule["kept_at_the_edge"])
    as_of = datetime.fromisoformat(rule["as_of"])
    cutoff = as_of - timedelta(days=rule["retention_days"])
    for source in rule["sources"]:
        await update_source(
            world.db,
            world.access,
            WORKSPACE,
            source,
            SourceUpdate(retention_days=rule["retention_days"]),
        )
        await expire_source(world.db, world.sources[source], as_of=as_of)

    for ref in expired:
        record = world.record_id(ref)
        assert await rows(world.db, "SELECT 1 FROM record WHERE id = %s", record) == [], ref
        assert await rows(world.db, "SELECT 1 FROM chunk WHERE record_id = %s", record) == []
        assert (
            await rows(world.db, "SELECT 1 FROM record_person WHERE record_id = %s", record) == []
        )
    for ref in kept:
        assert await rows(world.db, "SELECT 1 FROM record WHERE id = %s", world.record_id(ref)), ref
    live_before_cutoff = {
        ref
        for ref, record in final_records().items()
        if not record.deleted and ref.source in rule["sources"] and record.occurred_at < cutoff
    }
    assert live_before_cutoff == expired

    app = world.app()
    async with mcp_client(app, await world.token("read")) as client:
        meetings = ok(await client.call_tool("list_meetings", {"limit": 100}))["items"]
        messages = ok(await client.call_tool("list_messages", {"limit": 100}))["items"]
        cbor = ok(await client.call_tool("list_meetings", {"query": "CBOR"}))["items"]
    assert not refs(meetings) & expired
    assert not refs(messages) & expired
    assert refs(cbor) == {RecordRef("meetings", "meeting", "notes-0930-weekly-sync")}


# --- The other comms tools -----------------------------------------------------------------------


async def test_list_meetings_without_a_query_is_newest_first_and_follows_merges(
    world: World,
) -> None:
    attended = sorted(
        (
            (record.occurred_at, ref)
            for ref, record in final_records().items()
            if ref.kind == "meeting"
            and not record.deleted
            and any(
                name in ("Mira Castellane", "Мира Кастелан")
                for name in record.payload.get("participants", [])
            )
        ),
        reverse=True,
    )
    app = world.app()
    async with mcp_client(app, await world.token("read")) as client:
        result = ok(await client.call_tool("list_meetings", {}))
        assert [refs([m]).pop() for m in result["items"]] == [ref for _, ref in attended]
        first = result["items"][0]
        assert first["external_id"] == "notes-0930-weekly-sync"  # hers only through the merge
        assert first["anchor"] is None
        assert first["snippet"] is None
        assert first["score"] is None
        assert first["title"]
        assert first["participants"]

        # The person ingest made of "Мира Кастелан" stands for Mira now: its canonical person.
        by_merged = ok(await client.call_tool("list_meetings", {"person": str(world.merged[me()])}))
        assert by_merged["persons"] == result["persons"]
        assert by_merged["items"] == result["items"]

        # Another person, by alias; a time window on the start.
        lev = ok(
            await client.call_tool(
                "list_meetings",
                {
                    "persons": ["Lev Zarechny"],
                    "since": "2026-09-11T00:00:00Z",
                    "until": "2026-10-01T00:00:00Z",
                },
            )
        )
        assert [p["person_id"] for p in lev["persons"]] == [str(world.persons["lev"])]
        assert lev["persons"][0]["is_me"] is False
        assert [m["external_id"] for m in lev["items"]] == [
            "notes-0930-weekly-sync",
            "notes-0911-firmware-review",
        ]
        summary = text(await client.call_tool("list_meetings", {"limit": 1}))
        assert summary.startswith("1 meeting.")
        assert "meetings:notes-0930-weekly-sync" in summary
        assert "(me)" in summary


async def test_list_messages_by_role_and_time(world: World) -> None:
    app = world.app()
    async with mcp_client(app, await world.token("read")) as client:
        to_me = ok(await client.call_tool("list_messages", {"limit": 100}))["items"]
        times = [m["occurred_at"] for m in to_me]
        assert times == sorted(times, reverse=True)
        expected = {
            (ref, message_id)
            for ref, record in final_records().items()
            if ref.kind == "thread" and not record.deleted
            for message_id in messages_to_mira(record.payload)
        }
        assert {(refs([m]).pop(), m["id"]) for m in to_me} == expected

        mentions = ok(
            await client.call_tool("list_messages", {"roles": ["mention"], "limit": 100})
        )["items"]
        assert mentions
        assert all(m["roles"] == ["mention"] for m in mentions)
        assert all("U01MIRA" in m["mentions"] for m in mentions)

        from_lev = ok(
            await client.call_tool(
                "list_messages",
                {
                    "persons": ["lev.zarechny@saltreed.example.test"],
                    "roles": ["author"],
                    "sources": ["chat"],
                    "since": "2026-09-14T00:00:00Z",
                    "limit": 100,
                },
            )
        )["items"]
        assert from_lev
        for message in from_lev:
            assert message["author"] == "U03LEV"
            assert message["source"] == "chat"
            assert message["occurred_at"] >= "2026-09-14T00:00:00Z"

        summary = text(await client.call_tool("list_messages", {"limit": 2}))
        assert summary.startswith("2 messages.")
        assert "evidence: record_id" in summary
        assert "```" in summary


def messages_to_mira(payload: Json) -> list[str]:
    mira = cast("list[Json]", next(p for p in people()["people"] if p["key"] == me())["aliases"])
    names = {str(alias["value"]) for alias in mira}
    found: list[str] = []
    for message in cast("list[Json]", payload["items"]):
        named = [
            *message.get("recipients", []),
            *message.get("cc", []),
            *message.get("mentions", []),
        ]
        addresses = {n[n.rindex("<") + 1 : -1] if n.endswith(">") else n for n in named}
        if addresses & names:
            found.append(str(message["id"]))
    return found


async def test_get_meeting_and_get_thread(world: World) -> None:
    sync = RecordRef("meetings", "meeting", "notes-0930-weekly-sync")
    thread = RecordRef("chat", "thread", "C0BEACON01/1788858900.000100")
    app = world.app()
    token = await world.token("read")
    async with mcp_client(app, token) as client:
        meeting = ok(await client.call_tool("get_meeting", {"id": world.record_id(sync)}))
        assert meeting["transcript"] is None
        assert meeting["segments"] > 0
        assert meeting["summary"]
        assert meeting["title"]
        mira = next(p for p in meeting["persons"] if p["is_me"])
        assert mira["person_id"] == str(world.persons[me()])  # through the merge
        assert mira["roles"] == ["participant"]
        assert "Мира Кастелан" in meeting["participants"]
        full = ok(
            await client.call_tool(
                "get_meeting",
                {
                    "source": "meetings",
                    "external_id": sync.external_id,
                    "include_transcript": True,
                },
            )
        )
        assert full["record_id"] == meeting["record_id"]
        assert [s["anchor"] for s in full["transcript"]][:1] == ["seg-s01"]
        assert "include_transcript" in text(
            await client.call_tool("get_meeting", {"id": world.record_id(sync)})
        )

        got = ok(
            await client.call_tool(
                "get_thread", {"source": "chat", "external_id": thread.external_id}
            )
        )
        payload = final_records()[thread].payload
        assert [m["id"] for m in got["items"]] == [m["id"] for m in payload["items"]]
        first = got["items"][0]
        assert first["anchor"] == "msg-1788858900.000100"
        assert first["locator"] == f"chat:{thread.external_id}#msg-1788858900.000100"
        assert first["link"] == payload["items"][0]["url"]
        roles = {p["person_id"]: p["roles"] for p in got["persons"]}
        assert roles[str(world.persons[me()])] == ["author", "mention"]

        assert "use get_thread" in refused(
            await client.call_tool("get_meeting", {"id": world.record_id(thread)})
        )
        assert (
            refused(
                await client.call_tool("get_thread", {"source": "chat", "external_id": "nothing"})
            )
            == "No such thread."
        )
        deleted = RecordRef("meetings", "meeting", "notes-0929-quick-call")
        assert (
            refused(await client.call_tool("get_meeting", {"id": world.record_id(deleted)}))
            == "No such meeting."
        )

    async with http_client(app, token) as http:
        response = await http.get(
            f"/v1/workspaces/{WORKSPACE}/sources/chat/threads/{thread.external_id}"
        )
        assert response.status_code == 200, response.text
        assert response.json() == got
        response = await http.get(
            f"/v1/meetings/{world.record_id(sync)}", params={"include_transcript": "true"}
        )
        assert response.status_code == 200
        assert response.json() == full
        response = await http.get(f"/v1/threads/{world.record_id(sync)}")
        assert (response.status_code, response.json()["error"]["code"]) == (422, "wrong_kind")


async def test_the_persons_filter_of_search(world: World) -> None:
    reader: ReadFilter = authorize_read(
        Principal(principal_id=world.owner, token_id=uuid.uuid4(), scopes=frozenset({"read"}))
    )

    async def found(text: str, **filters: Any) -> set[str]:
        hits = await search(
            world.db,
            world.embedder,
            reader,
            SearchQuery(text=text, mode="lexical", limit=100, filters=SearchFilters(**filters)),
        )
        return {hit.record_id for hit in hits}

    everything = await found("CBOR")
    mine = await found("CBOR", persons=("me",))
    assert mine
    assert mine < everything
    linked = {
        row[0]
        for row in await rows(
            world.db,
            "SELECT record_id FROM record_person"
            " WHERE person_id IN (SELECT person_members(person_root(%s)))",
            world.persons[me()],
        )
    }
    assert mine <= linked
    firmware = world.record_id(RecordRef("meetings", "meeting", "notes-0911-firmware-review"))
    weekly = world.record_id(RecordRef("meetings", "meeting", "notes-0930-weekly-sync"))
    assert firmware in everything
    assert firmware not in mine
    assert weekly in mine  # through the merge
    # The same person by its id, by the merged person's id, by an alias of either kind.
    for person in (
        world.persons[me()],
        world.merged[me()],
        "U01MIRA",
        "Mira Castellane <mira.castellane@saltreed.example.test>",
        "Мира Кастелан",
    ):
        assert await found("CBOR", persons=(person,)) == mine, person
    # Roles narrow it; a name no one has gives no hits.
    authored = await found("CBOR", persons=("U03LEV",), roles=("author",))
    assert authored
    assert authored <= await found("CBOR", persons=("U03LEV",))
    assert await found("CBOR", persons=("nobody@example.test",)) == set()

    async with mcp_client(world.app(), await world.token("read")) as client:
        hits = ok(
            await client.call_tool(
                "search",
                {"query": "CBOR", "mode": "lexical", "persons": ["me"], "kinds": ["meeting"]},
            )
        )["items"]
        assert {hit["record_id"] for hit in hits} == {r for r in mine if r in meeting_ids(world)}


def meeting_ids(world: World) -> set[str]:
    return {world.record_id(ref) for ref in final_records() if ref.kind == "meeting"}


async def test_who_me_is_and_unknown_persons(world: World) -> None:
    app = world.app()
    async with mcp_client(app, await world.token("read")) as client:
        assert (
            refused(await client.call_tool("list_messages", {"persons": ["nobody@example.test"]}))
            == "No such person."
        )
        await unset_me(world.db, world.access, WORKSPACE)
        assert "No person is marked as me" in refused(await client.call_tool("list_meetings", {}))
        assert "list_meetings" in [t.name for t in (await client.list_tools()).tools]
        assert "put_document" not in [t.name for t in (await client.list_tools()).tools]


async def test_add_document_stores_an_indexed_document(world: World) -> None:
    app = world.app()
    document = {
        "workspace": WORKSPACE,
        "source": "docs",
        "title": "Decision: berth fees in the pilot",
        "text": "# Berth fees\n\nThe pilot charges no berth fees until December.",
    }
    async with mcp_client(app, await world.token("read")) as client:
        with pytest.raises(MCPError, match="Unknown tool"):  # a read token has no write tool
            await client.call_tool("put_document", document)

    async with mcp_client(app, await world.token("read", "ingest")) as client:
        added = ok(await client.call_tool("put_document", document))
        assert added["stored"] == "inserted"
        assert added["indexed"] is True
        assert added["external_id"].startswith("doc-")
        assert added["locator"] == f"docs:{added['external_id']}"
        again = ok(
            await client.call_tool(
                "put_document", {**document, "occurred_at": added["occurred_at"]}
            )
        )
        assert (again["stored"], again["record_id"]) == ("unchanged", added["record_id"])
        hits = ok(
            await client.call_tool(
                "search", {"query": "berth fees December", "mode": "lexical", "sources": ["docs"]}
            )
        )["items"]
        assert added["record_id"] in {hit["record_id"] for hit in hits}
        summary = text(await client.call_tool("put_document", document))
        assert summary.startswith(f"Document {added['locator']} in {WORKSPACE}/docs:")
        refusal = refused(await client.call_tool("put_document", {**document, "source": "nope"}))
        assert refusal == "No such source."

    # A source that is not a push source is refused; the push API's errors come through.
    await world.db.execute("UPDATE source SET kind = 'mcp' WHERE id = %s", (world.sources["mail"],))
    async with http_client(app, await world.token("read", "ingest")) as http:
        response = await http.post(
            f"/v1/workspaces/{WORKSPACE}/sources/mail/documents",
            json={"title": "x", "text": "y"},
        )
        assert (response.status_code, response.json()["error"]["code"]) == (409, "not_ingestible")
        response = await http.post(
            f"/v1/workspaces/{WORKSPACE}/sources/docs/documents",
            json={"title": "   ", "text": "y"},
        )
        assert (response.status_code, response.json()["error"]["code"]) == (422, "invalid_batch")
        assert "title" in response.json()["error"]["message"]
        response = await http.post(
            f"/v1/workspaces/{WORKSPACE}/sources/docs/documents",
            json={"title": "Notes", "text": "Nothing yet.", "external_id": "notes-1"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["stored"] == "inserted"
        assert response.json()["external_id"] == "notes-1"
