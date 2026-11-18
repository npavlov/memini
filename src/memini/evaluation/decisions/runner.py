"""The decision eval run: extract from every record, match with the labels, replay supersession.

:func:`run_decision_eval` takes the sets and a :class:`Pipeline` (an extractor and a supersession
classifier, :mod:`memini.extraction`) and, set by set:

1. Extracts from each live thread and meeting, in the order of the set (by ``occurred_at``), and
   matches the proposed items with the record's labels
   (:func:`~memini.evaluation.decisions.matching.match_record`). A labelled item found again in
   a later record counts as a duplicate there.
2. Replays the journal the pipeline would write: each proposed item, false ones included, is
   classified against the items active at that moment in the set's workspace, in the order they
   were proposed. ``new`` adds it, ``duplicate`` adds nothing, ``replaces`` and ``answers`` add it
   and take the items it names out of the active ones. Every active item is a candidate: the
   sets are small, and the top-k search the pipeline will put in front of the classifier is not
   measured here. A verdict that names something that was no candidate is invalid: it counts as
   wrong, and the item is added as new.
3. Scores (:mod:`memini.evaluation.decisions.report`): precision and recall of extraction, and
   the supersession cases. A case is judged on the verdict of the proposal that first found its
   item (or, for a restatement, the restatement), read back to labels through the journal: a
   candidate stands for the label its proposal matched, and one that matched none, or a trap,
   stands for nothing labelled.

No database and no model of its own: what the pipeline needs, it brings.
"""

from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from memini.evaluation.decisions.dataset import DecisionSet
from memini.evaluation.decisions.matching import (
    SIMILARITY_THRESHOLD,
    Match,
    match_record,
    targets_of,
)
from memini.evaluation.decisions.report import (
    CaseOutcome,
    DecisionReport,
    ExtractionSummary,
    ItemOutcome,
    SupersessionSummary,
)
from memini.extraction import (
    Candidate,
    CandidateEvidence,
    Extractor,
    ProposedItem,
    RecordInput,
    RecordRef,
    SupersessionClassifier,
    Verdict,
)
from memini.knowledge import ITEM_KINDS, Relation

__all__ = [
    "CASE_RELATIONS",
    "MATCHING",
    "DecisionEvalError",
    "LabelVerdict",
    "Pipeline",
    "run_decision_eval",
]

MATCHING: Final = "dice-terms/1"
"""The name of the statement similarity of :mod:`~memini.evaluation.decisions.matching`, in the
report: change it with the function."""

CASE_RELATIONS: Final = ("new", "duplicate", "replaces", "answers")
"""What a case expects, as the groups ``relation:<relation>`` of the report name it."""


class DecisionEvalError(Exception):
    """The extractor or the classifier failed; the message names the record."""


@dataclass(frozen=True, slots=True)
class Pipeline:
    """What the eval measures: an extractor and a classifier, often one object."""

    extractor: Extractor
    classifier: SupersessionClassifier
    aclose: Callable[[], Awaitable[None]] | None = None
    """Releases what they hold (an HTTP client); the caller calls it after the run."""
    summary: Callable[[], str] | None = None
    """What the run cost and what it refused (calls, tokens, rejected items), for the caller to
    print after the run; not part of the report."""


@dataclass(frozen=True, slots=True)
class LabelVerdict:
    """A verdict read back to labels: the candidates it names, as the labels they stand for."""

    duplicate_of: str | None = None
    supersedes: frozenset[tuple[str, Relation]] = frozenset()

    def describe(self) -> str:
        if self.duplicate_of is not None:
            return f"duplicate of {self.duplicate_of}"
        if not self.supersedes:
            return "new"
        return ", ".join(f"{relation} {label}" for label, relation in sorted(self.supersedes))

    @property
    def relation(self) -> str:
        if self.duplicate_of is not None:
            return "duplicate"
        relations = sorted({relation for _, relation in self.supersedes})
        if not relations:
            return "new"
        return relations[0] if len(relations) == 1 else "mixed"

    @property
    def targets(self) -> frozenset[str]:
        found = {label for label, _ in self.supersedes}
        if self.duplicate_of is not None:
            found.add(self.duplicate_of)
        return frozenset(found)


