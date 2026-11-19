"""M4's acceptance check, against a real Postgres and a scripted LLM: run with
`make test-integration`.

The reversal of ``eval/comms`` (labels/decisions.json, ``scenarios``): thread A (#beacon,
8 September) decides CBOR packets, the telemetry sync of 10 September restates it, and thread B
(#beacon, 23 September) reverses it to gzipped JSON. The three records go through the push API
with extraction on (an LLM provider is configured), and the ``extract`` jobs the API enqueued run
through the service's handler. The fake LLM answers from the labels: an extraction returns the
labelled items and restatements of the record it is shown, with their anchors; a supersession
check says what the labels say of the new item and each candidate shown. So what is checked is
the pipeline around the model: A becomes ``superseded`` with ``invalidated_at`` set, the link is
stored on B only, the default list shows B alone and the history the chain with the evidence of
both threads; the restatement is a duplicate and stores no second item; a link back from A to B,
which would close a cycle, is refused by the library and by the database.

The records and labels are the synthetic fixtures of ``eval/comms`` (SYNTHETIC.md).
"""

import json
import re
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, timedelta
from typing import Any, cast

import httpx2
import pytest
from psycopg import AsyncConnection, errors
from psycopg.rows import TupleRow
from pydantic import SecretStr

from comms_fixtures import RecordRef, decision_labels, final_records
from llm_helpers import chat_answer, llm_settings
from memini.api import create_app
from memini.auth import Principal, ReadFilter, WriteAccess, authorize_read, create_token
from memini.config import Settings
from memini.db import Pool, connect, open_pool
from memini.embeddings import FakeEmbedder
from memini.extraction.job import extract_handler
from memini.jobs import EXTRACT_KIND, execute_job
from memini.jobs.queue import claim
from memini.knowledge import (
    ALL_STATUSES,
    ItemFilters,
    NotActiveError,
    SupersedesRef,
    get_item,
    list_items,
    supersede,
)
from memini.llm import create_llm_gate
from memini.workspaces import ensure_principal

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

type Conn = AsyncConnection[TupleRow]
type Json = dict[str, Any]

WORKSPACE = "harbor"
THREAD_A = RecordRef("chat", "thread", "C0BEACON01/1788858900.000100")
SYNC = RecordRef("meetings", "meeting", "notes-0910-telemetry-sync")
THREAD_B = RecordRef("chat", "thread", "C0BEACON01/1790152800.000100")
RESTATED = "Beacon v2.3 packets go out as CBOR, as the #beacon thread agreed."
"""What the fake extracts from the telemetry sync: the restatement of d-cbor in other words."""

# What the supersession check says of a candidate: the schema's words.
SAME, REPLACED = "same", "replaced"


def _labels() -> Json:
    return decision_labels()


def _item(label: str) -> Json:
    return next(item for item in _labels()["items"] if item["id"] == label)


def _ref(value: Mapping[str, Any]) -> RecordRef:
    return RecordRef(value["source"], value["record_kind"], value["external_id"])


RECORDS = (THREAD_A, SYNC, THREAD_B)
"""In the order they were said."""


def _at(ref: RecordRef) -> str:
    """The record's time as the data block shows it."""
    return final_records()[ref].occurred_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


# --- The fake LLM --------------------------------------------------------------------------------

_BLOCK = re.compile(r"<<<BEGIN DATA ([0-9a-f]{16})>>>\n(.*)\n<<<END DATA \1>>>", re.DOTALL)


def _lines(body: Mapping[str, Any]) -> list[Json]:
    found = _BLOCK.search(body["messages"][-1]["content"])
    assert found is not None
    return [json.loads(line) for line in found.group(2).split("\n")]


