"""Rebuild a workspace's index from its raw records: what ``memini reindex`` runs.

Principle 1 says every index can be rebuilt from raw records; principle 9 says a changed embedding
model or chunker blocks search until it is. :func:`reindex` does the rebuild for one workspace under
the fingerprint the embedder and this chunker make (:func:`~memini.index.current_fingerprint`):

1. It takes a session advisory lock on the workspace, so two rebuilds of it never overlap.
2. It creates the HNSW index of the new embedding key (:mod:`memini.index.vectors`).
3. It visits every record of the workspace, in transactions of one record each. A record with a
   chunker for its kind is read from raw (:class:`~memini.index.raw.RawContent`), chunked again and
   written by the index writer (:func:`~memini.index.write_record_chunks`): rows whose text, vector
   key and place are unchanged are left alone, and only texts with no vector under the new key in
   the workspace are embedded. A tombstone, a record with nothing to index, or one of a kind without
   a chunker loses its chunks. Records come a page at a time; a provider that reads ahead
   (:class:`~memini.index.raw.Prefetcher`) is told each page's records to read before the first
   of them, so the files of a repository are read from its mirror in batches, not one git
   process per file.
4. A last transaction locks the workspace's ``index_state`` row, deletes the chunks still under
   another embedding key and records the new fingerprint. Search under the new settings is refused
   (:func:`~memini.index.check_index`) until this transaction commits, and passes from then on.
5. Vector indexes of keys that no workspace uses any more are dropped.

Writing goes through the writer's comparisons, so a rebuild of an index that is current changes no
row and embeds nothing, and a rebuild that stopped halfway (a crash, a failed embedding call) can
simply be run again: what it wrote is kept and not embedded twice. While a rebuild to a new
fingerprint runs, the ordinary writer of the workspace is refused as search is, for as long as the
stored fingerprint is the old one; ingest of raw records goes on, and those records are indexed
when their sync runs again after the rebuild. A rebuild under the stored fingerprint blocks
nothing: search and the writer go on beside it, and the record lock orders their writes.

Records are chunked in a worker thread (tree-sitter is CPU-bound) before the record's transaction
starts. If the record changes in between, it is read and chunked again under the record's lock.
"""

import hashlib
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Final
from uuid import UUID

import anyio
import psycopg
from psycopg import AsyncConnection, sql
from psycopg.rows import TupleRow

from memini.chunking import FileChunks, chunk_file, chunk_markdown
from memini.embeddings import Embedder
from memini.ids import RecordKind
from memini.index.fingerprint import (
    IndexFingerprint,
    current_fingerprint,
    read_index_state,
    set_index_fingerprint,
)
from memini.index.raw import (
    RECORD_COLUMNS,
    Prefetcher,
    RawContent,
    RawDocument,
    RecordRef,
    record_ref,
)
from memini.index.vectors import VectorIndex, drop_unused_vector_indexes, ensure_vector_index
from memini.index.writer import (
    IndexCounts,
    clear_record_chunks,
    lock_record,
    write_record_chunks,
)
from memini.runs import current_run

__all__ = [
    "DEFAULT_CHUNKERS",
    "REINDEX_LOCK_NAMESPACE",
    "RecordChunker",
    "ReindexInProgressError",
    "ReindexResult",
    "chunk_code",
    "chunk_document",
    "reindex",
    "reindex_lock_key",
]

log = logging.getLogger(__name__)

type Conn = AsyncConnection[TupleRow]

type RecordChunker = Callable[[RecordRef, RawDocument], FileChunks]
"""Cuts the raw content of a record into chunks. Pure; runs in a worker thread."""


def chunk_code(record: RecordRef, document: RawDocument) -> FileChunks:
    """The chunks of a code file: :func:`memini.chunking.chunk_file` at the document's path."""
    if document.path is None:
        raise ValueError(f"the raw content of code record {record.id} has no path")
    return chunk_file(record.id, document.path, document.content)


def chunk_document(record: RecordRef, document: RawDocument) -> FileChunks:
    """The chunks of a document: :func:`memini.chunking.chunk_markdown`, by headings.

    A Markdown file of a repository is named by its path in the chunk headers; a document without
    one (pushed, from M2 on) by its external id.
    """
    return chunk_markdown(record.id, document.path or record.external_id, document.content)


DEFAULT_CHUNKERS: Final[Mapping[RecordKind, RecordChunker]] = {
    "code_file": chunk_code,
    "document": chunk_document,
}
"""The chunker of each kind of record. Threads and meetings get theirs with their intake; until
then their records have nothing to index."""

