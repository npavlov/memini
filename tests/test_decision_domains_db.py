"""Decisions by project and domain against a real Postgres, over HTTP, with the assign_domains job
on a fake LLM: the acceptance of memini-qpk.6.

On a synthetic harbour workspace with two projects: a decision from a source with a default is
listed under its project, and under the default's domain; a project and domains pushed with a
record come before the default; a decision recorded with domains is listed under them, and an
unlink removes one; a domain filter returns its subdomains' decisions newest first; the count of
the project's decisions without a domain is right; get_domain lists the domain's decisions; search
returns the decisions of the projects and domains named, subdomains included, the same over HTTP
and MCP, and the threads that belong to them by a push, a default or the model (memini-qpk.13); a
link made by hand survives a run of assign_domains again, and an answer, "none" included, is not
asked again until the catalog changes; a source with llm_policy none is skipped and counted; the
journal tables and their triggers do not change. The library's rules are in
test_decision_context.py.

Every record, name and statement here is synthetic.
"""

import json
import re
from collections.abc import AsyncGenerator, AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, LiteralString
from uuid import UUID

import httpx2
import pytest
from fastapi import FastAPI
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from psycopg import AsyncConnection, sql
from psycopg.rows import TupleRow, dict_row
from pydantic import SecretStr

from llm_helpers import chat_answer, llm_settings
from memini.api import create_app
from memini.auth import Scope, WriteAccess, create_token
from memini.config import Settings
from memini.db import Pool, connect, open_pool
from memini.decisions.assign import LlmAssigner, assign_handler, assigner_name, run_assignment
from memini.embeddings import FakeEmbedder
from memini.evaluation.domains import LlmEvalAssigner, load_set, run_domain_eval
from memini.jobs import ASSIGN_DOMAINS_KIND, execute_job
from memini.jobs.queue import claim
from memini.llm import LlmGate, create_llm_gate
from memini.logs import clear_secrets
from memini.mcp import MCP_PATH
from memini.runs import wait_for_run_writes
from memini.sources import NewPushSource, SourceUpdate, create_push_source, update_source
from memini.workspaces import create_workspace, ensure_principal

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

type Conn = AsyncConnection[TupleRow]
type Json = dict[str, Any]

BASE_URL = "http://memini.test"
T0 = datetime(2026, 5, 4, 9, 0, tzinfo=UTC)
DAY = timedelta(days=1)
ROOT = Path(__file__).resolve().parents[1]
DECISIONS = "/v1/workspaces/harbor/decisions"
JOURNAL = (
    "knowledge_item",
    "knowledge_evidence",
    "knowledge_participant",
    "knowledge_supersession",
    "knowledge_extraction",
)
LINK_TABLES = (
    "knowledge_item_domain",
    "knowledge_domain_assignment",
    "record_catalog",
    "source_catalog",
)

PORT: Json = {
    "title": "Port operations",
    "domains": [
        {
            "name": "fees",
            "title": "Fees",
            "description": "What a call at the harbour costs.",
            "subdomains": [
                {"name": "berth", "title": "Berth fees", "description": "Mooring by the hour."},
                {"name": "pilot", "title": "Pilot fees", "description": "Pilotage by draught."},
            ],
        },
        {"name": "bookings", "title": "Bookings", "description": "Reserving berths online."},
    ],
}
FLEET: Json = {
    "title": "Tug fleet",
    "domains": [
        {"name": "crew", "title": "Crew", "description": "Rosters and shifts of the tugs."},
        {"name": "fees", "title": "Charter fees", "description": "What a tug charter costs."},
    ],
}


@pytest.fixture(autouse=True)
def _forget_secrets() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    yield
    clear_secrets()


# --- The fake LLM --------------------------------------------------------------------------------

_BLOCK = re.compile(r"<<<BEGIN DATA ([0-9a-f]{16})>>>\n(.*)\n<<<END DATA \1>>>", re.DOTALL)


