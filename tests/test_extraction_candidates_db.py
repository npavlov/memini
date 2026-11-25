"""The candidates of a supersession check against a real Postgres: run with
`make test-integration`.

With an embedder, the ``extract`` job finds candidates by the vectors of the statements as well
as by their words: a duplicate in another language, in a workspace with more eligible items than
a check is shown, reaches the check only through its vector. Each statement is embedded once:
the vectors of stored items are kept (``knowledge_item_vector``) and reused, items recorded by
hand get theirs before the next check, and vectors of another model are dropped. The embedder
here maps words of either language to shared concepts, so that a statement and its translation
get one vector. Every name and text here is synthetic.
"""

import json
import re
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2
import pytest
from psycopg import AsyncConnection
from psycopg.rows import TupleRow

from llm_helpers import chat_answer, llm_settings, verified_answer
from memini.auth import WriteAccess
from memini.db import connect
from memini.embeddings import BatchingEmbedder, EmbeddingSpec, FakeEmbedder
from memini.extraction.candidates import StatementVectors, similar_active_items
from memini.extraction.job import run_extraction
from memini.extraction.llm import LlmDecisions
from memini.ids import record_id
from memini.ingest import Batch, RecordIn, ingest
from memini.knowledge import EvidenceRef, NewItem, record_item
from memini.llm import LlmGate, create_llm_gate
from memini.sources import NewPushSource, create_push_source
from memini.workspaces import create_workspace, ensure_principal

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

type Conn = AsyncConnection[TupleRow]

T0 = datetime(2026, 4, 6, 9, 0, tzinfo=UTC)

CONCEPTS: Mapping[str, int] = {
    "crate": 0,
    "ящик": 0,
    "green": 1,
    "зелён": 1,
    "picking": 2,
    "собир": 2,
    "monday": 3,
    "понедельник": 3,
    "van": 4,
    "фургон": 4,
    "ladder": 5,
    "лестниц": 5,
    "orchard": 6,
    "red": 7,
}


class ConceptEmbedder(BatchingEmbedder):
    """A vector of the concepts a text names, whatever its language; counts what it embeds."""

    def __init__(self) -> None:
        super().__init__(
            EmbeddingSpec(provider="openai_compatible", model="concepts", dimension=9),
            batch_size=16,
        )
        self.texts: list[str] = []

    async def _embed_batch(self, texts: list[str]) -> Sequence[Sequence[float]]:
        self.texts.extend(texts)
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * 8 + [0.05]  # never zero
            for word in re.findall(r"\w+", text.lower()):
                for stem, concept in CONCEPTS.items():
                    if word.startswith(stem):
                        vector[concept] = 1.0
            vectors.append(vector)
        return vectors


# --- The fake LLM --------------------------------------------------------------------------------

_BLOCK = re.compile(r"<<<BEGIN DATA ([0-9a-f]{16})>>>\n(.*)\n<<<END DATA \1>>>", re.DOTALL)


@dataclass
class FakeLlm:
    """Extracts the next of ``extractions``; a check says ``same`` of the candidate whose
    statement is in ``same`` and records the statements it was shown."""

    extractions: list[list[dict[str, Any]]] = field(default_factory=list[list[dict[str, Any]]])
    same: set[str] = field(default_factory=set[str])
    shown: list[list[str]] = field(default_factory=list[list[str]])

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        if (verified := verified_answer(body)) is not None:
            return verified
        if body["response_format"]["json_schema"]["name"] == "decisions":
            items = self.extractions.pop(0) if self.extractions else []
            return chat_answer(json.dumps({"items": items}))
        found = _BLOCK.search(body["items"][-1]["content"])
        assert found is not None
        lines = [json.loads(line) for line in found.group(2).split("\n")][1:]
        self.shown.append([line["statement"] for line in lines])
        matches = [
            {"item": line["n"], "its_value": line["statement"], "verdict": "same"}
            for line in lines
            if line["statement"] in self.same
        ]
        return chat_answer(json.dumps({"about": "the matter", "matches": matches}))

    def gate(self) -> LlmGate:
        return create_llm_gate(
            llm_settings(cloud=False), transport=httpx2.MockTransport(self.handle)
        )


def decided(statement: str, anchor: str = "m1") -> dict[str, Any]:
    return {"anchors": [anchor], "basis": "decided", "statement": statement}


# --- The world -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class World:
    owner: uuid.UUID
    workspace: uuid.UUID
    chat: uuid.UUID

    @property
    def access(self) -> WriteAccess:
        return WriteAccess.for_owner(self.owner)

    def rid(self, external_id: str) -> str:
        return record_id(self.chat, "thread", external_id)


@pytest.fixture
async def db(migrated_database_url: str) -> AsyncIterator[Conn]:
    async with connect(migrated_database_url, autocommit=True) as conn:
        yield conn


@pytest.fixture
async def world(db: Conn) -> World:
    owner = await ensure_principal(db, "owner")
    workspace = await create_workspace(db, owner_id=owner, name="orchard")
    chat = await create_push_source(
        db, WriteAccess.for_owner(owner), "orchard", NewPushSource(name="chat")
    )
    return World(owner, workspace.id, chat.id)


async def push(db: Conn, world: World, external_id: str, text: str, day: int) -> str:
    at = T0 + timedelta(days=day)
    record = RecordIn(
        kind="thread",
        external_id=external_id,
        occurred_at=at,
        payload={
            "channel": "orchard",
            "messages": [
                {
                    "id": "m1",
                    "persons": ["Ann Example"],
                    "occurred_at": at.isoformat(),
                    "text": text,
                }
            ],
        },
    )
    await ingest(db, world.access, "orchard", "chat", Batch(records=[record]))
    return world.rid(external_id)


