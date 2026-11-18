"""The index layer: chunks, vectors and the fingerprint that says what they were built with.

So far the fingerprint (:mod:`memini.index.fingerprint`): a digest of the embedding spec and the
chunker version, stored per workspace, and the check that blocks search on an index built with
another one until ``memini reindex`` rebuilds it (principle 9).
"""

from memini.index.fingerprint import (
    REINDEX_COMMAND,
    IndexFingerprint,
    IndexMismatchError,
    IndexState,
    check_index,
    embedding_key,
    ensure_index,
    read_index_state,
    reindex_command,
    set_index_fingerprint,
)

__all__ = [
    "REINDEX_COMMAND",
    "IndexFingerprint",
    "IndexMismatchError",
    "IndexState",
    "check_index",
    "embedding_key",
    "ensure_index",
    "read_index_state",
    "reindex_command",
    "set_index_fingerprint",
]