@dataclass(slots=True)
class _Entry:
    """An item of the replayed journal."""

    id: str
    label: str | None
    """The labelled item its proposal found or restated; ``None`` for a false item."""
    record: RecordRef
    item: ProposedItem
    active: bool = True

    def candidate(self) -> Candidate:
        return Candidate(
            id=self.id,
            kind=self.item.kind,
            statement=self.item.statement,
            decided_at=self.item.decided_at,
            evidence=tuple(CandidateEvidence(self.record, a) for a in self.item.anchors),
        )


@dataclass(slots=True)
class _Proposal:
    record: RecordInput
    item: ProposedItem
    match: Match
    outcome: str
    """As :attr:`Match.outcome`, but a labelled item found again in a later record is a
    ``duplicate``."""
    language: str
    """The label's language for an item found; the record's otherwise."""
    verdict: LabelVerdict | None = None
    invalid: str | None = None
    shown: frozenset[str] = frozenset()
    """The labels the candidates shown with it stood for."""


@dataclass(slots=True)
class _SetRun:
    data: DecisionSet
    proposals: list[_Proposal] = field(default_factory=list[_Proposal])
    found: set[str] = field(default_factory=set[str])
    """The labelled items found: a true positive each."""
    first: dict[str, _Proposal] = field(default_factory=dict[str, _Proposal])
    """By item id: the first proposal, in the order classified, that matched the item where it
    is said (as found, or as its duplicate). Its case is judged on its verdict."""
    restated: dict[tuple[str, RecordRef], _Proposal] = field(
        default_factory=dict[tuple[str, RecordRef], _Proposal]
    )
    """By item id and record: the first proposal that matched a restatement."""


def _read_back(verdict: Verdict, shown: Mapping[str, _Entry]) -> tuple[LabelVerdict, str | None]:
    """``verdict`` with each candidate it names as its label (``?<id>`` for one that stands for
    no label, or was not shown), and why it is invalid: it names what was no candidate."""

    def label(candidate_id: str) -> str:
        entry = shown.get(candidate_id)
        if entry is None or entry.label is None:
            return f"?{candidate_id}"
        return entry.label

    unknown = sorted(name for name in verdict.named if name not in shown)
    invalid = f"it names {', '.join(unknown)}, no candidates" if unknown else None
    if verdict.duplicate_of is not None:
        return LabelVerdict(duplicate_of=label(verdict.duplicate_of)), invalid
    links: frozenset[tuple[str, Relation]] = frozenset(
        (label(s.candidate_id), s.relation) for s in verdict.supersedes
    )
    return LabelVerdict(supersedes=links), invalid


async def _extract(pipeline: Pipeline, record: RecordInput) -> list[ProposedItem]:
    try:
        return list(await pipeline.extractor.extract(record))
    except Exception as error:
        raise DecisionEvalError(
            f"the extractor failed on {record.ref}: {type(error).__name__}: {error}"
        ) from error


async def _classify(
    pipeline: Pipeline, item: ProposedItem, record: RecordInput, candidates: list[Candidate]
) -> Verdict:
    try:
        return await pipeline.classifier.classify(item, record, candidates)
    except Exception as error:
        raise DecisionEvalError(
            f"the classifier failed on an item of {record.ref}: {type(error).__name__}: {error}"
        ) from error


