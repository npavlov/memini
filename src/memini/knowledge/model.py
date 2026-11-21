"""Types of the knowledge layer: what a caller passes in, what it gets back, and the errors.

The database checks the same lists and limits (the knowledge migration); the checks here run first
so that a refusal names the field and the reason before anything is written.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal, get_args
from uuid import UUID

from memini.ids import RecordKind

__all__ = [
    "ALL_STATUSES",
    "ITEM_KINDS",
    "MANUAL",
    "MAX_ANCHOR_LENGTH",
    "MAX_EVIDENCE",
    "MAX_LIST_LIMIT",
    "MAX_PARTICIPANTS",
    "MAX_STATEMENT_LENGTH",
    "MAX_SUPERSEDES",
    "RELATIONS",
    "Evidence",
    "EvidenceRef",
    "Extraction",
    "InvalidItemError",
    "Item",
    "ItemChain",
    "ItemFilters",
    "ItemKind",
    "ItemStatus",
    "Link",
    "NewItem",
    "NotActiveError",
    "Participant",
    "RecordState",
    "Relation",
    "SupersedesRef",
    "SupersessionError",
    "SupersessionRule",
    "check_anchor",
    "check_content_hash",
    "check_extractor",
    "check_new_item",
    "check_record_id",
    "check_supersedes",
]

ItemKind = Literal["decision", "open_question"]
"""decision: something decided. open_question: something asked and not decided yet."""

ItemStatus = Literal["active", "superseded", "closed", "retracted"]
"""Where an item stands (requirements, section 3).

- ``active``: current; the default view shows only these.
- ``superseded``: a newer item replaces it.
- ``closed``: a decision answers it, or it was closed by hand without a replacement.
- ``retracted``: it was wrong (a misread extraction, a mistake); it stays in the history.

An item is recorded active and changes status at most once.
"""

Relation = Literal["replaces", "answers"]
"""How a newer item supersedes an earlier one: ``replaces`` makes it superseded; ``answers`` (a
decision answering an open question) makes it closed."""

RecordState = Literal["stored", "deleted", "expired", "forgotten", "gone"]
"""What became of the record a piece of evidence names.

- ``stored``: the record is in the store.
- ``deleted``: a tombstone; the source deleted it, and the store keeps it until retention.
- ``expired``: removed by the retention of its source.
- ``forgotten``: removed by hand (forget).
- ``gone``: no longer stored, its source deleted.

Without the record, the reference (source, kind, external id and URL) is what is left.
"""

ITEM_KINDS: Final[tuple[ItemKind, ...]] = get_args(ItemKind)
ALL_STATUSES: Final[tuple[ItemStatus, ...]] = get_args(ItemStatus)
"""Every status: the history view."""
RELATIONS: Final[tuple[Relation, ...]] = get_args(Relation)

MANUAL: Final = "manual"
"""The extractor of an item a person recorded."""

MAX_STATEMENT_LENGTH: Final = 2000
MAX_ANCHOR_LENGTH: Final = 1000
MAX_EVIDENCE: Final = 50
MAX_PARTICIPANTS: Final = 100
MAX_SUPERSEDES: Final = 50
MAX_LIST_LIMIT: Final = 500

_EXTRACTOR: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/+=-]{0,199}")
_RECORD_ID: Final = re.compile(r"[0-9a-f]{64}")
_COMMIT_ANCHOR: Final = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})(?:#.+)?", re.DOTALL)
_CONTROL: Final = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_MAX_CONTENT_HASH_LENGTH: Final = 200


# --- Errors --------------------------------------------------------------------------------------


class InvalidItemError(ValueError):
    """An item, its evidence or its links were refused before anything was written. The message
    names the field and the reason, never the value, so it can be shown to the caller and logged."""

    def __init__(self, field: str, reason: str) -> None:
        super().__init__(f"{field}: {reason}")
        self.field = field
        self.reason = reason


class NotActiveError(Exception):
    """The item is superseded, closed or retracted, and the operation needs an active one: only an
    active item is superseded, closed or retracted, and only an active item supersedes others."""

    def __init__(self, item_id: UUID, status: ItemStatus) -> None:
        super().__init__(f"knowledge item {item_id} is {status}, not active")
        self.item_id = item_id
        self.status: ItemStatus = status


SupersessionRule = Literal["cycle", "relation", "order", "manual"]
"""Why a supersession link was refused:

