"""Manual operations on the knowledge journal against a real Postgres, over HTTP and MCP: record,
supersede, close and retract by hand; a manual correction that survives re-extraction (the
acceptance test of memini-asn); scopes, the workspace limit of a token, and the refusals of the
append-only journal as 409s that name their constraint. The library's rules are tested in
tests/test_knowledge_db.py.

Run with `make test-integration`. Every record, person and statement here is synthetic.
"""

from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, LiteralString
from uuid import UUID

import httpx2
import pytest
from fastapi import FastAPI
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp_types import TextContent
from psycopg import AsyncConnection
from psycopg.rows import TupleRow
from pydantic import SecretStr

from ingest_helpers import HOUR, T0, iso, thread_payload
from memini.api import create_app
from memini.auth import Principal, Scope, WriteAccess, authorize_read, create_token
from memini.config import Settings
from memini.db import Pool, connect, open_pool
from memini.decisions import DecisionError, decision_errors
from memini.embeddings import FakeEmbedder
from memini.ids import record_id
from memini.knowledge import (
    ALL_STATUSES,
    EvidenceRef,
    Extraction,
    Item,
    ItemFilters,
    NewItem,
    SupersedesRef,
    SupersessionError,
    get_item,
    list_items,
    record_extraction,
    supersede,
)
from memini.mcp import MCP_PATH
from memini.query import QueryServices
from memini.removal import expire_source
from memini.workspaces import ensure_principal

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

type Conn = AsyncConnection[TupleRow]
type Json = dict[str, Any]

BASE_URL = "http://memini.test"
DECISIONS = "/v1/workspaces/alpha/decisions"
EXTRACTOR_V1 = "test-model@decisions-v1"
EXTRACTOR_V2 = "test-model@decisions-v2"
SAID = T0 + 9 * HOUR  # when the thread's last message was sent
COMMIT = "4f" * 20


@pytest.fixture
async def db(migrated_database_url: str) -> AsyncIterator[Conn]:
    async with connect(migrated_database_url, autocommit=True) as conn:
        yield conn


@pytest.fixture
async def pool(migrated_database_url: str) -> AsyncIterator[Pool]:
    async with open_pool(migrated_database_url, min_size=1, max_size=4, timeout=5.0) as pool:
        yield pool


@pytest.fixture
def app(migrated_database_url: str, pool: Pool) -> FastAPI:
    return create_app(Settings(database_url=SecretStr(migrated_database_url)), pool=pool)


@asynccontextmanager
async def http_client(app: FastAPI, token: str) -> AsyncGenerator[httpx2.AsyncClient]:
    """A client of the HTTP routes. The application's lifespan runs only with :func:`mcp_client`:
    the routes do not need it, and it runs once at a time."""
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        base_url=BASE_URL,
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        yield client


@asynccontextmanager
async def mcp_client(app: FastAPI, token: str) -> AsyncGenerator[Client]:
    async with app.router.lifespan_context(app), http_client(app, token) as http:
        transport = streamable_http_client(f"{BASE_URL}{MCP_PATH}", http_client=http)
        async with Client(transport, mode="legacy") as client:
            yield client


def ok(response: httpx2.Response, status_code: int = 200) -> Json:
    assert response.status_code == status_code, response.text
    body: Json = response.json()
    return body


def refused(response: httpx2.Response, status_code: int, code: str) -> str:
    assert response.status_code == status_code, response.text
    error: Json = response.json()["error"]
    assert error["code"] == code, error
    message: str = error["message"]
    return message


async def scalar(db: Conn, query: LiteralString, *params: Any) -> Any:
    cur = await db.execute(query, params)
    row = await cur.fetchone()
    assert row is not None
    return row[0]


@dataclass(frozen=True)
class World:
    """Workspaces alpha and beta of one owner, a push source chat in each, a git source repo in
    alpha, records and persons, and tokens."""

    owner: UUID
    alpha: UUID
    beta: UUID
    thread: str  # alpha: chat thread garden/0001
    later_thread: str  # alpha: chat thread garden/0002, a day later
    code: str  # alpha: repo refs/heads/main:src/pump.py
    beta_thread: str  # beta: chat thread kiln/0001
    person: UUID  # alpha
    beta_person: UUID  # beta
    tokens: dict[str, str]

    @property
    def access(self) -> WriteAccess:
        """What the extractor writes with: the owner, in every workspace."""
        return WriteAccess.for_owner(self.owner)