REINDEX_LOCK_NAMESPACE: Final = 0x6D726978
"""First key of the session advisory lock a rebuild holds on its workspace: "mrix" in ASCII."""

_DEFAULT_PAGE_SIZE: Final = 500


def reindex_lock_key(workspace_id: UUID) -> tuple[int, int]:
    """The two int4 keys of the advisory lock a rebuild of the workspace holds."""
    digest = hashlib.blake2b(workspace_id.bytes, digest_size=4).digest()
    return REINDEX_LOCK_NAMESPACE, int.from_bytes(digest, "big", signed=True)


class ReindexInProgressError(Exception):
    """Another rebuild of the workspace is running."""

    def __init__(self, workspace: str) -> None:
        super().__init__(f"the index of workspace {workspace} is being rebuilt already")
        self.workspace = workspace


@dataclass(frozen=True, slots=True)
class ReindexResult:
    """What a rebuild did."""

    workspace_id: UUID
    workspace: str
    fingerprint: IndexFingerprint
    """What the index is built with now."""
    previous: IndexFingerprint | None
    """What it was built with before; ``None`` if nothing was indexed."""
    records: int
    """Records of the workspace visited."""
    records_indexed: int
    """Records chunked and written; the others had nothing to index and lost their chunks."""
    counts: IndexCounts
    """The writes over all records, the stale chunks of the last step included in ``deleted``."""
    stale_deleted: int
    """Chunks under another embedding key, deleted when the rebuild finished."""
    dropped_indexes: tuple[str, ...]
    """Vector indexes of keys no workspace uses any more."""


async def reindex(
    conn: Conn,
    embedder: Embedder,
    *,
    workspace_id: UUID,
    raw: RawContent,
    chunkers: Mapping[RecordKind, RecordChunker] = DEFAULT_CHUNKERS,
    page_size: int = _DEFAULT_PAGE_SIZE,
) -> ReindexResult:
    """Rebuild the index of the workspace from raw records under the embedder's fingerprint.

    ``conn`` must be in autocommit mode and outside a transaction: the rebuild commits record by
    record, and holds its lock on the workspace on this connection. ``raw`` may read on the same
    connection. See the module for the steps.

    Raises:
        LookupError: no such workspace.
        ReindexInProgressError: another rebuild of the workspace is running.
        RawContentUnavailableError: a record's raw content cannot be read now; what was rebuilt so
            far is kept, the fingerprint is not changed, and running the rebuild again resumes it.
        EmbeddingError: the model failed; likewise.
    """
    if not conn.autocommit or conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        raise ValueError("reindex needs a connection in autocommit mode, outside a transaction")
    if page_size < 1:
        raise ValueError("page_size must be at least 1")
    fingerprint = current_fingerprint(embedder.spec)
    index = VectorIndex.of(embedder.spec)
    workspace, owner_id = await _workspace(conn, workspace_id)

    async with _workspace_lock(conn, workspace_id, workspace):
        state = await read_index_state(conn, workspace_id)
        log.info(
            "rebuilding the index of workspace %s with %s",
            workspace,
            fingerprint.describe(),
            extra={"workspace_id": workspace_id, "fingerprint": fingerprint.digest},
        )
        async with conn.transaction():
            await ensure_vector_index(conn, index)

        totals = IndexCounts()
        records = indexed = 0
        async with _step("rebuild_records"):
            async for page in _pages(conn, workspace_id, page_size):
                if isinstance(raw, Prefetcher):
                    await raw.prefetch(
                        [
                            record
                            for record in page
                            if not record.deleted and record.kind in chunkers
                        ]
                    )
                for record in page:
                    counts, wrote = await _rebuild_record(conn, embedder, record, raw, chunkers)
                    counts.report()  # the record's transaction has committed
                    totals += counts
                    records += 1
                    indexed += wrote

        async with _step("finish"), conn.transaction():
            # Wait for writers that checked the old fingerprint, and keep new ones out until the
            # new one is recorded.
            await conn.execute(
                "SELECT 1 FROM index_state WHERE workspace_id = %s FOR UPDATE", (workspace_id,)
            )
            cur = await conn.execute(
                "DELETE FROM chunk WHERE workspace_id = %s AND embedding_key <> %s",
                (workspace_id, fingerprint.embedding_key),
            )
            stale = cur.rowcount
            await set_index_fingerprint(
                conn, workspace_id=workspace_id, owner_id=owner_id, fingerprint=fingerprint
            )
        dropped = await drop_unused_vector_indexes(conn, keep=index)

    totals += IndexCounts(deleted=stale)
    run = current_run()
    if run is not None:
        run.count("records_indexed", indexed)
        run.count("records_cleared", records - indexed)
        run.count("chunks_deleted", stale)
    result = ReindexResult(
        workspace_id=workspace_id,
        workspace=workspace,
        fingerprint=fingerprint,
        previous=None if state is None else state.built_with,
        records=records,
        records_indexed=indexed,
        counts=totals,
        stale_deleted=stale,
        dropped_indexes=tuple(dropped),
    )
    log.info(
        "rebuilt the index of workspace %s",
        workspace,
        extra={
            "workspace_id": workspace_id,
            "records": records,
            "records_indexed": indexed,
            "chunks": totals.chunks,
            "texts_embedded": totals.embedded,
            "chunks_deleted": totals.deleted,
        },
    )
    return result


