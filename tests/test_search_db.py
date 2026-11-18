"""Search against a real Postgres: every mode and filter on an indexed synthetic corpus, in English
and Russian. Acceptance of memini-g05: tests on fixtures for each filter and mode; a pathological
regex is cut off by the timeout. Every value is synthetic; vectors come from the FakeEmbedder,
whose vectors carry no meaning, so a dense query here is the exact text of a chunk."""

import time
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime

import pytest
from psycopg import AsyncConnection, sql
from psycopg.rows import TupleRow

from index_helpers import FILE_V1, PATH, Scope, add_record, make_scope
from memini.auth import Principal, ReadFilter, authorize_read
from memini.chunking import Chunk, chunk_file
from memini.db import connect
from memini.embeddings import EmbeddingSpec, FakeEmbedder
from memini.index import IndexFingerprint, IndexMismatchError, VectorIndex, index_record
from memini.search import (
    MODES,
    Hit,
    InvalidSearchError,
    Mode,
    SearchFilters,
    SearchOptions,
    SearchQuery,
    SearchTimeoutError,
    UnsupportedFilterError,
    retrieval,
    search,
)
from memini.search.retrieval import ChunkScope, dense_statement

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

type Conn = AsyncConnection[TupleRow]

OTHER_MODEL = EmbeddingSpec(provider="openai_compatible", model="fake-embedder-2", dimension=12)

OGOROD_PATH = "src/огород/ogorod.py"
OGOROD = '''"""Огород: грядки, урожай и журнал поливов."""

NORM = 40


def harvest(bed, kilograms):
    """Собрать урожай на грядке и записать вес в журнал."""
    total = bed.weight + kilograms
    bed.weight = total
    if total > 500:
        bed.full = True
    bed.log.append(total)
    return total


def полить(грядка, литры):
    """Полить грядку, но не больше нормы."""
    объём = min(литры, NORM)
    грядка.влажность += объём
    if грядка.влажность > 100:
        грядка.влажность = 100
    return объём
'''

LEDGER_PATH = "web/static/ledger.ts"
LEDGER = """export function totalHarvest(beds: number[]): number {
  let total = 0;
  for (const bed of beds) {
    total += bed;
  }
  console.log("harvest total", total);
  return total;
}
"""


def letters(count: int) -> str:
    """An irregular run of the letters a to j: a linear congruential sequence."""
    out: list[str] = []
    x = 7
    for _ in range(count):
        x = (x * 1103515245 + 12345) % 2**31
        out.append("abcdefghij"[(x >> 16) % 10])
    return "".join(out)


PATTERN_PATH = "data/pattern.txt"
PATTERN = f"# A synthetic line for the regex timeout.\n{letters(400)}\nend = True\n"
CATASTROPHIC = r"^(.*)(.*)(.*)(.*)\4\3\2\1$"
"""Back-references make Postgres's regex engine try every split of a line: on the 400 letters
above it runs for minutes."""

WATERING = (
    "Решение: поливать теплицу дважды в день, утром и вечером.\n"
    "Норма полива: сорок литров на грядку.\n"
    "Ответственный ведёт журнал поливов."
)
PRUNING = (
    "Plan: prune the tomatoes every Friday and log the harvest weight.\n"
    "The greenhouse lamp stays on for twelve hours."
)
OLD_WATERING = "Старое решение: поливать теплицу раз в неделю."


def at(month: int, day: int) -> datetime:
    return datetime(2026, month, day, tzinfo=UTC)


@dataclass(frozen=True)
class Corpus:
    scope: Scope
    records: dict[str, str]
    """Record ids by label."""
    chunks: dict[str, list[Chunk]]
    """The chunks of each record, by label."""
    stream: uuid.UUID
    """The stream (git ref) of the greenhouse repository."""

    def chunk(self, label: str, symbol: str | None = None) -> Chunk:
        return next(c for c in self.chunks[label] if symbol is None or c.symbol == symbol)


@pytest.fixture
async def db(migrated_database_url: str) -> AsyncIterator[Conn]:
    async with connect(migrated_database_url, autocommit=True) as conn:
        yield conn


@pytest.fixture
def fake() -> FakeEmbedder:
    return FakeEmbedder()


async def put(
    db: Conn,
    fake: FakeEmbedder,
    *,
    workspace: uuid.UUID,
    owner: uuid.UUID,
    source: uuid.UUID,
    external_id: str,
    content: str | bytes,
    path: str,
    occurred_at: datetime,
    kind: str = "code_file",
    url: str | None = None,
    stream: uuid.UUID | None = None,
) -> tuple[str, list[Chunk]]:
    data = content.encode() if isinstance(content, str) else content
    rid = await add_record(
        db,
        workspace_id=workspace,
        owner_id=owner,
        source_id=source,
        external_id=external_id,
        content=data,
        kind="code_file" if kind == "code_file" else "document",
    )
    await db.execute(
        "UPDATE record SET occurred_at = %s, external_url = %s, stream_id = %s WHERE id = %s",
        (occurred_at, url, stream, rid),
    )
    chunks = chunk_file(rid, path, data)
    await index_record(
        db, fake, record_id=rid, path=path if kind == "code_file" else None, chunks=chunks
    )
    return rid, list(chunks.chunks)


