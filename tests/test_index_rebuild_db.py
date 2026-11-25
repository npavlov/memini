"""Rebuilding a workspace's index from raw records (memini.index.reindex) against a real Postgres.

Acceptance of memini-sg8: a rebuild from raw produces the same chunk ids; a changed embedding model
is refused until the rebuild finishes, after which no chunk of the old key is left. Every value is
synthetic; vectors come from the FakeEmbedder, raw content from an in-memory store.
"""

from collections.abc import AsyncIterator
from typing import Any

import pytest
from psycopg import AsyncConnection
from psycopg.rows import TupleRow

from index_helpers import (
    FILE_V1,
    LAMP,
    PATH,
    PRUNE,
    WATER,
    WATER_EDITED,
    FakeRaw,
    Scope,
    add_record,
    blob_id,
    chunks_of,
    make_scope,
    rows,
    source,
    versions,
)
from memini.chunking import FileChunks, chunk_file, chunk_payload
from memini.db import connect
from memini.embeddings import EmbeddingSpec, FakeEmbedder
from memini.ids import RecordKind
from memini.index import (
    DEFAULT_CHUNKERS,
    IndexCounts,
    IndexMismatchError,
    RawContentUnavailableError,
    RawDocument,
    RecordChunker,
    RecordRef,
    ReindexInProgressError,
    StoredPayload,
    VectorIndex,
    check_index,
    current_fingerprint,
    default_raw_content,
    index_record,
    read_index_state,
    reindex,
    vector_index_exists,
)
from memini.index.rebuild import reindex_lock_key
from memini.ingest import canonical_json
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


@pytest.fixture
def raw() -> FakeRaw:
    return FakeRaw()


async def store_file(
    db: Conn,
    scope: Scope,
    raw: FakeRaw,
    content: bytes,
    *,
    path: str = PATH,
    source_id: Any = None,
) -> str:
    """A code record with ``content`` at ``path``, in the raw store too; not indexed."""
    rid = await add_record(
        db,
        workspace_id=scope.acme,
        owner_id=scope.owner,
        source_id=source_id or scope.repo,
        external_id=f"main:{path}",
        content=content,
    )
    raw.put(rid, content, path)
    return rid


async def index_file(
    db: Conn, fake: FakeEmbedder, scope: Scope, raw: FakeRaw, content: bytes, path: str = PATH
) -> str:
    """A code record, indexed by the writer as a sync would, and in the raw store."""
    rid = await store_file(db, scope, raw, content, path=path)
    await index_record(db, fake, record_id=rid, path=path, chunks=chunk_file(rid, path, content))
    return rid


async def snapshot(db: Conn) -> list[tuple[Any, ...]]:
    """What the index holds: every chunk with its text hash and vector, every symbol."""
    chunks = await rows(
        db,
        "SELECT id, record_id, key, start_line, text_hash, embedding_key, embedding::real[]"
        " FROM chunk ORDER BY id",
    )
    symbols = await rows(
        db,
        "SELECT record_id, qualified_name, ordinal, chunk_id, start_line FROM symbol"
        " ORDER BY record_id, qualified_name, ordinal",
    )
    return chunks + symbols


# --- Acceptance ----------------------------------------------------------------------------------


async def test_a_rebuild_of_a_current_index_embeds_nothing_and_changes_no_row(
    db: Conn, scope: Scope, fake: FakeEmbedder, raw: FakeRaw
) -> None:
    await index_file(db, fake, scope, raw, FILE_V1)
    await index_file(db, fake, scope, raw, source(WATER, LAMP), path="src/other.py")
    before = await versions(db)
    fake.reset()

    result = await reindex(db, fake, workspace_id=scope.acme, raw=raw)

    assert fake.batches == []
    assert await versions(db) == before
    assert result.records == result.records_indexed == 2
    assert result.counts == IndexCounts(unchanged=7, symbols_unchanged=9)
    assert result.stale_deleted == 0
    assert result.previous == result.fingerprint == current_fingerprint(fake.spec)