def data_lines(body: Mapping[str, Any]) -> list[dict[str, Any]]:
    found = _BLOCK.search(body["items"][-1]["content"])
    assert found is not None
    return [json.loads(line) for line in found.group(2).split("\n")]


@dataclass
class FakeAssigner:
    """Answers an assignment with the domains ``answers`` gives the statement
    (``project:domain``), none for any other; records every request."""

    answers: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    requests: list[httpx2.Request] = field(default_factory=list[httpx2.Request])

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        body = json.loads(request.content)
        assert body["response_format"]["json_schema"]["name"] == "domain_assignment"
        item, *domains = data_lines(body)
        wanted = self.answers.get(item["statement"], [])
        picks = [d["n"] for d in domains if f"{d['project']}:{d['domain']}" in wanted]
        return chat_answer(json.dumps({"about": "the matter", "picks": picks}))

    def statements(self) -> list[str]:
        return [data_lines(json.loads(r.content))[0]["statement"] for r in self.requests]

    def gate(self) -> LlmGate:
        return create_llm_gate(
            llm_settings(cloud=False), transport=httpx2.MockTransport(self.handle)
        )


# --- The world -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class World:
    owner: UUID
    harbor: UUID
    tokens: dict[str, str]

    @property
    def access(self) -> WriteAccess:
        return WriteAccess.for_owner(self.owner)


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
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        base_url=BASE_URL,
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        yield client


def ok(response: httpx2.Response, status_code: int = 200) -> Json:
    assert response.status_code == status_code, response.text
    body: Json = response.json()
    return body


def refused(response: httpx2.Response, status_code: int, code: str) -> str:
    assert response.status_code == status_code, response.text
    error: Json = response.json()["error"]
    assert error["code"] == code, error
    return str(error["message"])


async def new_token(db: Conn, owner: UUID, *scopes: Scope) -> str:
    token = await create_token(db, owner_id=owner, name="test agent", scopes=scopes)
    return token.secret.get_secret_value()


@pytest.fixture
async def world(app: FastAPI, db: Conn) -> World:
    """harbor: push sources mail and chat, projects port and fleet (both have a domain fees)."""
    owner = await ensure_principal(db, "owner")
    harbor = await create_workspace(db, owner_id=owner, name="harbor")
    access = WriteAccess.for_owner(owner)
    for name in ("mail", "chat"):
        await create_push_source(db, access, "harbor", NewPushSource(name=name))
    tokens = {
        "writer": await new_token(db, owner, "read", "ingest"),
        "admin": await new_token(db, owner, "read", "admin"),
    }
    async with http_client(app, tokens["writer"]) as http:
        for project, document in (("port", PORT), ("fleet", FLEET)):
            ok(await http.put(f"/v1/workspaces/harbor/projects/{project}", json=document))
    return World(owner, harbor.id, tokens)


def thread(external_id: str, at: datetime, text: str, **place: Any) -> Json:
    """A thread of one message, id m1, said at ``at``; ``place`` is project and domains."""
    return {
        "kind": "thread",
        "external_id": external_id,
        "occurred_at": at.isoformat(),
        "payload": {
            "channel": "harbour",
            "messages": [
                {"id": "m1", "author": "Ann Example", "sent_at": at.isoformat(), "text": text}
            ],
        },
        **place,
    }


async def push(
    app: FastAPI, w: World, source: str, *records: Json, stream: str | None = None
) -> None:
    body: Json = {"records": list(records)}
    if stream is not None:
        body["stream"] = stream
    async with http_client(app, w.tokens["writer"]) as http:
        ok(await http.post(f"/v1/workspaces/harbor/sources/{source}/records", json=body))


async def record(
    app: FastAPI,
    w: World,
    source: str,
    external_id: str,
    statement: str,
    at: datetime,
    **place: Any,
) -> Json:
    """A decision recorded by hand, citing message m1 of the thread."""
    body = {
        "statement": statement,
        "decided_at": at.isoformat(),
        "evidence": [{"source": source, "external_id": external_id, "anchor": "m1"}],
        **place,
    }
    async with http_client(app, w.tokens["writer"]) as http:
        return ok(await http.post(DECISIONS, json=body), 201)


