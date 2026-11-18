"""The index writer against a real Postgres (migration 0005): upsert by id, skip unchanged, delete
vanished. Acceptance of memini-sg8: indexing unchanged input again makes no embedding call and
changes no row. Every value is synthetic; vectors come from the FakeEmbedder."""

from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import pytest
from psycopg import AsyncConnection, errors, sql
from psycopg.rows import TupleRow

from index_helpers import (
    FILE_V1,
    LAMP,
    PATH,
    PRUNE,
    WATER,
    WATER_EDITED,
    Scope,
    add_record,
    chunks_of,
    make_scope,
    rows,
    source,
    versions,
)
from memini.chunking import chunk_file
from memini.db import connect
from memini.embeddings import EmbeddingError, EmbeddingSpec, FakeEmbedder
from memini.embeddings.base import finish_vector
from memini.embeddings.fake import fake_vector
from memini.index import (
    IndexCounts,
    IndexMismatchError,
    RecordNotFoundError,
    VectorIndex,
    check_index,
    current_fingerprint,
    delete_record_chunks,
    embedding_key,
    ensure_vector_index,
    index_record,
    read_index_state,
    vector_index_exists,
)
from memini.runs import record_run

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

type Conn = AsyncConnection[TupleRow]

OTHER_MODEL = EmbeddingSpec(provider="openai_compatible", model="fake-embedder-2", dimension=12)


@pytest.fixture
async def db(migrated_database_url: str) -> AsyncIterator[Conn]:
    async with connect(migrated_database_url, autocommit=True) as conn:
        yield conn


@pytest.fixture
async def scope(db: Conn) -> Scope:
    return await make_scope(db)


@pytest.fixture
def fake() -> FakeEmbedder:
    return FakeEmbedder()


async def index_file(
    db: Conn,
    fake: FakeEmbedder,
    scope: Scope,
    content: bytes,
    *,
    source_id: Any = None,
    workspace_id: Any = None,
    path: str = PATH,
) -> tuple[str, IndexCounts]:
    rid = await add_record(
        db,
        workspace_id=workspace_id or scope.acme,
        owner_id=scope.owner,
        source_id=source_id or scope.repo,
        external_id=f"main:{path}",
        content=content,
    )
    counts = await index_record(
        db, fake, record_id=rid, path=path, chunks=chunk_file(rid, path, content)
    )
    return rid, counts


# --- First write ---------------------------------------------------------------------------------


async def test_a_record_is_stored_with_chunks_vectors_and_symbols(
    db: Conn, scope: Scope, fake: FakeEmbedder
) -> None:
    rid, counts = await index_file(db, fake, scope, FILE_V1)

    expected = chunks_of(rid, FILE_V1)
    assert counts == IndexCounts(inserted=4, embedded=4, symbols_inserted=5)
    assert fake.texts == [chunk.text for chunk in expected.chunks]
    stored = await rows(
        db,
        "SELECT id, record_kind, key, kind, language, path, symbol, start_line, end_line,"
        " header || E'\\n' || content, text_hash, embedding_key, embedding::real[]"
        " FROM chunk ORDER BY start_line",
    )
    key = embedding_key(fake.spec)
    assert stored == [
        (
            chunk.id,
            "code_file",
            chunk.key,
            chunk.kind,
            "python",
            PATH,
            chunk.symbol,
            chunk.start_line,
            chunk.end_line,
            chunk.text,
            chunk.content_hash,
            key,
            finish_vector(fake_vector(chunk.text, 8), fake.spec),
        )
        for chunk in expected.chunks
    ]
    symbols = await rows(
        db,
        "SELECT qualified_name, kind, parent, chunk_id, path, start_line, language FROM symbol"
        " ORDER BY start_line, qualified_name",
    )
    assert symbols == [
        (s.qualified_name, s.kind, s.parent, s.chunk_id, PATH, s.start_line, "python")
        for s in sorted(expected.symbols, key=lambda s: (s.start_line, s.qualified_name))
    ]


async def test_the_first_write_records_the_fingerprint_and_creates_the_vector_index(
    db: Conn, scope: Scope, fake: FakeEmbedder
) -> None:
    await index_file(db, fake, scope, FILE_V1)

    state = await read_index_state(db, scope.acme)
    assert state is not None
    assert state.built_with == current_fingerprint(fake.spec)
    assert await vector_index_exists(db, VectorIndex.of(fake.spec))


# --- Acceptance: unchanged input changes nothing -------------------------------------------------