async def test_a_rebuild_from_raw_gives_the_same_ids_as_the_first_index(
    db: Conn, scope: Scope, fake: FakeEmbedder, raw: FakeRaw
) -> None:
    await index_file(db, fake, scope, raw, FILE_V1)
    await index_file(db, fake, scope, raw, source(PRUNE, LAMP), path="src/other.py")
    first = await snapshot(db)
    await db.execute("DELETE FROM chunk")

    result = await reindex(db, fake, workspace_id=scope.acme, raw=raw, page_size=1)

    assert await snapshot(db) == first
    assert result.counts.inserted == 7
    assert result.records == 2


async def test_a_changed_model_is_refused_until_the_rebuild_finishes(
    db: Conn, migrated_database_url: str, scope: Scope, fake: FakeEmbedder
) -> None:
    rids: list[str] = []
    for source_id in (scope.repo, scope.fork):  # the same file in two repositories
        rid = await add_record(
            db,
            workspace_id=scope.acme,
            owner_id=scope.owner,
            source_id=source_id,
            external_id=f"main:{PATH}",
            content=FILE_V1,
        )
        await index_record(db, fake, record_id=rid, path=PATH, chunks=chunks_of(rid, FILE_V1))
        rids.append(rid)
    ids_before = await rows(db, "SELECT id FROM chunk ORDER BY id")
    old, new = VectorIndex.of(fake.spec), VectorIndex.of(OTHER_MODEL)
    rebuilt = current_fingerprint(OTHER_MODEL)
    other = FakeEmbedder(OTHER_MODEL)
    refusals: list[str] = []

    class Watching(FakeRaw):
        """Checks, at each read, that search under the new model is still refused."""

        async def read(self, record: RecordRef) -> RawDocument | None:
            async with connect(migrated_database_url) as search:
                try:
                    await check_index(search, scope.acme, rebuilt)
                except IndexMismatchError as error:
                    refusals.append(error.command)
            return await super().read(record)

    raw = Watching()
    for rid in rids:
        raw.put(rid, FILE_V1)

    with pytest.raises(IndexMismatchError, match=r"run `memini reindex --workspace acme`"):
        await check_index(db, scope.acme, rebuilt)
    result = await reindex(db, other, workspace_id=scope.acme, raw=raw)

    assert refusals == ["memini reindex --workspace acme"] * 2
    state = await check_index(db, scope.acme, rebuilt)
    assert state is not None
    assert state.built_with == rebuilt
    assert result.previous == current_fingerprint(fake.spec)
    assert await rows(db, "SELECT id FROM chunk ORDER BY id") == ids_before
    assert await rows(db, "SELECT DISTINCT embedding_key FROM chunk") == [(new.embedding_key,)]
    assert await rows(db, "SELECT DISTINCT vector_dims(embedding) FROM chunk") == [(12,)]
    # The two files share their texts: each is embedded once under the new model, then reused.
    assert result.counts.embedded == 4
    assert result.counts.reused == 4
    assert result.counts.updated == 8
    assert await vector_index_exists(db, new)
    assert not await vector_index_exists(db, old)
    assert result.dropped_indexes == (old.name,)
    with pytest.raises(IndexMismatchError):
        await check_index(db, scope.acme, current_fingerprint(fake.spec))


async def test_the_old_vector_index_stays_while_another_workspace_uses_its_key(
    db: Conn, scope: Scope, fake: FakeEmbedder, raw: FakeRaw
) -> None:
    await index_file(db, fake, scope, raw, FILE_V1)
    rid = await add_record(
        db,
        workspace_id=scope.other,
        owner_id=scope.owner,
        source_id=scope.other_repo,
        external_id=f"main:{PATH}",
        content=FILE_V1,
    )
    await index_record(db, fake, record_id=rid, path=PATH, chunks=chunks_of(rid, FILE_V1))

    result = await reindex(db, FakeEmbedder(OTHER_MODEL), workspace_id=scope.acme, raw=raw)

    assert result.dropped_indexes == ()
    assert await vector_index_exists(db, VectorIndex.of(fake.spec))
    assert await check_index(db, scope.other, current_fingerprint(fake.spec)) is not None


# --- What a rebuild reads and clears -------------------------------------------------------------