@pytest.fixture
async def corpus(db: Conn, fake: FakeEmbedder) -> Corpus:
    scope = await make_scope(db)
    cur = await db.execute(
        "INSERT INTO stream (workspace_id, owner_id, source_id, key, cursor)"
        " VALUES (%s, %s, %s, 'refs/heads/main', '{\"synthetic_pin\": \"c0ffee\"}') RETURNING id",
        (scope.acme, scope.owner, scope.repo),
    )
    row = await cur.fetchone()
    assert row is not None
    stream: uuid.UUID = row[0]
    acme, other, owner = scope.acme, scope.other, scope.owner
    files = {
        # label: (workspace, source, path, content, kind, occurred_at)
        "greenhouse": (acme, scope.repo, PATH, FILE_V1, "code_file", at(1, 2)),
        "ogorod": (acme, scope.repo, OGOROD_PATH, OGOROD, "code_file", at(4, 1)),
        "ledger": (acme, scope.repo, LEDGER_PATH, LEDGER, "code_file", at(4, 1)),
        "pattern": (acme, scope.repo, PATTERN_PATH, PATTERN, "code_file", at(1, 2)),
        "fork": (acme, scope.fork, PATH, FILE_V1, "code_file", at(2, 15)),
        "other": (other, scope.other_repo, PATH, FILE_V1, "code_file", at(1, 2)),
        "watering": (acme, scope.notes, "notes/watering.md", WATERING, "document", at(3, 10)),
        "pruning": (acme, scope.notes, "notes/pruning.md", PRUNING, "document", at(5, 20)),
        "old_watering": (acme, scope.notes, "notes/old.md", OLD_WATERING, "document", at(2, 1)),
    }
    records: dict[str, str] = {}
    chunks: dict[str, list[Chunk]] = {}
    for label, (workspace, source, path, content, kind, occurred_at) in files.items():
        code = kind == "code_file"
        records[label], chunks[label] = await put(
            db,
            fake,
            workspace=workspace,
            owner=owner,
            source=source,
            external_id=f"main:{path}" if code else f"decision-{label}",
            content=content,
            path=path,
            occurred_at=occurred_at,
            kind=kind,
            url=None if code else f"https://notes.example.invalid/{label}",
            stream=stream if source == scope.repo else None,
        )
    # A tombstone whose chunks are still there: the writer deletes them in a step of its own.
    await db.execute(
        "UPDATE record SET deleted_at = now() WHERE id = %s", (records["old_watering"],)
    )
    return Corpus(scope=scope, records=records, chunks=chunks, stream=stream)


def reader(scope: Scope, workspace: uuid.UUID | None = None, **narrow: object) -> ReadFilter:
    """The read filter of a token with the read scope, limited to ``workspace`` if given."""
    principal = Principal(
        principal_id=scope.owner,
        token_id=uuid.uuid4(),
        scopes=frozenset({"read"}),
        workspace_id=workspace,
    )
    return authorize_read(principal, **narrow)  # type: ignore[arg-type]


async def find(
    db: Conn,
    fake: FakeEmbedder,
    access: ReadFilter,
    text: str,
    mode: Mode = "hybrid",
    *,
    options: SearchOptions | None = None,
    **query: object,
) -> list[Hit]:
    filters = query.pop("filters", SearchFilters())
    return await search(
        db,
        fake,
        access,
        SearchQuery(text=text, mode=mode, filters=filters, **query),  # type: ignore[arg-type]
        options=options,
    )


def places(hits: Sequence[Hit]) -> list[tuple[str, str, str | None]]:
    """Where each hit is: workspace, source, path (or external id)."""
    return [(h.workspace, h.source, h.path or h.external_id) for h in hits]


def text_of(chunk: Chunk) -> str:
    """What the writer embedded for ``chunk``; as a dense query it finds the chunk at distance 0."""
    return chunk.text


# --- Modes ---------------------------------------------------------------------------------------


async def test_hybrid_fuses_dense_and_lexical_candidates_once_each(
    db: Conn, fake: FakeEmbedder, corpus: Corpus
) -> None:
    water = corpus.chunk("greenhouse", "water")

    hits = await find(db, fake, reader(corpus.scope, corpus.scope.acme), text_of(water), limit=20)

    ids = [h.chunk_id for h in hits]
    assert len(ids) == len(set(ids))
    # The water function of the repository and of its fork: the same text, so the same vector
    # and the same words; both are first in both retrievers' lists.
    top = {h.chunk_id: h for h in hits[:2]}
    assert set(top) == {water.id, corpus.chunk("fork", "water").id}
    for found in top.values():
        assert found.dense_rank in (1, 2)
        assert found.lexical_rank in (1, 2)
    # The default fusion: k = 1, the dense ranks weighted 3, the lexical ones 1.
    defaults = SearchOptions()
    k, dense, lexical = defaults.rrf_k, defaults.dense_weight, defaults.lexical_weight
    assert (k, dense, lexical) == (1, 3.0, 1.0)
    first = hits[0]
    assert first.score == pytest.approx(
        dense / (k + (first.dense_rank or 0)) + lexical / (k + (first.lexical_rank or 0))
    )
    # Some hits come from one retriever only, and score only its part.
    single = [h for h in hits if (h.dense_rank is None) != (h.lexical_rank is None)]
    assert single
    for h in single:
        if h.dense_rank is not None:
            assert h.score == pytest.approx(dense / (k + h.dense_rank))
        else:
            assert h.score == pytest.approx(lexical / (k + (h.lexical_rank or 0)))
    assert [h.score for h in hits] == sorted((h.score or 0 for h in hits), reverse=True)


async def test_the_rrf_k_and_weights_come_from_the_options(
    db: Conn, fake: FakeEmbedder, corpus: Corpus
) -> None:
    water = corpus.chunk("greenhouse", "water")
    options = SearchOptions(rrf_k=5, dense_weight=0.5, lexical_weight=2)

    hits = await find(db, fake, reader(corpus.scope), text_of(water), options=options)

    for hit in hits:
        parts = [
            weight / (5 + rank)
            for weight, rank in ((0.5, hit.dense_rank), (2, hit.lexical_rank))
            if rank is not None
        ]
        assert hit.score == pytest.approx(sum(parts))


