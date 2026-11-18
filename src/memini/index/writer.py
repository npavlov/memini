"""The index writer: the chunks and symbols of a record into the index, changing only what changed.

:func:`index_record` takes a record and what the chunker made of it (:class:`FileChunks`) and, in
one transaction:

1. locks the record row (``FOR NO KEY UPDATE``), so that two writers of one record take turns, and
   refuses a record that does not exist or is a tombstone;
2. checks the workspace's index fingerprint (:func:`~memini.index.ensure_index`): a first index
   records it, an index built with another one refuses the write (principle 9);
3. creates the HNSW index of the embedding key if it is missing (:mod:`memini.index.vectors`);
4. reads the record's stored chunks and compares each new chunk with the row of the same id: a row
   whose text hash, embedding key, place, anchor, URL and time are unchanged gets no statement
   at all; a new id is inserted; a changed row is updated, with a new vector only if its text or
   the embedding key changed; a stored id the chunker no longer makes is deleted;
5. embeds only the texts that have no vector yet in the workspace under the embedding key
   (:func:`~memini.embeddings.embed_missing`, principle 6): an unchanged text is never embedded
   again, wherever it was seen before;
6. writes the symbols the same way, by qualified name and ordinal.

So writing the same input again makes no embedding call and changes no row: the rows keep their
``xmin``. Editing one function embeds the text of its chunk and no other. If the edit changes the
function's size, the chunks and symbols after it are updated with their new lines and byte
offsets, and keep their vectors; otherwise no other row is touched.

:func:`delete_record_chunks` removes the chunks and symbols of a record, for a tombstone. Deleting
the record row removes them as well (ON DELETE CASCADE): forget and retention need nothing here.

Both return :class:`IndexCounts`. Counts reach the run report of the job
(:func:`memini.runs.current_run`) only for writes that committed. In a transaction of its own,
:func:`index_record` or :func:`delete_record_chunks` adds them after the commit; in the caller's
transaction (a savepoint), the caller adds them (:meth:`IndexCounts.report`) once its transaction
has committed, so a batch that rolls back leaves nothing in the report.
:func:`write_record_chunks` and :func:`clear_record_chunks` run in the caller's transaction and
never report.

The text stored and embedded is the chunk's text as the chunker made it. Postgres text cannot hold a
NUL character, so the chunker replaces NUL bytes (:mod:`memini.chunking`); a chunk that still holds
one is refused before anything is written.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from typing import Any, Final

from psycopg import AsyncConnection, pq, sql
from psycopg.rows import TupleRow

from memini.chunking import Chunk, FileChunks, Symbol
from memini.embeddings import EmbeddedTexts, Embedder, Vector, embed_missing, text_hash
from memini.index.fingerprint import current_fingerprint, embedding_key, ensure_index
from memini.index.raw import RECORD_COLUMNS, RecordRef, record_ref
from memini.index.vectors import VectorIndex, ensure_vector_index
from memini.runs import current_run

__all__ = [
    "IndexCounts",
    "RecordNotFoundError",
    "clear_record_chunks",
    "delete_record_chunks",
    "index_record",
    "lock_record",
    "write_record_chunks",
]

type Conn = AsyncConnection[TupleRow]


@dataclass(frozen=True, slots=True)
class IndexCounts:
    """What one write did. Add two with ``+``."""

    inserted: int = 0
    """Chunks written that were not stored before."""
    updated: int = 0
    """Stored chunks rewritten: their text, vector or place changed."""
    unchanged: int = 0
    """Stored chunks left as they were: no statement touched them."""
    deleted: int = 0
    """Stored chunks removed: the chunker no longer makes them, or the record has no content."""
    embedded: int = 0
    """Distinct texts sent to the embedding model."""
    reused: int = 0
    """Distinct texts whose vector was already stored in the workspace under the embedding key."""
    symbols_inserted: int = 0
    symbols_updated: int = 0
    symbols_unchanged: int = 0
    symbols_deleted: int = 0

    def __add__(self, other: "IndexCounts") -> "IndexCounts":
        return IndexCounts(
            **{f.name: getattr(self, f.name) + getattr(other, f.name) for f in fields(self)}
        )

    @property
    def changed(self) -> bool:
        """Whether any row was written or deleted."""
        return any(
            (
                self.inserted,
                self.updated,
                self.deleted,
                self.symbols_inserted,
                self.symbols_updated,
                self.symbols_deleted,
            )
        )

    @property
    def chunks(self) -> int:
        """Chunks of the records written: inserted, updated and unchanged."""
        return self.inserted + self.updated + self.unchanged

    def report(self) -> None:
        """Add the counts to the run being recorded, if any, under :data:`RUN_COUNTS` names.

        Call it once the writes counted have committed."""
        run = current_run()
        if run is None:
            return
        for field, name in RUN_COUNTS.items():
            run.count(name, getattr(self, field))


RUN_COUNTS: Final[Mapping[str, str]] = {
    "inserted": "chunks_inserted",
    "updated": "chunks_updated",
    "unchanged": "chunks_unchanged",
    "deleted": "chunks_deleted",
    "embedded": "texts_embedded",
    "reused": "vectors_reused",
    "symbols_inserted": "symbols_inserted",
    "symbols_updated": "symbols_updated",
    "symbols_unchanged": "symbols_unchanged",
    "symbols_deleted": "symbols_deleted",
}
"""The name of each :class:`IndexCounts` field in a run report."""


class RecordNotFoundError(LookupError):
    """The record to index does not exist."""

    def __init__(self, record_id: str) -> None:
        super().__init__(f"no record {record_id}")
        self.record_id = record_id


async def lock_record(conn: Conn, record_id: str) -> RecordRef | None:
    """The record ``record_id``, locked until the transaction ends; ``None`` if there is none.

    ``FOR NO KEY UPDATE``: two writers of the record take turns, while the foreign keys of its
    chunks (which take ``FOR KEY SHARE``) are not held up.
    """
    cur = await conn.execute(
        sql.SQL("SELECT {} FROM record WHERE id = %s FOR NO KEY UPDATE").format(RECORD_COLUMNS),
        (record_id,),
    )
    row = await cur.fetchone()
    return None if row is None else record_ref(row)


async def index_record(
    conn: Conn,
    embedder: Embedder,
    *,
    record_id: str,
    path: str | None,
    chunks: FileChunks,
) -> IndexCounts:
    """Write the chunks and symbols of the record to the index, changing only what changed.

    Runs in the caller's transaction if there is one (as a savepoint), else in one of its own. See
    the module for the steps. ``path`` is the file's path in its repository, required for a code
    file; ``chunks`` is what :func:`memini.chunking.chunk_file` made of the record's content. In a
    transaction of its own it adds the counts to the run report after the commit; in the caller's,
    the caller does once its transaction commits.

    Raises:
        RecordNotFoundError: no record ``record_id``.
        ValueError: the record is a tombstone (use :func:`delete_record_chunks`), ``chunks``
            holds one chunk id twice, or a chunk's text holds a NUL character.
        IndexMismatchError: the workspace's index was built with another fingerprint than the
            embedder's spec and this chunker make; it must be rebuilt first (``memini reindex``).
        EmbeddingError: the model failed; nothing is written.
    """
    own = _owns_transaction(conn)
    async with conn.transaction():
        record = await lock_record(conn, record_id)
        if record is None:
            raise RecordNotFoundError(record_id)
        if record.deleted:
            raise ValueError(f"record {record_id} is deleted; delete its chunks instead")
        await ensure_index(
            conn,
            workspace_id=record.workspace_id,
            owner_id=record.owner_id,
            expected=current_fingerprint(embedder.spec),
        )
        counts = await write_record_chunks(conn, embedder, record, path=path, chunks=chunks)
    if own:
        counts.report()
    return counts


async def delete_record_chunks(conn: Conn, *, record_id: str) -> IndexCounts:
    """Delete the chunks and symbols of the record, for a tombstone. Nothing if there is no record.

    Needs no fingerprint check: removing rows from an index is right whatever it was built with.
    Reports its counts as :func:`index_record` does.
    """
    own = _owns_transaction(conn)
    async with conn.transaction():
        record = await lock_record(conn, record_id)
        if record is None:  # gone, and its chunks with it
            return IndexCounts()
        counts = await clear_record_chunks(conn, record)
    if own:
        counts.report()
    return counts


def _owns_transaction(conn: Conn) -> bool:
    """Whether ``conn.transaction()`` starts a transaction that commits when its block ends,
    rather than a savepoint in the caller's transaction."""
    return conn.info.transaction_status == pq.TransactionStatus.IDLE