async def decisions(app: FastAPI, w: World, **params: Any) -> Json:
    async with http_client(app, w.tokens["writer"]) as http:
        return ok(await http.get("/v1/decisions", params=params))


def ids(listed: Json) -> list[str]:
    return [item["id"] for item in listed["items"]]


def placed(item: Json) -> tuple[Any, ...]:
    context = item["context"]
    return (
        context["project"],
        context["project_via"],
        [(d["project"], d["domain"], d["via"]) for d in context["domains"]],
    )


async def digests(db: Conn, *tables: str) -> dict[str, tuple[int, str]]:
    found: dict[str, tuple[int, str]] = {}
    for table in tables:
        query = sql.SQL(
            "SELECT count(*), md5(coalesce(string_agg(t::text, '|' ORDER BY t::text), ''))"
            " FROM {} t"
        ).format(sql.Identifier(table))
        row = await (await db.execute(query)).fetchone()
        assert row is not None
        found[table] = (row[0], row[1])
    return found


async def rows(db: Conn, query: LiteralString, *params: Any) -> list[dict[str, Any]]:
    async with db.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        return await cur.fetchall()


# --- Defaults and pushed names -------------------------------------------------------------------


async def test_a_source_default_and_a_pushed_project_place_a_decision(
    app: FastAPI, db: Conn, world: World
) -> None:
    async with http_client(app, world.tokens["writer"]) as http:
        default = "/v1/workspaces/harbor/sources/chat/catalog-default"
        ok(await http.put(default, json={"project": "port", "domains": ["bookings"]}))
        stream = {"stream": "inbox", "project": "port"}
        ok(await http.put("/v1/workspaces/harbor/sources/mail/catalog-default", json=stream))
        listed = ok(await http.get("/v1/workspaces/harbor/sources/mail/catalog-default"))
        assert [(d["stream"], d["project"], d["domains"]) for d in listed["defaults"]] == [
            ("inbox", "port", [])
        ]
        refused(
            await http.put(default, json={"project": "port", "domains": ["crew"]}),
            422,
            "unknown_place",
        )

    await push(app, world, "chat", thread("c1", T0, "Book berths a week ahead."))
    await push(
        app,
        world,
        "chat",
        thread("c2", T0, "Crews swap through the roster.", project="fleet", domains=["crew"]),
    )
    await push(app, world, "mail", thread("m1", T0, "Pilots on call at night."), stream="inbox")
    await push(app, world, "mail", thread("m2", T0, "The canteen opens at seven."))

    by_default = await record(app, world, "chat", "c1", "Berths are booked a week ahead.", T0)
    by_push = await record(app, world, "chat", "c2", "Crews swap through the roster.", T0)
    by_stream = await record(app, world, "mail", "m1", "Pilots are on call at night.", T0)
    nowhere = await record(app, world, "mail", "m2", "The canteen opens at seven.", T0)

    assert placed(by_default) == ("port", "default", [("port", "bookings", "default")])
    assert placed(by_push) == ("fleet", "push", [("fleet", "crew", "push")])
    assert placed(by_stream) == ("port", "default", [])
    assert placed(nowhere) == (None, None, [])

    port = await decisions(app, world, projects=["port"])
    assert sorted(ids(port)) == sorted([by_default["id"], by_stream["id"]])
    assert port["without_domain"] == [{"project": "port", "items": 1}]
    bookings = await decisions(app, world, domains=["bookings"])
    assert ids(bookings) == [by_default["id"]]
    assert ids(await decisions(app, world, projects=["fleet"], domains=["crew"])) == [by_push["id"]]
    # The pushed names follow the record: pushed again without them, it takes the default.
    await push(app, world, "chat", thread("c2", T0, "Crews swap through the roster."))
    assert ids(await decisions(app, world, domains=["bookings"])) == sorted(
        [by_default["id"], by_push["id"]], reverse=True
    )
    assert await rows(db, "SELECT record_id FROM record_catalog") == []