async def test_dense_finds_the_nearest_chunk(db: Conn, fake: FakeEmbedder, corpus: Corpus) -> None:
    harvest = corpus.chunk("ogorod", "harvest")

    hits = await find(db, fake, reader(corpus.scope), text_of(harvest), "dense", limit=3)

    assert hits[0].chunk_id == harvest.id
    assert hits[0].score == pytest.approx(1.0, abs=1e-6)  # cosine similarity
    assert [h.dense_rank for h in hits] == [1, 2, 3]
    assert all(h.lexical_rank is None for h in hits)


async def test_dense_embeds_the_query_with_the_query_prefix(
    db: Conn, corpus: Corpus, fake: FakeEmbedder
) -> None:
    prefixed = FakeEmbedder(fake.spec, query_prefix="query: ")

    await find(db, prefixed, reader(corpus.scope), "water the plants", "dense")

    assert prefixed.texts == ["query: water the plants"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("урожай", {("ogorod", "harvest")}),
        ("журнал поливов", {("ogorod", None), ("watering", None)}),
        ("tomatoes friday", {("pruning", None)}),
        ("plant.soil litres", {("greenhouse", "water"), ("fork", "water")}),
    ],
)
async def test_lexical_finds_words_in_english_and_russian(
    db: Conn,
    fake: FakeEmbedder,
    corpus: Corpus,
    text: str,
    expected: set[tuple[str, str | None]],
) -> None:
    fake.reset()

    hits = await find(db, fake, reader(corpus.scope, corpus.scope.acme), text, "lexical")

    found = {(_label(corpus, h), h.symbol) for h in hits}
    assert expected <= found
    assert [h.lexical_rank for h in hits] == list(range(1, len(hits) + 1))
    assert all(h.dense_rank is None for h in hits)
    assert fake.batches == []  # a lexical search embeds nothing


def _label(corpus: Corpus, hit: Hit) -> str:
    return next(label for label, rid in corpus.records.items() if rid == hit.record_id)


async def test_lexical_ranks_chunks_with_more_of_the_words_first(
    db: Conn, fake: FakeEmbedder, corpus: Corpus
) -> None:
    hits = await find(
        db, fake, reader(corpus.scope, corpus.scope.acme), "журнал поливов урожай", "lexical"
    )

    # The head of ogorod.py holds all three words; the watering decision and harvest two each.
    assert (_label(corpus, hits[0]), hits[0].symbol) == ("ogorod", None)
    assert {(_label(corpus, h), h.symbol) for h in hits[1:3]} == {
        ("watering", None),
        ("ogorod", "harvest"),
    }
    assert (hits[0].score or 0) > (hits[1].score or 0) >= (hits[2].score or 0)


async def test_lexical_breaks_a_tie_by_the_chunk_id(
    db: Conn, fake: FakeEmbedder, corpus: Corpus
) -> None:
    hits = await find(
        db, fake, reader(corpus.scope, corpus.scope.acme), "plant.soil litres", "lexical"
    )

    # The water function of the repository and of its fork: the same text, the same rank.
    water, fork = corpus.chunk("greenhouse", "water"), corpus.chunk("fork", "water")
    assert {h.chunk_id for h in hits[:2]} == {water.id, fork.id}
    assert hits[0].score == hits[1].score
    assert hits[0].chunk_id < hits[1].chunk_id


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Stemming: other forms of the words the text holds, in both languages.
        ("грядкам урожаев", {("ogorod", "harvest")}),
        ("pruning tomato", {("pruning", None)}),
        ("harvested", {("ledger", "totalHarvest")}),
        # Identifiers cut into words: camel case, and the path in the header.
        ("total", {("ledger", "totalHarvest")}),
        ("ogorod", {("ogorod", None), ("ogorod", "harvest"), ("ogorod", "полить")}),
        # A whole identifier still finds its definition.
        ("totalHarvest", {("ledger", "totalHarvest")}),
    ],
)
async def test_lexical_stems_words_and_splits_identifiers(
    db: Conn,
    fake: FakeEmbedder,
    corpus: Corpus,
    text: str,
    expected: set[tuple[str, str | None]],
) -> None:
    hits = await find(db, fake, reader(corpus.scope, corpus.scope.acme), text, "lexical")

    assert expected <= {(_label(corpus, h), h.symbol) for h in hits}


async def test_lexical_ignores_stop_words(db: Conn, fake: FakeEmbedder, corpus: Corpus) -> None:
    access = reader(corpus.scope, corpus.scope.acme)

    assert await find(db, fake, access, "the and of", "lexical") == []
    assert await find(db, fake, access, "и в на", "lexical") == []
    with_stop_words = await find(db, fake, access, "the tomatoes of friday", "lexical")
    assert [_label(corpus, h) for h in with_stop_words] == ["pruning"]


async def test_the_lexical_column_is_what_the_retriever_parses_queries_with(
    db: Conn, corpus: Corpus
) -> None:
    """Migration 0012 makes the column; the retriever parses query words with the same function
    and configuration. If one changes without the other, words stop meeting."""
    del corpus
    document = retrieval.lexical_statement.document(sql.SQL("header || E'\\n' || content"))
    cur = await db.execute(
        sql.SQL("SELECT count(*), count(*) FILTER (WHERE lexical = {}) FROM chunk").format(document)
    )
    row = await cur.fetchone()
    assert row is not None
    total, same = row
    assert total > 0
    assert same == total