async def new_token(db: Conn, owner: UUID, *scopes: Scope, workspace_id: UUID | None = None) -> str:
    token = await create_token(
        db, owner_id=owner, name="test agent", scopes=scopes, workspace_id=workspace_id
    )
    return token.secret.get_secret_value()


def thread_record(external_id: str, at: Any, text: str = "Water the beds at six?") -> Json:
    return {
        "kind": "thread",
        "external_id": external_id,
        "occurred_at": iso(at),
        "payload": thread_payload(at, text),
    }


async def push(app: FastAPI, token: str, workspace: str, *records: Json) -> None:
    async with http_client(app, token) as http:
        ok(
            await http.post(
                f"/v1/workspaces/{workspace}/sources/chat/records",
                json={"records": list(records)},
            )
        )


async def record_id_of(db: Conn, workspace: UUID, external_id: str) -> str:
    return await scalar(
        db,
        "SELECT id FROM record WHERE workspace_id = %s AND external_id = %s",
        workspace,
        external_id,
    )


async def add_person(db: Conn, owner: UUID, workspace: UUID, name: str) -> UUID:
    return await scalar(
        db,
        "INSERT INTO person (workspace_id, owner_id, display_name) VALUES (%s, %s, %s)"
        " RETURNING id",
        workspace,
        owner,
        name,
    )


@pytest.fixture
async def world(app: FastAPI, db: Conn) -> World:
    owner = await ensure_principal(db, "test-owner")
    admin = await new_token(db, owner, "read", "admin")
    ids: dict[str, UUID] = {}
    async with http_client(app, admin) as http:
        for name in ("alpha", "beta"):
            ids[name] = UUID(ok(await http.post("/v1/workspaces", json={"name": name}), 201)["id"])
            ok(
                await http.post(
                    f"/v1/workspaces/{name}/sources", json={"kind": "push", "name": "chat"}
                ),
                201,
            )
    pusher = await new_token(db, owner, "ingest")
    await push(
        app,
        pusher,
        "alpha",
        thread_record("garden/0001", SAID),
        thread_record("garden/0002", SAID + timedelta(days=1), "Seven, then."),
    )
    await push(app, pusher, "beta", thread_record("kiln/0001", SAID))
    # A git source with one file record: code is synced, not pushed.
    repo = await scalar(
        db,
        "INSERT INTO source (workspace_id, owner_id, name, kind)"
        " VALUES (%s, %s, 'repo', 'repository')"
        " RETURNING id",
        ids["alpha"],
        owner,
    )
    code = record_id(repo, "code_file", "refs/heads/main:src/pump.py")
    await db.execute(
        "INSERT INTO record (id, workspace_id, owner_id, source_id, kind, external_id,"
        " content_hash, occurred_at) VALUES (%s, %s, %s, %s, 'code_file', %s, %s, %s)",
        (code, ids["alpha"], owner, repo, "refs/heads/main:src/pump.py", "c0" * 20, SAID),
    )
    return World(
        owner=owner,
        alpha=ids["alpha"],
        beta=ids["beta"],
        thread=await record_id_of(db, ids["alpha"], "garden/0001"),
        later_thread=await record_id_of(db, ids["alpha"], "garden/0002"),
        code=code,
        beta_thread=await record_id_of(db, ids["beta"], "kiln/0001"),
        person=await add_person(db, owner, ids["alpha"], "Person One"),
        beta_person=await add_person(db, owner, ids["beta"], "Person Two"),
        tokens={
            "admin": admin,
            "pusher": pusher,
            "reader": await new_token(db, owner, "read"),
            "writer": await new_token(db, owner, "read", "ingest"),
            "beta_writer": await new_token(db, owner, "read", "ingest", workspace_id=ids["beta"]),
        },
    )