async def test_records_with_nothing_to_index_lose_their_chunks(
    db: Conn, scope: Scope, fake: FakeEmbedder, raw: FakeRaw
) -> None:
    kept = await index_file(db, fake, scope, raw, FILE_V1)
    tombstone = await index_file(db, fake, scope, raw, FILE_V1, path="src/gone.py")
    await db.execute("UPDATE record SET deleted_at = now() WHERE id = %s", (tombstone,))
    vanished = await index_file(db, fake, scope, raw, FILE_V1, path="src/lost.py")
    del raw.files[vanished]  # the raw store has nothing for it
    thread = await add_record(
        db,
        workspace_id=scope.acme,
        owner_id=scope.owner,
        source_id=scope.notes,
        external_id="thread-1",
        content=b"A synthetic thread.",
        kind="thread",
    )

    chunkers: dict[RecordKind, RecordChunker] = {**DEFAULT_CHUNKERS}
    del chunkers["thread"]
    result = await reindex(
        db, fake, workspace_id=scope.acme, raw=raw, page_size=2, chunkers=chunkers
    )

    assert await rows(db, "SELECT DISTINCT record_id FROM chunk") == [(kept,)]
    assert await rows(db, "SELECT DISTINCT record_id FROM symbol") == [(kept,)]
    assert result.records == 4
    assert result.records_indexed == 1
    assert result.counts.deleted == 8
    # A tombstone is not read, nor a record of a kind without a chunker.
    assert sorted(raw.reads) == sorted([kept, vanished])
    assert thread not in raw.reads


async def test_a_stored_payload_is_chunked_by_the_chunker_of_its_kind(
    db: Conn, scope: Scope, fake: FakeEmbedder
) -> None:
    note = b"# Synthetic note\n\nThe greenhouse vents open at noon.\n"
    rid = await add_record(
        db,
        workspace_id=scope.acme,
        owner_id=scope.owner,
        source_id=scope.notes,
        external_id="note-1",
        content=note,
        kind="document",
    )

    def chunk_note(record: RecordRef, document: RawDocument) -> FileChunks:
        return chunk_file(record.id, f"notes/{record.external_id}.md", document.content)

    result = await reindex(
        db,
        fake,
        workspace_id=scope.acme,
        raw=StoredPayload(db),
        chunkers={**DEFAULT_CHUNKERS, "document": chunk_note},
    )

    assert result.records_indexed == 1
    found = await rows(db, "SELECT record_id, record_kind, kind, path, content FROM chunk")
    assert found == [(rid, "document", "window", None, note.decode().rstrip("\n"))]


NOTE = b"# Synthetic note\n\nThe greenhouse vents open at noon.\n"
NOTE_PAYLOAD = canonical_json({"title": "Vents", "text": NOTE.decode()})


async def test_documents_are_read_by_the_kind_of_their_source_and_chunked_by_headings(
    db: Conn, scope: Scope, fake: FakeEmbedder
) -> None:
    # A document of a push source keeps its payload, JSON with its title and text; one of a git
    # source is a file in the mirror.
    pushed = await add_record(
        db,
        workspace_id=scope.acme,
        owner_id=scope.owner,
        source_id=scope.notes,
        external_id="note-1",
        content=NOTE_PAYLOAD,
        kind="document",
    )
    synced = await add_record(
        db,
        workspace_id=scope.acme,
        owner_id=scope.owner,
        source_id=scope.repo,
        external_id="refs/heads/main:docs/vents.md",
        content=NOTE,
        kind="document",
    )
    await db.execute("UPDATE record SET payload = NULL WHERE id = %s", (synced,))
    mirror = FakeRaw()
    mirror.put(synced, NOTE, path="docs/vents.md")

    with pytest.raises(RawContentUnavailableError, match=f"document record {synced}"):
        await reindex(db, fake, workspace_id=scope.acme, raw=default_raw_content(db))
    result = await reindex(
        db, fake, workspace_id=scope.acme, raw=default_raw_content(db, code=mirror)
    )

    assert result.records_indexed == 2
    assert mirror.reads == [synced]
    found = await rows(
        db,
        "SELECT record_id, record_kind, kind, key, path, header, start_line, anchor FROM chunk"
        " ORDER BY path",
    )
    assert found == [
        (synced, "document", "section", "Synthetic note#0", "docs/vents.md",
         "docs/vents.md: Synthetic note", 1, None),
        (pushed, "document", "section", "Synthetic note#0", None, "Vents: Synthetic note", 3,
         "Synthetic note"),
    ]  # fmt: skip