# --- The writes, without the checks: for index_record and a rebuild -----------------------------

_CHUNK_COLUMNS: Final = sql.SQL(
    "id, owner_id, key, kind, language, path, symbol, start_line, end_line, start_byte, end_byte,"
    " text_hash, embedding_key, anchor, url, occurred_at"
)

_INSERT_CHUNK: Final = (
    "INSERT INTO chunk (id, owner_id, key, kind, language, path, symbol, start_line, end_line,"
    " start_byte, end_byte, text_hash, embedding_key, anchor, url, occurred_at, workspace_id,"
    " source_id, record_id, record_kind, header, content, embedding)"
    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
    " %s, %s, %s::vector)"
)

_UPDATE_CHUNK: Final = (
    "UPDATE chunk SET owner_id = %s, key = %s, kind = %s, language = %s, path = %s, symbol = %s,"
    " start_line = %s, end_line = %s, start_byte = %s, end_byte = %s, text_hash = %s,"
    " embedding_key = %s, anchor = %s, url = %s, occurred_at = %s, header = %s, content = %s,"
    " embedding = coalesce(%s::vector, embedding), updated_at = now()"
    " WHERE id = %s"
)

_SYMBOL_COLUMNS: Final = sql.SQL(
    "qualified_name, ordinal, owner_id, chunk_id, name, kind, language, path, start_line,"
    " end_line, start_byte, end_byte, parent"
)

