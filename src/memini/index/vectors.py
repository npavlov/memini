"""HNSW indexes over the untyped ``chunk.embedding`` column: one per embedding key.

The column has no dimension (migration 0005), so that a change of model needs no migration. An
HNSW index needs one, so it is built on a cast of the column, and only over the rows of one
embedding key, whose vectors all have that key's dimension::

    CREATE INDEX chunk_embedding_<first 32 hex digits of the key> ON chunk
        USING hnsw ((embedding::vector(768)) vector_cosine_ops)
        WHERE embedding_key = '<key>'

pgvector's HNSW takes ``vector`` up to 2000 dimensions and ``halfvec`` up to 4000; between the two
the index is built on a cast to ``halfvec``, and above 4000 there is none, so a search compares
every vector of the key (exact, slower). Vectors are L2-normalized (the fingerprint says so), so
cosine distance orders them as the inner product would; cosine also stays right should a provider
ever return vectors of another length.

The writer creates the index of its key before it writes the first chunk of that key
(:func:`ensure_vector_index`), and a rebuild does so before it starts. A finished rebuild drops the
indexes of keys that no workspace's index uses any more (:func:`drop_unused_vector_indexes`).

A search uses the index by ordering on :meth:`VectorIndex.distance` under
:meth:`VectorIndex.predicate`. The key goes into the query as a literal, never as a parameter: the
planner can match a partial index only against a value it sees, and a prepared statement planned
generically would not use it::

    index = VectorIndex.of(spec)
    query = sql.SQL(
        "SELECT c.id FROM chunk c WHERE {predicate} AND {access}"
        " ORDER BY {distance} LIMIT %s"
    ).format(
        predicate=index.predicate("c"),
        access=access.where(...),
        distance=index.distance(sql.Placeholder(), "c"),
    )
    await conn.execute(query, (query_vector, k))  # query_vector: a list of floats
"""

import hashlib
import logging
import re
from dataclasses import dataclass
from typing import Final, Literal

from psycopg import AsyncConnection, errors, sql
from psycopg.rows import TupleRow

from memini.embeddings.base import EmbeddingSpec
from memini.index.fingerprint import embedding_key

__all__ = [
    "MAX_HALFVEC_INDEX_DIMENSION",
    "MAX_VECTOR_INDEX_DIMENSION",
    "VECTOR_INDEX_LOCK_NAMESPACE",
    "VectorIndex",
    "drop_unused_vector_indexes",
    "ensure_vector_index",
    "vector_index_exists",
]

log = logging.getLogger(__name__)

type Conn = AsyncConnection[TupleRow]

MAX_VECTOR_INDEX_DIMENSION: Final = 2000
"""The most dimensions pgvector's HNSW indexes as ``vector``."""
MAX_HALFVEC_INDEX_DIMENSION: Final = 4000
"""The most dimensions pgvector's HNSW indexes as ``halfvec``; above it there is no index."""

VECTOR_INDEX_LOCK_NAMESPACE: Final = 0x6D766978
"""First key of the advisory lock taken to create a vector index: "mvix" in ASCII. Two writers
that both find the index missing create it one after the other."""

_PREFIX: Final = "chunk_embedding_"
_NAME: Final = re.compile(r"chunk_embedding_[0-9a-f]{32}")
_KEY: Final = re.compile(r"[0-9a-f]{64}")
_DROP_LOCK_TIMEOUT: Final = "2s"


