"""Re-extraction against a real Postgres and a fake LLM: run with `make test-integration`.

``memini extract`` (:mod:`memini.extraction.reextract`, and its route
``POST /v1/workspaces/{workspace}/extract``): the backfill of records stored before an LLM
provider was configured, and the re-run over every record after the extractor changes. Each
record is enqueued once, oldest first, and a second run enqueues nothing; records the current
extractor has read, and sources no LLM may read, are skipped; items recorded by hand are never
touched; a new reading does not bring back an item that was replaced, answered or retracted. The
LLM is a fake OpenAI-compatible server (tests/llm_helpers.py) that answers by the content of the
request. Every name, id and text here is synthetic.
"""

import json
import re
import uuid
from collections.abc import AsyncIterator, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2
import pytest
from psycopg import AsyncConnection
from psycopg.rows import TupleRow, dict_row
from pydantic import SecretStr

from llm_helpers import LOCAL_MODEL, chat_answer, llm_settings
from memini.api import create_app
from memini.auth import Scope, WriteAccess, create_token
from memini.config import Settings
from memini.db import Pool, connect, open_pool
from memini.embeddings import FakeEmbedder
from memini.extraction.job import extract_handler, run_extraction
from memini.extraction.llm import LlmDecisions, extractor_name
from memini.extraction.reextract import Reextraction, ReextractionCounts, enqueue_reextraction
from memini.ids import RecordKind, record_id
from memini.ingest import Batch, RecordIn, ingest
from memini.jobs import EXTRACT_KIND, execute_job, extraction_dedupe_key
from memini.jobs.queue import claim, complete, unlock_source
from memini.knowledge import EvidenceRef, NewItem, record_item, retract_item
from memini.llm import LlmGate, configured_providers, create_llm_gate, set_workspace_llm_policy
from memini.logs import clear_secrets
from memini.runs import wait_for_run_writes
from memini.sources import NewPushSource, SourceUpdate, create_push_source, update_source
from memini.workspaces import NotFoundError, create_workspace, ensure_principal

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

type Conn = AsyncConnection[TupleRow]
type Json = dict[str, Any]

T0 = datetime(2026, 4, 6, 9, 0, tzinfo=UTC)
DAY = timedelta(days=1)
MINUTE = timedelta(minutes=1)
LOCAL_ONLY = llm_settings(cloud=False)


@pytest.fixture(autouse=True)
def _forget_secrets() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    yield
    clear_secrets()


def iso(when: datetime) -> str:
    return when.isoformat().replace("+00:00", "Z")


# --- The fake LLM --------------------------------------------------------------------------------

_BLOCK = re.compile(r"<<<BEGIN DATA ([0-9a-f]{16})>>>\n(.*)\n<<<END DATA \1>>>", re.DOTALL)