async def extract(
    db: Conn,
    world: World,
    llm: FakeLlm,
    record: str,
    *,
    vectors: StatementVectors | None,
    candidates: int = 2,
) -> None:
    async with llm.gate() as gate:
        await run_extraction(
            db,
            LlmDecisions(gate),
            world.access,
            workspace_id=world.workspace,
            record_id=record,
            candidates=candidates,
            vectors=vectors,
        )


async def statements(db: Conn) -> list[str]:
    cur = await db.execute("SELECT statement FROM knowledge_item ORDER BY recorded_at, id")
    return [row[0] for row in await cur.fetchall()]


async def vector_rows(db: Conn) -> list[tuple[str, str]]:
    cur = await db.execute(
        "SELECT i.statement, v.embedding_key FROM knowledge_item_vector v"
        " JOIN knowledge_item i ON i.id = v.item_id ORDER BY i.recorded_at, i.id"
    )
    return [(row[0], row[1]) for row in await cur.fetchall()]


EARLIER = (
    "Crates are green.",
    "The orchard van is red.",
    "Picking ladders stay in the orchard.",
    "The orchard opens on Monday.",
)
"""Four items, more than the two a check is shown: three share the word orchard with the new
one, the first only its meaning."""
NEW = "Ящики в саду зелёные (orchard)."


async def fill(db: Conn, world: World, llm: FakeLlm, vectors: StatementVectors | None) -> None:
    for n, statement in enumerate(EARLIER, start=1):
        llm.extractions.append([decided(statement)])
        record = await push(db, world, f"t{n}", statement, n)
        await extract(db, world, llm, record, vectors=vectors)


@pytest.mark.parametrize("hybrid", [False, True], ids=["lexical", "hybrid"])
async def test_a_duplicate_in_another_language_reaches_the_check_by_its_vector(
    db: Conn, world: World, hybrid: bool
) -> None:
    embedder = ConceptEmbedder()
    vectors = StatementVectors(embedder) if hybrid else None
    llm = FakeLlm()
    await fill(db, world, llm, vectors)
    llm.same.add("Crates are green.")
    llm.extractions.append([decided(NEW)])
    record = await push(db, world, "t9", NEW, 9)
    await extract(db, world, llm, record, vectors=vectors)

    shown = llm.shown[-1]
    assert len(shown) == 2
    if hybrid:
        assert "Crates are green." in shown  # the nearest vector
        assert await statements(db) == list(EARLIER)  # a duplicate: not stored again
    else:
        assert "Crates are green." not in shown  # no word in common; orchard ranks the others
        assert await statements(db) == [*EARLIER, NEW]


async def test_statement_vectors_are_kept_reused_and_filled_in(db: Conn, world: World) -> None:
    embedder = ConceptEmbedder()
    vectors = StatementVectors(embedder)
    llm = FakeLlm()
    await fill(db, world, llm, vectors)
    key = vectors.key
    assert await vector_rows(db) == [(statement, key) for statement in EARLIER]
    assert embedder.texts == list(EARLIER)  # each embedded once, for its own check

    # The same statement said again in a new record: its vector is reused, not embedded.
    llm.extractions.append([decided(EARLIER[1])])
    await extract(db, world, llm, await push(db, world, "t5", EARLIER[1], 5), vectors=vectors)
    assert embedder.texts == list(EARLIER)
    assert len(await vector_rows(db)) == len(EARLIER) + 1

    # An item recorded by hand gets its vector before the next check.
    manual = "Picking starts on Monday."
    await record_item(
        db,
        world.access,
        "orchard",
        NewItem(
            kind="decision",
            statement=manual,
            decided_at=T0 + timedelta(days=6),
            evidence=[EvidenceRef(world.rid("t5"), "m1")],
        ),
    )
    llm.extractions.append([decided("Собираем яблоки в понедельник.")])
    await extract(
        db, world, llm, await push(db, world, "t7", "Собираем в понедельник.", 7), vectors=vectors
    )
    assert embedder.texts[-2:] == ["Собираем яблоки в понедельник.", manual]
    assert llm.shown[-1][0] == manual  # found by meaning, though no word is shared
    assert (manual, key) in await vector_rows(db)

    # Another model: its vectors replace the old ones as the items are checked again.
    other = StatementVectors(FakeEmbedder())
    assert other.key != key
    llm.extractions.append([decided("The van is blue.")])
    await extract(db, world, llm, await push(db, world, "t8", "Van: blue.", 8), vectors=other)
    rows = await vector_rows(db)
    assert {row[1] for row in rows} == {other.key}
    assert len(rows) == len(await statements(db))


async def test_without_a_vector_the_candidates_are_lexical(db: Conn, world: World) -> None:
    llm = FakeLlm()
    await fill(db, world, llm, None)
    shown = await similar_active_items(
        db,
        workspace_id=world.workspace,
        record_id=world.rid("t1"),
        statement="Is the orchard van green?",
        policies=("local", "cloud"),
        limit=3,
    )
    # Its own record first, then by rank, then the latest.
    assert [c.candidate.statement for c in shown] == [
        "Crates are green.",
        "The orchard van is red.",
        "The orchard opens on Monday.",
    ]
