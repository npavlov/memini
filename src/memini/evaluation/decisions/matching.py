"""Matching the items an extractor proposes in a record with the labels of that record.

Deterministic, no model. Matching is per record, so a proposed item only ever matches a label of
the record it was extracted from (evidence first, principle 2).

Targets
    The labels of a record are its targets: each item with evidence in the record (its anchors
    there), each restatement of an item in it (its anchors, the item's kind and statement), and
    each trap (a wrong item a careless extractor finds there).

Eligible pairs
    A proposed item and a target of the same kind are a pair when they share an anchor, or,
    failing that, when their statements are alike: :func:`similarity` at least
    :data:`SIMILARITY_THRESHOLD`. Evidence decides first; the statement is the fallback for an
    item cited at another part of the record than the label names (the summary instead of the
    segment that says it). Kinds must agree: a decision labelled where an open question was
    proposed is a miss and a false item, not a match.

Assignment
    Pairs are ranked: shared anchors before alike statements, then by the Jaccard index of the
    two anchor sets, then by similarity, then by the order of the proposals and of the targets.
    Greedily in that order, each target takes one proposal and each proposal one target. A
    proposal left without a target while it has a pair with a target already taken matches that
    one as a **duplicate**: a label found twice in one record counts once. So a proposal ends as
    one of:

    - ``item``: it found a labelled item (a true positive);
    - ``restatement``: it found a restatement (neither true nor false: the supersession check
      decides whether it is a duplicate);
    - ``trap``: it found a trap (a false positive);
    - ``duplicate``: a target another proposal of the record found already (neither);
    - ``none``: nothing (a false positive).
"""

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

from memini.evaluation.decisions.dataset import DecisionSet
from memini.extraction import ProposedItem, RecordRef
from memini.knowledge import ItemKind

__all__ = [
    "SIMILARITY_THRESHOLD",
    "STOPWORDS",
    "Match",
    "Outcome",
    "Target",
    "TargetKind",
    "match_record",
    "similarity",
    "targets_of",
    "terms",
]

SIMILARITY_THRESHOLD: Final = 0.5
"""The least :func:`similarity` at which two statements without a shared anchor match."""

_ENGLISH_STOPWORDS: Final = """
    an as at be by do if in is it me my no of on or so to up us we the and for are but not you
    all any can had her was our out has his how its may who did get let say she too use with
    that this from they will have been were what when which their there then than into only
    also some such does should would could must about after before over under again still just
    every
"""
_RUSSIAN_STOPWORDS: Final = """
    и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по только ее
    её мне было вот от меня еще ещё нет о из ему теперь когда даже ну ли если уже или ни быть
    был него до вас нибудь опять уж вам ведь там потом себя ничего ей может они тут где есть
    надо ней для мы тебя их чем была сам чтоб без будто чего раз тоже себе под будет ж тогда
    кто этот того потому этого какой совсем ним здесь этом почти мой тем чтобы нее сейчас были
    куда зачем всех никогда можно при наконец об другой хоть после над больше тот через эти нас
    про всего них какая много разве эту моя впрочем свою этой перед иногда лучше чуть том
    нельзя такой им более всегда конечно всю между
"""  # noqa: RUF001 - Russian words, Cyrillic on purpose
STOPWORDS: Final = frozenset(_ENGLISH_STOPWORDS.split()) | frozenset(_RUSSIAN_STOPWORDS.split())
"""Function words of English and Russian, left out of :func:`terms`."""

_WORD: Final = re.compile(r"\w+")
_STEM: Final = 5

TargetKind = Literal["item", "restatement", "trap"]
Outcome = Literal["item", "restatement", "trap", "duplicate", "none"]


def terms(text: str) -> frozenset[str]:
    """The terms of a statement: its words (NFKC, case-folded), without :data:`STOPWORDS` and
    one-letter words; a word with a digit as it is (``1.8`` is ``1`` and ``8``, ``v2`` is
    ``v2``); any other cut to its first five letters, after an English plural ``s`` (a crude
    stemmer that holds for English and Russian alike: ``packets`` and ``packet`` are ``packe``,
    ``решили`` and ``решение`` are ``решил`` and ``решен``, which do not meet)."""
    found: set[str] = set()
    for word in _WORD.findall(unicodedata.normalize("NFKC", text).casefold()):
        if word in STOPWORDS:
            continue
        if any(char.isdigit() for char in word):
            found.add(word)
            continue
        if len(word) < 2:
            continue
        if word.isascii() and len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        found.add(word[:_STEM])
    return frozenset(found)