- ``cycle``: the earlier item already supersedes the newer one, through a chain of links, or
  they are the same item.
- ``relation``: ``answers`` links a decision to an open question, nothing else.
- ``order``: the newer item was said before the one it would supersede.
- ``manual``: an extracted item never supersedes or answers an item a person recorded.
"""

_RULE_REASONS: Final[dict[SupersessionRule, str]] = {
    "cycle": "the link would close a cycle of supersession",
    "relation": "a decision answers an open question; other items replace each other",
    "order": "an item supersedes only items said no later than itself",
    "manual": "extraction never supersedes or answers an item recorded by hand",
}


class SupersessionError(Exception):
    """A supersession link was refused by one of the rules of :data:`SupersessionRule`."""

    def __init__(self, rule: SupersessionRule) -> None:
        super().__init__(_RULE_REASONS[rule])
        self.rule: SupersessionRule = rule


# --- Input ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    """A record an item comes from, and where in it.

    ``anchor`` is the place in the record, in the form of the record's kind: in a thread the id of
    the message; in a meeting a segment, a time offset or a section; in a code file the commit,
    optionally followed by ``#`` and a symbol or lines (``<sha>#Greenhouse.water``,
    ``<sha>#L12-L30``); in a document a heading trail or lines. A thread or a meeting needs one, a
    code file needs the commit; ``None`` cites a whole document.
    """

    record_id: str
    anchor: str | None = None


@dataclass(frozen=True, slots=True)
class SupersedesRef:
    """An earlier item the new item replaces or answers."""

    item_id: UUID
    relation: Relation = "replaces"


@dataclass(frozen=True, slots=True)
class NewItem:
    """A decision or an open question to record."""

    kind: ItemKind
    statement: str
    """One or two sentences: what was decided or asked. Surrounding whitespace is dropped."""
    decided_at: datetime
    """When it was said or asked (event time); timezone-aware."""
    evidence: Sequence[EvidenceRef]
    """At least one: an item without evidence is not stored (principle 2)."""
    participants: Sequence[UUID] = ()
    """Persons of the item's workspace."""
    supersedes: Sequence[SupersedesRef] = ()
    """Active items of the workspace this one replaces or answers, linked when it is recorded."""


def check_record_id(value: str, field: str = "record_id") -> str:
    """``value`` if it is a record id (64 lowercase hex characters)."""
    if _RECORD_ID.fullmatch(value) is None:
        raise InvalidItemError(field, "a record id is 64 lowercase hexadecimal characters")
    return value


def check_extractor(value: str) -> str:
    """``value`` if it names an extractor: the model and prompt version of an extraction.

    1 to 200 characters of letters, digits and ``. _ : @ / + = -``, starting with a letter or
    digit; ``manual`` is reserved for items a person records.
    """
    if _EXTRACTOR.fullmatch(value) is None:
        raise InvalidItemError(
            "extractor",
            "1 to 200 letters, digits or '._:@/+=-', starting with a letter or digit",
        )
    if value == MANUAL:
        raise InvalidItemError("extractor", f"{MANUAL!r} is reserved for items recorded by hand")
    return value


def check_content_hash(value: str) -> str:
    """``value`` if it can be a record's content hash."""
    if not value or len(value) > _MAX_CONTENT_HASH_LENGTH or _CONTROL.search(value):
        raise InvalidItemError("content_hash", "the content hash of the record that was read")
    return value


