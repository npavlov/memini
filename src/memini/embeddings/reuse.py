"""Embed only what has no vector yet: principle 6, "an unchanged content hash is never re-embedded".

A vector depends on two things only: the text that was embedded and the embedding part of the
index fingerprint (:attr:`memini.index.IndexFingerprint.embedding_key`: model, digest, dimension,
pooling, token limit, document prefix). It does not depend on the chunker, the record or the
commit. So the index writer keys each stored vector by :func:`text_hash` of the chunk's text and by
that embedding key, and before it embeds a batch of chunks it asks for the vectors it already holds
under the same two keys. Only texts with none reach the model.

The writer passes :func:`embed_missing` a lookup over its own storage, scoped to one workspace and
the current embedding key (vectors never cross a workspace: they are derived from its private
text). A chunk table with ``workspace_id``, ``embedding_key``, ``text_hash`` and ``embedding``
columns serves it with one query::

    SELECT DISTINCT ON (text_hash) text_hash, embedding FROM chunk
    WHERE workspace_id = %s AND embedding_key = %s AND text_hash = ANY(%s)

That covers an unchanged file seen again, a text repeated across files or repositories, and a
rebuild after a chunker change, as long as the old rows are still there while the new ones are
written: the chunker version is not part of the embedding key.
"""

import hashlib
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass

from memini.embeddings.base import Embedder, TextKind, Vector

__all__ = ["EmbeddedTexts", "VectorLookup", "embed_missing", "text_hash"]

type VectorLookup = Callable[[Sequence[str]], Awaitable[Mapping[str, Vector]]]
"""Given text hashes, the vectors already stored for some of them, by hash. Hashes it has no
vector for are left out. It must be scoped to one workspace and one embedding key."""


def text_hash(text: str) -> str:
    """SHA-256 of ``text`` in UTF-8, as 64 lowercase hex digits; the key a vector is reused by.

    The same as ``encode(sha256(convert_to(text, 'UTF8')), 'hex')`` in Postgres.
    """
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class EmbeddedTexts:
    """The vectors of a set of texts, by :func:`text_hash`, and where they came from."""

    vectors: Mapping[str, Vector]
    reused: int
    """Distinct texts whose vector the lookup had."""
    embedded: int
    """Distinct texts that went to the model."""

    def vector(self, text: str) -> Vector:
        """The vector of ``text``, which must be one of the texts asked for."""
        return self.vectors[text_hash(text)]


async def embed_missing(
    embedder: Embedder,
    texts: Iterable[str],
    lookup: VectorLookup | None = None,
    *,
    kind: TextKind = "document",
) -> EmbeddedTexts:
    """Vectors of ``texts``, embedding only those that ``lookup`` has no vector for.

    A text that occurs more than once is embedded once. Texts go to the model in batches of the
    embedder's ``batch_size``. Without ``lookup``, every distinct text is embedded.

    Raises:
        ValueError: ``lookup`` returned a vector of another dimension than the embedder's, which
            means it is not scoped to the current embedding key.
        EmbeddingError: the model failed; nothing is returned for any text.
    """
    by_hash: dict[str, str] = {}
    for text in texts:
        by_hash.setdefault(text_hash(text), text)
    known: dict[str, Vector] = {}
    if lookup is not None and by_hash:
        found = await lookup(list(by_hash))
        dimension = embedder.spec.dimension
        for key, vector in found.items():
            if key not in by_hash:
                continue
            if len(vector) != dimension:
                raise ValueError(
                    f"the lookup returned a vector of dimension {len(vector)} for an embedder of"
                    f" dimension {dimension}; it must be scoped to the current embedding key"
                )
            known[key] = vector
    missing = [key for key in by_hash if key not in known]
    computed = await embedder.embed([by_hash[key] for key in missing], kind=kind) if missing else []
    vectors = known | dict(zip(missing, computed, strict=True))
    return EmbeddedTexts(
        vectors={key: vectors[key] for key in by_hash},
        reused=len(known),
        embedded=len(missing),
    )