# --- Links by hand -------------------------------------------------------------------------------


async def test_a_decision_recorded_with_domains_is_listed_under_them_and_unlink_removes_one(
    app: FastAPI, world: World
) -> None:
    await push(app, world, "mail", thread("m1", T0, "Berths by the hour, booked online."))
    item = await record(
        app,
        world,
        "mail",
        "m1",
        "Berths are charged by the hour and booked online.",
        T0,
        project="port",
        domains=["fees/berth", "bookings"],
    )
    assert placed(item) == (
        "port",
        "manual",
        [("port", "bookings", "manual"), ("port", "fees/berth", "manual")],
    )
    assert ids(await decisions(app, world, domains=["fees"], projects=["port"])) == [item["id"]]
    assert ids(await decisions(app, world, domains=["bookings"])) == [item["id"]]

    links = f"{DECISIONS}/{item['id']}/domains"
    async with http_client(app, world.tokens["writer"]) as http:
        after = ok(await http.delete(f"{links}/bookings", params={"project": "port"}))
        assert placed(after) == ("port", "manual", [("port", "fees/berth", "manual")])
        refused(
            await http.delete(f"{links}/bookings", params={"project": "port"}), 409, "not_a_link"
        )
        linked = ok(await http.post(links, json={"domains": ["crew"]}))
        assert ("fleet", "crew", "manual") in placed(linked)[2]
        # fees is a domain of both projects: without one named, it is refused.
        refused(await http.post(links, json={"domains": ["fees"]}), 422, "unknown_place")
        refused(await http.post(links, json={"domains": ["nowhere"]}), 422, "unknown_place")
        refused(await http.post(links, json={"domains": ["Not A Name"]}), 422, "invalid_place")
    assert ids(await decisions(app, world, domains=["bookings"])) == []
    assert ids(await decisions(app, world, domains=["crew"])) == [item["id"]]


# --- Filters, order and counts -------------------------------------------------------------------


async def test_a_domain_filter_returns_its_subdomains_newest_first_and_counts_the_rest(
    app: FastAPI, world: World
) -> None:
    await push(app, world, "mail", thread("m1", T0, "Harbour matters."))
    said: dict[str, Json] = {}
    for n, (name, place) in enumerate(
        (
            ("berth", {"project": "port", "domains": ["fees/berth"]}),
            ("pilot", {"project": "port", "domains": ["fees/pilot"]}),
            ("fees", {"project": "port", "domains": ["fees"]}),
            ("bare", {"project": "port"}),
            ("charter", {"project": "fleet", "domains": ["fees"]}),
        )
    ):
        said[name] = await record(
            app, world, "mail", "m1", f"Decision {name}.", T0 + n * DAY, **place
        )

    port_fees = await decisions(app, world, domains=["fees"], projects=["port"])
    assert ids(port_fees) == [said[n]["id"] for n in ("fees", "pilot", "berth")]
    assert port_fees["without_domain"] == [{"project": "port", "items": 1}]
    first_two = await decisions(app, world, domains=["fees"], projects=["port"], limit=2)
    assert ids(first_two) == [said["fees"]["id"], said["pilot"]["id"]]
    every_fees = await decisions(app, world, domains=["fees"])
    assert ids(every_fees) == [said[n]["id"] for n in ("charter", "fees", "pilot", "berth")]
    assert {(c["project"], c["items"]) for c in every_fees["without_domain"]} == {
        ("port", 1),
        ("fleet", 0),
    }
    assert ids(await decisions(app, world, domains=["fees/berth"])) == [said["berth"]["id"]]

    async with http_client(app, world.tokens["writer"]) as http:
        refused(
            await http.get("/v1/decisions", params={"domains": ["tides"]}), 422, "unknown_place"
        )
        domain = ok(await http.get("/v1/workspaces/harbor/projects/port/domains/fees"))
        assert [d["id"] for d in domain["decisions"]] == ids(port_fees)
        assert domain["decisions_total"] == 3
        berth = ok(await http.get("/v1/workspaces/harbor/projects/port/domains/fees/berth"))
        assert ([d["id"] for d in berth["decisions"]], berth["decisions_total"]) == (
            [said["berth"]["id"]],
            1,
        )
        # get_decision carries the context too.
        chain = ok(await http.get(f"/v1/decisions/{said['pilot']['id']}"))
        assert placed(chain["item"]) == ("port", "manual", [("port", "fees/pilot", "manual")])