async def test_indexing_unchanged_input_again_embeds_nothing_and_changes_no_row(
    db: Conn, scope: Scope, fake: FakeEmbedder
) -> None:
    rid, _ = await index_file(db, fake, scope, FILE_V1)
    before = await versions(db)
    fake.reset()

    counts = await index_record(
        db, fake, record_id=rid, path=PATH, chunks=chunk_file(rid, PATH, FILE_V1)
    )

    assert fake.batches == []
    assert counts == IndexCounts(unchanged=4, symbols_unchanged=5)
    assert not counts.changed
    assert await versions(db) == before
    assert len(before) == 9


async def test_editing_one_function_embeds_only_its_chunk(
    db: Conn, scope: Scope, fake: FakeEmbedder
) -> None:
    rid, _ = await index_file(db, fake, scope, FILE_V1)
    vectors_before = dict(await rows(db, "SELECT id, embedding::real[] FROM chunk"))
    before = await versions(db)
    fake.reset()
    edited = source(WATER_EDITED, PRUNE, LAMP)

    _, counts = await index_file(db, fake, scope, edited)

    head, water, prune, lamp = chunks_of(rid, edited).chunks
    assert fake.texts == [water.text]
    # prune and Lamp keep their text and vector; only their byte offsets moved, and so did the
    # end of water and the offsets of every symbol but LIMIT.
    assert counts == IndexCounts(
        updated=3, unchanged=1, embedded=1, symbols_updated=4, symbols_unchanged=1
    )
    after = await versions(db)
    assert after[f"chunk:{head.id}"] == before[f"chunk:{head.id}"]
    vectors_after = dict(await rows(db, "SELECT id, embedding::real[] FROM chunk"))
    assert vectors_after[water.id] == finish_vector(fake_vector(water.text, 8), fake.spec)
    assert vectors_after[water.id] != vectors_before[water.id]
    for unchanged in (head, prune, lamp):
        assert vectors_after[unchanged.id] == vectors_before[unchanged.id]


async def test_an_edit_that_keeps_the_size_touches_only_its_chunk(
    db: Conn, scope: Scope, fake: FakeEmbedder
) -> None:
    rid, _ = await index_file(db, fake, scope, FILE_V1)
    before = await versions(db)
    fake.reset()
    edited = source(WATER.replace("min(litres", "max(litres"), PRUNE, LAMP)

    _, counts = await index_file(db, fake, scope, edited)

    water = next(c for c in chunks_of(rid, edited).chunks if c.symbol == "water")
    assert fake.texts == [water.text]
    assert counts == IndexCounts(updated=1, unchanged=3, embedded=1, symbols_unchanged=5)
    after = await versions(db)
    assert {name for name in after if after[name] != before[name]} == {f"chunk:{water.id}"}


async def test_a_chunk_that_only_moved_keeps_its_vector(
    db: Conn, scope: Scope, fake: FakeEmbedder
) -> None:
    rid, _ = await index_file(db, fake, scope, FILE_V1)
    fake.reset()
    moved = source(
        WATER,
        PRUNE,
        LAMP,
        head='"""Greenhouse."""\n\n# Synthetic.\n# Test.\n\nimport math\n\nLIMIT = 40\n',
    )

    _, counts = await index_file(db, fake, scope, moved)

    # The head changed; water, prune and Lamp moved down three lines and kept their text.
    assert counts.updated == 4
    assert counts.embedded == 1
    assert counts.symbols_updated == 5
    lines = await rows(db, "SELECT symbol, start_line FROM chunk WHERE symbol = 'water'")
    assert lines == [
        ("water", next(c.start_line for c in chunks_of(rid, moved).chunks if c.symbol == "water"))
    ]


async def test_removing_a_function_deletes_its_chunk_and_symbol(
    db: Conn, scope: Scope, fake: FakeEmbedder
) -> None:
    rid, _ = await index_file(db, fake, scope, FILE_V1)
    prune = next(c for c in chunks_of(rid, FILE_V1).chunks if c.symbol == "prune")
    fake.reset()

    _, counts = await index_file(db, fake, scope, source(WATER, LAMP))

    # Lamp moved up: its row and its symbols get their new place, not a new vector.
    assert counts == IndexCounts(
        updated=1,
        unchanged=2,
        deleted=1,
        symbols_updated=2,
        symbols_unchanged=2,
        symbols_deleted=1,
    )
    assert fake.batches == []
    assert await rows(db, "SELECT id FROM chunk WHERE id = %s", (prune.id,)) == []
    assert await rows(db, "SELECT qualified_name FROM symbol WHERE qualified_name = 'prune'") == []


