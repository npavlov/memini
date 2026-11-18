"""Reciprocal rank fusion: several ranked lists of chunk ids into one.

Cormack, Clarke and Buettcher, "Reciprocal Rank Fusion outperforms Condorcet and individual Rank
Learning Methods" (SIGIR 2009). Each item scores the sum, over the lists that hold it, of
``weight / (k + rank)`` with ``rank`` counted from 1 and the list's weight (1 unless given). Only
ranks count, not the retrievers' own scores, so a cosine distance and a lexical rank need no common
scale. ``k`` damps the weight of the very top ranks: with k = 60, rank 1 scores 1/61 and rank 10
scores 1/70. A weight above another lets one retriever's ranks count more: with weights 2 and 1, a
chunk the first list ranks 10th (2/70) still beats one the second list ranks first (1/61).
"""

import math
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


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[str]], *, k: int, weights: Sequence[float] | None = None
) -> list[Fused]:
    """Fuse ``rankings``, each a list of ids best first, into one list, best first.

    ``weights``, one per list (all 1 when not given), multiply each list's terms. An id that occurs
    twice in one list counts at its first place only. Ties in score go to the item with the better
    best rank, then to the smaller id, so the order is deterministic.
    """
    if k < 1:
        raise ValueError("k must be at least 1")
    factors = [1.0] * len(rankings) if weights is None else [float(w) for w in weights]
    if len(factors) != len(rankings):
        raise ValueError("one weight per ranking")
    if not all(math.isfinite(w) and w > 0 for w in factors):
        raise ValueError("weights must be positive numbers")
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
            score=sum(
                factors[position] / (k + rank)
                for position, rank in enumerate(item_ranks)
                if rank is not None
            ),
            ranks=tuple(item_ranks),
        )
        for item, item_ranks in ranks.items()
    ]
    fused.sort(key=lambda f: (-f.score, min(r for r in f.ranks if r is not None), f.id))
    return fused