async def test_threads_and_meetings_are_chunked_from_their_stored_payloads(
    db: Conn, scope: Scope, fake: FakeEmbedder
) -> None:
    thread = canonical_json(
        {"messages": [{"author": "Ann Example", "sent_at": "2026-03-01T09:00:00Z", "text": "Hi."}]}
    )
    meeting = canonical_json({"title": "Sync", "started_at": "2026-03-01T14:00:00Z"})
    kinds: tuple[tuple[RecordKind, bytes], ...] = (("thread", thread), ("meeting", meeting))
    records = {
        kind: await add_record(
            db,
            workspace_id=scope.acme,
            owner_id=scope.owner,
            source_id=scope.notes,
            external_id=f"{kind}-1",
            content=payload,
            kind=kind,
        )
        for kind, payload in kinds
    }

    result = await reindex(db, fake, workspace_id=scope.acme, raw=default_raw_content(db))

    assert result.records_indexed == 2
    found = await rows(db, "SELECT record_id, kind, anchor, content FROM chunk ORDER BY kind")
    (message_id,) = [c.anchor for c in chunk_payload(records["thread"], "thread", thread).chunks]
    assert found == [
        (records["thread"], "messages", message_id, "Ann Example · 2026-03-01 09:00 UTC\nHi."),
        (records["meeting"], "summary", "overview", "Sync\n2026-03-01 14:00 UTC"),
    ]


async def test_a_stored_payload_not_of_its_kind_stops_the_rebuild(
    db: Conn, scope: Scope, fake: FakeEmbedder
) -> None:
    rid = await add_record(
        db,
        workspace_id=scope.acme,
        owner_id=scope.owner,
        source_id=scope.notes,
        external_id="thread-1",
        content=b"A synthetic thread, not JSON.",
        kind="thread",
    )
    with pytest.raises(ValueError, match=f"the payload of thread record {rid} is not valid"):
        await reindex(db, fake, workspace_id=scope.acme, raw=default_raw_content(db))


async def test_a_record_changed_during_the_rebuild_is_read_again(
    db: Conn, migrated_database_url: str, scope: Scope, fake: FakeEmbedder
) -> None:
    edited = source(WATER_EDITED, PRUNE, LAMP)

    class Racing(FakeRaw):
        """The first read of the record is overtaken by a sync that stores a new version."""

        async def read(self, record: RecordRef) -> RawDocument | None:
            document = await super().read(record)
            if len(self.reads) == 1:
                async with connect(migrated_database_url, autocommit=True) as sync:
                    await sync.execute(
                        "UPDATE record SET content_hash = %s WHERE id = %s",
                        (blob_id(edited), record.id),
                    )
                self.put(record.id, edited)
            return document

    raw = Racing()
    rid = await store_file(db, scope, raw, FILE_V1)
    raw.files[rid] = RawDocument(FILE_V1, PATH)

    result = await reindex(db, fake, workspace_id=scope.acme, raw=raw)

    assert raw.reads == [rid, rid]
    assert result.records_indexed == 1
    stored = await rows(db, "SELECT text_hash FROM chunk ORDER BY start_line")
    assert stored == [(chunk.content_hash,) for chunk in chunks_of(rid, edited).chunks]


# --- Failures and concurrency --------------------------------------------------------------------