async def test_exact_finds_a_substring_and_its_line(
    db: Conn, fake: FakeEmbedder, corpus: Corpus
) -> None:
    access = reader(corpus.scope, corpus.scope.acme)

    hits = await find(db, fake, access, "plant.soil += amount", "exact")

    assert places(hits) == [
        ("acme", "greenhouse", PATH),
        ("acme", "greenhouse-fork", PATH),
    ]
    water = corpus.chunk("greenhouse", "water")
    assert [h.chunk_id for h in hits][:1] == [water.id]
    assert hits[0].match_line == water.start_line + 3
    assert hits[0].score is None


async def test_exact_is_case_sensitive_unless_asked(
    db: Conn, fake: FakeEmbedder, corpus: Corpus
) -> None:
    access = reader(corpus.scope, corpus.scope.acme)

    assert await find(db, fake, access, "PLANT.SOIL", "exact") == []
    folded = await find(db, fake, access, "PLANT.SOIL", "exact", case_sensitive=False)
    assert len(folded) == 2
    assert folded[0].match_line == corpus.chunk("greenhouse", "water").start_line + 3
    russian = await find(db, fake, access, "НОРМЫ", "exact", case_sensitive=False)
    assert [(_label(corpus, h), h.match_line) for h in russian] == [("ogorod", 17)]


async def test_exact_takes_like_wildcards_literally(
    db: Conn, fake: FakeEmbedder, corpus: Corpus
) -> None:
    access = reader(corpus.scope, corpus.scope.acme)

    # bed.weight is in ogorod.py: neither _ nor % may stand for another character.
    assert await find(db, fake, access, "bed_weight", "exact") == []
    assert await find(db, fake, access, "bed%weight", "exact") == []
    assert len(await find(db, fake, access, "bed.weight", "exact")) == 1


async def test_exact_finds_russian_text_in_code_and_documents(
    db: Conn, fake: FakeEmbedder, corpus: Corpus
) -> None:
    hits = await find(db, fake, reader(corpus.scope), "журнал поливов", "exact")

    assert sorted(_label(corpus, h) for h in hits) == ["ogorod", "watering"]
    by_label = {_label(corpus, h): h for h in hits}
    assert by_label["ogorod"].match_line == 1
    assert by_label["watering"].match_line == 3


async def test_regex_matches_lines_like_grep(db: Conn, fake: FakeEmbedder, corpus: Corpus) -> None:
    access = reader(corpus.scope, corpus.scope.acme)

    hits = await find(db, fake, access, r"^\s+plant\.soil \+= \w+$", "regex")

    water = corpus.chunk("greenhouse", "water")
    assert places(hits) == [("acme", "greenhouse", PATH), ("acme", "greenhouse-fork", PATH)]
    assert hits[0].match_line == water.start_line + 3
    # Russian identifiers, case folded.
    russian = await find(db, fake, access, r"^DEF полить\(", "regex", case_sensitive=False)
    assert [(h.symbol, h.match_line) for h in russian] == [("полить", 16)]
    # Line mode: . does not cross a line break.
    assert await find(db, fake, access, r"litres.*LIMIT // 2", "regex") == []
    # Embedded options are kept.
    assert len(await find(db, fake, access, r"(?i)^def WATER", "regex")) == 2


async def test_an_invalid_regex_is_the_callers_mistake(
    db: Conn, fake: FakeEmbedder, corpus: Corpus
) -> None:
    access = reader(corpus.scope)

    with pytest.raises(InvalidSearchError, match="regular expression"):
        await find(db, fake, access, "(unclosed", "regex")

    assert len(await find(db, fake, access, "plant.soil", "exact")) == 3


async def test_a_pathological_regex_is_cut_off_by_the_timeout(
    db: Conn, fake: FakeEmbedder, corpus: Corpus
) -> None:
    access = reader(corpus.scope, corpus.scope.acme)
    options = SearchOptions(timeout=0.3)

    started = time.monotonic()
    with pytest.raises(SearchTimeoutError, match=r"stopped after 0\.3 s"):
        await find(db, fake, access, CATASTROPHIC, "regex", options=options)
    elapsed = time.monotonic() - started

    assert elapsed < 5
    # The connection is usable at once: the transaction was rolled back.
    assert len(await find(db, fake, access, "end = True", "exact", options=options)) == 1


async def test_symbol_finds_definitions_by_name_or_qualified_name(
    db: Conn, fake: FakeEmbedder, corpus: Corpus
) -> None:
    access = reader(corpus.scope, corpus.scope.acme)

    water = await find(db, fake, access, "water", "symbol")
    assert [(h.source, h.definition and h.definition.qualified_name) for h in water] == [
        ("greenhouse", "water"),
        ("greenhouse-fork", "water"),
    ]
    definition = water[0].definition
    assert definition is not None
    assert (definition.kind, definition.path, definition.start_line, definition.end_line) == (
        "function",
        PATH,
        corpus.chunk("greenhouse", "water").start_line,
        corpus.chunk("greenhouse", "water").end_line,
    )
    assert water[0].chunk_id == corpus.chunk("greenhouse", "water").id

    method = await find(db, fake, access, "Lamp.on", "symbol")
    assert {(h.definition.name, h.definition.parent) for h in method if h.definition} == {
        ("on", "Lamp")
    }
    by_name = await find(db, fake, access, "on", "symbol")
    assert {h.definition.qualified_name for h in by_name if h.definition} == {"Lamp.on"}
    russian = await find(db, fake, access, "полить", "symbol")
    assert [h.definition.qualified_name for h in russian if h.definition] == ["полить"]


