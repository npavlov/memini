"""The extractors the decision eval can run, by name, and the two stubs it ships with.

``memini eval decisions --extractor NAME`` takes a :class:`ExtractorVariant` from
:data:`EXTRACTORS`. Its ``create`` gets a :class:`VariantContext` (the sets, and the service's
settings) and returns a :class:`~memini.evaluation.decisions.runner.Pipeline`.

- ``oracle`` reads the labels and returns them: each labelled item and restatement where its
  evidence is, with the label's statement, and the labelled verdict for each. Its metrics are 1;
  it proves the harness, and a broken copy of it proves the metrics (the tests).
- ``keyword`` is a deterministic heuristic without a model: a decision where a message, a segment
  or a summary's "Decisions" section says one in so many words (``agreed``, ``decided``,
  ``let's``; ``решили``, ``договорились``), an open question where a part asks one (a question
  mark after a cue such as ``should``, ``do we``, ``нужно ли``); supersession by how alike two
  statements are (:func:`~memini.evaluation.decisions.matching.similarity`). Its numbers are the
  committed baseline (``eval/decisions/baseline-keyword.json``): the floor a model must clear.

Adding the LLM pipeline (memini-mpf): an :class:`ExtractorVariant` whose ``create`` builds an
extractor and a classifier on :class:`memini.llm.LlmGate` from the settings, and an ``aclose``
that closes the gate. The gate decides by the ``llm_policy`` of a workspace and its sources in
the database, so such a variant prepares a scratch database with the sets' workspaces and
sources, as the retrieval eval does (:mod:`memini.evaluation.runner`).
"""

import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from memini.config import Settings
from memini.evaluation.decisions.dataset import DecisionSet
from memini.evaluation.decisions.matching import similarity, targets_of, terms
from memini.evaluation.decisions.runner import Pipeline
from memini.extraction import (
    Candidate,
    Part,
    ProposedItem,
    RecordInput,
    RecordRef,
    Supersedes,
    Verdict,
)
from memini.knowledge import ItemKind

__all__ = [
    "DEFAULT_EXTRACTOR",
    "EXTRACTORS",
    "ExtractorVariant",
    "KeywordStub",
    "OracleStub",
    "VariantContext",
]


@dataclass(frozen=True, slots=True)
class VariantContext:
    sets: tuple[DecisionSet, ...]
    settings: Settings | None = None


@dataclass(frozen=True, slots=True)
class ExtractorVariant:
    name: str
    description: str
    create: Callable[[VariantContext], Pipeline]


# --- The oracle ----------------------------------------------------------------------------------


class OracleStub:
    """Extracts what the labels hold and classifies as the labels say (see the module)."""

    name = "stub-oracle/1"

    def __init__(self, sets: Sequence[DecisionSet]) -> None:
        self._sets = {each.workspace: each for each in sets}

    def _set(self, record: RecordInput) -> DecisionSet | None:
        return self._sets.get(record.workspace)

    async def extract(self, record: RecordInput) -> list[ProposedItem]:
        data = self._set(record)
        if data is None:
            return []
        found: list[ProposedItem] = []
        for item in data.items:
            anchors = [e.anchor for e in item.evidence if e.ref == record.ref]
            if anchors:
                found.append(
                    ProposedItem(item.kind, item.statement, item.decided_at, tuple(anchors))
                )
        for each in data.restatements:
            if each.ref == record.ref:
                item = data.item(each.item)
                anchors = tuple(sorted(each.anchors))
                times = [p.at for a in anchors if (p := record.part(a)) and p.at is not None]
                said = min(times) if times else record.occurred_at
                found.append(ProposedItem(item.kind, item.statement, said, anchors))
        return sorted(found, key=lambda p: p.decided_at)

    def _label_of(
        self, data: DecisionSet, ref: RecordRef, anchors: Iterable[str | None]
    ) -> tuple[str, bool] | None:
        """The labelled item that evidence at ``anchors`` of ``ref`` stands for, and whether it
        restates the item there: the item or restatement whose anchors are most like
        ``anchors`` (Jaccard), an item before a restatement."""
        cited = frozenset(a for a in anchors if a is not None)
        best: tuple[float, int] | None = None
        found: tuple[str, bool] | None = None
        for order, target in enumerate(targets_of(data, ref)):
            shared = target.anchors & cited
            if target.kind == "trap" or not shared:
                continue
            rank = (len(shared) / len(target.anchors | cited), -order)
            if best is None or rank > best:
                best, found = rank, (target.label, target.kind == "restatement")
        return found

    async def classify(
        self, item: ProposedItem, record: RecordInput, candidates: Sequence[Candidate]
    ) -> Verdict:
        data = self._set(record)
        found = None if data is None else self._label_of(data, record.ref, item.anchors)
        if data is None or found is None:
            return Verdict()
        label, restated = found
        stands_for: dict[str, str] = {}
        for candidate in candidates:
            cited: dict[RecordRef, list[str | None]] = {}
            for evidence in candidate.evidence:
                cited.setdefault(evidence.record, []).append(evidence.anchor)
            for ref, anchors in cited.items():
                known = self._label_of(data, ref, anchors)
                if known is not None:
                    stands_for.setdefault(known[0], candidate.id)
        if label in stands_for:
            return Verdict(duplicate_of=stands_for[label])
        if restated:
            return Verdict()
        links = tuple(
            Supersedes(stands_for[link.old], link.relation)
            for link in data.links_from(label)
            if link.old in stands_for
        )
        return Verdict(supersedes=links)