@dataclass(frozen=True, slots=True)
class VectorIndex:
    """The HNSW index of one embedding key, and the expressions a search orders by."""

    embedding_key: str
    dimension: int

    def __post_init__(self) -> None:
        if _KEY.fullmatch(self.embedding_key) is None:
            raise ValueError("an embedding key is 64 lowercase hex characters")
        if self.dimension < 1:
            raise ValueError("a vector dimension is at least 1")

    @classmethod
    def of(cls, spec: EmbeddingSpec) -> "VectorIndex":
        """The index of the vectors ``spec`` describes."""
        return cls(embedding_key(spec), spec.dimension)

    @property
    def name(self) -> str:
        """``chunk_embedding_`` and the first 32 hex digits of the key."""
        return _PREFIX + self.embedding_key[:32]

    @property
    def indexed(self) -> bool:
        """Whether pgvector can build an HNSW index of this dimension."""
        return self.dimension <= MAX_HALFVEC_INDEX_DIMENSION

    @property
    def cast_type(self) -> Literal["vector", "halfvec"]:
        """The type the column is cast to: ``halfvec`` from 2001 to 4000 dimensions."""
        if MAX_VECTOR_INDEX_DIMENSION < self.dimension <= MAX_HALFVEC_INDEX_DIMENSION:
            return "halfvec"
        return "vector"

    def _typed(self, value: sql.Composable) -> sql.Composable:
        return sql.SQL("({}::{}({}))").format(
            value, sql.SQL(self.cast_type), sql.Literal(self.dimension)
        )

    def column(self, alias: str | None = None) -> sql.Composable:
        """The expression the index is built on: ``(embedding::vector(768))``."""
        return self._typed(_column("embedding", alias))

    def predicate(self, alias: str | None = None) -> sql.Composable:
        """``embedding_key = '<key>'``, the key as a literal, as the partial index needs it."""
        return sql.SQL("{} = {}").format(
            _column("embedding_key", alias), sql.Literal(self.embedding_key)
        )

    def distance(self, query: sql.Composable, alias: str | None = None) -> sql.Composable:
        """Cosine distance from ``query`` (an SQL value, e.g. a placeholder for a list of floats)
        to a chunk's vector, in the form the index orders by. 0 is the same direction, 2 the
        opposite one."""
        return sql.SQL("{} <=> {}").format(self.column(alias), self._typed(query))

    def create_statement(self) -> sql.Composed:
        """``CREATE INDEX IF NOT EXISTS`` for this index. Only when :attr:`indexed`."""
        if not self.indexed:
            raise ValueError(f"pgvector has no HNSW index for {self.dimension} dimensions")
        return sql.SQL(
            "CREATE INDEX IF NOT EXISTS {name} ON chunk USING hnsw ({column} {ops}) WHERE {where}"
        ).format(
            name=sql.Identifier(self.name),
            column=self.column(),
            ops=sql.SQL(f"{self.cast_type}_cosine_ops"),
            where=self.predicate(),
        )

    def lock_key(self) -> tuple[int, int]:
        """The two int4 keys of the advisory lock taken to create the index."""
        digest = hashlib.blake2b(self.embedding_key.encode(), digest_size=4).digest()
        return VECTOR_INDEX_LOCK_NAMESPACE, int.from_bytes(digest, "big", signed=True)


def _column(name: str, alias: str | None) -> sql.Composable:
    return sql.Identifier(alias, name) if alias else sql.Identifier(name)


async def vector_index_exists(conn: Conn, index: VectorIndex) -> bool:
    """Whether the index of ``index``'s key exists, as this transaction sees it."""
    cur = await conn.execute("SELECT to_regclass(%s) IS NOT NULL", (index.name,))
    row = await cur.fetchone()
    return bool(row and row[0])


async def ensure_vector_index(conn: Conn, index: VectorIndex) -> bool:
    """Create the HNSW index of ``index``'s key unless it exists. Returns whether it created it.

    Call it before writing chunks of the key, in the writer's transaction: the index then exists
    exactly when the chunks do. Creating it takes a SHARE lock on ``chunk`` until the transaction
    ends, which waits for other writers and holds back new ones (reads go on); it happens once per
    key, usually on an empty set of rows. Two writers that both find it missing take turns on an
    advisory lock, and the second finds it there. Does nothing for a dimension above
    :data:`MAX_HALFVEC_INDEX_DIMENSION`.
    """
    if not index.indexed or await vector_index_exists(conn, index):
        return False
    async with conn.transaction():  # a savepoint inside the caller's transaction
        await conn.execute("SELECT pg_advisory_xact_lock(%s, %s)", index.lock_key())
        if await vector_index_exists(conn, index):
            return False
        await conn.execute(index.create_statement())
    log.info(
        "created the vector index %s",
        index.name,
        extra={"index": index.name, "dimension": index.dimension},
    )
    return True


async def drop_unused_vector_indexes(conn: Conn, *, keep: VectorIndex | None = None) -> list[str]:
    """Drop the vector indexes of keys that no workspace's ``index_state`` names, except ``keep``.

    For the end of a rebuild, in a transaction of its own after the one that finishes it. Dropping
    an index needs an exclusive lock on ``chunk``; an index whose lock is not granted within a short
    timeout is left for the next rebuild, so a long write elsewhere does not hold up every search
    behind the drop. Dropping is never wrong: a writer of a key whose index is gone creates it
    again before its next write. Returns the names dropped.
    """
    cur = await conn.execute(
        "SELECT i.relname FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid"
        " WHERE x.indrelid = 'chunk'::regclass ORDER BY i.relname"
    )
    names = [name for (name,) in await cur.fetchall() if _NAME.fullmatch(name)]
    cur = await conn.execute("SELECT DISTINCT embedding_key FROM index_state")
    used = {_PREFIX + key[:32] for (key,) in await cur.fetchall()}
    if keep is not None:
        used.add(keep.name)
    dropped: list[str] = []
    for name in names:
        if name in used:
            continue
        try:
            async with conn.transaction():
                await conn.execute(
                    sql.SQL("SET LOCAL lock_timeout = {}").format(sql.Literal(_DROP_LOCK_TIMEOUT))
                )
                await conn.execute(sql.SQL("DROP INDEX IF EXISTS {}").format(sql.Identifier(name)))
        except errors.LockNotAvailable:
            log.warning("the unused vector index %s is busy; it is left for the next rebuild", name)
            continue
        dropped.append(name)
        log.info("dropped the unused vector index %s", name, extra={"index": name})
    return dropped