def check_anchor(kind: RecordKind, anchor: str | None) -> None:
    """Refuse an anchor that does not fit a record of ``kind``; see :class:`EvidenceRef`."""
    if kind in ("thread", "meeting") and anchor is None:
        raise InvalidItemError("evidence", f"evidence in a {kind} names the message it is in")
    if kind == "code_file" and (anchor is None or _COMMIT_ANCHOR.fullmatch(anchor) is None):
        raise InvalidItemError(
            "evidence",
            "evidence in code names the commit: '<sha>', '<sha>#symbol' or '<sha>#L1-L9'",
        )


def _check_statement(value: str) -> str:
    statement = value.strip()
    if not statement:
        raise InvalidItemError("statement", "empty")
    if len(statement) > MAX_STATEMENT_LENGTH:
        raise InvalidItemError("statement", f"longer than {MAX_STATEMENT_LENGTH} characters")
    if "\x00" in statement:
        raise InvalidItemError("statement", "contains a NUL character")
    return statement


def _check_evidence_ref(ref: EvidenceRef) -> EvidenceRef:
    check_record_id(ref.record_id, "evidence")
    if ref.anchor is None:
        return ref
    anchor = ref.anchor.strip()
    if not anchor:
        raise InvalidItemError("evidence", "an anchor is not empty; None cites the whole record")
    if len(anchor) > MAX_ANCHOR_LENGTH:
        raise InvalidItemError("evidence", f"an anchor is at most {MAX_ANCHOR_LENGTH} characters")
    if _CONTROL.search(anchor):
        raise InvalidItemError("evidence", "an anchor contains no control characters")
    return EvidenceRef(record_id=ref.record_id, anchor=anchor)


def check_new_item(new: NewItem) -> NewItem:
    """``new`` with its statement and anchors trimmed and repeated references dropped, or
    :class:`InvalidItemError`. Whether the records, persons and items it names exist is checked
    when it is recorded."""
    if new.kind not in ITEM_KINDS:
        raise InvalidItemError("kind", f"one of {', '.join(ITEM_KINDS)}")
    statement = _check_statement(new.statement)
    if new.decided_at.utcoffset() is None:
        raise InvalidItemError("decided_at", "a time with a timezone")
    if not new.evidence:
        raise InvalidItemError("evidence", "at least one record the item comes from")
    if len(new.evidence) > MAX_EVIDENCE:
        raise InvalidItemError("evidence", f"at most {MAX_EVIDENCE} references")
    evidence = tuple(dict.fromkeys(_check_evidence_ref(ref) for ref in new.evidence))
    if len(new.participants) > MAX_PARTICIPANTS:
        raise InvalidItemError("participants", f"at most {MAX_PARTICIPANTS} persons")
    participants = tuple(dict.fromkeys(new.participants))
    if len(new.supersedes) > MAX_SUPERSEDES:
        raise InvalidItemError("supersedes", f"at most {MAX_SUPERSEDES} items")
    supersedes = check_supersedes(new.supersedes)
    return NewItem(
        kind=new.kind,
        statement=statement,
        decided_at=new.decided_at,
        evidence=evidence,
        participants=participants,
        supersedes=supersedes,
    )


def check_supersedes(refs: Sequence[SupersedesRef]) -> tuple[SupersedesRef, ...]:
    """``refs`` without repeats, or :class:`InvalidItemError` for an unknown relation or an item
    named with two relations."""
    relations: dict[UUID, Relation] = {}
    for ref in refs:
        if ref.relation not in RELATIONS:
            raise InvalidItemError("supersedes", f"a relation is one of {', '.join(RELATIONS)}")
        if relations.setdefault(ref.item_id, ref.relation) != ref.relation:
            raise InvalidItemError("supersedes", "an item is named once, with one relation")
    return tuple(SupersedesRef(item_id, relation) for item_id, relation in relations.items())


