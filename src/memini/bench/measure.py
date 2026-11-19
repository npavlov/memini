"""What the benchmark measures with: latency summaries, the process's peak memory, an embedder that
times its model calls, and a fake embedder of the default model's dimension.

:class:`TimedEmbedder` wraps the embedder the benchmark indexes with and adds up the time spent in
:meth:`~memini.embeddings.Embedder.embed` and the texts sent, so a phase's wall time splits into
embedding and the rest. :class:`BenchFakeEmbedder` gives deterministic vectors of any dimension
without keeping the texts it saw (the test :class:`~memini.embeddings.FakeEmbedder` keeps every
batch, which would grow with the corpus and distort the memory measured).
"""

import math
import resource
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass

from memini.embeddings import BatchingEmbedder, Embedder, EmbeddingSpec, TextKind, Vector
from memini.embeddings.fake import fake_vector

__all__ = [
    "BenchFakeEmbedder",
    "Latency",
    "TimedEmbedder",
    "peak_rss_bytes",
    "percentile",
]


def peak_rss_bytes() -> int:
    """The most resident memory this process has held so far, in bytes (``ru_maxrss``: bytes on
    macOS, KiB on Linux)."""
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak) if sys.platform == "darwin" else int(peak) * 1024


def percentile(values: Sequence[float], fraction: float) -> float:
    """The ``fraction`` (0 to 1) percentile of ``values`` by linear interpolation; 0 if empty."""
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


@dataclass(frozen=True, slots=True)
class Latency:
    """A summary of durations, in milliseconds."""

    count: int
    p50: float
    p95: float
    p99: float
    mean: float
    max: float

    @classmethod
    def of(cls, seconds: Sequence[float]) -> "Latency":
        ms = [value * 1000.0 for value in seconds]
        return cls(
            count=len(ms),
            p50=percentile(ms, 0.50),
            p95=percentile(ms, 0.95),
            p99=percentile(ms, 0.99),
            mean=sum(ms) / len(ms) if ms else 0.0,
            max=max(ms) if ms else 0.0,
        )


class TimedEmbedder:
    """An :class:`~memini.embeddings.Embedder` that adds up the time and texts of its calls."""

    def __init__(self, inner: Embedder) -> None:
        self.inner = inner
        self.seconds = 0.0
        self.texts = 0
        self.calls = 0

    @property
    def spec(self) -> EmbeddingSpec:
        return self.inner.spec

    @property
    def batch_size(self) -> int:
        return self.inner.batch_size

    def reset(self) -> None:
        self.seconds = 0.0
        self.texts = 0
        self.calls = 0

    async def embed(self, texts: Sequence[str], *, kind: TextKind = "document") -> list[Vector]:
        started = time.perf_counter()
        try:
            return await self.inner.embed(texts, kind=kind)
        finally:
            self.seconds += time.perf_counter() - started
            self.texts += len(texts)
            self.calls += 1

    async def aclose(self) -> None:
        await self.inner.aclose()


class BenchFakeEmbedder(BatchingEmbedder):
    """Deterministic vectors (:func:`~memini.embeddings.fake_vector`) of ``dimension``, under a spec
    of its own (model ``bench-fake``), keeping nothing of what it embeds.

    The vectors carry no meaning: random directions are the hardest case for an HNSW index, so
    the dense recall measured with them is a floor, and their build and search times are of the
    right order for the dimension."""

    def __init__(self, dimension: int, *, batch_size: int = 32) -> None:
        super().__init__(
            EmbeddingSpec(provider="openai_compatible", model="bench-fake", dimension=dimension),
            batch_size=batch_size,
        )

    async def _embed_batch(self, texts: list[str]) -> Sequence[Sequence[float]]:
        return [fake_vector(text, self.spec.dimension) for text in texts]