async def content_hash(db: Conn, record: str) -> str:
    return await scalar(db, "SELECT content_hash FROM record WHERE id = %s", record)


def extracted(
    statement: str, record: str, *, hours: int = 0, supersedes: tuple[UUID, ...] = ()
) -> NewItem:
    """An item an extractor found in ``record``, said ``hours`` after the thread."""
    return NewItem(
        kind="decision",
        statement=statement,
        decided_at=SAID + hours * HOUR,
        evidence=(EvidenceRef(record, "m2"),),
        supersedes=tuple(SupersedesRef(each) for each in supersedes),
    )


async def extract(db: Conn, w: World, extractor: str, *items: NewItem) -> Extraction:
    """What the extraction job does for the thread garden/0001: one call per content and
    extractor."""
    return await record_extraction(
        db,
        w.access,
        record_id=w.thread,
        content_hash=await content_hash(db, w.thread),
        extractor=extractor,
        items=items,
    )


def owner_principal(w: World) -> Principal:
    return Principal(
        principal_id=w.owner,
        token_id=UUID("00000000-0000-7000-8000-00000000c0de"),
        scopes=frozenset({"read"}),
    )


async def item(db: Conn, w: World, item_id: UUID | str) -> Item:
    """The item as the owner reads it."""
    reader = authorize_read(owner_principal(w))
    return (await get_item(db, reader, UUID(str(item_id)))).item


def correction(w: World, **changes: Any) -> Json:
    body: Json = {
        "statement": "Water the beds at seven, not six.",
        "occurred_at": iso(SAID + timedelta(hours=1)),
        "evidence": [{"record_id": w.thread, "anchor": "m2"}],
    }
    body.update(changes)
    return body


# --- The acceptance test -------------------------------------------------------------------------


async def test_a_manual_correction_survives_a_reextraction_of_the_same_record(
    app: FastAPI, db: Conn, world: World
) -> None:
    # The extractor reads the thread and gets it wrong.
    first = await extract(
        db, world, EXTRACTOR_V1, extracted("Water the beds at six.", world.thread)
    )
    (wrong,) = first.item_ids

    # A person corrects it by hand: a new item that replaces the extracted one.
    async with http_client(app, world.tokens["writer"]) as http:
        fixed = ok(
            await http.post(
                DECISIONS,
                json=correction(
                    world, participants=[str(world.person)], supersedes=[{"item_id": str(wrong)}]
                ),
            ),
            201,
        )
    manual = UUID(fixed["id"])
    assert (fixed["status"], fixed["manual"], fixed["extractor"]) == ("active", True, "manual")
    assert fixed["evidence"][0]["record_id"] == world.thread
    assert fixed["evidence"][0]["anchor"] == "m2"
    assert [p["display_name"] for p in fixed["participants"]] == ["Person One"]
    assert [(link["supersedes_id"], link["relation"]) for link in fixed["supersedes"]] == [
        (str(wrong), "replaces")
    ]
    assert (await item(db, world, wrong)).status == "superseded"
    before = await item(db, world, manual)

    # The same content by the same extractor: nothing is written.
    again = await extract(
        db, world, EXTRACTOR_V1, extracted("Water the beds at six.", world.thread)
    )
    assert (again.created, again.item_ids) == (False, (wrong,))

    # A new extractor version that would supersede the manual item is refused, and writes nothing.
    with pytest.raises(SupersessionError) as refusal:
        await extract(
            db,
            world,
            EXTRACTOR_V2,
            # Said after the correction, so only the rule for manual items stands in the way.
            extracted("Water the beds at six.", world.thread, hours=2, supersedes=(manual,)),
        )
    assert refusal.value.rule == "manual"
    assert await scalar(db, "SELECT count(*) FROM knowledge_extraction") == 1

    # The new version's item stands beside the manual one.
    second = await extract(
        db, world, EXTRACTOR_V2, extracted("Water the beds at six.", world.thread, hours=2)
    )
    (beside,) = second.item_ids
    assert second.created

    # The supersession check cannot link it over the manual item either, by hand or as a job.
    with pytest.raises(SupersessionError) as link_refusal:
        await supersede(db, world.access, beside, [SupersedesRef(manual)])
    assert link_refusal.value.rule == "manual"
    async with http_client(app, world.tokens["writer"]) as http:
        message = refused(
            await http.post(
                f"{DECISIONS}/{beside}/supersede", json={"supersedes": [{"item_id": str(manual)}]}
            ),
            409,
            "supersession_refused",
        )
    assert message.endswith("(constraint knowledge_supersession_manual_check).")

    # New content of the record, read again by the new version: still no change to the manual item.
    await push(
        app,
        world.tokens["pusher"],
        "alpha",
        thread_record("garden/0001", SAID, "Water the beds at six, or later?"),
    )
    third = await extract(db, world, EXTRACTOR_V2, extracted("Water the beds later.", world.thread))
    assert third.created

    after = await item(db, world, manual)
    assert after == before
    assert (after.status, after.invalidated_at, after.superseded_by) == ("active", None, None)
    reader = authorize_read(owner_principal(world))
    active = {each.id for each in await list_items(db, reader, ItemFilters(workspaces=("alpha",)))}
    assert manual in active
    assert wrong not in active
    history = await list_items(db, reader, ItemFilters(statuses=ALL_STATUSES))
    assert {each.id: each.status for each in history}[wrong] == "superseded"