async def test_symbol_prefix_and_case(db: Conn, fake: FakeEmbedder, corpus: Corpus) -> None:
    access = reader(corpus.scope, corpus.scope.acme, workspaces=[corpus.scope.acme])
    repo = SearchFilters(repositories=("greenhouse",))

    def names(hits: list[Hit]) -> list[str]:
        return [h.definition.qualified_name for h in hits if h.definition]

    assert names(await find(db, fake, access, "Lam", "symbol", filters=repo)) == []
    prefix = await find(db, fake, access, "Lam", "symbol", filters=repo, symbol_match="prefix")
    assert names(prefix) == ["Lamp"]
    members = await find(db, fake, access, "Lamp.", "symbol", filters=repo, symbol_match="prefix")
    assert names(members) == ["Lamp.on"]
    assert names(await find(db, fake, access, "lamp", "symbol", filters=repo)) == []
    folded = await find(db, fake, access, "lamp", "symbol", filters=repo, case_sensitive=False)
    assert names(folded) == ["Lamp"]
    # Whole names first, then shorter ones.
    total = await find(
        db, fake, access, "total", "symbol", symbol_match="prefix", case_sensitive=False
    )
    assert names(total) == ["totalHarvest"]


# --- Filters -------------------------------------------------------------------------------------


QUERIES: dict[Mode, str] = {
    "hybrid": "plant water soil",
    "dense": "",  # the text of the water chunk, set in the test
    "lexical": "plant",
    "exact": "plant",
    "regex": r"plant\.\w+",
    "symbol": "water",
}


@pytest.mark.parametrize("mode", MODES)
async def test_filters_apply_to_every_retriever(
    db: Conn, fake: FakeEmbedder, corpus: Corpus, mode: Mode
) -> None:
    text = QUERIES[mode] or text_of(corpus.chunk("greenhouse", "water"))
    filters = SearchFilters(repositories=("greenhouse-fork",), paths=("src/*.py",))

    hits = await find(db, fake, reader(corpus.scope), text, mode, filters=filters, limit=50)

    assert hits
    assert {(h.workspace, h.source, h.path) for h in hits} == {("acme", "greenhouse-fork", PATH)}


async def test_the_workspaces_filter(db: Conn, fake: FakeEmbedder, corpus: Corpus) -> None:
    everything = reader(corpus.scope)
    text = "plant.soil += amount"

    other = await find(
        db, fake, everything, text, "exact", filters=SearchFilters(workspaces=("other",))
    )
    by_id = await find(
        db, fake, everything, text, "exact", filters=SearchFilters(workspaces=(corpus.scope.acme,))
    )

    assert places(other) == [("other", "greenhouse", PATH)]
    assert {h.workspace for h in by_id} == {"acme"}
    unknown = SearchFilters(workspaces=("no-such-workspace",))
    assert await find(db, fake, everything, text, "exact", filters=unknown) == []


async def test_the_kinds_filter(db: Conn, fake: FakeEmbedder, corpus: Corpus) -> None:
    access = reader(corpus.scope)

    documents = await find(
        db, fake, access, "журнал поливов", "exact", filters=SearchFilters(kinds=("document",))
    )
    code = await find(
        db, fake, access, "журнал поливов", "exact", filters=SearchFilters(kinds=("code_file",))
    )

    assert [(h.record_kind, _label(corpus, h)) for h in documents] == [("document", "watering")]
    assert [(h.record_kind, _label(corpus, h)) for h in code] == [("code_file", "ogorod")]
    threads = SearchFilters(kinds=("thread", "meeting"))
    assert await find(db, fake, access, "журнал", "lexical", filters=threads) == []


async def test_the_sources_filter_by_name_or_id(
    db: Conn, fake: FakeEmbedder, corpus: Corpus
) -> None:
    access = reader(corpus.scope)

    notes = await find(
        db, fake, access, "полив", "exact", filters=SearchFilters(sources=("notes",))
    )
    fork = await find(
        db, fake, access, "plant", "exact", filters=SearchFilters(sources=(corpus.scope.fork,))
    )
    # A name matches the source of that name in every workspace searched.
    named = await find(
        db, fake, access, "plant.soil", "exact", filters=SearchFilters(sources=("greenhouse",))
    )

    assert {(h.source, h.source_kind, h.record_kind) for h in notes} == {
        ("notes", "push", "document")
    }
    assert {h.source for h in fork} == {"greenhouse-fork"}
    assert sorted(places(named)) == [("acme", "greenhouse", PATH), ("other", "greenhouse", PATH)]
    missing = SearchFilters(sources=("no-such-source",))
    assert await find(db, fake, access, "plant", "exact", filters=missing) == []


async def test_the_repositories_filter_takes_git_sources_only(
    db: Conn, fake: FakeEmbedder, corpus: Corpus
) -> None:
    access = reader(corpus.scope)

    fork = await find(
        db,
        fake,
        access,
        "plant",
        "exact",
        filters=SearchFilters(repositories=("greenhouse-fork",)),
    )
    notes = await find(
        db, fake, access, "полив", "exact", filters=SearchFilters(repositories=("notes",))
    )
    both = await find(
        db,
        fake,
        access,
        "plant",
        "exact",
        filters=SearchFilters(sources=("greenhouse", "notes"), repositories=("greenhouse-fork",)),
    )

    assert {(h.repository, h.source_kind) for h in fork} == {("greenhouse-fork", "git")}
    assert notes == []
    assert both == []  # the two filters apply together


