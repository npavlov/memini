"""The embedder interface, what pins a model's vectors, and the steps every provider shares.

An embedder turns texts into vectors of one fixed dimension. Every provider returns vectors that
are L2-normalized (length 1, so cosine similarity is the inner product) and rounded to float32,
the precision of a pgvector ``vector`` column, so a vector read back from the database equals the
one computed. A provider sends texts to its model in batches of at most ``batch_size``; one call
of :meth:`Embedder.embed` with N texts makes ceil(N / batch_size) calls to the model.

Asymmetric models embed a query and a document differently, usually by a prefix such as
``query: `` and ``passage: ``. :meth:`Embedder.embed` takes the kind of text and prepends the
configured prefix: the document prefix is part of :class:`EmbeddingSpec` (it changes stored
vectors), the query prefix is not (it changes only what a search compares with them).
"""

import math
import time
from abc import ABC, abstractmethod
from array import array
from collections.abc import Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Final, Literal, Protocol, Self

from memini.config import EMBEDDING_MAX_DIMENSION, EmbeddingPooling, EmbeddingProvider
from memini.metrics import EMBEDDING_DURATION, EMBEDDING_REQUESTS, EMBEDDING_TEXTS

__all__ = [
    "NORMALIZATION",
    "BatchingEmbedder",
    "Embedder",
    "EmbeddingError",
    "EmbeddingSpec",
    "EmbeddingsNotConfiguredError",
    "Normalization",
    "TextKind",
    "Vector",
    "finish_vector",
]

type Vector = list[float]
"""One embedding: ``dimension`` floats, L2-normalized, each exactly representable as float32."""

TextKind = Literal["document", "query"]
"""What a text is: something stored in the index, or a search query."""

Normalization = Literal["l2"]

NORMALIZATION: Final[Normalization] = "l2"
"""How every provider scales vectors. Part of the index fingerprint, so a change of it would
invalidate stored vectors."""


class EmbeddingError(Exception):
    """Vectors could not be computed, or the provider is configured wrongly.

    The text names the provider, the model and the cause. It never holds an API key or a text that
    was to be embedded.
    """


class EmbeddingsNotConfiguredError(EmbeddingError):
    """No embedding provider is configured, so nothing can be indexed or searched by vector."""

    def __init__(self) -> None:
        super().__init__(
            "no embedding provider is configured: set MEMINI_EMBEDDING_PROVIDER"
            " (openai_compatible or local) and the settings it needs"
        )


@dataclass(frozen=True, slots=True)
class EmbeddingSpec:
    """Everything that decides the vectors a document gets: the embedding part of the index
    fingerprint (:mod:`memini.index`). Never a secret, never an endpoint URL.

    ``digest`` pins the model's content: for ``local`` the digest of the model files, verified
    before they are loaded; for ``openai_compatible`` whatever the user pinned for the served model
    (an Ollama digest, a version), recorded but not verifiable through the API; ``""`` if none.
    ``pooling`` and ``max_tokens`` apply to ``local`` only and are ``None`` otherwise: a server
    decides them for itself.
    """

    provider: EmbeddingProvider
    model: str
    dimension: int
    digest: str = ""
    pooling: EmbeddingPooling | None = None
    max_tokens: int | None = None
    document_prefix: str = ""
    normalization: Normalization = NORMALIZATION

    def __post_init__(self) -> None:
        if not self.model:
            raise ValueError("an embedding spec needs a model name")
        if not 1 <= self.dimension <= EMBEDDING_MAX_DIMENSION:
            raise ValueError(f"an embedding dimension is from 1 to {EMBEDDING_MAX_DIMENSION}")
        local = self.provider == "local"
        if (self.pooling is not None, self.max_tokens is not None) != (local, local):
            raise ValueError("pooling and max_tokens are set for the local provider, and only then")

    def describe(self) -> str:
        """The spec in a few words, for messages: ``model X (local, dimension 384, ...)``."""
        parts = [self.provider, f"dimension {self.dimension}"]
        if self.digest:
            parts.append(f"digest {_short(self.digest)}")
        if self.pooling is not None:
            parts.append(f"{self.pooling} pooling")
        if self.max_tokens is not None:
            parts.append(f"{self.max_tokens} tokens")
        if self.document_prefix:
            parts.append(f"document prefix {self.document_prefix!r}")
        return f"model {self.model} ({', '.join(parts)})"