async def test_a_text_seen_in_another_record_of_the_workspace_is_not_embedded_again(
    db: Conn, scope: Scope, fake: FakeEmbedder
) -> None:
    await index_file(db, fake, scope, FILE_V1)
    fake.reset()

    _, counts = await index_file(db, fake, scope, FILE_V1, source_id=scope.fork)

    assert fake.batches == []
    assert counts == IndexCounts(inserted=4, reused=4, symbols_inserted=5)


async def test_workspaces_are_isolated(db: Conn, scope: Scope, fake: FakeEmbedder) -> None:
    await index_file(db, fake, scope, FILE_V1)
    fake.reset()

    _, counts = await index_file(
        db, fake, scope, FILE_V1, source_id=scope.other_repo, workspace_id=scope.other
    )

    # Vectors never cross a workspace: they are derived from its private text.
    assert counts == IndexCounts(inserted=4, embedded=4, symbols_inserted=5)
    by_workspace = await rows(
        db,
        "SELECT w.name, count(*) FROM chunk c JOIN workspace w ON w.id = c.workspace_id"
        " GROUP BY w.name ORDER BY w.name",
    )
    assert by_workspace == [("acme", 4), ("other", 4)]
    # Each workspace has its own fingerprint row.
    assert await read_index_state(db, scope.other) is not None


async def test_counts_go_to_the_run_report(
    db: Conn, migrated_database_url: str, scope: Scope, fake: FakeEmbedder
) -> None:
    async with connect(migrated_database_url, autocommit=True) as reports:
        async with record_run(
            reports, workspace_id=scope.acme, owner_id=scope.owner, kind="git_sync"
        ) as run:
            await index_file(db, fake, scope, FILE_V1)
        counts = run.report()["counts"]

    assert counts["chunks_inserted"] == 4
    assert counts["texts_embedded"] == 4
    assert counts["symbols_inserted"] == 5
    assert counts["chunks_unchanged"] == 0


class _RolledBack(Exception):
    pass


async def test_in_the_callers_transaction_counts_are_left_to_the_caller(
    db: Conn, migrated_database_url: str, scope: Scope, fake: FakeEmbedder
) -> None:
    rid = await add_record(
        db,
        workspace_id=scope.acme,
        owner_id=scope.owner,
        source_id=scope.repo,
        external_id=f"main:{PATH}",
        content=FILE_V1,
    )

    async def rolled_back_batch() -> None:
        async with db.transaction():
            counts = await index_record(
                db, fake, record_id=rid, path=PATH, chunks=chunks_of(rid, FILE_V1)
            )
            assert counts.inserted == 4
            raise _RolledBack

    async with (
        connect(migrated_database_url, autocommit=True) as reports,
        record_run(reports, workspace_id=scope.acme, owner_id=scope.owner, kind="git_sync") as run,
    ):
        # A batch that rolls back: what its writer counted never reaches the report.
        with pytest.raises(_RolledBack):
            await rolled_back_batch()
        assert run.report()["counts"] == {}
        assert await rows(db, "SELECT count(*) FROM chunk") == [(0,)]

        # A batch that commits: the caller reports after the commit.
        async with db.transaction():
            counts = await index_record(
                db, fake, record_id=rid, path=PATH, chunks=chunks_of(rid, FILE_V1)
            )
        assert run.report()["counts"] == {}
        counts.report()
        assert run.report()["counts"]["chunks_inserted"] == 4

        # In a transaction of its own, the writer reports after its commit.
        await db.execute("UPDATE record SET deleted_at = now() WHERE id = %s", (rid,))
        await delete_record_chunks(db, record_id=rid)
        report = run.report()["counts"]

    assert report["chunks_deleted"] == 4
    assert report["symbols_deleted"] == 5


async def test_a_nul_character_is_stored_as_a_replacement_character(
    db: Conn, scope: Scope, fake: FakeEmbedder
) -> None:
    # Past the first 8000 bytes a NUL does not make the file binary; Postgres text cannot hold it.
    content = b"x = 1\n" * 2000 + b"y = '\x00'\n"
    path = "data/settings.txt"

    rid, counts = await index_file(db, fake, scope, content, path=path)

    assert counts.inserted > 0
    found = await rows(db, "SELECT id, text_hash FROM chunk WHERE strpos(content, U&'\\FFFD') > 0")
    (chunk,) = [
        c for c in chunk_file(rid, path, content).chunks if "\N{REPLACEMENT CHARACTER}" in c.content
    ]
    # The chunker replaced it, so the stored text is the chunk's text, hash and all.
    assert found == [(chunk.id, chunk.content_hash)]