async def test_a_correction_over_mcp_survives_a_reextraction(
    app: FastAPI, db: Conn, world: World
) -> None:
    first = await extract(
        db, world, EXTRACTOR_V1, extracted("Water the beds at six.", world.thread)
    )
    (wrong,) = first.item_ids

    async with mcp_client(app, world.tokens["writer"]) as client:
        result = await client.call_tool(
            "record_decision",
            {
                "workspace": "alpha",
                **correction(
                    world,
                    evidence=[{"record_id": world.thread, "anchor": "m2"}],
                    supersedes=[{"item_id": str(wrong), "relation": "replaces"}],
                ),
            },
        )
    assert result.is_error is False, result.content
    fixed: Json = result.structured_content
    manual = UUID(fixed["id"])
    assert (fixed["status"], fixed["manual"]) == ("active", True)
    (content,) = result.content
    assert isinstance(content, TextContent)
    assert content.text.startswith(f"Decision {manual} in workspace alpha: active.")

    await extract(db, world, EXTRACTOR_V2, extracted("Water the beds at six.", world.thread))
    assert (await item(db, world, manual)).status == "active"
    assert (await item(db, world, wrong)).status == "superseded"


# --- Linking, closing, retracting ----------------------------------------------------------------


async def test_items_are_linked_closed_and_retracted_by_hand(
    app: FastAPI, db: Conn, world: World
) -> None:
    async with http_client(app, world.tokens["writer"]) as http:
        question = ok(
            await http.post(
                DECISIONS,
                json=correction(
                    world,
                    kind="open_question",
                    statement="When do we water the beds?",
                    occurred_at=iso(SAID),
                ),
            ),
            201,
        )
        answer = ok(
            await http.post(
                DECISIONS,
                json=correction(
                    world,
                    evidence=[{"record_id": world.later_thread, "anchor": "m2"}],
                    occurred_at=iso(SAID + timedelta(days=1)),
                ),
            ),
            201,
        )
        linked = ok(
            await http.post(
                f"{DECISIONS}/{answer['id']}/supersede",
                json={"supersedes": [{"item_id": question["id"], "relation": "answers"}]},
            )
        )
        assert linked["status"] == "active"
        assert [(link["supersedes_id"], link["relation"]) for link in linked["supersedes"]] == [
            (question["id"], "answers")
        ]
        assert (await item(db, world, question["id"])).status == "closed"

        wrong = ok(
            await http.post(
                DECISIONS,
                json=correction(
                    world,
                    statement="The pump runs at night.",
                    evidence=[{"record_id": world.code, "anchor": f"{COMMIT}#Pump.start"}],
                ),
            ),
            201,
        )
        assert wrong["evidence"][0]["record_kind"] == "code_file"
        retracted = ok(await http.post(f"{DECISIONS}/{wrong['id']}/retract"))
        assert retracted["status"] == "retracted"
        assert retracted["invalidated_at"] is not None

        stale = ok(
            await http.post(DECISIONS, json=correction(world, statement="Fence the beds.")), 201
        )
        closed = ok(await http.post(f"{DECISIONS}/{stale['id']}/close"))
        assert closed["status"] == "closed"
        assert closed["superseded_by"] is None