_INSERT_SYMBOL: Final = (
    "INSERT INTO symbol (qualified_name, ordinal, owner_id, chunk_id, name, kind, language, path,"
    " start_line, end_line, start_byte, end_byte, parent, workspace_id, source_id, record_id)"
    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
)

_UPDATE_SYMBOL: Final = (
    "UPDATE symbol SET owner_id = %s, chunk_id = %s, name = %s, kind = %s, language = %s,"
    " path = %s, start_line = %s, end_line = %s, start_byte = %s, end_byte = %s, parent = %s,"
    " updated_at = now()"
    " WHERE record_id = %s AND qualified_name = %s AND ordinal = %s"
)


@dataclass(frozen=True, slots=True)
class _ChunkRow:
    """A chunk as stored, in the order of ``_CHUNK_COLUMNS``, plus its text."""

    stored: tuple[Any, ...]
    header: str
    content: str

    @property
    def id(self) -> str:
        chunk_id: str = self.stored[0]
        return chunk_id

    @property
    def text_hash(self) -> str:
        value: str = self.stored[11]
        return value

    @property
    def text(self) -> str:
        return f"{self.header}\n{self.content}"


def _chunk_row(record: RecordRef, chunk: Chunk, path: str | None, key: str) -> _ChunkRow:
    header, content = chunk.header, chunk.content
    # Postgres text cannot hold a NUL character; the chunker replaces them, so this is a bug.
    if "\0" in header or "\0" in content:
        raise ValueError(f"chunk {chunk.key} of {record.id} holds a NUL character")
    stored = (
        chunk.id,
        record.owner_id,
        chunk.key,
        chunk.kind,
        chunk.language,
        path,
        chunk.symbol,
        chunk.start_line,
        chunk.end_line,
        chunk.start_byte,
        chunk.end_byte,
        text_hash(f"{header}\n{content}"),
        key,
        chunk.anchor,
        chunk.url,
        chunk.occurred_at,
    )
    return _ChunkRow(stored=stored, header=header, content=content)