def data_lines(body: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The JSON lines of the data block of a request's user message."""
    found = _BLOCK.search(body["messages"][-1]["content"])
    assert found is not None
    return [json.loads(line) for line in found.group(2).split("\n")]


@dataclass
class FakeLlm:
    """Answers an extraction with the items of ``readings`` for the record whose first part has
    that text (none: no items), and a supersession check of a new item with what ``verdicts``
    says for its statement: existing items named by their statement, each with what it says.
    Records every request body."""

    readings: dict[str, list[dict[str, Any]]] = field(default_factory=dict[str, list[Json]])
    verdicts: dict[str, dict[str, str]] = field(default_factory=dict[str, dict[str, str]])
    bodies: list[Json] = field(default_factory=list[Json])

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        lines = data_lines(body)
        if body["response_format"]["json_schema"]["name"] == "decisions":
            return chat_answer(json.dumps({"items": self.readings.get(lines[1]["text"], [])}))
        shown = {line["statement"]: line["n"] for line in lines[1:]}
        verdict = self.verdicts.get(lines[0]["statement"], {})
        matches = [
            {"item": shown[statement], "its_value": "-", "verdict": says}
            for statement, says in verdict.items()
            if statement in shown
        ]
        return chat_answer(json.dumps({"about": "the matter", "matches": matches}))

    def extractions(self) -> int:
        return sum(
            body["response_format"]["json_schema"]["name"] == "decisions" for body in self.bodies
        )

    def checks(self) -> list[tuple[str, list[str]]]:
        """Each supersession check: the new item's statement and the statements shown with it."""
        found: list[tuple[str, list[str]]] = []
        for body in self.bodies:
            if body["response_format"]["json_schema"]["name"] == "supersession":
                lines = data_lines(body)
                found.append((lines[0]["statement"], [line["statement"] for line in lines[1:]]))
        return found

    def gate(self, settings: Settings = LOCAL_ONLY) -> LlmGate:
        return create_llm_gate(settings, transport=httpx2.MockTransport(self.handle))


def decided(anchors: list[str], statement: str, basis: str = "decided") -> Json:
    return {"anchors": anchors, "basis": basis, "statement": statement}


# --- The world -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class World:
    owner: uuid.UUID
    workspace: uuid.UUID
    chat: uuid.UUID  # push source, llm_policy local
    notes: uuid.UUID  # push source, llm_policy local

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
async def world(db: Conn) -> World:
    owner = await ensure_principal(db, "owner")
    workspace = await create_workspace(db, owner_id=owner, name="orchard")
    access = WriteAccess.for_owner(owner)
    chat = await create_push_source(db, access, "orchard", NewPushSource(name="chat"))
    notes = await create_push_source(db, access, "orchard", NewPushSource(name="notes"))
    return World(owner=owner, workspace=workspace.id, chat=chat.id, notes=notes.id)


def thread(external_id: str, start: datetime, *texts: str) -> RecordIn:
    """A thread of ``texts``, one message a minute from ``start``, ids ``m1``, ``m2``, …"""
    messages = [
        {
            "id": f"m{n}",
            "author": "Ann Example" if n % 2 else "Bo Sample",
            "sent_at": iso(start + timedelta(minutes=n)),
            "text": text,
        }
        for n, text in enumerate(texts, start=1)
    ]
    return RecordIn(
        kind="thread",
        external_id=external_id,
        occurred_at=start + timedelta(minutes=len(texts)),
        payload={"channel": "orchard", "messages": messages},
    )


def meeting(external_id: str, start: datetime, summary: str) -> RecordIn:
    return RecordIn(
        kind="meeting",
        external_id=external_id,
        occurred_at=start,
        payload={
            "title": "Harvest planning",
            "started_at": iso(start),
            "participants": ["Ann Example", "Bo Sample"],
            "summary": summary,
        },
    )


async def push(
    db: Conn, world: World, source: str, *records: RecordIn, extract: bool = False
) -> None:
    """Store ``records``; without ``extract``, as ingest does while no provider is configured."""
    await ingest(db, world.access, "orchard", source, Batch(records=list(records)), extract=extract)


def rid(world: World, external_id: str, source: str = "chat") -> str:
    kind: RecordKind = "thread" if source == "chat" else "meeting"
    return record_id(world.chat if source == "chat" else world.notes, kind, external_id)


async def reextract(
    db: Conn,
    world: World,
    settings: Settings = LOCAL_ONLY,
    **options: Any,
) -> Reextraction:
    return await enqueue_reextraction(
        db, world.access, "orchard", configured_providers(settings), **options
    )


def counts(result: Reextraction | ReextractionCounts) -> tuple[int, int, int, int, int]:
    """records, current, enqueued, queued, skipped."""
    totals = result.totals if isinstance(result, Reextraction) else result
    return totals.records, totals.current, totals.enqueued, totals.queued, totals.skipped


async def rows(db: Conn, query: Any, params: Any = ()) -> list[dict[str, Any]]:
    async with db.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        return await cur.fetchall()


async def queued_jobs(db: Conn) -> list[str]:
    """The record of each queued extract job, in the order a worker takes them."""
    found = await rows(
        db,
        "SELECT args FROM job WHERE kind = %s AND status = 'queued' ORDER BY run_after, id",
        (EXTRACT_KIND,),
    )
    return [row["args"]["record_id"] for row in found]


async def drain(db: Conn, pool: Pool, gate: LlmGate) -> list[str]:
    """Run every queued extract job as a worker would, in its order; the records they read."""
    handlers = {EXTRACT_KIND: extract_handler(pool, gate)}
    ran: list[str] = []
    while True:
        job = await claim(db, kinds=[EXTRACT_KIND], worker="test/0", lease=timedelta(minutes=1))
        if job is None:
            break
        await execute_job(job, handlers, pool)
        assert await complete(db, job)
        assert job.source_id is not None
        await unlock_source(db, job.source_id)
        ran.append(job.args["record_id"])
    await wait_for_run_writes()
    return ran


async def items(db: Conn) -> list[dict[str, Any]]:
    """Every knowledge item, in the order recorded."""
    return await rows(
        db,
        "SELECT id, kind, statement, status, extractor, invalidated_at FROM knowledge_item"
        " ORDER BY recorded_at, id",
    )


async def extractions(db: Conn, extractor: str) -> list[str]:
    found = await rows(
        db,
        "SELECT record_id FROM knowledge_extraction WHERE extractor = %s ORDER BY record_id",
        (extractor,),
    )
    return [row["record_id"] for row in found]


# --- The backfill --------------------------------------------------------------------------------


async def test_records_stored_without_a_provider_are_enqueued_once_and_read_once(
    db: Conn, world: World, pool: Pool
) -> None:
    await push(
        db,
        world,
        "chat",
        thread("t1", T0, "Shall we pick on Monday?", "Agreed, Monday."),
        thread("t2", T0 + DAY, "Crates are green."),
        thread("t3", T0 + DAY, "A thread deleted at its source."),
        RecordIn(
            kind="document",
            external_id="d1",
            occurred_at=T0,
            payload={"title": "Handbook", "text": "Picking rules."},
        ),
    )
    await push(db, world, "chat", RecordIn(kind="thread", external_id="t3", deleted=True))
    await push(db, world, "notes", meeting("h1", T0 + 2 * DAY, "Harvest ends on Friday."))
    assert await queued_jobs(db) == []  # no provider: ingest enqueued nothing

    first = await reextract(db, world)
    # Threads and meetings, live: not the document, not the tombstone.
    assert counts(first) == (3, 0, 3, 0, 0)
    assert [(s.name, s.extractor, s.skipped) for s in first.sources] == [
        ("chat", extractor_name(LOCAL_MODEL), None),
        ("notes", extractor_name(LOCAL_MODEL), None),
    ]
    t1, t2, h1 = rid(world, "t1"), rid(world, "t2"), rid(world, "h1", "notes")
    assert await queued_jobs(db) == [t1, t2, h1]  # oldest first
    [job] = await rows(db, "SELECT * FROM job WHERE args->>'record_id' = %s", (t1,))
    [record] = await rows(db, "SELECT content_hash FROM record WHERE id = %s", (t1,))
    assert job["dedupe_key"] == extraction_dedupe_key(t1, record["content_hash"])
    assert job["source_id"] == world.chat

    second = await reextract(db, world)  # the jobs wait: not doubled
    assert counts(second) == (3, 0, 0, 3, 0)
    assert await queued_jobs(db) == [t1, t2, h1]

    fake = FakeLlm(readings={"Shall we pick on Monday?": [decided(["m2"], "Monday.")]})
    async with fake.gate() as gate:
        assert await drain(db, pool, gate) == [t1, t2, h1]
    assert fake.extractions() == 3
    assert await extractions(db, extractor_name(LOCAL_MODEL)) == sorted([t1, t2, h1])

    third = await reextract(db, world)  # read by the current extractor: nothing to do
    assert counts(third) == (3, 3, 0, 0, 0)
    assert await queued_jobs(db) == []


# --- A new extractor version ---------------------------------------------------------------------


async def test_after_a_version_bump_each_record_is_read_once_and_manual_items_stay(
    db: Conn, world: World, pool: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    await push(
        db,
        world,
        "chat",
        thread("t1", T0, "Shall we pick on Monday?", "Agreed, Monday."),
        thread("t2", T0 + DAY, "The van leaves at six."),
        extract=True,
    )
    await push(
        db, world, "notes", meeting("h1", T0 + 2 * DAY, "Harvest ends on Friday."), extract=True
    )
    t1, t2, h1 = rid(world, "t1"), rid(world, "t2"), rid(world, "h1", "notes")
    v1 = extractor_name(LOCAL_MODEL)
    first_reading = FakeLlm(
        readings={
            "Shall we pick on Monday?": [decided(["m2"], "Picking starts on Monday.")],
            "Harvest ends on Friday.": [decided(["summary"], "Harvest ends on Friday.")],
        }
    )
    async with first_reading.gate() as gate:
        assert sorted(await drain(db, pool, gate)) == sorted([t1, t2, h1])
    manual = await record_item(
        db,
        world.access,
        "orchard",
        NewItem(
            kind="decision",
            statement="The van leaves at six.",
            decided_at=T0 + DAY,
            evidence=[EvidenceRef(t2, "m1")],
        ),
    )
    before = await items(db)

    monkeypatch.setattr("memini.extraction.llm.PROMPT_VERSION", "decisions-next")
    v2 = extractor_name(LOCAL_MODEL)
    assert v2 != v1
    second_reading = FakeLlm(
        readings={
            "Shall we pick on Monday?": [
                decided(["m2"], "Picking starts on Monday."),
                decided(["m1"], "Who drives the van?", basis="open"),
            ],
            "The van leaves at six.": [decided(["m1"], "The van leaves at seven.")],
            "Harvest ends on Friday.": [decided(["summary"], "Harvest ends on Friday.")],
        },
        verdicts={
            "Picking starts on Monday.": {"Picking starts on Monday.": "same"},
            "The van leaves at seven.": {"The van leaves at six.": "replaced"},
            "Harvest ends on Friday.": {"Harvest ends on Friday.": "same"},
        },
    )
    async with second_reading.gate() as gate:
        # One record read by the new version already, before the command: it is current.
        await run_extraction(
            db, LlmDecisions(gate), world.access, workspace_id=world.workspace, record_id=h1
        )

        preview = await reextract(db, world, dry_run=True)
        assert (preview.dry_run, counts(preview)) == (True, (3, 1, 2, 0, 0))
        assert await queued_jobs(db) == []  # a dry run enqueues nothing
        assert [s.extractor for s in preview.sources] == [v2, v2]

        first = await reextract(db, world)
        assert counts(first) == (3, 1, 2, 0, 0)
        second = await reextract(db, world)
        assert counts(second) == (3, 1, 0, 2, 0)
        assert await queued_jobs(db) == [t1, t2]

        assert await drain(db, pool, gate) == [t1, t2]
    assert second_reading.extractions() == 3  # each record read once by the new version
    assert await extractions(db, v2) == sorted([t1, t2, h1])
    assert await extractions(db, v1) == sorted([t1, t2, h1])  # the earlier readings stay

    after = {row["statement"]: row for row in await items(db)}
    # The manual item is untouched: the new reading that changes it is stored beside it.
    [manual_before] = [row for row in before if row["id"] == manual.id]
    assert after["The van leaves at six."] == manual_before
    assert after["The van leaves at seven."]["status"] == "active"
    links = await rows(db, "SELECT * FROM knowledge_supersession")
    assert links == []
    # The items the new version reads the same stay as they were, under the earlier version.
    assert (
        after["Picking starts on Monday."]["extractor"],
        after["Picking starts on Monday."]["status"],
    ) == (v1, "active")
    assert after["Harvest ends on Friday."]["extractor"] == v1
    assert (after["Who drives the van?"]["extractor"], after["Who drives the van?"]["kind"]) == (
        v2,
        "open_question",
    )
    assert [row for row in await items(db) if row["id"] in {r["id"] for r in before}] == before

    third = await reextract(db, world)
    assert counts(third) == (3, 3, 0, 0, 0)


async def test_another_model_is_another_extractor(db: Conn, world: World) -> None:
    await push(db, world, "chat", thread("t1", T0, "Agreed, Monday."))
    fake = FakeLlm()
    async with fake.gate() as gate:
        await run_extraction(
            db,
            LlmDecisions(gate),
            world.access,
            workspace_id=world.workspace,
            record_id=rid(world, "t1"),
        )
    assert counts(await reextract(db, world)) == (1, 1, 0, 0, 0)

    other = llm_settings(cloud=False, llm_local_model="fake-local-model:14b")
    result = await reextract(db, world, other)
    assert counts(result) == (1, 0, 1, 0, 0)
    assert result.sources[0].extractor == extractor_name("fake-local-model:14b")


# --- What is skipped -----------------------------------------------------------------------------


async def test_sources_no_llm_may_read_are_skipped_and_counted(db: Conn, world: World) -> None:
    await update_source(db, world.access, "orchard", "chat", SourceUpdate(llm_policy="none"))
    await push(db, world, "chat", thread("t1", T0, "Agreed, Monday."), thread("t2", T0, "Fine."))
    await push(db, world, "notes", meeting("h1", T0, "Harvest ends on Friday."))

    result = await reextract(db, world)
    assert counts(result) == (3, 0, 1, 0, 2)
    chat, notes = result.sources
    assert (chat.extractor, chat.skipped, counts(chat.counts)) == (
        None,
        "policy_none",
        (2, 0, 0, 0, 2),
    )
    assert (notes.skipped, counts(notes.counts)) == (None, (1, 0, 1, 0, 0))
    assert await queued_jobs(db) == [rid(world, "h1", "notes")]

    # A cloud provider only: notes is local, so none may read it.
    cloud_only = llm_settings(local=False)
    result = await reextract(db, world, cloud_only)
    assert [(s.name, s.skipped) for s in result.sources] == [
        ("chat", "policy_none"),
        ("notes", "no_provider"),
    ]
    assert counts(result) == (3, 0, 0, 0, 3)

    # No provider at all, and a workspace no LLM may read.
    assert counts(await reextract(db, world, llm_settings(local=False, cloud=False))) == (
        3,
        0,
        0,
        0,
        3,
    )
    await set_workspace_llm_policy(db, world.access, "orchard", "none")
    result = await reextract(db, world)
    assert {s.skipped for s in result.sources} == {"policy_none"}
    assert await queued_jobs(db) == [rid(world, "h1", "notes")]


async def test_records_go_oldest_first_and_the_options_narrow_them(db: Conn, world: World) -> None:
    await push(
        db,
        world,
        "chat",
        thread("t-late", T0 + 3 * DAY, "Late."),
        thread("t-a", T0 + DAY, "Same time, one."),
        thread("t-b", T0 + DAY, "Same time, two."),
        thread("t-early", T0, "Early."),
        thread("t-mid", T0 + 2 * DAY, "Middle."),
    )
    # At the time of the two threads in the middle: ties go by record id.
    await push(db, world, "notes", meeting("h1", T0 + DAY + MINUTE, "Harvest ends on Friday."))
    # A dry run of one source since a time: counts, and enqueues nothing.
    preview = await reextract(db, world, sources=["chat"], since=T0 + DAY, dry_run=True)
    assert counts(preview) == (4, 0, 4, 0, 0)
    assert [s.name for s in preview.sources] == ["chat"]
    assert await queued_jobs(db) == []

    # Pages of two: the order holds across pages, ties by record id.
    result = await reextract(db, world, page_size=2)
    assert counts(result) == (6, 0, 6, 0, 0)
    same_time = sorted([rid(world, "t-a"), rid(world, "t-b"), rid(world, "h1", "notes")])
    assert await queued_jobs(db) == [
        rid(world, "t-early"),
        *same_time,
        rid(world, "t-mid"),
        rid(world, "t-late"),
    ]
    preview = await reextract(db, world, since=T0 + 2 * DAY, dry_run=True)
    assert counts(preview) == (2, 0, 0, 2, 0)  # the queued jobs are found in a dry run too

    with pytest.raises(NotFoundError):
        await reextract(db, world, sources=["chat", "nowhere"])
    other = await ensure_principal(db, "stranger")
    with pytest.raises(NotFoundError):
        await enqueue_reextraction(
            db, WriteAccess.for_owner(other), "elsewhere", configured_providers(LOCAL_ONLY)
        )
    limited = WriteAccess(owner_id=world.owner, workspace_ids=frozenset({uuid.uuid4()}))
    with pytest.raises(NotFoundError):
        await enqueue_reextraction(db, limited, "orchard", configured_providers(LOCAL_ONLY))


# --- Earlier items -------------------------------------------------------------------------------


async def test_a_new_reading_brings_back_nothing_replaced_answered_or_retracted(
    db: Conn, world: World, pool: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    await push(
        db,
        world,
        "chat",
        thread(
            "t1", T0, "Shall we pick on Monday? Which crates?", "Agreed, Monday. Bins to the barn."
        ),
        extract=True,
    )
    await push(
        db,
        world,
        "chat",
        thread("t2", T0 + 30 * DAY, "Wednesday, and crates are green."),
        extract=True,
    )
    first_reading = FakeLlm(
        readings={
            "Shall we pick on Monday? Which crates?": [
                decided(["m2"], "Picking starts on Monday."),
                decided(["m1"], "Which colour are the crates?", basis="open"),
                decided(["m2"], "Bins go to the barn."),
            ],
            "Wednesday, and crates are green.": [
                decided(["m1"], "Picking starts on Wednesday; crates are green.")
            ],
        },
        verdicts={
            "Picking starts on Wednesday; crates are green.": {
                "Picking starts on Monday.": "replaced",
                "Which colour are the crates?": "answered",
            }
        },
    )
    async with first_reading.gate() as gate:
        await drain(db, pool, gate)
    by_statement = {row["statement"]: row for row in await items(db)}
    await retract_item(db, world.access, by_statement["Bins go to the barn."]["id"])
    before = await items(db)
    assert {row["statement"]: row["status"] for row in before} == {
        "Picking starts on Monday.": "superseded",
        "Which colour are the crates?": "closed",
        "Bins go to the barn.": "retracted",
        "Picking starts on Wednesday; crates are green.": "active",
    }

    monkeypatch.setattr("memini.extraction.llm.PROMPT_VERSION", "decisions-next")
    second_reading = FakeLlm(
        readings={
            "Shall we pick on Monday? Which crates?": [
                decided(["m2"], "Picking starts on Monday."),
                decided(["m1"], "Which colour are the crates?", basis="open"),
                decided(["m2"], "Bins go to the barn."),
                decided(["m2"], "Bins go to the shed."),
            ],
            "Wednesday, and crates are green.": [
                decided(["m1"], "Picking starts on Wednesday; crates are green.")
            ],
        },
        verdicts={
            "Picking starts on Monday.": {"Picking starts on Monday.": "same"},
            "Which colour are the crates?": {"Which colour are the crates?": "same"},
            "Bins go to the barn.": {"Bins go to the barn.": "same"},
            # A link to an item that is no longer active is dropped; the item is new.
            "Bins go to the shed.": {"Bins go to the barn.": "replaced"},
            "Picking starts on Wednesday; crates are green.": {
                "Picking starts on Wednesday; crates are green.": "same"
            },
        },
    )
    assert counts(await reextract(db, world)) == (2, 0, 2, 0, 0)
    async with second_reading.gate() as gate:
        assert await drain(db, pool, gate) == [rid(world, "t1"), rid(world, "t2")]

    after = await items(db)
    assert after[: len(before)] == before  # nothing earlier changed
    assert [(row["statement"], row["status"]) for row in after[len(before) :]] == [
        ("Bins go to the shed.", "active")
    ]
    assert await rows(db, "SELECT count(*) AS n FROM knowledge_supersession") == [{"n": 2}]
    # The earlier items of the record were shown to the check of each new item, after the
    # active ones.
    checks = dict(second_reading.checks())
    assert checks["Picking starts on Monday."][-3:] == [
        "Bins go to the barn.",
        "Which colour are the crates?",
        "Picking starts on Monday.",
    ]
    reports = await rows(
        db,
        "SELECT report FROM run WHERE kind = %s AND report->'details'->>'extractor' = %s",
        (EXTRACT_KIND, extractor_name(LOCAL_MODEL)),
    )
    t1_report = [
        r["report"]["counts"] for r in reports if r["report"]["counts"]["items_proposed"] == 4
    ]
    assert [(c["items_duplicate"], c["items_stored"], c["links_dropped"]) for c in t1_report] == [
        (3, 1, 1)
    ]


# --- The route -----------------------------------------------------------------------------------


@pytest.fixture
async def client(migrated_database_url: str, pool: Pool) -> AsyncIterator[httpx2.AsyncClient]:
    settings = llm_settings(cloud=False, database_url=SecretStr(migrated_database_url))
    app = create_app(settings, pool=pool, embedder=FakeEmbedder())
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://memini.test") as client:
        yield client


async def bearer(
    db: Conn, owner: uuid.UUID, *scopes: Scope, workspace_id: uuid.UUID | None = None
) -> dict[str, str]:
    new = await create_token(
        db, owner_id=owner, name="test agent", scopes=scopes, workspace_id=workspace_id
    )
    return {"Authorization": f"Bearer {new.secret.get_secret_value()}"}


async def test_the_route_enqueues_and_counts_per_source(
    db: Conn, world: World, client: httpx2.AsyncClient
) -> None:
    await push(db, world, "chat", thread("t1", T0, "Agreed, Monday."), thread("t2", T0, "Fine."))
    await push(db, world, "notes", meeting("h1", T0 + DAY, "Harvest ends on Friday."))
    admin = await bearer(db, world.owner, "read", "admin")
    url = "/v1/workspaces/orchard/extract"

    response = await client.post(url, json={"dry_run": True, "since": iso(T0 + DAY)}, headers=admin)
    assert response.status_code == 200, response.text
    answer = response.json()
    assert answer["dry_run"] is True
    assert answer["since"] == "2026-04-07T09:00:00Z"
    assert answer["totals"] == {
        "records": 1,
        "current": 0,
        "enqueued": 1,
        "queued": 0,
        "skipped": 0,
    }
    assert await queued_jobs(db) == []

    response = await client.post(url, headers=admin)  # no body: every source, every record
    assert response.status_code == 202, response.text
    answer = response.json()
    assert answer["totals"]["enqueued"] == 3
    assert [
        (s["source"], s["extractor"], s["skipped_reason"], s["records"], s["enqueued"])
        for s in answer["sources"]
    ] == [
        ("chat", extractor_name(LOCAL_MODEL), None, 2, 2),
        ("notes", extractor_name(LOCAL_MODEL), None, 1, 1),
    ]
    response = await client.post(url, json={"sources": ["notes"]}, headers=admin)
    assert response.status_code == 202, response.text
    assert response.json()["totals"] == {
        "records": 1,
        "current": 0,
        "enqueued": 0,
        "queued": 1,
        "skipped": 0,
    }
    assert len(await queued_jobs(db)) == 3

    refused = await client.post(url, json={"sources": ["nowhere"]}, headers=admin)
    assert (refused.status_code, refused.json()["error"]["code"]) == (404, "not_found")
    refused = await client.post(url, json={"since": "2026-04-07T09:00:00"}, headers=admin)
    assert refused.status_code == 422  # a time without a zone
    refused = await client.post(url, json={"dry_run": True, "all": True}, headers=admin)
    assert refused.status_code == 422
    reader = await bearer(db, world.owner, "read")
    refused = await client.post(url, json={}, headers=reader)
    assert (refused.status_code, refused.json()["error"]["code"]) == (403, "insufficient_scope")
    elsewhere = await create_workspace(db, owner_id=world.owner, name="elsewhere")
    limited = await bearer(db, world.owner, "admin", workspace_id=elsewhere.id)
    refused = await client.post(url, json={}, headers=limited)
    assert (refused.status_code, refused.json()["error"]["code"]) == (404, "not_found")