async def test_the_mcp_tools_link_close_and_retract(app: FastAPI, db: Conn, world: World) -> None:
    async with mcp_client(app, world.tokens["writer"]) as client:

        async def call(tool: str, arguments: Json) -> Json:
            result = await client.call_tool(tool, arguments)
            assert result.is_error is False, result.content
            structured: Json = result.structured_content
            return structured

        older = await call("record_decision", {"workspace": "alpha", **correction(world)})
        newer = await call(
            "record_decision",
            {
                "workspace": "alpha",
                **correction(
                    world,
                    statement="Water the beds at eight.",
                    occurred_at=iso(SAID + timedelta(days=1)),
                    evidence=[{"record_id": world.later_thread, "anchor": "m2"}],
                ),
            },
        )
        linked = await call(
            "supersede_decision",
            {"id": newer["id"], "supersedes": [{"item_id": older["id"]}]},
        )
        assert linked["supersedes"][0]["supersedes_id"] == older["id"]
        assert (await item(db, world, older["id"])).status == "superseded"

        closed = await call("close_decision", {"id": newer["id"]})
        assert closed["status"] == "closed"

        third = await call("record_decision", {"workspace": "alpha", **correction(world)})
        assert (await call("retract_decision", {"id": third["id"]}))["status"] == "retracted"

        again = await client.call_tool("retract_decision", {"id": third["id"]})
    assert again.is_error is True
    (content,) = again.content
    assert isinstance(content, TextContent)
    assert content.text.startswith(f"Knowledge item {third['id']} is retracted, not active")
    assert content.text.endswith("(constraint knowledge_item_status_transition).")


# --- Refusals of the append-only journal ---------------------------------------------------------


async def test_refusals_of_the_journal_are_409s_that_name_the_constraint(
    app: FastAPI, db: Conn, world: World
) -> None:
    async with http_client(app, world.tokens["writer"]) as http:
        old = ok(await http.post(DECISIONS, json=correction(world, occurred_at=iso(SAID))), 201)
        new = ok(
            await http.post(
                DECISIONS,
                json=correction(
                    world,
                    statement="Water at eight.",
                    occurred_at=iso(SAID + timedelta(days=1)),
                    supersedes=[{"item_id": old["id"]}],
                ),
            ),
            201,
        )
        cases = [
            # The superseded item cannot be retracted, closed or superseded again.
            (
                f"{DECISIONS}/{old['id']}/retract",
                None,
                "not_active",
                "knowledge_item_status_transition",
            ),
            (
                f"{DECISIONS}/{old['id']}/close",
                None,
                "not_active",
                "knowledge_item_status_transition",
            ),
            (
                f"{DECISIONS}/{new['id']}/supersede",
                {"supersedes": [{"item_id": old["id"]}]},
                "not_active",
                "knowledge_supersession_active_check",
            ),
            # An item does not supersede itself, nor one said after it.
            (
                f"{DECISIONS}/{new['id']}/supersede",
                {"supersedes": [{"item_id": new["id"]}]},
                "supersession_refused",
                "knowledge_supersession_cycle_check",
            ),
            (
                DECISIONS,
                correction(world, occurred_at=iso(SAID), supersedes=[{"item_id": new["id"]}]),
                "supersession_refused",
                "knowledge_supersession_order_check",
            ),
            # A decision answers an open question only.
            (
                DECISIONS,
                correction(
                    world,
                    occurred_at=iso(SAID + timedelta(days=2)),
                    supersedes=[{"item_id": new["id"], "relation": "answers"}],
                ),
                "supersession_refused",
                "knowledge_supersession_relation_check",
            ),
        ]
        for path, body, code, constraint in cases:
            message = refused(await http.post(path, json=body), 409, code)
            assert message.endswith(f"(constraint {constraint})."), message

    # Nothing changed: the refused writes recorded no item and moved no status.
    assert (await item(db, world, old["id"])).status == "superseded"
    assert (await item(db, world, new["id"])).status == "active"
    assert await scalar(db, "SELECT count(*) FROM knowledge_item") == 2