async def test_a_chunk_with_a_nul_character_is_refused_and_nothing_is_written(
    db: Conn, scope: Scope, fake: FakeEmbedder
) -> None:
    rid, _ = await index_file(db, fake, scope, FILE_V1)
    before = await versions(db)
    good = chunk_file(rid, PATH, FILE_V1)
    bad = replace(good, chunks=(replace(good.chunks[0], content="x = '\x00'"), *good.chunks[1:]))
    fake.reset()

    with pytest.raises(ValueError, match="holds a NUL character"):
        await index_record(db, fake, record_id=rid, path=PATH, chunks=bad)

    assert fake.batches == []
    assert await versions(db) == before


# --- Refusals ------------------------------------------------------------------------------------


async def test_a_changed_model_refuses_the_write_and_changes_nothing(
    db: Conn, scope: Scope, fake: FakeEmbedder
) -> None:
    rid, _ = await index_file(db, fake, scope, FILE_V1)
    before = await versions(db)
    other = FakeEmbedder(OTHER_MODEL)

    with pytest.raises(IndexMismatchError, match=r"run `memini reindex --workspace acme`"):
        await index_record(
            db, other, record_id=rid, path=PATH, chunks=chunk_file(rid, PATH, FILE_V1)
        )
    with pytest.raises(IndexMismatchError):
        await check_index(db, scope.acme, current_fingerprint(OTHER_MODEL))

    assert other.batches == []
    assert await versions(db) == before


async def test_a_missing_or_deleted_record_is_refused(
    db: Conn, scope: Scope, fake: FakeEmbedder
) -> None:
    rid, _ = await index_file(db, fake, scope, FILE_V1)
    with pytest.raises(RecordNotFoundError):
        await index_record(
            db, fake, record_id="f" * 64, path=PATH, chunks=chunk_file("f" * 64, PATH, FILE_V1)
        )

    await db.execute("UPDATE record SET deleted_at = now() WHERE id = %s", (rid,))
    with pytest.raises(ValueError, match="is deleted"):
        await index_record(db, fake, record_id=rid, path=PATH, chunks=chunks_of(rid, FILE_V1))


async def test_a_failing_model_writes_nothing(db: Conn, scope: Scope) -> None:
    failing = FakeEmbedder(fail_with=EmbeddingError("synthetic outage"))

    with pytest.raises(EmbeddingError):
        await index_file(db, failing, scope, FILE_V1)

    assert await rows(db, "SELECT count(*) FROM chunk") == [(0,)]
    assert await rows(db, "SELECT count(*) FROM index_state") == [(0,)]


# --- Deletion ------------------------------------------------------------------------------------


async def test_a_tombstone_loses_its_chunks_and_symbols(
    db: Conn, scope: Scope, fake: FakeEmbedder
) -> None:
    rid, _ = await index_file(db, fake, scope, FILE_V1)
    await db.execute("UPDATE record SET deleted_at = now() WHERE id = %s", (rid,))

    counts = await delete_record_chunks(db, record_id=rid)

    assert counts == IndexCounts(deleted=4, symbols_deleted=5)
    assert await versions(db) == {}
    assert await delete_record_chunks(db, record_id=rid) == IndexCounts()
    assert await delete_record_chunks(db, record_id="e" * 64) == IndexCounts()


@pytest.mark.parametrize("deleted", ["record", "source", "workspace"])
async def test_deleting_the_record_its_source_or_workspace_deletes_its_chunks(
    db: Conn, scope: Scope, fake: FakeEmbedder, deleted: str
) -> None:
    rid, _ = await index_file(db, fake, scope, FILE_V1)
    await index_file(db, fake, scope, FILE_V1, source_id=scope.other_repo, workspace_id=scope.other)

    if deleted == "record":  # forget, retention
        await db.execute("DELETE FROM record WHERE id = %s", (rid,))
    elif deleted == "source":
        await db.execute("DELETE FROM source WHERE id = %s", (scope.repo,))
    else:
        await db.execute("DELETE FROM workspace WHERE id = %s", (scope.acme,))

    assert await rows(db, "SELECT DISTINCT workspace_id FROM chunk") == [(scope.other,)]
    assert await rows(db, "SELECT DISTINCT workspace_id FROM symbol") == [(scope.other,)]


# --- Constraints ---------------------------------------------------------------------------------


async def test_a_chunk_cannot_point_into_another_workspace_or_source(
    db: Conn, scope: Scope, fake: FakeEmbedder
) -> None:
    rid, _ = await index_file(db, fake, scope, FILE_V1)
    for column, value in (("workspace_id", scope.other), ("source_id", scope.fork)):
        with pytest.raises(errors.ForeignKeyViolation):
            await db.execute(
                sql.SQL("UPDATE chunk SET {} = %s WHERE record_id = %s").format(
                    sql.Identifier(column)
                ),
                (value, rid),
            )
    with pytest.raises(errors.ForeignKeyViolation):
        await db.execute("UPDATE chunk SET record_kind = 'document' WHERE record_id = %s", (rid,))