async def _run_set(data: DecisionSet, pipeline: Pipeline, threshold: float) -> _SetRun:
    run = _SetRun(data)
    entries: list[_Entry] = []
    for record in data.records:
        items = await _extract(pipeline, record)
        matches = match_record(items, targets_of(data, record.ref), threshold=threshold)
        for item, match in zip(items, matches, strict=True):
            target = match.target
            outcome: str = match.outcome
            if outcome == "item" and target is not None and target.label in run.found:
                outcome = "duplicate"
            language = data.languages.get(record.ref, "en")
            if outcome == "item" and target is not None:
                language = data.item(target.label).language
                run.found.add(target.label)
            proposal = _Proposal(record, item, match, outcome, language)
            run.proposals.append(proposal)
            if target is not None and target.kind == "item":
                run.first.setdefault(target.label, proposal)
            elif target is not None and target.kind == "restatement":
                run.restated.setdefault((target.label, record.ref), proposal)

            active = {entry.id: entry for entry in entries if entry.active}
            verdict = await _classify(
                pipeline, item, record, [entry.candidate() for entry in active.values()]
            )
            proposal.shown = frozenset(e.label for e in active.values() if e.label is not None)
            proposal.verdict, proposal.invalid = _read_back(verdict, active)
            if proposal.invalid is None:
                if verdict.duplicate_of is not None:
                    continue
                for link in verdict.supersedes:
                    active[link.candidate_id].active = False
            label = None if target is None or target.kind == "trap" else target.label
            entries.append(_Entry(f"c{len(entries) + 1}", label, record.ref, item))
    return run


# --- Scoring -------------------------------------------------------------------------------------


class _Counts:
    """Counters by group, the groups in the order first named."""

    def __init__(self, names: Iterable[str]) -> None:
        self.groups: dict[str, defaultdict[str, int]] = {name: defaultdict(int) for name in names}

    def add(self, groups: Iterable[str], counter: str, amount: int = 1) -> None:
        for group in groups:
            self.groups.setdefault(group, defaultdict(int))[counter] += amount


def _group_names(sets: Sequence[DecisionSet], middle: Iterable[str]) -> list[str]:
    languages = sorted(
        {item.language for each in sets for item in each.items}
        | {language for each in sets for language in each.languages.values()}
    )
    return [
        "all",
        *middle,
        *(f"language:{language}" for language in languages),
        *(f"set:{each.name}" for each in sets),
    ]


def _proposal_json(data: DecisionSet, proposal: _Proposal) -> dict[str, Any]:
    target = proposal.match.target
    return {
        "set": data.name,
        "record": str(proposal.record.ref),
        "kind": proposal.item.kind,
        "statement": proposal.item.statement,
        "anchors": list(proposal.item.anchors),
        "outcome": proposal.outcome,
        "label": None if target is None else target.label,
        "by": proposal.match.by,
        "verdict": None if proposal.verdict is None else proposal.verdict.describe(),
        **({"invalid": proposal.invalid} if proposal.invalid else {}),
    }


def _extraction(
    runs: Sequence[_SetRun],
) -> tuple[dict[str, ExtractionSummary], dict[str, ItemOutcome], list[dict[str, Any]]]:
    counts = _Counts(_group_names([r.data for r in runs], (f"kind:{k}" for k in ITEM_KINDS)))
    items: dict[str, ItemOutcome] = {}
    proposals: list[dict[str, Any]] = []
    for run in runs:
        data = run.data
        for item in data.items:
            found = item.id in run.found
            groups = ("all", f"kind:{item.kind}", f"language:{item.language}", f"set:{data.name}")
            counts.add(groups, "items")
            counts.add(groups, "found", int(found))
            items[f"{data.name}/{item.id}"] = ItemOutcome(found, item.kind, item.language)
        for proposal in run.proposals:
            groups = (
                "all",
                f"kind:{proposal.item.kind}",
                f"language:{proposal.language}",
                f"set:{data.name}",
            )
            counts.add(groups, "proposed")
            if proposal.outcome in ("trap", "none"):
                counts.add(groups, "false")
            counter = {"trap": "traps", "duplicate": "duplicates", "restatement": "restatements"}
            if proposal.outcome in counter:
                counts.add(groups, counter[proposal.outcome])
            proposals.append(_proposal_json(data, proposal))
    summaries = {
        name: ExtractionSummary(
            items=c["items"],
            found=c["found"],
            proposed=c["proposed"],
            false=c["false"],
            traps=c["traps"],
            duplicates=c["duplicates"],
            restatements=c["restatements"],
        )
        for name, c in counts.groups.items()
    }
    return summaries, items, proposals