async def test_a_refusal_by_a_trigger_is_a_journal_refusal(db: Conn, world: World) -> None:
    first = await extract(
        db, world, EXTRACTOR_V1, extracted("Water the beds at six.", world.thread)
    )
    with pytest.raises(DecisionError) as info, decision_errors():
        await db.execute(
            "UPDATE knowledge_item SET statement = 'Water the beds at noon.' WHERE id = %s",
            (first.item_ids[0],),
        )
    assert (info.value.status, info.value.code) == (409, "journal_refused")
    assert info.value.message == (
        "The knowledge journal refused the change (constraint knowledge_item_append_only)."
    )


# --- Evidence and participants -------------------------------------------------------------------


async def test_evidence_and_participants_are_checked(app: FastAPI, db: Conn, world: World) -> None:
    async with http_client(app, world.tokens["writer"]) as http:

        async def post(**changes: Any) -> httpx2.Response:
            return await http.post(DECISIONS, json=correction(world, **changes))

        missing = [{"record_id": "9" * 64, "anchor": "m1"}]
        assert refused(await post(evidence=missing), 404, "not_found") == "No such record."
        elsewhere = [{"record_id": world.beta_thread, "anchor": "m1"}]
        assert refused(await post(evidence=elsewhere), 404, "not_found") == "No such record."
        no_anchor = [{"record_id": world.thread}]
        assert refused(await post(evidence=no_anchor), 422, "invalid_item") == (
            "The item is not valid: evidence: evidence in a thread names the message it is in."
        )
        no_commit = [{"record_id": world.code, "anchor": "Pump.start"}]
        refused(await post(evidence=no_commit), 422, "invalid_item")
        assert refused(await post(participants=[str(world.beta_person)]), 404, "not_found") == (
            "No such person."
        )
        details = await post(evidence=[])
        assert details.status_code == 422
        assert details.json()["error"]["code"] == "validation_error"

        # A document and a thread under one external id are two records, each with its own id.
        await push(
            app,
            world.tokens["pusher"],
            "alpha",
            {
                "kind": "document",
                "external_id": "garden/0001",
                "occurred_at": iso(SAID),
                "payload": {"title": "Watering", "text": "Water at seven."},
            },
        )
        document = await scalar(
            db,
            "SELECT id FROM record WHERE workspace_id = %s AND external_id = %s"
            " AND kind = 'document'",
            world.alpha,
            "garden/0001",
        )
        assert document != world.thread
        evidence = [{"record_id": document}, {"record_id": world.thread, "anchor": "m2"}]
        recorded = ok(await post(evidence=evidence), 201)
    assert [e["record_kind"] for e in recorded["evidence"]] == ["document", "thread"]
    assert await scalar(db, "SELECT count(*) FROM knowledge_item") == 1


# --- Scopes and the workspace limit --------------------------------------------------------------


async def test_writes_need_the_ingest_scope(app: FastAPI, world: World) -> None:
    async with http_client(app, world.tokens["reader"]) as http:
        response = await http.post(DECISIONS, json=correction(world))
    refused(response, 403, "insufficient_scope")

    async with mcp_client(app, world.tokens["reader"]) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert "record_decision" not in listed