async def test_the_text_hash_must_match_the_text(
    db: Conn, scope: Scope, fake: FakeEmbedder
) -> None:
    rid, _ = await index_file(db, fake, scope, FILE_V1)

    with pytest.raises(errors.CheckViolation) as refused:
        await db.execute("UPDATE chunk SET content = content || ' ' WHERE record_id = %s", (rid,))

    assert refused.value.diag.constraint_name == "chunk_text_hash_check"


# --- Indexes -------------------------------------------------------------------------------------


async def test_lexical_trigram_and_vector_indexes_exist(
    db: Conn, scope: Scope, fake: FakeEmbedder
) -> None:
    await index_file(db, fake, scope, FILE_V1)
    index = VectorIndex.of(fake.spec)

    found = dict(
        await rows(db, "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'chunk'")
    )

    assert "USING gin (lexical)" in found["chunk_lexical_idx"]
    assert "USING gin (content gin_trgm_ops)" in found["chunk_content_trgm_idx"]
    assert "(workspace_id, embedding_key, text_hash)" in found["chunk_vector_reuse_idx"]
    assert found[index.name] == (
        f"CREATE INDEX {index.name} ON public.chunk USING hnsw (((embedding)::vector(8))"
        f" vector_cosine_ops) WHERE (embedding_key = '{index.embedding_key}'::text)"
    )
    symbol_indexes = dict(
        await rows(db, "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'symbol'")
    )
    assert "(workspace_id, name, source_id)" in symbol_indexes["symbol_workspace_name_idx"]
    assert (
        "(workspace_id, qualified_name, source_id)"
        in (symbol_indexes["symbol_workspace_qualified_name_idx"])
    )


async def test_a_nearest_neighbour_query_uses_the_hnsw_index(
    db: Conn, scope: Scope, fake: FakeEmbedder
) -> None:
    await index_file(db, fake, scope, FILE_V1)
    index = VectorIndex.of(fake.spec)
    (query_vector,) = await fake.embed(["how much water does a plant get"], kind="query")
    query = sql.SQL(
        "SELECT c.id FROM chunk c WHERE {predicate} AND c.workspace_id = %s"
        " ORDER BY {distance} LIMIT 3"
    ).format(predicate=index.predicate("c"), distance=index.distance(sql.Placeholder(), "c"))

    found = await rows(db, query, (scope.acme, query_vector))
    async with db.transaction():
        await db.execute("SET LOCAL enable_seqscan = off")
        plan = await rows(db, sql.SQL("EXPLAIN {}").format(query), (scope.acme, query_vector))

    assert len(found) == 3
    assert f"Index Scan using {index.name} on chunk c" in "\n".join(line for (line,) in plan)


async def test_lexical_and_trigram_searches_find_the_chunk(
    db: Conn, scope: Scope, fake: FakeEmbedder
) -> None:
    rid, _ = await index_file(db, fake, scope, FILE_V1)
    water = next(c for c in chunks_of(rid, FILE_V1).chunks if c.symbol == "water")

    lexical = await rows(
        db, "SELECT id FROM chunk WHERE lexical @@ websearch_to_tsquery('simple', 'plant water')"
    )
    exact = await rows(
        db, "SELECT id FROM chunk WHERE content LIKE %s", ("%plant.soil += amount%",)
    )
    regex = await rows(
        db, "SELECT id FROM chunk WHERE content ~ %s", (r"def \w+\(plant, litres\)",)
    )

    assert lexical == [(water.id,)]
    assert exact == [(water.id,)]
    assert regex == [(water.id,)]


@pytest.mark.parametrize(
    ("dimension", "expected"),
    [(3000, "((embedding)::halfvec(3000)) halfvec_cosine_ops"), (4001, None)],
)
async def test_large_dimensions_index_as_halfvec_or_not_at_all(
    db: Conn, dimension: int, expected: str | None
) -> None:
    spec = EmbeddingSpec(provider="openai_compatible", model="wide", dimension=dimension)
    index = VectorIndex.of(spec)

    async with db.transaction():
        created = await ensure_vector_index(db, index)

    assert created == (expected is not None)
    found = await rows(db, "SELECT indexdef FROM pg_indexes WHERE indexname = %s", (index.name,))
    if expected is None:
        assert found == []
    else:
        assert expected in found[0][0]