def _cases(run: _SetRun) -> Iterable[tuple[str, str, LabelVerdict, _Proposal | None]]:
    """Each case of a set: its key, language, expected verdict and the proposal judged."""
    data = run.data
    for item in data.items:
        links: frozenset[tuple[str, Relation]] = frozenset(
            (link.old, link.relation) for link in data.links_from(item.id)
        )
        yield (
            f"{data.name}/{item.id}",
            item.language,
            LabelVerdict(supersedes=links),
            run.first.get(item.id),
        )
    for each in data.restatements:
        yield (
            f"{data.name}/{each.item}@{each.ref}",
            data.languages.get(each.ref, "en"),
            LabelVerdict(duplicate_of=each.item),
            run.restated.get((each.item, each.ref)),
        )


def _supersession(
    runs: Sequence[_SetRun],
) -> tuple[dict[str, SupersessionSummary], dict[str, CaseOutcome]]:
    counts = _Counts(
        _group_names([r.data for r in runs], (f"relation:{r}" for r in CASE_RELATIONS))
    )
    outcomes: dict[str, CaseOutcome] = {}
    for run in runs:
        for key, language, expected, proposal in _cases(run):
            groups = (
                "all",
                f"relation:{expected.relation}",
                f"language:{language}",
                f"set:{run.data.name}",
            )
            counts.add(groups, "cases")
            got = None if proposal is None else proposal.verdict
            reached = proposal is not None and expected.targets <= proposal.shown
            correct = (
                proposal is not None and proposal.invalid is None and proposal.verdict == expected
            )
            counts.add(groups, "extracted", int(proposal is not None))
            counts.add(groups, "reached", int(reached))
            counts.add(groups, "correct", int(correct))
            outcomes[key] = CaseOutcome(
                expected=expected.describe(),
                got=None if got is None else got.describe(),
                reached=reached,
                correct=correct,
            )
    summaries = {
        name: SupersessionSummary(
            cases=c["cases"], extracted=c["extracted"], reached=c["reached"], correct=c["correct"]
        )
        for name, c in counts.groups.items()
    }
    return summaries, outcomes


def _setup(
    sets: Sequence[DecisionSet], extractor: str, variant: str, threshold: float
) -> dict[str, Any]:
    return {
        "sets": [
            {
                "name": each.name,
                "workspace": each.workspace,
                "digest": each.digest,
                "records": len(each.records),
                "items": len(each.items),
                "links": len(each.links),
                "restatements": len(each.restatements),
                "traps": len(each.traps),
                "no_items": len(each.no_items),
            }
            for each in sets
        ],
        "extractor": extractor,
        "variant": variant,
        "matching": {"similarity": MATCHING, "threshold": threshold},
    }


async def run_decision_eval(
    sets: Sequence[DecisionSet],
    pipeline: Pipeline,
    *,
    variant: str,
    threshold: float = SIMILARITY_THRESHOLD,
    say: Callable[[str], None] | None = None,
) -> DecisionReport:
    """Run ``pipeline`` over ``sets`` and score it (see the module). ``variant`` names it in
    the report, with the extractor's own name.

    Raises:
        DecisionEvalError: the extractor or the classifier raised.
    """
    runs: list[_SetRun] = []
    for data in sets:
        run = await _run_set(data, pipeline, threshold)
        if say is not None:
            say(
                f"set {data.name}: {len(data.records)} records, {len(run.proposals)} items proposed"
            )
        runs.append(run)
    extraction, items, proposals = _extraction(runs)
    supersession, cases = _supersession(runs)
    notes: list[str] = []
    invalid = sum(1 for run in runs for p in run.proposals if p.invalid is not None)
    if invalid:
        notes.append(f"{invalid} verdicts named items that were no candidates; each counts wrong")
    return DecisionReport(
        setup=_setup(sets, pipeline.extractor.name, variant, threshold),
        extraction=extraction,
        supersession=supersession,
        items=items,
        cases=cases,
        proposals=tuple(proposals),
        notes=tuple(notes),
    )