# --- The decisions of search ---------------------------------------------------------------------


@asynccontextmanager
async def search_clients(
    migrated_database_url: str, pool: Pool, token: str
) -> AsyncGenerator[tuple[httpx2.AsyncClient, Client]]:
    """An HTTP and an MCP client of one application with an embedder, as search needs one."""
    settings = Settings(database_url=SecretStr(migrated_database_url))
    searching = create_app(settings, pool=pool, embedder=FakeEmbedder())
    async with (
        searching.router.lifespan_context(searching),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=searching),
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {token}"},
        ) as http,
    ):
        transport = streamable_http_client(f"{BASE_URL}{MCP_PATH}", http_client=http)
        async with Client(transport, mode="legacy") as mcp:
            yield http, mcp


async def test_search_returns_the_decisions_of_the_projects_and_domains_named(
    app: FastAPI, migrated_database_url: str, pool: Pool, world: World
) -> None:
    await push(app, world, "mail", thread("m1", T0, "Harbour matters."))
    await push(app, world, "chat", thread("c1", T0, "Crew matters.", project="fleet"))
    said: dict[str, str] = {}
    for n, (name, source, external_id, place) in enumerate(
        (
            ("berth", "mail", "m1", {"project": "port", "domains": ["fees/berth"]}),
            ("bookings", "mail", "m1", {"project": "port", "domains": ["bookings"]}),
            # Its project comes by the record, its domain by a link: both count.
            ("crew", "chat", "c1", {"domains": ["crew"]}),
            ("nowhere", "mail", "m1", {}),
        )
    ):
        item = await record(
            app, world, source, external_id, f"Tide fees for {name}.", T0 + n * DAY, **place
        )
        said[item["id"]] = name

    async with search_clients(migrated_database_url, pool, world.tokens["writer"]) as (http, mcp):

        async def found(**filters: Any) -> list[str]:
            arguments = {"query": "tide fees", "mode": "lexical", **filters}
            over_http = ok(await http.get("/v1/search", params=arguments))
            result = await mcp.call_tool("search", arguments)
            assert not result.is_error, result.content
            assert result.structured_content is not None
            over_mcp: Json = result.structured_content
            listed = [item["id"] for item in over_http["decisions"]]
            assert [item["id"] for item in over_mcp["decisions"]] == listed
            return sorted(said[item_id] for item_id in listed)

        assert await found() == ["berth", "bookings", "crew", "nowhere"]
        # A domain includes its subdomains; fees of fleet holds none of these.
        assert await found(domains=["fees"]) == ["berth"]
        assert await found(domains=["fees/berth"]) == ["berth"]
        assert await found(domains=["fees/pilot"]) == []
        assert await found(domains=["crew"]) == ["crew"]
        assert await found(domains=["bookings", "crew"]) == ["bookings", "crew"]
        assert await found(projects=["port"]) == ["berth", "bookings"]
        assert await found(projects=["fleet"]) == ["crew"]
        assert await found(projects=["fleet"], domains=["fees"]) == []
        assert await found(domains=["fees"], kinds=["decision"]) == ["berth"]


# --- The threads of search ----------------------------------------------------------------------


