"""What decision extraction reads and returns, and the two protocols an extractor implements.

Requirements, section 5: one LLM call per new or changed thread or meeting extracts decisions and
open questions; then, for each item extracted, one call classifies it against similar active
items of the workspace as new, a duplicate, or replacing or answering specific items. The two
steps are two protocols, so the decision eval (:mod:`memini.evaluation.decisions`) measures any
implementation of them, the stubs of the eval and the LLM pipeline alike:

- :class:`Extractor`: one record, as a :class:`RecordInput` (its normalized text, the parts
  evidence may cite, and its metadata), to the :class:`ProposedItem` objects it finds there.
- :class:`SupersessionClassifier`: one proposed item and the active items it may duplicate,
  replace or answer (:class:`Candidate`), to a :class:`Verdict`.

Pure types: no database and no model. A proposed item names its evidence by anchors of the record
it was extracted from (:attr:`Part.anchor`); the caller turns them into
:class:`memini.knowledge.EvidenceRef` with the record's id, and a verdict into
:class:`memini.knowledge.SupersedesRef`, when it stores the item.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal, Protocol, get_args

from memini.knowledge import ITEM_KINDS, RELATIONS, ItemKind, Relation
from memini.payloads import Rows

__all__ = [
    "EXTRACTED_KINDS",
    "PART_KINDS",
    "VERDICT_LABELS",
    "Candidate",
    "CandidateEvidence",
    "ExtractedKind",
    "Extractor",
    "Part",
    "PartKind",
    "ProposedItem",
    "RecordInput",
    "RecordRef",
    "Supersedes",
    "SupersessionClassifier",
    "Verdict",
    "VerdictLabel",
]

ExtractedKind = Literal["thread", "meeting"]
"""The kinds of record decisions are extracted from. Documents and code are not (section 5)."""
EXTRACTED_KINDS: Final[tuple[ExtractedKind, ...]] = get_args(ExtractedKind)

PartKind = Literal["message", "segment", "summary", "action-items"]
"""What a part of a record is: a message of a thread; a transcript segment, the summary or the
action items of a meeting."""
PART_KINDS: Final[tuple[PartKind, ...]] = get_args(PartKind)

VerdictLabel = Literal["new", "duplicate", "replaces", "answers", "mixed"]
"""A verdict in one word: ``mixed`` replaces some items and answers others."""
VERDICT_LABELS: Final[tuple[VerdictLabel, ...]] = get_args(VerdictLabel)


@dataclass(frozen=True, slots=True, order=True)
class RecordRef:
    """A record as the decision tools and the labels name it: its source's name, its kind and
    its id in the source."""

    source: str
    kind: str
    external_id: str

    def __str__(self) -> str:
        return f"{self.source}/{self.kind}/{self.external_id}"


@dataclass(frozen=True, slots=True)
class Part:
    """A place in a record that evidence can cite: a message, a segment, the summary, the action
    items. ``anchor`` is what :class:`memini.knowledge.EvidenceRef` stores as its anchor."""

    anchor: str
    """The message id; the segment id; ``summary``; ``action-items``."""
    kind: PartKind
    text: str
    """The part's own text, redacted as the stored record is: the message, the segment, the
    summary (Markdown), or the action items as the normalized text lists them."""
    rows: Rows
    """Where the part is in :attr:`RecordInput.text`, its heading line included."""
    who: str | None = None
    """The author of a message, the speaker of a segment, as the source shows them."""
    at: datetime | None = None
    """When it was said: the message's time, the segment's (the meeting's start plus its offset);
    ``None`` for a meeting's summary and action items, and a segment without an offset."""


@dataclass(frozen=True, slots=True)
class RecordInput:
    """One thread or meeting as an extractor reads it."""

    workspace: str
    ref: RecordRef
    occurred_at: datetime
    content_hash: str
    """Of the stored payload: what extraction is idempotent by, with the extractor's name."""
    text: str
    """The record's normalized text, as ingest stores it (:func:`memini.payloads.normalize`)."""
    parts: tuple[Part, ...]
    title: str | None = None
    """A thread's channel and title, a meeting's title."""
    external_url: str | None = None
    record_id: str | None = None
    """The store's id of the record; ``None`` when the record is read from files (the eval)."""

    @property
    def anchors(self) -> frozenset[str]:
        """The anchors evidence in this record may name."""
        return frozenset(part.anchor for part in self.parts)

    def part(self, anchor: str) -> Part | None:
        for part in self.parts:
            if part.anchor == anchor:
                return part
        return None


@dataclass(frozen=True, slots=True)
class ProposedItem:
    """A decision or an open question an extractor found in one record."""

    kind: ItemKind
    statement: str
    """One or two sentences: what was decided or asked (requirements, section 3)."""
    decided_at: datetime
    """When it was said: the time of the part that says it. Timezone-aware."""
    anchors: tuple[str, ...]
    """The parts of the record that say it, the one that decides it first; at least one."""
    participants: tuple[str, ...] = ()
    """Names as the record shows them (authors, speakers); persons are resolved when stored."""

    def __post_init__(self) -> None:
        if self.kind not in ITEM_KINDS:
            raise ValueError(f"an item's kind is one of {', '.join(ITEM_KINDS)}")
        if not self.statement.strip():
            raise ValueError("an item has a statement")
        if self.decided_at.utcoffset() is None:
            raise ValueError("decided_at is a time with a timezone")
        if not self.anchors:
            raise ValueError("an item cites at least one part of its record (principle 2)")


@dataclass(frozen=True, slots=True)
class CandidateEvidence:
    record: RecordRef
    anchor: str | None


@dataclass(frozen=True, slots=True)
class Candidate:
    """An active item a new one may duplicate, replace or answer."""

    id: str
    """Opaque to the classifier: the caller's id of the item, returned in the verdict."""
    kind: ItemKind
    statement: str
    decided_at: datetime
    evidence: tuple[CandidateEvidence, ...] = ()


@dataclass(frozen=True, slots=True)
class Supersedes:
    """A candidate the new item replaces or answers."""

    candidate_id: str
    relation: Relation


@dataclass(frozen=True, slots=True)
class Verdict:
    """What a new item is, against the candidates it was shown: new (the default); a duplicate of
    one candidate (it is not stored again); or replacing or answering some of them."""

    duplicate_of: str | None = None
    supersedes: tuple[Supersedes, ...] = ()

    def __post_init__(self) -> None:
        if self.duplicate_of is not None and self.supersedes:
            raise ValueError("a duplicate replaces or answers nothing")
        ids = [link.candidate_id for link in self.supersedes]
        if len(ids) != len(set(ids)):
            raise ValueError("a candidate is named once, with one relation")
        if any(link.relation not in RELATIONS for link in self.supersedes):
            raise ValueError(f"a relation is one of {', '.join(RELATIONS)}")

    @property
    def label(self) -> VerdictLabel:
        if self.duplicate_of is not None:
            return "duplicate"
        relations = {link.relation for link in self.supersedes}
        if not relations:
            return "new"
        if relations == {"replaces"}:
            return "replaces"
        if relations == {"answers"}:
            return "answers"
        return "mixed"

    @property
    def named(self) -> frozenset[str]:
        """Every candidate the verdict names."""
        found = {link.candidate_id for link in self.supersedes}
        if self.duplicate_of is not None:
            found.add(self.duplicate_of)
        return frozenset(found)


class Extractor(Protocol):
    """Finds the decisions and open questions of one record (requirements, section 5)."""

    @property
    def name(self) -> str:
        """The model and prompt version, as :func:`memini.knowledge.check_extractor` takes it:
        stored with every item and extraction, and recorded in the eval's report."""
        ...

    async def extract(self, record: RecordInput) -> Sequence[ProposedItem]:
        """The items ``record`` holds, none for chatter. Each cites anchors of ``record``."""
        ...


class SupersessionClassifier(Protocol):
    """Decides how a new item stands to the active items most like it (section 5)."""

    async def classify(
        self, item: ProposedItem, record: RecordInput, candidates: Sequence[Candidate]
    ) -> Verdict:
        """``item``, extracted from ``record``, against ``candidates``: new, a duplicate of one,
        or replacing or answering some of them. The verdict names candidates by their ids."""
        ...
