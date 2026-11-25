"""The index fingerprint: what an index was built with, and the check that keeps a stale index out
of use (principle 9: fail closed on index mismatch).

Two digests, both :func:`memini.ids.hash_fields` (SHA-256 over length-prefixed fields, 64 lowercase
hex characters):

- the **embedding key**, over the domain ``memini.embedding.v1`` and the parts of the
  :class:`~memini.embeddings.EmbeddingSpec` in this order: provider, model, digest, dimension
  (decimal), normalization, pooling (``""`` if none), max_tokens (decimal, ``0`` if none) and
  document prefix, each in UTF-8. It names the vector space; a stored vector is reused by it
  (:mod:`memini.embeddings.reuse`).
- the **fingerprint**, over the domain ``memini.index.v1``, the 32 bytes of the embedding key and
  the chunker version in UTF-8. It names the whole index.

A change of what goes in, or of the encoding, takes a new domain version. The query prefix, the
batch size, the timeout and the endpoint URL are not parts: they do not change stored vectors.

Each workspace's index records its fingerprint in ``index_state`` (migration 0004). The service
computes the fingerprint its settings call for, :func:`current_fingerprint` of
``embedding_spec(settings)`` (the spec and :func:`memini.chunking.chunker_fingerprint`: the chunker
version and the versions of tree-sitter and its grammars; then the version of the redaction that the
writer applies to chunk text, :data:`memini.redaction.REDACTION_VERSION`), and:

- search calls :func:`check_index` first: a stored fingerprint that differs raises
  :class:`IndexMismatchError`, whose message names ``memini reindex``, or, while a rebuild of the
  workspace waits or runs (:mod:`memini.index.progress`), :class:`IndexRebuildingError`, whose
  message says how far it is;
- the index writer calls :func:`ensure_index` in the transaction that writes chunks: it records the
  fingerprint of a first index, and refuses to add to an index built with another one;
- a rebuild calls :func:`set_index_fingerprint` in the transaction that finishes it.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, get_args
from uuid import UUID

from psycopg import AsyncConnection, sql
from psycopg.rows import TupleRow

from memini.auth import ReadFilter
from memini.chunking import chunker_fingerprint
from memini.config import EmbeddingPooling, EmbeddingProvider
from memini.embeddings.base import EmbeddingSpec, Normalization
from memini.ids import hash_fields
from memini.index.progress import RebuildProgress, read_rebuild
from memini.redaction import REDACTION_VERSION

__all__ = [
    "EMBEDDING_KEY_DOMAIN",
    "INDEX_FINGERPRINT_DOMAIN",
    "REINDEX_COMMAND",
    "IndexFingerprint",
    "IndexMismatchError",
    "IndexRebuildingError",
    "IndexState",
    "check_index",
    "current_fingerprint",
    "embedding_key",
    "ensure_index",
    "read_index_state",
    "read_index_states",
    "reindex_command",
    "set_index_fingerprint",
]

EMBEDDING_KEY_DOMAIN: Final = "memini.embedding.v1"
INDEX_FINGERPRINT_DOMAIN: Final = "memini.index.v1"

REINDEX_COMMAND: Final = "memini reindex"
"""The command that rebuilds an index, named by every refusal."""

type Conn = AsyncConnection[TupleRow]


def reindex_command(workspace: str) -> str:
    """The command line that rebuilds the index of ``workspace``."""
    return f"{REINDEX_COMMAND} --workspace {workspace}"


def embedding_key(spec: EmbeddingSpec) -> str:
    """The digest of the vector space ``spec`` describes."""
    return hash_fields(
        EMBEDDING_KEY_DOMAIN,
        spec.provider.encode(),
        spec.model.encode(),
        spec.digest.encode(),
        str(spec.dimension).encode(),
        spec.normalization.encode(),
        (spec.pooling or "").encode(),
        str(spec.max_tokens or 0).encode(),
        spec.document_prefix.encode(),
    )


@dataclass(frozen=True, slots=True)
class IndexFingerprint:
    """What an index is built with: the embedding spec and the chunker version."""

    embedding: EmbeddingSpec
    chunker_version: str

    def __post_init__(self) -> None:
        if not self.chunker_version:
            raise ValueError("an index fingerprint needs a chunker version")

    @property
    def embedding_key(self) -> str:
        """:func:`embedding_key` of the spec."""
        return embedding_key(self.embedding)

    @property
    def digest(self) -> str:
        """The fingerprint: 64 lowercase hex characters."""
        return hash_fields(
            INDEX_FINGERPRINT_DOMAIN,
            bytes.fromhex(self.embedding_key),
            self.chunker_version.encode(),
        )

    def describe(self) -> str:
        """For messages: ``model X (local, dimension 384, ...), chunker 1``."""
        return f"{self.embedding.describe()}, chunker {self.chunker_version}"

    def changes(self, other: "IndexFingerprint") -> tuple[str, ...]:
        """The names of the parts in which ``other`` differs from this fingerprint."""
        mine, theirs = self.embedding, other.embedding
        parts = {
            "provider": mine.provider != theirs.provider,
            "model": mine.model != theirs.model,
            "model digest": mine.digest != theirs.digest,
            "dimension": mine.dimension != theirs.dimension,
            "normalization": mine.normalization != theirs.normalization,
            "pooling": mine.pooling != theirs.pooling,
            "max tokens": mine.max_tokens != theirs.max_tokens,
            "document prefix": mine.document_prefix != theirs.document_prefix,
            "chunker version": self.chunker_version != other.chunker_version,
        }
        return tuple(name for name, changed in parts.items() if changed)


def current_fingerprint(spec: EmbeddingSpec) -> IndexFingerprint:
    """The fingerprint of an index built now with ``spec``: what search checks a stored one against,
    and what the writer and a rebuild record. Its chunker part is
    :func:`memini.chunking.chunker_fingerprint` and the redaction version: chunk text is what the
    chunker cut with its secrets redacted (:mod:`memini.index.writer`)."""
    return IndexFingerprint(spec, f"{chunker_fingerprint()} redaction/{REDACTION_VERSION}")


@dataclass(frozen=True, slots=True)
class IndexState:
    """The ``index_state`` row of a workspace: the fingerprint its index was built with."""

    workspace_id: UUID
    workspace: str
    """The name of the workspace, for messages."""
    fingerprint: str
    """The stored fingerprint, as written by the code of its time."""
    built_with: IndexFingerprint
    """The parts the fingerprint was computed from."""
    created_at: datetime
    updated_at: datetime


class IndexMismatchError(Exception):
    """The index of a workspace was built with another fingerprint than the settings call for.

    Search and writes to that index are refused until it is rebuilt. The message names the
    workspace, what changed, both sides and the command that rebuilds it. It holds no secret: the
    parts of a fingerprint are names and numbers, never an endpoint or a key.
    """

    def __init__(self, state: IndexState, expected: IndexFingerprint) -> None:
        self.workspace_id = state.workspace_id
        self.workspace = state.workspace
        self.stored = state.built_with
        self.expected = expected
        self.changed = state.built_with.changes(expected) or ("fingerprint encoding",)
        self.command = reindex_command(state.workspace)
        super().__init__(
            f"the index of workspace {state.workspace} is out of date: it was built with"
            f" {self.stored.describe()}, but the service is configured for"
            f" {expected.describe()} (changed: {', '.join(self.changed)}). Search and indexing"
            f" in this workspace are blocked until the index is rebuilt: run `{self.command}`"
        )


class IndexRebuildingError(IndexMismatchError):
    """The index of a workspace is out of date, and its rebuild waits or runs: after an upgrade,
    a service with ``auto_reindex`` rebuilds it by itself. Search is back when it finishes; the
    message says how far it is instead of asking for ``memini reindex``."""

    def __init__(
        self, state: IndexState, expected: IndexFingerprint, rebuild: RebuildProgress
    ) -> None:
        super().__init__(state, expected)
        self.rebuild = rebuild
        Exception.__init__(
            self,
            f"the index of workspace {state.workspace} is being rebuilt for"
            f" {expected.describe()} (changed: {', '.join(self.changed)}): {rebuild.describe()}."
            " Search in this workspace is back when the rebuild finishes",
        )


_STATE_COLUMNS: Final = sql.SQL(
    "s.workspace_id, w.name, s.fingerprint, s.provider, s.model, s.model_digest, s.dimension,"
    " s.normalization, s.pooling, s.max_tokens, s.document_prefix, s.chunker_version,"
    " s.created_at, s.updated_at"
)


async def read_index_state(
    conn: Conn, workspace_id: UUID, *, for_share: bool = False
) -> IndexState | None:
    """The index state of the workspace, or ``None`` if nothing is indexed in it.

    ``for_share`` locks the row until the transaction ends, so that a rebuild cannot finish (and
    change the fingerprint) while the caller writes to the index. It needs a transaction: in
    autocommit mode the lock ends with the statement.
    """
    query = sql.SQL(
        "SELECT {columns} FROM index_state s JOIN workspace w ON w.id = s.workspace_id"
        " WHERE s.workspace_id = %s{lock}"
    ).format(
        columns=_STATE_COLUMNS,
        lock=sql.SQL(" FOR SHARE OF s") if for_share else sql.SQL(""),
    )
    cur = await conn.execute(query, (workspace_id,))
    row = await cur.fetchone()
    return None if row is None else _state(row)


async def check_index(
    conn: Conn, workspace_id: UUID, expected: IndexFingerprint
) -> IndexState | None:
    """Refuse an index built with another fingerprint than ``expected``. For search.

    Returns the state, or ``None`` if nothing is indexed in the workspace yet (there is nothing to
    search, and nothing stale).

    Raises:
        IndexRebuildingError: the stored fingerprint differs, and a rebuild waits or runs.
        IndexMismatchError: the stored fingerprint differs from ``expected``.
    """
    state = await read_index_state(conn, workspace_id)
    if state is not None and state.fingerprint != expected.digest:
        rebuild = await read_rebuild(conn, workspace_id)
        if rebuild is not None:
            raise IndexRebuildingError(state, expected, rebuild)
        raise IndexMismatchError(state, expected)
    return state


async def read_index_states(conn: Conn, access: ReadFilter) -> dict[UUID, IndexState]:
    """The index state of every workspace ``access`` may read that has one, by workspace id. For
    status (:mod:`memini.status`): a workspace without a row has nothing indexed yet."""
    query = sql.SQL(
        "SELECT {columns} FROM index_state s JOIN workspace w ON w.id = s.workspace_id"
        " WHERE {readable}"
    ).format(
        columns=_STATE_COLUMNS,
        readable=access.where(
            workspace_id=sql.Identifier("s", "workspace_id"),
            owner_id=sql.Identifier("s", "owner_id"),
        ),
    )
    cur = await conn.execute(query)
    return {state.workspace_id: state for state in map(_state, await cur.fetchall())}


async def ensure_index(
    conn: Conn, *, workspace_id: UUID, owner_id: UUID, expected: IndexFingerprint
) -> IndexState:
    """Record ``expected`` for a first index, or check it against the stored one. For the writer.

    Call it in the transaction that writes chunks, before them. The row stays locked (shared)
    until that transaction ends, so a rebuild cannot change the fingerprint in between.

    Raises:
        IndexMismatchError: the workspace's index was built with another fingerprint; nothing may
            be added to it until it is rebuilt.
    """
    await conn.execute(
        sql.SQL(
            "INSERT INTO index_state ({columns}) VALUES ({values})"
            " ON CONFLICT (workspace_id) DO NOTHING"
        ).format(**_insert_parts()),
        _insert_values(workspace_id, owner_id, expected),
    )
    state = await read_index_state(conn, workspace_id, for_share=True)
    if state is None:  # inserted above, or there already
        raise RuntimeError(f"index_state of workspace {workspace_id} vanished in ensure_index")
    if state.fingerprint != expected.digest:
        raise IndexMismatchError(state, expected)
    return state


async def set_index_fingerprint(
    conn: Conn, *, workspace_id: UUID, owner_id: UUID, fingerprint: IndexFingerprint
) -> IndexState:
    """Record that the workspace's index is now built with ``fingerprint``. For a rebuild.

    Call it in the transaction that finishes the rebuild, so that search is unblocked exactly when
    the rebuilt index is there. The owner of an existing row stays as it is.
    """
    await conn.execute(
        sql.SQL(
            "INSERT INTO index_state ({columns}) VALUES ({values})"
            " ON CONFLICT (workspace_id) DO UPDATE SET {updates}, updated_at = now()"
        ).format(
            **_insert_parts(),
            updates=sql.SQL(", ").join(
                sql.SQL("{column} = EXCLUDED.{column}").format(column=sql.Identifier(column))
                for column in _PART_COLUMNS
            ),
        ),
        _insert_values(workspace_id, owner_id, fingerprint),
    )
    state = await read_index_state(conn, workspace_id)
    if state is None:
        raise RuntimeError(f"index_state of workspace {workspace_id} vanished after an upsert")
    return state


_PART_COLUMNS: Final = (
    "fingerprint",
    "embedding_key",
    "provider",
    "model",
    "model_digest",
    "dimension",
    "normalization",
    "pooling",
    "max_tokens",
    "document_prefix",
    "chunker_version",
)


def _insert_parts() -> dict[str, sql.Composable]:
    columns = ("workspace_id", "owner_id", *_PART_COLUMNS)
    return {
        "columns": sql.SQL(", ").join(sql.Identifier(column) for column in columns),
        "values": sql.SQL(", ").join(sql.Placeholder() for _ in columns),
    }


def _insert_values(workspace_id: UUID, owner_id: UUID, fingerprint: IndexFingerprint) -> list[Any]:
    spec = fingerprint.embedding
    return [
        workspace_id,
        owner_id,
        fingerprint.digest,
        fingerprint.embedding_key,
        spec.provider,
        spec.model,
        spec.digest,
        spec.dimension,
        spec.normalization,
        spec.pooling,
        spec.max_tokens,
        spec.document_prefix,
        fingerprint.chunker_version,
    ]


def _state(row: Sequence[Any]) -> IndexState:
    spec = EmbeddingSpec(
        provider=_one_of(row[3], get_args(EmbeddingProvider)),
        model=row[4],
        digest=row[5],
        dimension=row[6],
        normalization=_one_of(row[7], get_args(Normalization)),
        pooling=None if row[8] is None else _one_of(row[8], get_args(EmbeddingPooling)),
        max_tokens=row[9],
        document_prefix=row[10],
    )
    return IndexState(
        workspace_id=row[0],
        workspace=row[1],
        fingerprint=row[2],
        built_with=IndexFingerprint(embedding=spec, chunker_version=row[11]),
        created_at=row[12],
        updated_at=row[13],
    )


def _one_of[T: str](value: object, allowed: tuple[T, ...]) -> T:
    for option in allowed:
        if value == option:
            return option
    raise ValueError(f"unexpected value in index_state: {value!r}")