async def test_unreadable_raw_content_stops_the_rebuild_and_a_second_run_resumes_it(
    db: Conn, scope: Scope, fake: FakeEmbedder
) -> None:
    class Flaky(FakeRaw):
        """Fails on the second record it reads, while ``failing`` is set."""

        failing = True

        async def read(self, record: RecordRef) -> RawDocument | None:
            document = await super().read(record)
            if self.failing and len(self.reads) == 2:
                raise RawContentUnavailableError("the synthetic mirror is not cloned yet")
            return document

    raw = Flaky()
    files = {PATH: FILE_V1, "src/other.py": source(PRUNE, LAMP)}
    paths = {
        await index_file(db, fake, scope, raw, content, path): path
        for path, content in files.items()
    }
    other = FakeEmbedder(OTHER_MODEL)
    rebuilt = current_fingerprint(OTHER_MODEL)

    def texts(rid: str) -> list[str]:
        path = paths[rid]
        return [chunk.text for chunk in chunks_of(rid, files[path], path).chunks]

    with pytest.raises(RawContentUnavailableError):
        await reindex(db, other, workspace_id=scope.acme, raw=raw)

    # Search under the new model stays refused; under the old one it still passes.
    with pytest.raises(IndexMismatchError):
        await check_index(db, scope.acme, rebuilt)
    assert await check_index(db, scope.acme, current_fingerprint(fake.spec)) is not None
    first, second = raw.reads
    assert other.texts == texts(first)  # the first record was rewritten under the new model

    raw.failing = False
    other.reset()
    result = await reindex(db, other, workspace_id=scope.acme, raw=raw)

    # The first record is kept as written; only the second one goes to the model.
    assert other.texts == texts(second)
    assert result.counts.unchanged == len(texts(first))
    assert result.counts.updated == result.counts.embedded == len(texts(second))
    assert await check_index(db, scope.acme, rebuilt) is not None


async def test_a_second_rebuild_of_the_workspace_is_refused(
    db: Conn, migrated_database_url: str, scope: Scope, fake: FakeEmbedder, raw: FakeRaw
) -> None:
    async with connect(migrated_database_url, autocommit=True) as running:
        await running.execute("SELECT pg_advisory_lock(%s, %s)", reindex_lock_key(scope.acme))

        with pytest.raises(ReindexInProgressError, match="workspace acme is being rebuilt"):
            await reindex(db, fake, workspace_id=scope.acme, raw=raw)

        # Another workspace is not held up.
        await reindex(db, fake, workspace_id=scope.other, raw=raw)


async def test_the_lock_is_released_when_a_rebuild_fails(
    db: Conn, scope: Scope, fake: FakeEmbedder
) -> None:
    class Broken(FakeRaw):
        async def read(self, record: RecordRef) -> RawDocument | None:
            raise RawContentUnavailableError("synthetic")

    raw = Broken()
    await store_file(db, scope, raw, FILE_V1)

    for _ in range(2):  # the second attempt finds the lock free
        with pytest.raises(RawContentUnavailableError):
            await reindex(db, fake, workspace_id=scope.acme, raw=raw)


async def test_a_rebuild_needs_an_idle_autocommit_connection(
    migrated_database_url: str, scope: Scope, fake: FakeEmbedder, raw: FakeRaw
) -> None:
    async with connect(migrated_database_url) as conn:
        with pytest.raises(ValueError, match="autocommit"):
            await reindex(conn, fake, workspace_id=scope.acme, raw=raw)


async def test_an_unknown_workspace_is_refused(
    db: Conn, scope: Scope, fake: FakeEmbedder, raw: FakeRaw
) -> None:
    with pytest.raises(LookupError):
        await reindex(db, fake, workspace_id=scope.owner, raw=raw)


async def test_the_rebuild_is_reported_in_the_run(
    db: Conn, migrated_database_url: str, scope: Scope, fake: FakeEmbedder, raw: FakeRaw
) -> None:
    await store_file(db, scope, raw, FILE_V1)

    async with connect(migrated_database_url, autocommit=True) as reports:
        async with record_run(
            reports, workspace_id=scope.acme, owner_id=scope.owner, kind="reindex"
        ) as run:
            await reindex(db, fake, workspace_id=scope.acme, raw=raw)
        report = run.report()

    assert report["counts"]["records_indexed"] == 1
    assert report["counts"]["chunks_inserted"] == 4
    assert report["counts"]["texts_embedded"] == 4
    assert [step["name"] for step in report["steps"]] == ["rebuild_records", "finish"]
    state = await read_index_state(db, scope.acme)
    assert state is not None
    assert state.built_with == current_fingerprint(fake.spec)