async def test_search_by_a_project_or_domain_finds_the_threads_that_belong_to_it(
    app: FastAPI, db: Conn, migrated_database_url: str, pool: Pool, world: World
) -> None:
    """memini-qpk.13: a thread pushed with a domain is found by that domain and not by another, a
    subdomain counts for its domain, and so do a source default and, for a thread with no domain
    of its own, the model's domains of the decisions taken from it; HTTP and MCP agree."""
    async with search_clients(migrated_database_url, pool, world.tokens["writer"]) as (http, mcp):

        async def push_indexed(source: str, *records: Json) -> None:
            url = f"/v1/workspaces/harbor/sources/{source}/records"
            ok(await http.post(url, json={"records": list(records)}))

        default = "/v1/workspaces/harbor/sources/chat/catalog-default"
        ok(await http.put(default, json={"project": "port", "domains": ["bookings"]}))
        await push_indexed(
            "mail",
            thread(
                "berth",
                T0,
                "Tide notes: berths by the hour.",
                project="port",
                domains=["fees/berth"],
            ),
            thread("pilot", T0, "Tide notes: pilots at night."),
            thread("canteen", T0, "Tide notes: the canteen opens at seven."),
        )
        await push_indexed(
            "chat",
            thread("crew", T0, "Tide notes: crews swap shifts.", project="fleet", domains=["crew"]),
            thread("booking", T0, "Tide notes: berths booked a week ahead."),
        )
        # The pilot thread has no domain of its own: a decision taken from it gets one from the
        # model, which names a domain and its own subdomain; only the subdomain is kept
        # (memini-qpk.12).
        await record(app, world, "mail", "pilot", "Pilots are on call at night.", T0)
        fake = FakeAssigner(
            answers={"Pilots are on call at night.": ["port:fees", "port:fees/pilot"]}
        )
        async with fake.gate() as gate:
            await assign(db, world, gate)
        model = await rows(
            db, "SELECT project, domain FROM knowledge_item_domain WHERE origin = 'model'"
        )
        assert model == [{"project": "port", "domain": "fees/pilot"}]

        async def found(**filters: Any) -> list[str]:
            arguments = {"query": "tide notes", "mode": "lexical", "kinds": ["thread"], **filters}
            over_http = ok(await http.get("/v1/search", params=arguments))
            result = await mcp.call_tool("search", arguments)
            assert not result.is_error, result.content
            assert result.structured_content is not None
            over_mcp: Json = result.structured_content
            listed = sorted({hit["external_id"] for hit in over_http["items"]})
            assert sorted({hit["external_id"] for hit in over_mcp["items"]}) == listed
            return listed

        assert await found() == ["berth", "booking", "canteen", "crew", "pilot"]
        assert await found(domains=["fees/berth"]) == ["berth"]
        assert await found(domains=["crew"]) == ["crew"]
        # A domain includes its subdomains, by a push or by the model.
        assert await found(domains=["fees"]) == ["berth", "pilot"]
        assert await found(domains=["fees/pilot"]) == ["pilot"]
        assert await found(domains=["bookings"]) == ["booking"]
        assert await found(domains=["bookings", "crew"]) == ["booking", "crew"]
        assert await found(projects=["port"]) == ["berth", "booking", "pilot"]
        assert await found(projects=["fleet"]) == ["crew"]
        assert await found(projects=["fleet"], domains=["fees"]) == []
        assert await found(projects=["port"], domains=["fees"]) == ["berth", "pilot"]
        # The crew thread is in chat: a filter on mail leaves it out.
        assert await found(sources=["mail"], domains=["crew"]) == []

        # The record's own domain comes first: pushed with one, the model's no longer counts.
        await push_indexed(
            "mail",
            thread(
                "pilot", T0, "Tide notes: pilots at night.", project="port", domains=["bookings"]
            ),
        )
        assert await found(domains=["fees"]) == ["berth"]
        assert await found(domains=["bookings"]) == ["booking", "pilot"]


# --- assign_domains ------------------------------------------------------------------------------


async def assign(db: Conn, world: World, gate: LlmGate) -> Any:
    return await run_assignment(
        db, LlmAssigner(gate), world.access, workspace_id=world.harbor, record_id=None
    )