@pytest.mark.parametrize(
    ("glob", "expected"),
    [
        ("*.py", {"greenhouse", "fork", "ogorod"}),
        ("src/*.py", {"greenhouse", "fork"}),
        ("src/**/*.py", {"greenhouse", "fork", "ogorod"}),
        ("src/огород", {"ogorod"}),
        ("**/ledger.ts", {"ledger"}),
        ("web/", {"ledger"}),
        ("*.txt", {"pattern"}),
        ("*.md", set[str]()),  # documents have no path
        ("greenhouse.py", {"greenhouse", "fork"}),
        ("src/greenhouse.p?", {"greenhouse", "fork"}),
        ("src/greenhouse.p", set[str]()),
    ],
)
async def test_the_paths_filter(
    db: Conn, fake: FakeEmbedder, corpus: Corpus, glob: str, expected: set[str]
) -> None:
    filters = SearchFilters(paths=(glob,))

    hits = await find(
        db, fake, reader(corpus.scope, corpus.scope.acme), "e", "exact", filters=filters, limit=100
    )

    assert {_label(corpus, h) for h in hits} == expected


async def test_the_languages_filter(db: Conn, fake: FakeEmbedder, corpus: Corpus) -> None:
    access = reader(corpus.scope, corpus.scope.acme)

    typescript = await find(
        db, fake, access, "total", "exact", filters=SearchFilters(languages=("typescript",))
    )
    python = await find(
        db, fake, access, "total", "exact", filters=SearchFilters(languages=("python", "go"))
    )

    assert [(h.language, h.path) for h in typescript] == [("typescript", LEDGER_PATH)]
    assert {(h.language, h.path) for h in python} == {("python", PATH), ("python", OGOROD_PATH)}


async def test_the_time_range_filter(db: Conn, fake: FakeEmbedder, corpus: Corpus) -> None:
    access = reader(corpus.scope, corpus.scope.acme)
    notes = ("notes",)

    async def labels(**window: datetime) -> list[str]:
        filters = SearchFilters(sources=notes, **window)  # type: ignore[arg-type]
        hits = await find(db, fake, access, ".", "exact", filters=filters, limit=100)
        return sorted(_label(corpus, h) for h in hits)

    assert await labels() == ["pruning", "watering"]
    assert await labels(since=at(3, 10)) == ["pruning", "watering"]  # since is inclusive
    assert await labels(since=at(3, 11)) == ["pruning"]
    assert await labels(until=at(5, 20)) == ["watering"]  # until is exclusive
    assert await labels(since=at(3, 1), until=at(4, 1)) == ["watering"]
    assert await labels(since=at(6, 1)) == []


async def test_filters_combine(db: Conn, fake: FakeEmbedder, corpus: Corpus) -> None:
    filters = SearchFilters(
        workspaces=("acme",),
        repositories=("greenhouse",),
        kinds=("code_file",),
        paths=("src/**",),
        languages=("python",),
        since=at(3, 1),
        until=at(5, 1),
    )

    for mode, text in (("exact", "total"), ("lexical", "total"), ("symbol", "harvest")):
        hits = await find(db, fake, reader(corpus.scope), text, mode, filters=filters)  # type: ignore[arg-type]
        assert {(h.path, h.language) for h in hits} == {(OGOROD_PATH, "python")}


async def test_the_persons_filter_is_not_supported_yet(
    db: Conn, fake: FakeEmbedder, corpus: Corpus
) -> None:
    filters = SearchFilters(persons=("me",))
    fake.reset()

    for mode in MODES:
        with pytest.raises(UnsupportedFilterError, match="persons"):
            await find(db, fake, reader(corpus.scope), "water", mode, filters=filters)
    assert fake.texts == []


# --- Tombstones and isolation --------------------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
async def test_a_tombstoned_record_never_appears(
    db: Conn, fake: FakeEmbedder, corpus: Corpus, mode: Mode
) -> None:
    old = corpus.chunk("old_watering")
    text = {"exact": "раз в неделю", "regex": "раз в недел.", "lexical": "неделю"}.get(
        mode, text_of(old)
    )

    hits = await find(db, fake, reader(corpus.scope), text, mode, limit=100)

    assert old.id not in {h.chunk_id for h in hits}
    stored = await db.execute("SELECT count(*) FROM chunk WHERE id = %s", (old.id,))
    assert await stored.fetchone() == (1,)


async def test_a_workspace_limited_token_sees_only_its_workspace(
    db: Conn, fake: FakeEmbedder, corpus: Corpus
) -> None:
    water = corpus.chunk("other", "water")
    limited = reader(corpus.scope, corpus.scope.acme)
    everything = reader(corpus.scope)

    for mode, text in (
        ("exact", "plant.soil"),
        ("lexical", "plant"),
        ("dense", text_of(water)),
        ("hybrid", text_of(water)),
        ("symbol", "water"),
        ("regex", "plant"),
    ):
        seen = await find(db, fake, limited, text, mode, limit=100)  # type: ignore[arg-type]
        assert seen
        assert {h.workspace for h in seen} == {"acme"}, mode
        assert {h.workspace for h in await find(db, fake, everything, text, mode, limit=100)} == {  # type: ignore[arg-type]
            "acme",
            "other",
        }, mode
    # Asking for another workspace narrows; it never widens.
    other = SearchFilters(workspaces=("other",))
    assert await find(db, fake, limited, "plant", "exact", filters=other) == []


async def test_a_workspace_with_nothing_indexed_gives_nothing(db: Conn, fake: FakeEmbedder) -> None:
    scope = await make_scope(db)

    for mode in MODES:
        assert await find(db, fake, reader(scope), "water", mode) == []
    assert fake.batches == []  # nothing to search, nothing embedded


# --- Index mismatch ------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
async def test_a_changed_model_refuses_every_mode_before_embedding(
    db: Conn, corpus: Corpus, mode: Mode
) -> None:
    other_model = FakeEmbedder(OTHER_MODEL)

    with pytest.raises(IndexMismatchError, match=r"memini reindex --workspace acme"):
        await find(db, other_model, reader(corpus.scope), "water", mode)

    assert other_model.batches == []


