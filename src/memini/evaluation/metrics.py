"""Recall at k and reciprocal rank of one ranked list of hits, with ties and duplicates.

Pure functions. A hit is reduced to a :class:`Ranked`: the expected results it matches (indexes
into the question's list of expected results) and the score it was ranked by.

Duplicates
    Several hits may match the same expected result: two chunks of one function, a group and a
    section of one heading. An expected result counts once, at the first hit that matches it, so
    a second hit of the same result neither adds to recall nor moves the reciprocal rank.

Ties
    Hits with equal scores are in no order of the retriever's own: search breaks the tie by chunk
    id, a hash that changes with the chunker. To keep the metrics independent of that, a run of
    consecutive hits with the same score counts as a tie group, and a metric is its expected value
    over every order of each group, as McSherry and Najork compute it ("Computing information
    retrieval performance measures efficiently in the presence of tied scores", ECIR 2008). Hits
    without a score (``exact``, ``regex`` and ``symbol`` order by place) are never tied. Only hits
    returned can be ordered, so a run fetches more hits than its largest k
    (:attr:`memini.evaluation.runner.EvalConfig.depth`).

Recall at k
    The share of the expected results that some hit of the first k matches. A tie group that
    straddles k contributes a random subset of its hits: an expected result matched by c of its
    n hits, of which m fall within k, is found with probability ``1 - C(n - c, m) / C(n, m)``.

Reciprocal rank
    1 / the rank of the first hit that matches any expected result, 0 if none within the depth.
    In a tie group of n hits of which r match, the first match is at place j of the group with
    probability ``C(n - j, r - 1) / C(n, r)``.
"""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from math import comb, fsum

__all__ = ["Ranked", "first_ranks", "mean", "recall_at_k", "reciprocal_rank", "tie_groups"]


@dataclass(frozen=True, slots=True)
class Ranked:
    """One hit of a ranked list, as the metrics see it."""

    matches: frozenset[int]
    """The expected results it matches: indexes into the question's list."""
    score: float | None = None
    """What the list is ordered by; ``None`` when the order is by place (no ties)."""


def tie_groups(hits: Sequence[Ranked]) -> Iterator[tuple[int, Sequence[Ranked]]]:
    """Runs of consecutive hits with the same score, with the 0-based place of each run's first
    hit. A hit without a score is a run of its own."""
    start = 0
    while start < len(hits):
        end = start + 1
        score = hits[start].score
        if score is not None:
            while end < len(hits) and hits[end].score == score:
                end += 1
        yield start, hits[start:end]
        start = end


def recall_at_k(hits: Sequence[Ranked], expected: int, k: int) -> float:
    """The expected share of the ``expected`` results that the first ``k`` hits find."""
    if expected < 1:
        raise ValueError("a question expects at least one result")
    if k < 1:
        raise ValueError("k must be at least 1")
    found = [0.0] * expected
    for start, group in tie_groups(hits):
        if start >= k:
            break
        inside = min(len(group), k - start)
        for item in range(expected):
            if found[item] == 1.0:
                continue
            matching = sum(1 for hit in group if item in hit.matches)
            if matching == 0:
                continue
            missed = comb(len(group) - matching, inside) / comb(len(group), inside)
            # Groups before this one lie wholly within k, so ``found`` is 0 or 1 until the group
            # that straddles k, which is the last one looked at.
            found[item] = 1.0 - (1.0 - found[item]) * missed
    return fsum(found) / expected


def reciprocal_rank(hits: Sequence[Ranked], *, depth: int | None = None) -> float:
    """The expected 1 / rank of the first hit that matches an expected result; 0 if none does
    within ``depth`` hits (all hits by default)."""
    limit = len(hits) if depth is None else depth
    for start, group in tie_groups(hits):
        if start >= limit:
            break
        size = len(group)
        relevant = sum(1 for hit in group if hit.matches)
        if relevant == 0:
            continue
        total = comb(size, relevant)
        return fsum(
            comb(size - place, relevant - 1) / total / (start + place)
            for place in range(1, size - relevant + 2)
            if start + place <= limit
        )
    return 0.0


def first_ranks(hits: Sequence[Ranked], expected: int) -> tuple[int | None, ...]:
    """For each expected result, the 1-based place of the first hit that matches it, as the hits
    are ordered (ties as returned); ``None`` for a result no hit matches. For reports."""
    ranks: list[int | None] = [None] * expected
    for place, hit in enumerate(hits, start=1):
        for item in hit.matches:
            if 0 <= item < expected and ranks[item] is None:
                ranks[item] = place
    return tuple(ranks)


def mean(values: Sequence[float]) -> float:
    """The arithmetic mean; 0 for no values."""
    return fsum(values) / len(values) if values else 0.0