async def test_a_token_limited_to_one_workspace_writes_only_there(
    app: FastAPI, db: Conn, world: World
) -> None:
    async with http_client(app, world.tokens["writer"]) as http:
        alpha_item = ok(await http.post(DECISIONS, json=correction(world)), 201)["id"]

    beta = "/v1/workspaces/beta/decisions"
    async with http_client(app, world.tokens["beta_writer"]) as http:
        assert refused(await http.post(DECISIONS, json=correction(world)), 404, "not_found") == (
            "No such workspace."
        )
        for action in ("retract", "close"):
            refused(await http.post(f"{DECISIONS}/{alpha_item}/{action}"), 404, "not_found")
            message = refused(await http.post(f"{beta}/{alpha_item}/{action}"), 404, "not_found")
            assert message == "No such knowledge item."
        # An alpha record cited from beta is not found; a beta record is.
        alpha_record = [{"record_id": world.thread, "anchor": "m1"}]
        refused(
            await http.post(beta, json=correction(world, evidence=alpha_record)), 404, "not_found"
        )
        here = [{"record_id": world.beta_thread, "anchor": "m1"}]
        beta_item = ok(await http.post(beta, json=correction(world, evidence=here)), 201)
        assert beta_item["workspace"] == "beta"
        # Linking across workspaces finds no item to link.
        response = await http.post(
            f"{beta}/{beta_item['id']}/supersede", json={"supersedes": [{"item_id": alpha_item}]}
        )
        assert refused(response, 404, "not_found") == "No such knowledge item."

    async with mcp_client(app, world.tokens["beta_writer"]) as client:
        result = await client.call_tool("retract_decision", {"id": alpha_item})
        recorded = await client.call_tool(
            "record_decision", {"workspace": "alpha", **correction(world)}
        )
    assert result.is_error is True
    assert recorded.is_error is True
    for each, text in ((result, "No such knowledge item."), (recorded, "No such workspace.")):
        (content,) = each.content
        assert isinstance(content, TextContent)
        assert content.text == text

    # An unlimited token still finds an item only in its own workspace's path.
    async with http_client(app, world.tokens["writer"]) as http:
        refused(await http.post(f"{beta}/{alpha_item}/retract"), 404, "not_found")
    assert (await item(db, world, alpha_item)).status == "active"


async def test_journal_reads_and_search(app: FastAPI, db: Conn, world: World) -> None:
    first = await extract(db, world, EXTRACTOR_V1, extracted("Water at six.", world.thread))
    (old,) = first.item_ids
    async with http_client(app, world.tokens["writer"]) as http:
        new = ok(
            await http.post(
                DECISIONS,
                json=correction(
                    world,
                    statement="Distribute marigolds.",
                    participants=[str(world.person)],
                    supersedes=[{"item_id": str(old)}],
                ),
            ),
            201,
        )["id"]
    async with mcp_client(app, world.tokens["reader"]) as client:
        active = await client.call_tool("list_decisions", {"query": "marigolds"})
        assert not active.is_error
        assert [i["id"] for i in active.structured_content["items"]] == [new]
        result = await client.call_tool("get_decision", {"id": str(old)})
        assert not result.is_error
        assert [i["id"] for i in result.structured_content["chain"]] == [str(old), new]
        chat = await scalar(
            db,
            "UPDATE source SET retention_days = 1 WHERE workspace_id = %s"
            " AND name = 'chat' RETURNING id",
            world.alpha,
        )
        await expire_source(db, chat, as_of=SAID + timedelta(days=4))
        expired = await client.call_tool("get_decision", {"id": new})
        assert expired.structured_content["item"]["evidence"][0]["record_state"] == "expired"
        assert expired.structured_content["item"]["evidence"][0]["anchor"] == "m2"

    services: QueryServices = app.state.services
    services.embedder = FakeEmbedder()
    params = {"query": "marigolds", "mode": "lexical", "kinds": "decision"}
    async with http_client(app, world.tokens["reader"]) as http:
        found = ok(await http.get("/v1/search", params=params))
        assert found["items"] == []
        assert [i["id"] for i in found["decisions"]] == [new]
        assert found["decisions"][0]["evidence"][0]["anchor"] == "m2"
        for exclusions in (
            {"persons": str(world.beta_person)},
            {"sources": "other"},
            {"sources": "chat"},
        ):
            assert ok(await http.get("/v1/search", params=params | exclusions))["decisions"] == []
        assert (
            ok(await http.get("/v1/decisions", params={"persons": str(world.beta_person)}))["items"]
            == []
        )
    async with http_client(app, world.tokens["beta_writer"]) as http:
        refused(await http.get(f"/v1/decisions/{new}"), 404, "not_found")
        assert ok(await http.get("/v1/decisions?history=true"))["items"] == []
        assert ok(await http.get("/v1/search", params=params))["decisions"] == []