# --- The keyword heuristic -----------------------------------------------------------------------

_DECISION_CUES: Final = re.compile(
    r"\b(agreed|decided|it is decided|let's|let us|we will|we'll|we drop|go with|from now on"
    r"|решили|решено|договорились|согласен|согласна|так и делаем|ставим|не делаем|отказались"
    r"|давайте)\b",
    re.IGNORECASE,
)
_QUESTION_CUES: Final = re.compile(
    r"\b(should|shall|must|do we|can we|whether|is there|нужно ли|стоит ли|надо ли|можно ли"
    r"|будем ли)\b",
    re.IGNORECASE,
)
_SENTENCE_END: Final = re.compile(r"(?<=[.!?])\s+")
_HEADING: Final = re.compile(r"^#{1,6}\s*(.+?)\s*$")
_DECISIONS_HEADING: Final = re.compile(r"decisions?|решени[яе]", re.IGNORECASE)
_QUESTIONS_HEADING: Final = re.compile(r"questions?|вопрос", re.IGNORECASE)
_BULLET: Final = re.compile(r"^\s*[-*]\s+(.+)$")
_SHORT: Final = 3
"""A cue sentence of fewer terms takes the next sentence with it."""
_DUPLICATE_AT: Final = 0.5
_RELATED_AT: Final = 0.25


def _sentences(text: str) -> list[str]:
    found: list[str] = []
    for line in text.splitlines():
        if line.lstrip().startswith(">"):  # a quoted mail
            continue
        found += [sentence.strip() for sentence in _SENTENCE_END.split(line) if sentence.strip()]
    return found


def _summary_items(part: Part, said: datetime) -> Iterator[ProposedItem]:
    section: ItemKind | None = None
    for line in part.text.splitlines():
        heading = _HEADING.match(line)
        if heading is not None:
            title = heading.group(1)
            section = (
                "decision"
                if _DECISIONS_HEADING.search(title)
                else "open_question"
                if _QUESTIONS_HEADING.search(title)
                else None
            )
            continue
        bullet = _BULLET.match(line)
        if bullet is not None and section is not None:
            yield ProposedItem(section, bullet.group(1).strip(), said, (part.anchor,))


def _part_items(part: Part, said: datetime) -> Iterator[ProposedItem]:
    sentences = _sentences(part.text)
    for index, sentence in enumerate(sentences):
        if _DECISION_CUES.search(sentence):
            statement = sentence
            if len(terms(sentence)) < _SHORT and index + 1 < len(sentences):
                statement = f"{sentence} {sentences[index + 1]}"  # "Agreed." and what is agreed
            yield ProposedItem("decision", statement, said, (part.anchor,))
            return
    for sentence in sentences:
        if sentence.endswith("?") and _QUESTION_CUES.search(sentence):
            yield ProposedItem("open_question", sentence, said, (part.anchor,))
            return


class KeywordStub:
    """Cue words for extraction, statement likeness for supersession (see the module)."""

    name = "stub-keyword/1"

    async def extract(self, record: RecordInput) -> list[ProposedItem]:
        found: list[ProposedItem] = []
        for part in record.parts:
            said = part.at or record.occurred_at
            if part.kind == "summary":
                found += _summary_items(part, said)
            elif part.kind in ("message", "segment"):
                found += _part_items(part, said)
        return found

    async def classify(
        self, item: ProposedItem, record: RecordInput, candidates: Sequence[Candidate]
    ) -> Verdict:
        del record
        scored = [(similarity(item.statement, c.statement), i, c) for i, c in enumerate(candidates)]
        same = [s for s in scored if s[2].kind == item.kind]
        if same:
            best, _, candidate = max(same, key=lambda s: (s[0], -s[1]))
            if best >= _DUPLICATE_AT:
                return Verdict(duplicate_of=candidate.id)
        if item.kind != "decision":
            return Verdict()
        questions = [s for s in scored if s[2].kind == "open_question" and s[0] >= _RELATED_AT]
        if questions:
            _, _, candidate = max(questions, key=lambda s: (s[0], -s[1]))
            return Verdict(supersedes=(Supersedes(candidate.id, "answers"),))
        decisions = [s for s in same if s[0] >= _RELATED_AT]
        if decisions:
            _, _, candidate = max(decisions, key=lambda s: (s[0], -s[1]))
            return Verdict(supersedes=(Supersedes(candidate.id, "replaces"),))
        return Verdict()


def _oracle(context: VariantContext) -> Pipeline:
    stub = OracleStub(context.sets)
    return Pipeline(stub, stub)


def _keyword(context: VariantContext) -> Pipeline:
    del context
    stub = KeywordStub()
    return Pipeline(stub, stub)


EXTRACTORS: Final[Mapping[str, ExtractorVariant]] = {
    "keyword": ExtractorVariant(
        "keyword",
        "stub: cue words and statement likeness, no model; the committed baseline",
        _keyword,
    ),
    "oracle": ExtractorVariant("oracle", "stub: returns the labels; every metric is 1", _oracle),
}
DEFAULT_EXTRACTOR: Final = "keyword"