def _symbol_row(record: RecordRef, symbol: Symbol, language: str | None) -> tuple[Any, ...]:
    """A symbol as stored, in the order of ``_SYMBOL_COLUMNS``."""
    return (
        symbol.qualified_name,
        symbol.ordinal,
        record.owner_id,
        symbol.chunk_id,
        symbol.name,
        symbol.kind,
        language,
        symbol.path,
        symbol.start_line,
        symbol.end_line,
        symbol.start_byte,
        symbol.end_byte,
        symbol.parent,
    )


async def write_record_chunks(
    conn: Conn,
    embedder: Embedder,
    record: RecordRef,
    *,
    path: str | None,
    chunks: FileChunks,
) -> IndexCounts:
    """Steps 3 to 6 of :func:`index_record`, for a record the caller has locked and checked.

    No fingerprint check: :func:`index_record` makes it, and a rebuild writes under the new
    fingerprint while the stored one is still the old one. Call it in a transaction, and report
    the counts once it has committed.
    """
    key = embedding_key(embedder.spec)
    await ensure_vector_index(conn, VectorIndex(key, embedder.spec.dimension))

    rows: dict[str, _ChunkRow] = {}
    for chunk in chunks.chunks:
        if chunk.id in rows:
            raise ValueError(f"chunk id {chunk.id} occurs twice in the chunks of {record.id}")
        rows[chunk.id] = _chunk_row(record, chunk, path, key)
    cur = await conn.execute(
        sql.SQL(
            "SELECT {} FROM chunk WHERE workspace_id = %s AND source_id = %s AND record_id = %s"
        ).format(_CHUNK_COLUMNS),
        (record.workspace_id, record.source_id, record.id),
    )
    stored = {row[0]: tuple(row) for row in await cur.fetchall()}

    inserts = [row for row in rows.values() if row.id not in stored]
    updates = [row for row in rows.values() if row.id in stored and stored[row.id] != row.stored]
    unchanged = len(rows) - len(inserts) - len(updates)
    vanished = [chunk_id for chunk_id in stored if chunk_id not in rows]
    # A changed row needs a vector only if its text or its embedding key changed.
    revectored = {
        row.id
        for row in updates
        if stored[row.id][11:13] != row.stored[11:13]  # text_hash, embedding_key
    }
    texts = [row.text for row in inserts] + [row.text for row in updates if row.id in revectored]
    vectors = await _vectors(conn, embedder, record, key, texts)

    async with conn.cursor() as cursor:
        if inserts:
            await cursor.executemany(
                _INSERT_CHUNK,
                [
                    (
                        *row.stored,
                        record.workspace_id,
                        record.source_id,
                        record.id,
                        record.kind,
                        row.header,
                        row.content,
                        vectors.vector(row.text),
                    )
                    for row in inserts
                ],
            )
        if updates:
            await cursor.executemany(
                _UPDATE_CHUNK,
                [
                    (
                        *row.stored[1:],
                        row.header,
                        row.content,
                        vectors.vector(row.text) if row.id in revectored else None,
                        row.id,
                    )
                    for row in updates
                ],
            )

    symbols = await _write_symbols(conn, record, chunks)

    deleted = 0
    if vanished:
        cur = await conn.execute(
            "DELETE FROM chunk WHERE workspace_id = %s AND source_id = %s AND record_id = %s"
            " AND id = ANY(%s)",
            (record.workspace_id, record.source_id, record.id, vanished),
        )
        deleted = cur.rowcount

    return IndexCounts(
        inserted=len(inserts),
        updated=len(updates),
        unchanged=unchanged,
        deleted=deleted,
        embedded=vectors.embedded,
        reused=vectors.reused,
        symbols_inserted=symbols[0],
        symbols_updated=symbols[1],
        symbols_unchanged=symbols[2],
        symbols_deleted=symbols[3],
    )