@dataclass(frozen=True, slots=True)
class ItemFilters:
    """What :func:`memini.knowledge.list_items` returns. The default: active items of every
    workspace the caller may read, newest first."""

    workspaces: tuple[str | UUID, ...] | None = None
    """Only these workspaces, by name or id; ``None``: every workspace the caller may read."""
    kinds: tuple[ItemKind, ...] | None = None
    statuses: tuple[ItemStatus, ...] = ("active",)
    """``("active",)`` is the default view; :data:`ALL_STATUSES` is the history."""
    person_ids: tuple[UUID, ...] | None = None
    """Only items one of these persons took part in, or a person merged with one of them: the
    persons their canonical persons stand for (:mod:`memini.persons`)."""
    record_ids: tuple[str, ...] | None = None
    """Only items with evidence in one of these records."""
    decided_from: datetime | None = None
    """Only items said at or after this time."""
    decided_until: datetime | None = None
    """Only items said before this time."""
    limit: int = 50
    query: str | None = None
    """Rank statements matching query words before applying the limit."""
    sources: tuple[str, ...] | None = None
    repositories: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.limit <= MAX_LIST_LIMIT:
            raise InvalidItemError("limit", f"1 to {MAX_LIST_LIMIT}")
        if not self.statuses or any(status not in ALL_STATUSES for status in self.statuses):
            raise InvalidItemError("statuses", f"one or more of {', '.join(ALL_STATUSES)}")
        if self.kinds is not None and (
            not self.kinds or any(kind not in ITEM_KINDS for kind in self.kinds)
        ):
            raise InvalidItemError("kinds", f"one or more of {', '.join(ITEM_KINDS)}")
        if self.record_ids is not None:
            for record_id in self.record_ids:
                check_record_id(record_id, "record_ids")
        for name, value in (
            ("decided_from", self.decided_from),
            ("decided_until", self.decided_until),
        ):
            if value is not None and value.utcoffset() is None:
                raise InvalidItemError(name, "a time with a timezone")


# --- Output --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Evidence:
    """A record an item comes from, as stored with the item, and what became of the record."""

    record_id: str
    source_id: UUID
    source: str | None
    """The source's name; ``None`` when the source no longer exists."""
    record_kind: RecordKind
    external_id: str
    external_url: str | None
    anchor: str | None
    record_state: RecordState


@dataclass(frozen=True, slots=True)
class Participant:
    person_id: UUID
    display_name: str


@dataclass(frozen=True, slots=True)
class Link:
    """A supersession link: ``item_id`` replaces or answers ``supersedes_id``. Stored on the newer
    item, ``item_id``."""

    item_id: UUID
    supersedes_id: UUID
    relation: Relation
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Item:
    """A decision or an open question with its evidence, participants and links."""

    id: UUID
    workspace_id: UUID
    workspace: str
    kind: ItemKind
    statement: str
    decided_at: datetime
    status: ItemStatus
    recorded_at: datetime
    """When the service learned it."""
    invalidated_at: datetime | None
    """When it stopped being current; ``None`` while active."""
    extractor: str
    """The model and prompt version of the extraction, or :data:`MANUAL`."""
    extraction_id: UUID | None
    evidence: tuple[Evidence, ...]
    participants: tuple[Participant, ...]
    supersedes: tuple[Link, ...]
    """The earlier items this one replaces or answers."""
    superseded_by: Link | None
    """The link from the newer item that replaced or answered this one, if any."""

    @property
    def manual(self) -> bool:
        """Whether a person recorded the item."""
        return self.extractor == MANUAL


@dataclass(frozen=True, slots=True)
class ItemChain:
    """An item and its supersession chain, both directions: what :func:`get_item` returns."""

    item: Item
    chain: tuple[Item, ...]
    """Every item linked to :attr:`item` directly or through others, :attr:`item` included,
    oldest first (by ``decided_at``, then ``recorded_at``)."""


@dataclass(frozen=True, slots=True)
class Extraction:
    """One record's content read by one extractor, and the items it gave."""

    id: UUID
    workspace_id: UUID
    record_id: str
    content_hash: str
    extractor: str
    created_at: datetime
    item_ids: tuple[UUID, ...]
    created: bool
    """``False``: the same content had been extracted by the same extractor before; nothing was
    written, and :attr:`item_ids` are the items of that extraction."""