@dataclass
class LabelledLlm:
    """Extracts what the labels hold for the record shown, and judges a new item against each
    candidate as the labels relate them."""

    requests: list[Json] = field(default_factory=list[Json])

    def extraction(self, lines: list[Json]) -> list[Json]:
        """The labelled items and the restatement of the record the first line describes,
        known by its time."""
        [ref] = [ref for ref in RECORDS if _at(ref) == lines[0]["at"]]
        found: list[Json] = []
        labels = _labels()
        for item in labels["items"]:
            cited = [e["anchor"] for e in item["evidence"] if _ref(e) == ref]
            if cited:
                found.append({"anchors": cited, "basis": "decided", "statement": item["statement"]})
        for each in labels["restatements"]:
            if _ref(each["record"]) == ref:
                found.append(
                    {"anchors": each["anchors"], "basis": "reported", "statement": RESTATED}
                )
        return found

    def judgement(self, new: str, old: str) -> str | None:
        cbor, gzip = _item("d-cbor")["statement"], _item("d-json-gzip")["statement"]
        if (new, old) == (RESTATED, cbor):
            return SAME
        if (new, old) == (gzip, cbor):
            return REPLACED
        return None

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        lines = _lines(body)
        if body["response_format"]["json_schema"]["name"] == "decisions":
            return chat_answer(json.dumps({"items": self.extraction(lines)}))
        new = lines[0]["statement"]
        matches = [
            {"item": line["n"], "its_value": line["statement"], "verdict": says}
            for line in lines[1:]
            if (says := self.judgement(new, line["statement"])) is not None
        ]
        return chat_answer(json.dumps({"about": "Beacon v2.3 packets", "matches": matches}))

    def kinds(self) -> list[str]:
        return [body["response_format"]["json_schema"]["name"] for body in self.requests]


# --- The service ---------------------------------------------------------------------------------


@pytest.fixture
async def db(migrated_database_url: str) -> AsyncIterator[Conn]:
    async with connect(migrated_database_url, autocommit=True) as conn:
        yield conn


@pytest.fixture
async def pool(migrated_database_url: str) -> AsyncIterator[Pool]:
    async with open_pool(migrated_database_url, min_size=1, max_size=4, timeout=5.0) as pool:
        yield pool


@pytest.fixture
def settings(migrated_database_url: str) -> Settings:
    """A local LLM provider is configured: the push API enqueues extraction."""
    return llm_settings(cloud=False, database_url=SecretStr(migrated_database_url))


@pytest.fixture
async def client(settings: Settings, pool: Pool) -> AsyncIterator[httpx2.AsyncClient]:
    transport = httpx2.ASGITransport(app=create_app(settings, pool=pool))
    async with httpx2.AsyncClient(transport=transport, base_url="http://memini.test") as client:
        yield client


@dataclass(frozen=True)
class Harbor:
    owner: uuid.UUID
    headers: dict[str, str]

    @property
    def access(self) -> WriteAccess:
        return WriteAccess.for_owner(self.owner)

    @property
    def reader(self) -> ReadFilter:
        return authorize_read(
            Principal(
                principal_id=self.owner,
                token_id=uuid.uuid4(),
                scopes=frozenset({"read"}),
                workspace_id=None,
            )
        )


def ok(response: httpx2.Response, status_code: int = 200) -> Json:
    assert response.status_code == status_code, response.text
    return cast(Json, response.json())


@pytest.fixture
async def harbor(db: Conn, client: httpx2.AsyncClient) -> Harbor:
    owner = await ensure_principal(db, "mira")
    token = await create_token(db, owner_id=owner, name="test", scopes=("read", "ingest", "admin"))
    headers = {"Authorization": f"Bearer {token.secret.get_secret_value()}"}
    ok(await client.post("/v1/workspaces", json={"name": WORKSPACE}, headers=headers), 201)
    for source in ("chat", "meetings"):
        ok(
            await client.post(
                f"/v1/workspaces/{WORKSPACE}/sources",
                json={"kind": "push", "name": source},
                headers=headers,
            ),
            201,
        )
    return Harbor(owner, headers)


async def push(client: httpx2.AsyncClient, harbor: Harbor, ref: RecordRef) -> None:
    """The record as it stands after every batch of the fixtures, alone in a batch."""
    record = final_records()[ref]
    body = {"records": [record.item]}
    url = f"/v1/workspaces/{WORKSPACE}/sources/{ref.source}/records"
    ok(await client.post(url, json=body, headers=harbor.headers))


async def run_jobs(db: Conn, pool: Pool, llm: LabelledLlm, embedder: FakeEmbedder | None) -> int:
    """Run the extract jobs waiting, oldest first, as ``serve`` would; how many ran."""
    ran = 0
    transport = httpx2.MockTransport(llm.handle)
    async with create_llm_gate(llm_settings(cloud=False), transport=transport) as gate:
        handlers = {EXTRACT_KIND: extract_handler(pool, gate, embedder=embedder)}
        while job := await claim(
            db, kinds=[EXTRACT_KIND], worker="test/0", lease=timedelta(minutes=1)
        ):
            await execute_job(job, handlers, pool)
            ran += 1
    return ran