async def clear_record_chunks(conn: Conn, record: RecordRef) -> IndexCounts:
    """Delete every chunk and symbol of a record the caller has locked. Call it in a transaction,
    and report the counts once it has committed."""
    cur = await conn.execute("DELETE FROM symbol WHERE record_id = %s", (record.id,))
    symbols = cur.rowcount
    cur = await conn.execute(
        "DELETE FROM chunk WHERE workspace_id = %s AND source_id = %s AND record_id = %s",
        (record.workspace_id, record.source_id, record.id),
    )
    return IndexCounts(deleted=cur.rowcount, symbols_deleted=symbols)


async def _vectors(
    conn: Conn, embedder: Embedder, record: RecordRef, key: str, texts: Sequence[str]
) -> EmbeddedTexts:
    """Vectors of ``texts``: stored ones of the workspace under ``key``, the rest from the model."""
    if not texts:
        return EmbeddedTexts(vectors={}, reused=0, embedded=0)

    async def lookup(hashes: Sequence[str]) -> Mapping[str, Vector]:
        cur = await conn.execute(
            "SELECT DISTINCT ON (text_hash) text_hash, embedding::real[] FROM chunk"
            " WHERE workspace_id = %s AND embedding_key = %s AND text_hash = ANY(%s)",
            (record.workspace_id, key, list(hashes)),
            # Binary: each float4 arrives exactly, so a reused vector equals the one computed. As
            # text it would arrive as its shortest decimal form, a different double.
            binary=True,
        )
        return {row[0]: list(row[1]) for row in await cur.fetchall()}

    return await embed_missing(embedder, texts, lookup)


async def _write_symbols(
    conn: Conn, record: RecordRef, chunks: FileChunks
) -> tuple[int, int, int, int]:
    """Insert, update and delete the record's symbols by (qualified name, ordinal).

    Returns the symbols inserted, updated, unchanged and deleted.
    """
    rows: dict[tuple[str, int], tuple[Any, ...]] = {}
    for symbol in chunks.symbols:
        name = (symbol.qualified_name, symbol.ordinal)
        if name in rows:
            raise ValueError(
                f"symbol {symbol.qualified_name}#{symbol.ordinal} occurs twice in {record.id}"
            )
        rows[name] = _symbol_row(record, symbol, chunks.language)
    cur = await conn.execute(
        sql.SQL("SELECT {} FROM symbol WHERE record_id = %s").format(_SYMBOL_COLUMNS),
        (record.id,),
    )
    stored = {(row[0], row[1]): tuple(row) for row in await cur.fetchall()}

    inserts = [row for name, row in rows.items() if name not in stored]
    updates = [row for name, row in rows.items() if name in stored and stored[name] != row]
    vanished = [name for name in stored if name not in rows]
    async with conn.cursor() as cursor:
        if inserts:
            await cursor.executemany(
                _INSERT_SYMBOL,
                [(*row, record.workspace_id, record.source_id, record.id) for row in inserts],
            )
        if updates:
            await cursor.executemany(
                _UPDATE_SYMBOL, [(*row[2:], record.id, row[0], row[1]) for row in updates]
            )
        if vanished:
            await cursor.executemany(
                "DELETE FROM symbol WHERE record_id = %s AND qualified_name = %s AND ordinal = %s",
                [(record.id, qualified_name, ordinal) for qualified_name, ordinal in vanished],
            )
    unchanged = len(rows) - len(inserts) - len(updates)
    return len(inserts), len(updates), unchanged, len(vanished)
