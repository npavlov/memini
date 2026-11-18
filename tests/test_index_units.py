"""Parts of the index layer that need no database: vector index names and expressions, counts,
raw content providers. Every value is synthetic."""

import uuid

import pytest
from psycopg import sql

from memini.chunking import chunker_fingerprint
from memini.embeddings import EmbeddingSpec
from memini.ids import RecordKind
from memini.index import (
    ByKind,
    IndexCounts,
    MirrorNotAvailable,
    RawContentUnavailableError,
    RawDocument,
    RecordRef,
    VectorIndex,
    chunk_code,
    current_fingerprint,
    embedding_key,
)
from memini.redaction import REDACTION_VERSION

SPEC = EmbeddingSpec(provider="openai_compatible", model="synthetic-embed", dimension=768)


def record(kind: RecordKind = "code_file") -> RecordRef:
    return RecordRef(
        id="a" * 64,
        workspace_id=uuid.UUID(int=1),
        owner_id=uuid.UUID(int=2),
        source_id=uuid.UUID(int=3),
        kind=kind,
        external_id="main:src/greenhouse.py",
        content_hash="b" * 40,
        deleted=False,
    )


def render(composable: sql.Composable) -> str:
    """SQL as it would be sent; literals quoted without a connection."""
    return composable.as_string(None)


# --- Vector indexes ------------------------------------------------------------------------------


def test_a_vector_index_is_named_after_its_key() -> None:
    index = VectorIndex.of(SPEC)

    assert index.embedding_key == embedding_key(SPEC)
    assert index.name == "chunk_embedding_" + embedding_key(SPEC)[:32]
    assert len(index.name) <= 63  # an identifier in Postgres


def test_the_index_and_the_search_use_the_same_expression() -> None:
    index = VectorIndex.of(SPEC)
    key = index.embedding_key

    assert render(index.create_statement()) == (
        f'CREATE INDEX IF NOT EXISTS "{index.name}" ON chunk USING hnsw'
        f' (("embedding"::vector(768)) vector_cosine_ops) WHERE "embedding_key" = \'{key}\''
    )
    assert render(index.predicate("c")) == f'"c"."embedding_key" = \'{key}\''
    assert render(index.distance(sql.Placeholder(), "c")) == (
        '("c"."embedding"::vector(768)) <=> (%s::vector(768))'
    )


@pytest.mark.parametrize(
    ("dimension", "cast_type", "indexed"),
    [
        (1, "vector", True),
        (2000, "vector", True),
        (2001, "halfvec", True),
        (4000, "halfvec", True),
        (4001, "vector", False),
        (16000, "vector", False),
    ],
)
def test_wide_vectors_are_indexed_as_halfvec_up_to_4000(
    dimension: int, cast_type: str, indexed: bool
) -> None:
    index = VectorIndex("c" * 64, dimension)

    assert index.cast_type == cast_type
    assert index.indexed == indexed
    if not indexed:
        with pytest.raises(ValueError, match="no HNSW index"):
            index.create_statement()


@pytest.mark.parametrize(("key", "dimension"), [("C" * 64, 8), ("c" * 63, 8), ("c" * 64, 0)])
def test_a_vector_index_needs_a_key_and_a_dimension(key: str, dimension: int) -> None:
    with pytest.raises(ValueError, match=r"an embedding key|a vector dimension"):
        VectorIndex(key, dimension)


def test_the_key_goes_into_the_query_as_a_literal_that_cannot_break_out() -> None:
    # Keys are checked hex, so a quote never reaches the literal; the check is the guard.
    with pytest.raises(ValueError, match="64 lowercase hex"):
        VectorIndex("'" + "c" * 63, 8)


# --- Fingerprint ---------------------------------------------------------------------------------


def test_the_current_fingerprint_covers_the_chunker_its_grammars_and_the_redaction() -> None:
    fingerprint = current_fingerprint(SPEC)

    assert fingerprint.embedding == SPEC
    # Chunk text is what the chunker cut, with its secrets redacted by the writer.
    assert fingerprint.chunker_version == f"{chunker_fingerprint()} redaction/{REDACTION_VERSION}"
    assert fingerprint.chunker_version.startswith("memini-chunker/")


# --- Counts --------------------------------------------------------------------------------------


def test_counts_add_up() -> None:
    first = IndexCounts(inserted=2, embedded=2, symbols_inserted=1)
    second = IndexCounts(unchanged=3, deleted=1, reused=1)

    total = first + second

    assert total == IndexCounts(
        inserted=2, unchanged=3, deleted=1, embedded=2, reused=1, symbols_inserted=1
    )
    assert total.chunks == 5
    assert total.changed


def test_unchanged_rows_and_embeddings_alone_are_no_change() -> None:
    assert not IndexCounts(unchanged=4, symbols_unchanged=5).changed
    assert not IndexCounts().changed
    assert IndexCounts(symbols_deleted=1).changed


def test_counts_without_a_run_go_nowhere() -> None:
    IndexCounts(inserted=1).report()  # no run is being recorded: nothing happens


# --- Raw content ---------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_code_is_refused_without_a_mirror_reader() -> None:
    with pytest.raises(RawContentUnavailableError, match="no reader for repository mirrors"):
        await MirrorNotAvailable().read(record())


@pytest.mark.anyio
async def test_by_kind_reads_each_kind_from_its_provider() -> None:
    class Constant:
        def __init__(self, content: bytes) -> None:
            self.content = content

        async def read(self, record: RecordRef) -> RawDocument | None:
            return RawDocument(self.content)

    provider = ByKind({"thread": Constant(b"thread"), "meeting": Constant(b"meeting")})

    assert await provider.read(record("thread")) == RawDocument(b"thread")
    assert await provider.read(record("meeting")) == RawDocument(b"meeting")
    assert await provider.read(record("document")) is None


def test_code_needs_its_path() -> None:
    with pytest.raises(ValueError, match="has no path"):
        chunk_code(record(), RawDocument(b"x = 1\n"))

    chunks = chunk_code(record(), RawDocument(b"x = 1\n", path="src/settings.py"))
    assert chunks.language == "python"
    assert [chunk.header for chunk in chunks.chunks] == ["src/settings.py: x"]