async def statuses(db: Conn) -> dict[str, tuple[str, bool]]:
    cur = await db.execute(
        "SELECT statement, status, invalidated_at IS NOT NULL FROM knowledge_item"
    )
    return {row[0]: (row[1], row[2]) for row in await cur.fetchall()}


# --- The check -----------------------------------------------------------------------------------


@pytest.mark.parametrize("embedder", [None, FakeEmbedder()], ids=["lexical", "hybrid"])
async def test_thread_b_supersedes_thread_a_and_the_restatement_is_a_duplicate(
    db: Conn,
    pool: Pool,
    client: httpx2.AsyncClient,
    harbor: Harbor,
    embedder: FakeEmbedder | None,
) -> None:
    llm = LabelledLlm()
    cbor, gzip = _item("d-cbor")["statement"], _item("d-json-gzip")["statement"]
    for ref in RECORDS:
        await push(client, harbor, ref)
        assert await run_jobs(db, pool, llm, embedder) == 1

    # The sync's restatement was checked against A's item and not stored; B's replaced it.
    assert llm.kinds() == ["decisions", "decisions", "supersession", "decisions", "supersession"]
    assert await statuses(db) == {cbor: ("superseded", True), gzip: ("active", False)}
    cur = await db.execute("SELECT count(*) FROM knowledge_extraction")
    assert await cur.fetchone() == (3,)  # the sync was read, and nothing new found there

    [b] = await list_items(db, harbor.reader)  # the default view: active items only
    assert b.statement == gzip
    [a] = [
        i
        for i in await list_items(db, harbor.reader, ItemFilters(statuses=ALL_STATUSES))
        if i.id != b.id
    ]
    assert a.invalidated_at is not None
    assert a.invalidated_at >= b.recorded_at
    assert [(link.item_id, link.supersedes_id, link.relation) for link in b.supersedes] == [
        (b.id, a.id, "replaces")
    ]
    assert a.supersedes == ()  # the link is stored on the newer item only
    assert a.superseded_by is not None
    assert a.superseded_by.item_id == b.id
    cur = await db.execute("SELECT item_id, supersedes_id FROM knowledge_supersession")
    assert await cur.fetchall() == [(b.id, a.id)]

    # The history: the chain from either end, each with the evidence of its thread.
    for start in (a.id, b.id):
        chain = (await get_item(db, harbor.reader, start)).chain
        assert [item.statement for item in chain] == [cbor, gzip]
        assert [{e.external_id for e in item.evidence} for item in chain] == [
            {THREAD_A.external_id},
            {THREAD_B.external_id},
        ]
        assert [{e.anchor for e in item.evidence} for item in chain] == [
            {e["anchor"] for e in _item("d-cbor")["evidence"]},
            {e["anchor"] for e in _item("d-json-gzip")["evidence"]},
        ]

    # A link back from A to B would close a cycle: refused by the library, and by the database.
    with pytest.raises(NotActiveError):
        await supersede(db, harbor.access, a.id, [SupersedesRef(b.id)])
    with pytest.raises(errors.CheckViolation) as refused:
        await db.execute(
            "INSERT INTO knowledge_supersession"
            " (workspace_id, owner_id, item_id, supersedes_id, relation)"
            " VALUES (%s, %s, %s, %s, 'replaces')",
            (b.workspace_id, harbor.owner, a.id, b.id),
        )
    assert refused.value.diag.constraint_name == "knowledge_supersession_cycle_check"

    # Pushed again, nothing changes: no job, no call, no item.
    before = len(llm.requests)
    for ref in RECORDS:
        await push(client, harbor, ref)
    assert await run_jobs(db, pool, llm, embedder) == 0
    assert len(llm.requests) == before
    assert await statuses(db) == {cbor: ("superseded", True), gzip: ("active", False)}
    if embedder is not None:  # each stored statement was embedded once and kept with its item
        cur = await db.execute("SELECT count(*) FROM knowledge_item_vector")
        assert await cur.fetchone() == (2,)
