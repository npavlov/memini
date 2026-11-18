"""A fake embedder for tests: deterministic vectors, and a record of what it was asked to embed.

It goes through the same checks, prefixes, batching and finishing as a real provider
(:class:`~memini.embeddings.base.BatchingEmbedder`); only the model is replaced by a hash. Tests of
the index use it to count what reaches the model: ``batches`` holds every batch as the model got
it, so ``len(fake.batches)`` is the number of model calls and ``fake.texts`` every text embedded.
"""

import hashlib
from collections.abc import Sequence

from memini.embeddings.base import BatchingEmbedder, EmbeddingSpec

__all__ = ["FakeEmbedder", "fake_vector"]

_DEFAULT_SPEC = EmbeddingSpec(provider="openai_compatible", model="fake-embedder", dimension=8)


def fake_vector(text: str, dimension: int) -> list[float]:
    """A deterministic raw vector for ``text``: SHAKE-256 of the text, two bytes per value.

    Values are in [-1, 1) and not normalized; the embedder normalizes them like any model output.
    Different texts get different vectors, with overwhelming probability.
    """
    data = hashlib.shake_256(text.encode()).digest(2 * dimension)
    return [int.from_bytes(data[i : i + 2], "big") / 32768.0 - 1.0 for i in range(0, len(data), 2)]


class FakeEmbedder(BatchingEmbedder):
    """Vectors from :func:`fake_vector`; counts batches and texts. Reusable across tests.

    ``spec`` defaults to an ``openai_compatible`` model named ``fake-embedder`` of dimension 8.
    Pass another spec to stand in for another model, e.g. to change the index fingerprint.
    ``fail_with`` makes every model call raise that error, for tests of failure paths.
    """

    def __init__(
        self,
        spec: EmbeddingSpec = _DEFAULT_SPEC,
        *,
        batch_size: int = 16,
        query_prefix: str = "",
        fail_with: Exception | None = None,
    ) -> None:
        super().__init__(spec, batch_size=batch_size, query_prefix=query_prefix)
        self.batches: list[list[str]] = []
        self.fail_with = fail_with
        self.closed = False

    @property
    def texts(self) -> list[str]:
        """Every text the model got, prefixes included, in order."""
        return [text for batch in self.batches for text in batch]

    def reset(self) -> None:
        """Forget the batches seen so far."""
        self.batches.clear()

    async def _embed_batch(self, texts: list[str]) -> Sequence[Sequence[float]]:
        self.batches.append(list(texts))
        if self.fail_with is not None:
            raise self.fail_with
        return [fake_vector(text, self.spec.dimension) for text in texts]

    async def aclose(self) -> None:
        self.closed = True
