"""The index layer: chunks, vectors, symbols, and the fingerprint that says what built them.

- :mod:`memini.index.fingerprint`: a digest of the embedding spec and the chunker, stored per
  workspace, and the check that blocks search on an index built with another one until
  ``memini reindex`` rebuilds it (principle 9).
- :mod:`memini.index.writer`: :func:`index_record` writes the chunks and symbols of a record,
  touching only rows that changed and embedding only texts that have no vector yet (principle 6);
  :func:`delete_record_chunks` removes them for a tombstone.
- :mod:`memini.index.vectors`: the HNSW index of each embedding key over the untyped vector
  column, and the expressions a search orders by.
- :mod:`memini.index.rebuild`: :func:`reindex` rebuilds a workspace's index from raw records
  (principle 1), reading them through :mod:`memini.index.raw`.

The tables are ``chunk`` and ``symbol`` (migration 0005) and ``index_state`` (migration 0004).
"""

from memini.index.fingerprint import (
    REINDEX_COMMAND,
    IndexFingerprint,
    IndexMismatchError,
    IndexState,
    check_index,
    current_fingerprint,
    embedding_key,
    ensure_index,
    read_index_state,
    reindex_command,
    set_index_fingerprint,
)
from memini.index.raw import (
    ByKind,
    BySourceKind,
    MirrorNotAvailable,
    RawContent,
    RawContentUnavailableError,
    RawDocument,
    RecordRef,
    StoredPayload,
    default_raw_content,
)
from memini.index.rebuild import (
    DEFAULT_CHUNKERS,
    RecordChunker,
    ReindexInProgressError,
    ReindexResult,
    chunk_code,
    chunk_document,
    reindex,
)
from memini.index.vectors import (
    VectorIndex,
    drop_unused_vector_indexes,
    ensure_vector_index,
    vector_index_exists,
)
from memini.index.writer import (
    IndexCounts,
    RecordNotFoundError,
    clear_record_chunks,
    delete_record_chunks,
    index_record,
    lock_record,
    write_record_chunks,
)

__all__ = [
    "DEFAULT_CHUNKERS",
    "REINDEX_COMMAND",
    "ByKind",
    "BySourceKind",
    "IndexCounts",
    "IndexFingerprint",
    "IndexMismatchError",
    "IndexState",
    "MirrorNotAvailable",
    "RawContent",
    "RawContentUnavailableError",
    "RawDocument",
    "RecordChunker",
    "RecordNotFoundError",
    "RecordRef",
    "ReindexInProgressError",
    "ReindexResult",
    "StoredPayload",
    "VectorIndex",
    "check_index",
    "chunk_code",
    "chunk_document",
    "clear_record_chunks",
    "current_fingerprint",
    "default_raw_content",
    "delete_record_chunks",
    "drop_unused_vector_indexes",
    "embedding_key",
    "ensure_index",
    "ensure_vector_index",
    "index_record",
    "lock_record",
    "read_index_state",
    "reindex",
    "reindex_command",
    "set_index_fingerprint",
    "vector_index_exists",
    "write_record_chunks",
]