async def test_a_changed_chunker_refuses_exact_search_in_that_workspace(
    db: Conn, fake: FakeEmbedder, corpus: Corpus
) -> None:
    old = IndexFingerprint(fake.spec, "synthetic-chunker-0")
    await db.execute(
        "UPDATE index_state SET chunker_version = %s, fingerprint = %s WHERE workspace_id = %s",
        (old.chunker_version, old.digest, corpus.scope.acme),
    )

    with pytest.raises(IndexMismatchError, match="changed: chunker version") as refused:
        await find(db, fake, reader(corpus.scope), "plant", "exact")
    assert refused.value.workspace == "acme"

    # Another workspace's index is current: a search limited to it goes on.
    other = await find(
        db,
        fake,
        reader(corpus.scope),
        "plant",
        "exact",
        filters=SearchFilters(workspaces=("other",)),
    )
    assert {h.workspace for h in other} == {"other"}


# --- The vector index ----------------------------------------------------------------------------


def many_functions(count: int) -> bytes:
    """Functions of seven lines each, too long to be merged: one chunk each."""
    return "\n\n".join(
        f"def task_{i}(x):\n    a = x + {i}\n    b = a * 2\n    c = b - 1\n"
        "    d = c + a\n    e = d * b\n    return e\n"
        for i in range(count)
    ).encode()


async def test_the_dense_retriever_uses_the_hnsw_index_under_a_workspace_filter(
    db: Conn, migrated_database_url: str, fake: FakeEmbedder, corpus: Corpus
) -> None:
    # Many chunks in another workspace crowd the neighbourhood of any query.
    await put(
        db,
        fake,
        workspace=corpus.scope.other,
        owner=corpus.scope.owner,
        source=corpus.scope.other_repo,
        external_id="main:src/many.py",
        content=many_functions(200),
        path="src/many.py",
        occurred_at=at(1, 2),
    )
    await db.execute("ANALYZE chunk")
    in_acme = await db.execute(
        "SELECT count(*) FROM chunk c JOIN record r ON r.id = c.record_id"
        " WHERE c.workspace_id = %s AND r.deleted_at IS NULL",
        (corpus.scope.acme,),
    )
    row = await in_acme.fetchone()
    assert row is not None
    acme_chunks: int = row[0]
    index = VectorIndex.of(fake.spec)
    access = reader(corpus.scope, corpus.scope.acme)
    (vector,) = await fake.embed(["water the plants"], kind="query")
    scope = ChunkScope(
        access=access,
        workspaces={corpus.scope.acme: "acme"},
        index=index,
        source_ids=None,
        filters=SearchFilters(),
    )
    statement = dense_statement(scope, vector, 50)

    # A fresh connection: pgvector's settings are set before its library is loaded.
    async with connect(migrated_database_url, autocommit=True) as fresh:
        # A small workspace is cheaper to sort exactly; make the planner show the index path.
        await fresh.execute("SET enable_seqscan = off; SET enable_sort = off")
        hits = await search(
            fresh, fake, access, SearchQuery(text="water the plants", mode="dense", limit=50)
        )
        async with fresh.transaction():
            await fresh.execute(
                "SET LOCAL hnsw.ef_search = 40; SET LOCAL hnsw.iterative_scan = relaxed_order"
            )
            cur = await fresh.execute(
                sql.SQL("EXPLAIN {}").format(statement.query), statement.params
            )
            plan = "\n".join(line for (line,) in await cur.fetchall())
            iterative = await (await fresh.execute(statement.query, statement.params)).fetchall()
        async with fresh.transaction():
            await fresh.execute(
                "SET LOCAL hnsw.ef_search = 40; SET LOCAL hnsw.iterative_scan = off"
            )
            plain = await (await fresh.execute(statement.query, statement.params)).fetchall()

    assert f"Index Scan using {index.name} on chunk c" in plan
    assert f"embedding_key = '{index.embedding_key}'" not in plan  # matched by the partial index
    # The iterative scan goes on past ef_search until every acme chunk is found; a plain scan
    # stops at the 40 nearest of all workspaces, few of them in acme.
    assert len(iterative) == acme_chunks
    assert len(plain) < acme_chunks
    assert len(hits) == acme_chunks
    assert {h.workspace for h in hits} == {"acme"}


@pytest.mark.parametrize(
    ("mode", "text", "index"),
    [
        ("lexical", "plant water", "chunk_lexical_idx"),
        ("exact", "plant.soil", "chunk_content_trgm_idx"),
        ("regex", r"plant\.soil \+= \w+", "chunk_content_trgm_idx"),
    ],
)
async def test_the_lexical_exact_and_regex_retrievers_can_use_their_gin_indexes(
    db: Conn, fake: FakeEmbedder, corpus: Corpus, mode: str, text: str, index: str
) -> None:
    await put(
        db,
        fake,
        workspace=corpus.scope.acme,
        owner=corpus.scope.owner,
        source=corpus.scope.fork,
        external_id="main:src/many.py",
        content=many_functions(300),
        path="src/many.py",
        occurred_at=at(1, 2),
    )
    # VACUUM moves new rows from the GIN pending lists into the indexes, as autovacuum would.
    await db.execute("VACUUM ANALYZE chunk")
    scope = ChunkScope(
        access=reader(corpus.scope),
        workspaces={corpus.scope.acme: "acme", corpus.scope.other: "other"},
        index=VectorIndex.of(fake.spec),
        source_ids=None,
        filters=SearchFilters(),
    )
    if mode == "lexical":
        statement = retrieval.lexical_statement(scope, text, 50)
    else:
        statement = retrieval.match_statement(
            scope,
            "exact" if mode == "exact" else "regex",
            text,
            case_sensitive=True,
            limit=50,
        )

    async with db.transaction():
        await db.execute("SET LOCAL enable_seqscan = off")
        cur = await db.execute(sql.SQL("EXPLAIN {}").format(statement.query), statement.params)
        plan = "\n".join(line for (line,) in await cur.fetchall())

    assert f"Bitmap Index Scan on {index}" in plan