async def _workspace(conn: Conn, workspace_id: UUID) -> tuple[str, UUID]:
    cur = await conn.execute("SELECT name, owner_id FROM workspace WHERE id = %s", (workspace_id,))
    row = await cur.fetchone()
    if row is None:
        raise LookupError(f"no workspace {workspace_id}")
    return row[0], row[1]


@asynccontextmanager
async def _workspace_lock(conn: Conn, workspace_id: UUID, workspace: str) -> AsyncGenerator[None]:
    key = reindex_lock_key(workspace_id)
    cur = await conn.execute("SELECT pg_try_advisory_lock(%s, %s)", key)
    row = await cur.fetchone()
    if not (row and row[0]):
        raise ReindexInProgressError(workspace)
    try:
        yield
    finally:
        if not conn.closed:
            try:
                await conn.execute("SELECT pg_advisory_unlock(%s, %s)", key)
            except psycopg.Error:  # the session ends with the connection, and the lock with it
                log.warning("could not release the rebuild lock of workspace %s", workspace)


@asynccontextmanager
async def _step(name: str) -> AsyncGenerator[None]:
    run = current_run()
    if run is None:
        yield
        return
    async with run.step(name):
        yield


async def _pages(conn: Conn, workspace_id: UUID, page_size: int) -> AsyncIterator[list[RecordRef]]:
    """Every record of the workspace, by (source, kind, id), a page at a time."""
    after: tuple[UUID, str, str] | None = None
    while True:
        if after is None:
            cur = await conn.execute(
                sql.SQL(
                    "SELECT {} FROM record WHERE workspace_id = %s"
                    " ORDER BY source_id, kind, id LIMIT %s"
                ).format(RECORD_COLUMNS),
                (workspace_id, page_size),
            )
        else:
            cur = await conn.execute(
                sql.SQL(
                    "SELECT {} FROM record WHERE workspace_id = %s"
                    " AND (source_id, kind, id) > (%s, %s, %s)"
                    " ORDER BY source_id, kind, id LIMIT %s"
                ).format(RECORD_COLUMNS),
                (workspace_id, *after, page_size),
            )
        page = [record_ref(row) for row in await cur.fetchall()]
        if page:
            yield page
        if len(page) < page_size:
            return
        last = page[-1]
        after = (last.source_id, last.kind, last.id)


async def _prepare(
    record: RecordRef, raw: RawContent, chunker: RecordChunker | None
) -> tuple[RawDocument, FileChunks] | None:
    """The raw content of the record and its chunks; ``None`` if it has nothing to index."""
    if record.deleted or chunker is None:
        return None
    document = await raw.read(record)
    if document is None:
        return None
    chunks = await anyio.to_thread.run_sync(chunker, record, document)
    return document, chunks


async def _rebuild_record(
    conn: Conn,
    embedder: Embedder,
    record: RecordRef,
    raw: RawContent,
    chunkers: Mapping[RecordKind, RecordChunker],
) -> tuple[IndexCounts, bool]:
    """Write one record again from raw, in a transaction that has committed when it returns.
    Returns the counts, not yet reported, and whether it was indexed."""
    chunker = chunkers.get(record.kind)
    prepared = await _prepare(record, raw, chunker)
    async with conn.transaction():
        current = await lock_record(conn, record.id)
        if current is None:  # deleted meanwhile, its chunks with it
            return IndexCounts(), False
        if current != record:  # changed meanwhile: read it again, under the lock
            prepared = await _prepare(current, raw, chunker)
        if prepared is None:
            return await clear_record_chunks(conn, current), False
        document, chunks = prepared
        counts = await write_record_chunks(
            conn, embedder, current, path=document.path, chunks=chunks
        )
        return counts, True