async def test_a_manual_link_survives_assign_domains_and_an_answer_is_not_asked_again(
    app: FastAPI, db: Conn, world: World
) -> None:
    await push(app, world, "mail", thread("m1", T0, "Harbour matters."))
    manual = await record(
        app, world, "mail", "m1", "Pilots are billed by draught.", T0, domains=["fees/pilot"]
    )
    free = await record(app, world, "mail", "m1", "Berths are booked online.", T0 + DAY)
    chatter = await record(app, world, "mail", "m1", "The canteen opens at seven.", T0 + DAY)
    fake = FakeAssigner(
        answers={
            "Berths are booked online.": ["port:bookings"],
            # Asked, the model would move the manual item: it is never asked.
            "Pilots are billed by draught.": ["fleet:crew"],
        }
    )

    async with fake.gate() as gate:
        first = await assign(db, world, gate)
        assert (first.settled, first.assigned, first.none) == (1, 1, 1)
        assert sorted(fake.statements()) == sorted(
            ["Berths are booked online.", "The canteen opens at seven."]
        )
        assert first.assigner == assigner_name("fake-local-model:7b")

        again = await assign(db, world, gate)
        assert (again.settled, again.unchanged, again.assigned, again.none) == (1, 2, 0, 0)
        assert len(fake.requests) == 2

    listed = {item["id"]: item for item in (await decisions(app, world))["items"]}
    assert placed(listed[manual["id"]]) == ("port", "manual", [("port", "fees/pilot", "manual")])
    assert placed(listed[free["id"]]) == ("port", "model", [("port", "bookings", "model")])
    assert placed(listed[chatter["id"]]) == (None, None, [])
    answers = await rows(
        db, "SELECT item_id::text AS item, answer FROM knowledge_domain_assignment ORDER BY 1"
    )
    assert {row["item"]: row["answer"] for row in answers} == {
        free["id"]: "domains",
        chatter["id"]: "none",
    }
    model = await rows(
        db, "SELECT assigner, created_by FROM knowledge_item_domain WHERE origin = 'model'"
    )
    assert model == [{"assigner": assigner_name("fake-local-model:7b"), "created_by": world.owner}]

    # A person links the model's item by hand: the model's link no longer shows.
    async with http_client(app, world.tokens["writer"]) as http:
        linked = ok(
            await http.post(
                f"{DECISIONS}/{free['id']}/domains", json={"project": "port", "domains": ["fees"]}
            )
        )
    assert placed(linked) == ("port", "manual", [("port", "fees", "manual")])

    # The catalog changes: the items not settled are asked again, the others are not.
    changed = json.loads(json.dumps(PORT))
    changed["domains"][1]["description"] = "Reserving berths online, and cancelling them."
    async with http_client(app, world.tokens["writer"]) as http:
        ok(await http.put("/v1/workspaces/harbor/projects/port", json=changed))
    fake.requests.clear()
    async with fake.gate() as gate:
        third = await assign(db, world, gate)
    assert fake.statements() == ["The canteen opens at seven."]
    assert (third.settled, third.none) == (2, 1)


async def test_a_source_with_llm_policy_none_is_skipped_and_counted(
    app: FastAPI, db: Conn, pool: Pool, world: World
) -> None:
    await push(app, world, "mail", thread("m1", T0, "Berths online."))
    await record(app, world, "mail", "m1", "Berths are booked online.", T0)
    await update_source(db, world.access, "harbor", "mail", SourceUpdate(llm_policy="none"))

    async with http_client(app, world.tokens["admin"]) as http:
        accepted = ok(await http.post("/v1/workspaces/harbor/assign-domains"), 202)
        assert accepted["created"] is True
        again = ok(await http.post("/v1/workspaces/harbor/assign-domains"))
        assert again["job_id"] == accepted["job_id"]
    fake = FakeAssigner(answers={"Berths are booked online.": ["port:bookings"]})
    async with fake.gate() as gate:
        job = await claim(
            db, kinds=[ASSIGN_DOMAINS_KIND], worker="test/0", lease=timedelta(minutes=1)
        )
        assert job is not None
        await execute_job(job, {ASSIGN_DOMAINS_KIND: assign_handler(pool, gate)}, pool)
    await wait_for_run_writes()

    assert fake.requests == []
    [run] = await rows(db, "SELECT status, report FROM run WHERE kind = %s", ASSIGN_DOMAINS_KIND)
    assert run["status"] == "succeeded"
    assert run["report"]["counts"]["items_skipped_policy_none"] == 1
    assert run["report"]["counts"]["items_assigned"] == 0
    assert await rows(db, "SELECT * FROM knowledge_item_domain") == []
    assert await rows(db, "SELECT * FROM knowledge_domain_assignment") == []


