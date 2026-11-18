"""Reciprocal rank fusion: several ranked lists of chunk ids into one.

Cormack, Clarke and Buettcher, "Reciprocal Rank Fusion outperforms Condorcet and individual Rank
Learning Methods" (SIGIR 2009). Each item scores the sum, over the lists that hold it, of
``1 / (k + rank)`` with ``rank`` counted from 1. Only ranks count, not the retrievers' own scores,
so a cosine distance and a lexical rank need no common scale. ``k`` damps the weight of the very
top ranks: with k = 60, rank 1 scores 1/61 and rank 10 scores 1/70.
"""

from collections.abc import Sequence
from dataclasses import dataclass

__all__ = ["Fused", "reciprocal_rank_fusion"]


@dataclass(frozen=True, slots=True)
class Fused:
    """One item of the fused list."""

    id: str
    score: float
    ranks: tuple[int | None, ...]
    """Its 1-based rank in each input list, in the order of the lists; ``None`` where absent."""


def reciprocal_rank_fusion(rankings: Sequence[Sequence[str]], *, k: int) -> list[Fused]:
    """Fuse ``rankings``, each a list of ids best first, into one list, best first.

    An id that occurs twice in one list counts at its first place only. Ties in score go to the
    item with the better best rank, then to the smaller id, so the order is deterministic.
    """
    if k < 1:
        raise ValueError("k must be at least 1")
    ranks: dict[str, list[int | None]] = {}
    for position, ranking in enumerate(rankings):
        seen: set[str] = set()
        for item in ranking:
            if item in seen:
                continue
            seen.add(item)
            ranks.setdefault(item, [None] * len(rankings))[position] = len(seen)
    fused = [
        Fused(
            id=item,
            score=sum(1.0 / (k + rank) for rank in item_ranks if rank is not None),
            ranks=tuple(item_ranks),
        )
        for item, item_ranks in ranks.items()
    ]
    fused.sort(key=lambda f: (-f.score, min(r for r in f.ranks if r is not None), f.id))
    return fused