# --- Hits ----------------------------------------------------------------------------------------


async def test_a_code_hit_carries_its_source_kind_time_and_place(
    db: Conn, fake: FakeEmbedder, corpus: Corpus
) -> None:
    water = corpus.chunk("greenhouse", "water")
    filters = SearchFilters(repositories=("greenhouse",), workspaces=("acme",))

    (hit,) = await find(db, fake, reader(corpus.scope), "def water", "exact", filters=filters)

    assert hit.chunk_id == water.id
    assert hit.record_id == corpus.records["greenhouse"]
    assert (hit.workspace_id, hit.workspace) == (corpus.scope.acme, "acme")
    assert (hit.source_id, hit.source, hit.source_kind) == (corpus.scope.repo, "greenhouse", "git")
    assert (hit.record_kind, hit.chunk_kind, hit.language) == ("code_file", "function", "python")
    assert (hit.path, hit.symbol, hit.start_line, hit.end_line) == (
        PATH,
        "water",
        water.start_line,
        water.end_line,
    )
    assert (hit.external_id, hit.external_url) == (f"main:{PATH}", None)
    assert hit.occurred_at == at(1, 2)
    assert (hit.header, hit.snippet, hit.truncated) == (water.header, water.content, False)
    assert hit.commit is None  # until the git connector records pins
    assert hit.repository == "greenhouse"
    assert hit.locator == f"greenhouse:{PATH}#L{water.start_line}-L{water.end_line}"


async def test_a_document_hit_carries_its_url(db: Conn, fake: FakeEmbedder, corpus: Corpus) -> None:
    (hit,) = await find(db, fake, reader(corpus.scope), "дважды в день", "exact")

    assert (hit.source, hit.source_kind, hit.record_kind) == ("notes", "push", "document")
    assert (hit.path, hit.language, hit.start_line, hit.commit) == (None, None, 1, None)
    assert hit.external_url == "https://notes.example.invalid/watering"
    assert hit.locator == hit.external_url
    assert hit.occurred_at == at(3, 10)
    assert hit.snippet == WATERING  # stored text, as it is


async def test_snippets_are_cut_to_the_option(db: Conn, fake: FakeEmbedder, corpus: Corpus) -> None:
    options = SearchOptions(snippet_chars=12)

    (hit,) = await find(db, fake, reader(corpus.scope), "дважды в день", "exact", options=options)

    assert (hit.snippet, hit.truncated) == (WATERING[:12], True)


async def test_the_commit_comes_from_the_records_stream(
    db: Conn, fake: FakeEmbedder, corpus: Corpus, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[object] = []

    def pinned(kind: str, cursor: object) -> str | None:
        seen.append(cursor)
        return "c0ffee" if isinstance(cursor, dict) else None

    monkeypatch.setattr(retrieval, "pinned_commit", pinned)

    hits = await find(db, fake, reader(corpus.scope), "plant.soil", "exact")

    assert {(h.source, h.workspace, h.commit) for h in hits} == {
        ("greenhouse", "acme", "c0ffee"),  # stored through the stream of refs/heads/main
        ("greenhouse-fork", "acme", None),
        ("greenhouse", "other", None),
    }
    assert {"synthetic_pin": "c0ffee"} in seen
    water = next(h for h in hits if h.commit)
    assert water.locator.startswith(f"greenhouse@c0ffee:{PATH}#L")


# --- Reranking and the connection ----------------------------------------------------------------


class Reverse:
    """A reranker that reverses the order, so its effect is plain to see."""

    def __init__(self) -> None:
        self.seen: list[int] = []

    async def rerank(self, query: str, hits: Sequence[Hit]) -> list[Hit]:
        self.seen.append(len(hits))
        return [replace(h, score=float(i)) for i, h in enumerate(reversed(hits))]


async def test_a_reranker_reorders_the_top_of_a_ranked_search(
    db: Conn, fake: FakeEmbedder, corpus: Corpus
) -> None:
    access = reader(corpus.scope)
    options = SearchOptions(rerank_depth=5)
    query = SearchQuery(text="plant water soil", limit=3)
    reverse = Reverse()

    plain = await search(db, fake, access, replace(query, limit=5), options=options)
    reranked = await search(db, fake, access, query, options=options, reranker=reverse)

    assert reverse.seen == [5]
    assert [h.chunk_id for h in reranked] == [h.chunk_id for h in reversed(plain)][:3]
    exact = await search(
        db, fake, access, SearchQuery(text="plant", mode="exact", limit=3), reranker=reverse
    )
    assert reverse.seen == [5]  # exact, regex and symbol order by place: no reranking
    assert len(exact) == 3


async def test_search_needs_a_connection_without_a_transaction(
    db: Conn, fake: FakeEmbedder, corpus: Corpus
) -> None:
    async with db.transaction():
        with pytest.raises(RuntimeError, match="transaction of its own"):
            await find(db, fake, reader(corpus.scope), "plant", "exact")


async def test_search_leaves_a_non_autocommit_connection_idle(
    migrated_database_url: str, fake: FakeEmbedder, corpus: Corpus
) -> None:
    async with connect(migrated_database_url) as conn:
        for mode in MODES:
            await find(conn, fake, reader(corpus.scope), "water", mode)
            assert conn.info.transaction_status.name == "IDLE"