# --- The journal ---------------------------------------------------------------------------------


async def test_the_journal_tables_and_their_triggers_do_not_change(
    app: FastAPI, db: Conn, world: World
) -> None:
    await push(app, world, "mail", thread("m1", T0, "Berths online."))
    item = await record(app, world, "mail", "m1", "Berths are booked online.", T0)
    other = await record(app, world, "mail", "m1", "The canteen opens at seven.", T0)
    before = await digests(db, *JOURNAL)

    links = f"{DECISIONS}/{item['id']}/domains"
    async with http_client(app, world.tokens["writer"]) as http:
        ok(await http.post(links, json={"project": "port", "domains": ["bookings"]}))
        ok(await http.delete(f"{links}/bookings", params={"project": "port"}))
        project_only = ok(await http.post(links, json={"project": "port"}))
        assert placed(project_only) == ("port", "manual", [])
        ok(await http.delete(f"{DECISIONS}/{item['id']}/project/port"))
        ok(
            await http.put(
                "/v1/workspaces/harbor/sources/mail/catalog-default", json={"project": "fleet"}
            )
        )
        ok(await http.delete("/v1/workspaces/harbor/sources/mail/catalog-default"))
    fake = FakeAssigner(answers={"The canteen opens at seven.": ["fleet:crew"]})
    async with fake.gate() as gate:
        await assign(db, world, gate)
    assert ("fleet", "crew", "model") in placed(
        next(i for i in (await decisions(app, world))["items"] if i["id"] == other["id"])
    )[2]

    assert await digests(db, *JOURNAL) == before
    # The triggers of the journal are those 0008 made, and the new tables have none.
    migration = (ROOT / "src/memini/db/migrations/0008_knowledge.sql").read_text("utf-8")
    made = set(re.findall(r"CREATE (?:CONSTRAINT )?TRIGGER (\w+)\s+.*?\bON (\w+)", migration, re.S))
    found = await rows(
        db,
        "SELECT t.tgname AS name, c.relname AS table FROM pg_trigger t"
        " JOIN pg_class c ON c.oid = t.tgrelid"
        " WHERE NOT t.tgisinternal AND c.relname = ANY(%s)",
        [*JOURNAL, *LINK_TABLES],
    )
    assert {(row["name"], row["table"]) for row in found} == made
    later = sorted((ROOT / "src/memini/db/migrations").glob("0020_*.sql"))
    for path in later:
        text = path.read_text("utf-8")
        assert "TRIGGER" not in text
        assert not re.search(
            r"ALTER TABLE knowledge_(item|evidence|participant|supersession|extraction)\b", text
        )


# --- The eval on the gate ------------------------------------------------------------------------


async def test_the_domain_eval_runs_the_job_call_through_the_gate(
    migrated_database_url: str,
) -> None:
    data = load_set(ROOT / "eval" / "domains" / "set.json")
    expected = {case.statement: sorted(f"{p}:{d}" for p, d in case.expected) for case in data.cases}
    fake = FakeAssigner(answers=expected)
    settings = llm_settings(cloud=False, database_url=SecretStr(migrated_database_url))
    assigner = LlmEvalAssigner(settings, transport=httpx2.MockTransport(fake.handle))
    try:
        report = await run_domain_eval(data, assigner)
    finally:
        await assigner.aclose()

    assert report.accuracy == 1.0
    assert report.none_rate == report.expected_none_rate
    assert len(fake.requests) == len(data.cases)
    assert report.assigner == assigner_name("fake-local-model:7b")
