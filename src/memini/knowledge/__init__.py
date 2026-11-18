"""The knowledge layer: decisions and open questions, with evidence, in an append-only journal.

Requirements, sections 2 (principles 2 and 4), 3 (knowledge layer), 5 and 6. The tables and the
rules the database enforces are described in the knowledge migration.

- An **item** is a decision or an open question: a statement, when it was said
  (``decided_at``), the persons involved, and its **evidence**, one or more records with an
  anchor in each (the message of a thread, the commit of a code file). An item without evidence
  is not stored (principle 2).
- The journal is **append-only**: a stored item, its evidence and its participants never change.
  Corrections are new items, or **supersession links** stored on the newer item: ``replaces``
  makes the earlier item superseded, ``answers`` (a decision answering an open question) makes it
  closed. An item can also be closed without a replacement, or retracted when it was wrong. The
  status changes once, from active, and sets ``invalidated_at``; nothing is deleted
  (principle 4).
- Evidence outlives its record: after retention, forget or a deleted source, it keeps the
  reference, and :attr:`Evidence.record_state` says the record is ``expired``, ``forgotten`` or
  ``gone``.

Who calls what:

- **Extraction** (one job per new or changed thread or meeting) calls :func:`record_extraction`
  with the record id, its content hash and the extractor (model and prompt version). It is
  idempotent: the same content by the same extractor is stored once, also when nothing was
  found. The supersession check then calls :func:`supersede` on the new items, or passes
  :attr:`NewItem.supersedes` with them. Extraction never supersedes or answers a manual item.
- **Manual operations** call :func:`record_item` (extractor :data:`MANUAL`, with ``supersedes``
  to replace an item with a new statement), :func:`supersede` to link two stored items,
  :func:`close_item` and :func:`retract_item`.
- **Tools** (``list_decisions``, ``get_decision``) call :func:`list_items`, active items by
  default and every status for the history, and :func:`get_item`, an item with its evidence and
  its supersession chain in both directions.

Reads take the caller's :class:`~memini.auth.ReadFilter`, writes its
:class:`~memini.auth.WriteAccess`; something the caller may not see raises
:class:`~memini.workspaces.NotFoundError`, as something that does not exist does.
"""

from memini.knowledge.journal import (
    close_item,
    record_extraction,
    record_item,
    retract_item,
    supersede,
)
from memini.knowledge.model import (
    ALL_STATUSES,
    ITEM_KINDS,
    MANUAL,
    MAX_ANCHOR_LENGTH,
    MAX_EVIDENCE,
    MAX_LIST_LIMIT,
    MAX_PARTICIPANTS,
    MAX_STATEMENT_LENGTH,
    MAX_SUPERSEDES,
    RELATIONS,
    Evidence,
    EvidenceRef,
    Extraction,
    InvalidItemError,
    Item,
    ItemChain,
    ItemFilters,
    ItemKind,
    ItemStatus,
    Link,
    NewItem,
    NotActiveError,
    Participant,
    RecordState,
    Relation,
    SupersedesRef,
    SupersessionError,
    SupersessionRule,
    check_extractor,
)
from memini.knowledge.reader import get_item, list_items
from memini.workspaces import NotFoundError

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
    "NotFoundError",
    "Participant",
    "RecordState",
    "Relation",
    "SupersedesRef",
    "SupersessionError",
    "SupersessionRule",
    "check_extractor",
    "close_item",
    "get_item",
    "list_items",
    "record_extraction",
    "record_item",
    "retract_item",
    "supersede",
]