def similarity(a: str, b: str) -> float:
    """How alike two statements are: the Dice coefficient of their :func:`terms`, from 0 (no
    term in common, or no terms) to 1 (the same terms)."""
    left, right = terms(a), terms(b)
    if not left or not right:
        return 0.0
    return 2 * len(left & right) / (len(left) + len(right))


@dataclass(frozen=True, slots=True)
class Target:
    """A label of one record that a proposed item may match."""

    kind: TargetKind
    label: str
    """The item's id (for a restatement, the restated item's); the trap's id."""
    item_kind: ItemKind
    statement: str
    anchors: frozenset[str]


def targets_of(data: DecisionSet, ref: RecordRef) -> list[Target]:
    """The targets of the record ``ref`` of ``data``: its items, restatements and traps, in the
    order of the labels."""
    targets: list[Target] = []
    for item in data.items:
        anchors = item.anchors_in(ref)
        if anchors:
            targets.append(Target("item", item.id, item.kind, item.statement, anchors))
    for each in data.restatements:
        if each.ref == ref:
            item = data.item(each.item)
            targets.append(Target("restatement", item.id, item.kind, item.statement, each.anchors))
    for trap in data.traps:
        if trap.ref == ref:
            targets.append(Target("trap", trap.id, trap.kind, trap.statement, trap.anchors))
    return targets


@dataclass(frozen=True, slots=True)
class Match:
    """What became of one proposed item."""

    outcome: Outcome
    target: Target | None = None
    """The target it matched; ``None`` for ``none``."""
    by: Literal["anchor", "statement"] | None = None
    similarity: float = 0.0
    kind_conflict: bool = False
    """For ``none``: it shares an anchor with a target of another kind."""


@dataclass(frozen=True, slots=True)
class _Pair:
    proposal: int
    target: int
    shared: bool
    jaccard: float
    similarity: float

    def rank(self) -> tuple[int, float, float, int, int]:
        return (-int(self.shared), -self.jaccard, -self.similarity, self.proposal, self.target)


def _pairs(
    proposals: Sequence[ProposedItem], targets: Sequence[Target], threshold: float
) -> tuple[list[_Pair], set[int]]:
    pairs: list[_Pair] = []
    conflicts: set[int] = set()
    for i, proposal in enumerate(proposals):
        anchors = frozenset(proposal.anchors)
        for j, target in enumerate(targets):
            shared = anchors & target.anchors
            if target.item_kind != proposal.kind:
                if shared:
                    conflicts.add(i)
                continue
            alike = similarity(proposal.statement, target.statement)
            if not shared and alike < threshold:
                continue
            jaccard = len(shared) / len(anchors | target.anchors)
            pairs.append(_Pair(i, j, bool(shared), jaccard, alike))
    pairs.sort(key=_Pair.rank)
    return pairs, conflicts


def match_record(
    proposals: Sequence[ProposedItem],
    targets: Sequence[Target],
    *,
    threshold: float = SIMILARITY_THRESHOLD,
) -> tuple[Match, ...]:
    """What each of ``proposals`` (the items extracted from one record) matched among the
    ``targets`` (that record's labels), in the order of ``proposals`` (see the module)."""
    pairs, conflicts = _pairs(proposals, targets, threshold)
    matched: dict[int, _Pair] = {}
    taken: set[int] = set()
    for pair in pairs:
        if pair.proposal in matched or pair.target in taken:
            continue
        matched[pair.proposal] = pair
        taken.add(pair.target)
    duplicates: dict[int, _Pair] = {}
    for pair in pairs:
        if pair.proposal not in matched and pair.proposal not in duplicates:
            duplicates[pair.proposal] = pair
    results: list[Match] = []
    for i in range(len(proposals)):
        pair = matched.get(i)
        if pair is not None:
            target = targets[pair.target]
            outcome: Outcome = target.kind
        else:
            pair = duplicates.get(i)
            if pair is None:
                results.append(Match("none", kind_conflict=i in conflicts))
                continue
            target = targets[pair.target]
            outcome = "duplicate"
        results.append(
            Match(
                outcome,
                target,
                by="anchor" if pair.shared else "statement",
                similarity=pair.similarity,
            )
        )
    return tuple(results)