def _short(digest: str) -> str:
    return digest if len(digest) <= 23 else digest[:20] + "..."


class Embedder(Protocol):
    """Computes vectors for texts. Async: a remote call or a model run does not block the loop."""

    @property
    def spec(self) -> EmbeddingSpec:
        """What decides the vectors; its ``dimension`` is the length of every vector returned."""
        ...

    @property
    def batch_size(self) -> int:
        """The most texts sent to the model at once."""
        ...

    async def embed(self, texts: Sequence[str], *, kind: TextKind = "document") -> list[Vector]:
        """One vector per text, in order. ``kind`` picks the prefix.

        Raises:
            ValueError: a text is empty, or ``texts`` is a single string.
            EmbeddingError: the model failed or returned something unusable.
        """
        ...

    async def aclose(self) -> None:
        """Release connections or the loaded model. The embedder is not used afterwards."""
        ...


class BatchingEmbedder(ABC):
    """The shared part of every provider: checks, prefixes, batching and the finishing of vectors.

    A provider implements :meth:`_embed_batch`, which gets at most ``batch_size`` texts, prefixes
    included, and returns their raw vectors in order.
    """

    def __init__(self, spec: EmbeddingSpec, *, batch_size: int, query_prefix: str = "") -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        self._spec = spec
        self._batch_size = batch_size
        self._query_prefix = query_prefix

    @property
    def spec(self) -> EmbeddingSpec:
        return self._spec

    @property
    def batch_size(self) -> int:
        return self._batch_size

    async def embed(self, texts: Sequence[str], *, kind: TextKind = "document") -> list[Vector]:
        if isinstance(texts, str):
            raise ValueError("embed takes a sequence of texts, not one string")
        if any(not text for text in texts):
            raise ValueError("cannot embed an empty text")
        prefix = self._spec.document_prefix if kind == "document" else self._query_prefix
        vectors: list[Vector] = []
        provider = self._spec.provider
        for start in range(0, len(texts), self._batch_size):
            batch = [prefix + text for text in texts[start : start + self._batch_size]]
            started = time.perf_counter()
            outcome = "error"
            try:
                raw = await self._embed_batch(batch)
                outcome = "ok"
            finally:
                EMBEDDING_REQUESTS.inc(provider=provider, kind=kind, outcome=outcome)
                EMBEDDING_DURATION.observe(time.perf_counter() - started, provider=provider)
            EMBEDDING_TEXTS.inc(len(batch), provider=provider, kind=kind)
            if len(raw) != len(batch):
                raise EmbeddingError(
                    f"{self._spec.describe()} returned {len(raw)} vectors for {len(batch)} texts"
                )
            vectors.extend(finish_vector(vector, self._spec) for vector in raw)
        return vectors

    @abstractmethod
    async def _embed_batch(self, texts: list[str]) -> Sequence[Sequence[float]]:
        """Raw vectors of ``texts`` (at most ``batch_size``), in order. Any scale, any float."""

    async def aclose(self) -> None:  # noqa: B027 - a provider with nothing to release keeps it
        """Release what the provider holds. Nothing by default."""

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()


def finish_vector(raw: Sequence[float], spec: EmbeddingSpec) -> Vector:
    """``raw`` checked against ``spec``, scaled to length 1 and rounded to float32.

    Raises:
        EmbeddingError: the dimension differs from ``spec.dimension``, a value is not finite, or
            the vector is zero and so has no direction.
    """
    if len(raw) != spec.dimension:
        raise EmbeddingError(
            f"model {spec.model} returned vectors of dimension {len(raw)}, but"
            f" embedding_dimension is {spec.dimension}; set MEMINI_EMBEDDING_DIMENSION to the"
            " model's dimension (a changed dimension needs `memini reindex`)"
        )
    norm = math.sqrt(math.fsum(value * value for value in raw))
    if not math.isfinite(norm):
        raise EmbeddingError(
            f"model {spec.model} returned a vector with a value that is not finite"
        )
    if norm == 0.0:
        raise EmbeddingError(f"model {spec.model} returned a zero vector")
    return array("f", [value / norm for value in raw]).tolist()
