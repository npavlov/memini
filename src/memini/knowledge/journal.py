"""Writing the knowledge layer: record items, supersede, close and retract them.

The journal is append-only, and the database enforces it (the knowledge migration): a stored item,
its evidence and its participants never change; its status changes once, from active, through one
of the functions here. They check what they can first and say why they refuse, before anything is
written; the database refuses the rest.

Every write takes the caller's :class:`~memini.auth.WriteAccess` and runs in a transaction of its
own, or in a savepoint when the caller has a transaction open, so a refused write leaves the
caller's connection usable. A workspace, record, person or item the access may not change raises
:class:`~memini.workspaces.NotFoundError`, as one that does not exist does.

Locks: the items a supersession links are locked (``FOR NO KEY UPDATE``) in id order, the order the
database's own check takes them in, so two writes that link the same items wait for each other
and the second sees what the first did.
"""

from collections.abc import Sequence
from typing import Any, Final
from uuid import UUID

from psycopg import AsyncConnection, errors, sql
from psycopg.rows import TupleRow

from memini.auth import WriteAccess
from memini.knowledge.model import (
    MANUAL,
    MAX_SUPERSEDES,
    Extraction,
    InvalidItemError,
    Item,
    ItemStatus,
    NewItem,
    NotActiveError,
    SupersedesRef,
    SupersessionError,
    SupersessionRule,
    check_anchor,
    check_content_hash,
    check_extractor,
    check_new_item,
    check_record_id,
    check_supersedes,
)
from memini.knowledge.reader import load_item
from memini.workspaces import NotFoundError

__all__ = [
    "close_item",
    "record_extraction",
    "record_item",
    "retract_item",
    "supersede",
]

type Conn = AsyncConnection[TupleRow]

# Refusals of the database's rules that the checks here do not make first, by constraint.
_RULES: Final[dict[str, SupersessionRule]] = {
    "knowledge_supersession_cycle_check": "cycle",
    "knowledge_supersession_self_check": "cycle",
    "knowledge_supersession_relation_check": "relation",
    "knowledge_supersession_order_check": "order",
    "knowledge_supersession_manual_check": "manual",
}
_NOT_FOUND: Final[dict[str, str]] = {
    "knowledge_evidence_record_id_fkey": "record",
    "knowledge_extraction_record_id_fkey": "record",
    "knowledge_participant_person_fkey": "person",
    "knowledge_supersession_item_fkey": "knowledge item",
    "knowledge_supersession_supersedes_fkey": "knowledge item",
}

_EXTRACTED_EVIDENCE: Final = "an extracted item cites the record it was extracted from"


def _refusal(error: errors.Error) -> Exception | None:
    """The error of this module for a refusal by the database, or ``None`` if it is not one."""
    name = error.diag.constraint_name or ""
    if isinstance(error, errors.CheckViolation):
        if name in _RULES:
            return SupersessionError(_RULES[name])
        if name == "knowledge_evidence_extraction_check":
            return InvalidItemError("evidence", _EXTRACTED_EVIDENCE)
    if isinstance(error, errors.ForeignKeyViolation) and name in _NOT_FOUND:
        return NotFoundError(_NOT_FOUND[name])
    return None


def _scope(access: WriteAccess, alias: str, *, source: str | None = None) -> sql.Composable:
    return access.where(
        workspace_id=sql.Identifier(alias, "workspace_id"),
        owner_id=sql.Identifier(alias, "owner_id"),
        source_id=None if source is None else sql.Identifier(alias, source),
    )


async def _writable_workspace(conn: Conn, access: WriteAccess, workspace: str | UUID) -> UUID:
    by = sql.SQL("w.id = %s" if isinstance(workspace, UUID) else "w.name = %s")
    query = sql.SQL("SELECT w.id FROM workspace w WHERE {} AND {}").format(
        by,
        access.where(
            workspace_id=sql.Identifier("w", "id"), owner_id=sql.Identifier("w", "owner_id")
        ),
    )
    cur = await conn.execute(query, (workspace,))
    row = await cur.fetchone()
    if row is None:
        raise NotFoundError("workspace")
    workspace_id: UUID = row[0]
    return workspace_id


async def _check_records(conn: Conn, access: WriteAccess, workspace_id: UUID, new: NewItem) -> None:
    """Every record ``new`` cites is in the workspace, and each anchor fits its record's kind."""
    query = sql.SQL(
        "SELECT r.id, r.kind FROM record r WHERE r.id = ANY(%s) AND r.workspace_id = %s AND {}"
    ).format(_scope(access, "r", source="source_id"))
    cur = await conn.execute(query, (sorted({ref.record_id for ref in new.evidence}), workspace_id))
    kinds: dict[str, Any] = {record_id: kind for record_id, kind in await cur.fetchall()}
    for ref in new.evidence:
        kind = kinds.get(ref.record_id)
        if kind is None:
            raise NotFoundError("record")
        check_anchor(kind, ref.anchor)


async def _check_persons(conn: Conn, access: WriteAccess, workspace_id: UUID, new: NewItem) -> None:
    if not new.participants:
        return
    query = sql.SQL(
        "SELECT count(*) FROM person p WHERE p.id = ANY(%s) AND p.workspace_id = %s AND {}"
    ).format(_scope(access, "p"))
    cur = await conn.execute(query, (list(new.participants), workspace_id))
    row = await cur.fetchone()
    if row is None or row[0] != len(new.participants):
        raise NotFoundError("person")


async def _insert_item(
    conn: Conn,
    access: WriteAccess,
    workspace_id: UUID,
    new: NewItem,
    *,
    extractor: str,
    extraction_id: UUID | None,
) -> UUID:
    """Insert a checked item with its evidence, participants and links; return its id."""
    await _check_records(conn, access, workspace_id, new)
    await _check_persons(conn, access, workspace_id, new)
    cur = await conn.execute(
        "INSERT INTO knowledge_item"
        " (workspace_id, owner_id, kind, statement, decided_at, extractor, extraction_id)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (
            workspace_id,
            access.owner_id,
            new.kind,
            new.statement,
            new.decided_at,
            extractor,
            extraction_id,
        ),
    )
    row = await cur.fetchone()
    if row is None:  # INSERT ... RETURNING always returns the row
        raise RuntimeError("INSERT INTO knowledge_item returned no row")
    item_id: UUID = row[0]
    # The database copies the rest of each reference from its record.
    await conn.execute(
        "INSERT INTO knowledge_evidence (workspace_id, owner_id, item_id, record_id, anchor)"
        " SELECT %s, %s, %s, e.record_id, e.anchor"
        " FROM unnest(%s::text[], %s::text[]) WITH ORDINALITY AS e (record_id, anchor, n)"
        " ORDER BY e.n",
        (
            workspace_id,
            access.owner_id,
            item_id,
            [ref.record_id for ref in new.evidence],
            [ref.anchor for ref in new.evidence],
        ),
    )
    if new.participants:
        await conn.execute(
            "INSERT INTO knowledge_participant (workspace_id, owner_id, item_id, person_id)"
            " SELECT %s, %s, %s, person_id FROM unnest(%s::uuid[]) AS person_id",
            (workspace_id, access.owner_id, item_id, list(new.participants)),
        )
    await _link(conn, access, workspace_id, item_id, new.supersedes)
    return item_id


async def _link(
    conn: Conn,
    access: WriteAccess,
    workspace_id: UUID,
    item_id: UUID,
    refs: Sequence[SupersedesRef],
) -> None:
    """Store the links from ``item_id`` to the items of ``refs``; the database supersedes or
    closes them. Every item involved is locked first and must be active."""
    if not refs:
        return
    if any(ref.item_id == item_id for ref in refs):
        raise SupersessionError("cycle")
    involved = sorted({item_id, *(ref.item_id for ref in refs)})
    query = sql.SQL(
        "SELECT i.id, i.status FROM knowledge_item i"
        " WHERE i.id = ANY(%s) AND i.workspace_id = %s AND {}"
        " ORDER BY i.id FOR NO KEY UPDATE"
    ).format(_scope(access, "i"))
    cur = await conn.execute(query, (involved, workspace_id))
    statuses: dict[UUID, ItemStatus] = {row[0]: row[1] for row in await cur.fetchall()}
    for each in (item_id, *(ref.item_id for ref in refs)):
        status = statuses.get(each)
        if status is None:
            raise NotFoundError("knowledge item")
        if status != "active":
            raise NotActiveError(each, status)
    await conn.execute(
        "INSERT INTO knowledge_supersession"
        " (workspace_id, owner_id, item_id, supersedes_id, relation)"
        " SELECT %s, %s, %s, s.supersedes_id, s.relation"
        " FROM unnest(%s::uuid[], %s::text[]) AS s (supersedes_id, relation)",
        (
            workspace_id,
            access.owner_id,
            item_id,
            [ref.item_id for ref in refs],
            [ref.relation for ref in refs],
        ),
    )


async def record_item(conn: Conn, access: WriteAccess, workspace: str | UUID, new: NewItem) -> Item:
    """Record an item by hand (extractor :data:`~memini.knowledge.MANUAL`) in ``workspace``, by
    name or id, with its evidence and participants, and link the items it supersedes.

    Every record ``new`` cites must be in the workspace: an item without evidence is not stored
    (principle 2). A decision taken in a conversation is first stored as a document, then cited.

    Raises:
        InvalidItemError: a field is refused, e.g. no evidence, an empty statement, or an anchor
            that does not fit its record; nothing was written.
        NotFoundError: no such workspace, record, person or superseded item that ``access`` may
            change.
        NotActiveError: an item to supersede is not active.
        SupersessionError: a link breaks a rule of supersession.
    """
    checked = check_new_item(new)
    try:
        async with conn.transaction():
            workspace_id = await _writable_workspace(conn, access, workspace)
            item_id = await _insert_item(
                conn, access, workspace_id, checked, extractor=MANUAL, extraction_id=None
            )
            return await load_item(conn, access, item_id)
    except (errors.CheckViolation, errors.ForeignKeyViolation) as error:
        refusal = _refusal(error)
        if refusal is None:
            raise
        raise refusal from None


async def record_extraction(
    conn: Conn,
    access: WriteAccess,
    *,
    record_id: str,
    content_hash: str,
    extractor: str,
    items: Sequence[NewItem],
) -> Extraction:
    """Store what ``extractor`` found in the content ``content_hash`` of the record
    ``record_id``: the extraction and its items, also when there are none.

    Idempotent per record content and extractor (requirements, section 5): when the same content
    was extracted by the same extractor before, nothing is written and the result names the items
    stored then (``created`` is ``False``). A new extractor version or new content is a new
    extraction; its items stand beside earlier ones until the supersession check links them.
    Items recorded by hand are never touched, and the database refuses an extracted item that
    supersedes or answers one.

    Each item cites the record it was extracted from, and only that record.

    Raises:
        InvalidItemError: a field is refused; nothing was written.
        NotFoundError: no such record, person or superseded item that ``access`` may change.
        NotActiveError: an item to supersede is not active.
        SupersessionError: a link breaks a rule of supersession.
    """
    check_record_id(record_id)
    check_content_hash(content_hash)
    check_extractor(extractor)
    checked = [check_new_item(item) for item in items]
    if any(ref.record_id != record_id for item in checked for ref in item.evidence):
        raise InvalidItemError("evidence", _EXTRACTED_EVIDENCE)
    try:
        async with conn.transaction():
            return await _record_extraction(
                conn, access, record_id, content_hash, extractor, checked
            )
    except (errors.CheckViolation, errors.ForeignKeyViolation) as error:
        refusal = _refusal(error)
        if refusal is None:
            raise
        raise refusal from None


async def _record_extraction(
    conn: Conn,
    access: WriteAccess,
    record_id: str,
    content_hash: str,
    extractor: str,
    items: Sequence[NewItem],
) -> Extraction:
    query = sql.SQL("SELECT r.workspace_id FROM record r WHERE r.id = %s AND {}").format(
        _scope(access, "r", source="source_id")
    )
    cur = await conn.execute(query, (record_id,))
    row = await cur.fetchone()
    if row is None:
        raise NotFoundError("record")
    workspace_id: UUID = row[0]
    key = (workspace_id, record_id, content_hash, extractor)
    cur = await conn.execute(
        "INSERT INTO knowledge_extraction"
        " (workspace_id, owner_id, record_id, content_hash, extractor)"
        " VALUES (%s, %s, %s, %s, %s)"
        " ON CONFLICT ON CONSTRAINT knowledge_extraction_key DO NOTHING"
        " RETURNING id, created_at",
        (workspace_id, access.owner_id, record_id, content_hash, extractor),
    )
    row = await cur.fetchone()
    if row is not None:
        extraction_id: UUID = row[0]
        item_ids = [
            await _insert_item(
                conn, access, workspace_id, item, extractor=extractor, extraction_id=extraction_id
            )
            for item in items
        ]
        return Extraction(
            id=extraction_id,
            workspace_id=workspace_id,
            record_id=record_id,
            content_hash=content_hash,
            extractor=extractor,
            created_at=row[1],
            item_ids=tuple(item_ids),
            created=True,
        )
    cur = await conn.execute(
        "SELECT x.id, x.created_at, coalesce(array_agg(i.id ORDER BY i.id)"
        "  FILTER (WHERE i.id IS NOT NULL), '{}')"
        " FROM knowledge_extraction x LEFT JOIN knowledge_item i ON i.extraction_id = x.id"
        " WHERE x.workspace_id = %s AND x.record_id = %s AND x.content_hash = %s"
        " AND x.extractor = %s"
        " GROUP BY x.id, x.created_at",
        key,
    )
    existing = await cur.fetchone()
    if existing is None:  # a concurrent extraction holds the key, not visible in this snapshot
        raise RuntimeError("the extraction exists but is not visible to this transaction")
    return Extraction(
        id=existing[0],
        workspace_id=workspace_id,
        record_id=record_id,
        content_hash=content_hash,
        extractor=extractor,
        created_at=existing[1],
        item_ids=tuple(existing[2]),
        created=False,
    )


async def supersede(
    conn: Conn, access: WriteAccess, item_id: UUID, supersedes: Sequence[SupersedesRef]
) -> Item:
    """Link the stored item ``item_id`` to earlier items it replaces or answers: they become
    superseded (``replaces``) or closed (``answers``), their ``invalidated_at`` the time of this
    transaction. The links are stored on ``item_id`` only.

    Both sides must be active and in one workspace. To replace an item with a new statement,
    record the new item with :attr:`~memini.knowledge.NewItem.supersedes` instead.

    Raises:
        InvalidItemError: no items, too many, or one named twice with two relations.
        NotFoundError: no such item that ``access`` may change, in the item's workspace.
        NotActiveError: ``item_id`` or an item it would supersede is not active.
        SupersessionError: a link breaks a rule of supersession.
    """
    if not supersedes:
        raise InvalidItemError("supersedes", "at least one item")
    if len(supersedes) > MAX_SUPERSEDES:
        raise InvalidItemError("supersedes", f"at most {MAX_SUPERSEDES} items")
    refs = check_supersedes(supersedes)
    try:
        async with conn.transaction():
            query = sql.SQL("SELECT i.workspace_id FROM knowledge_item i WHERE i.id = %s AND {}")
            cur = await conn.execute(query.format(_scope(access, "i")), (item_id,))
            row = await cur.fetchone()
            if row is None:
                raise NotFoundError("knowledge item")
            await _link(conn, access, row[0], item_id, refs)
            return await load_item(conn, access, item_id)
    except (errors.CheckViolation, errors.ForeignKeyViolation) as error:
        refusal = _refusal(error)
        if refusal is None:
            raise
        raise refusal from None


async def _end(conn: Conn, access: WriteAccess, item_id: UUID, status: ItemStatus) -> Item:
    async with conn.transaction():
        query = sql.SQL(
            "SELECT i.status FROM knowledge_item i WHERE i.id = %s AND {} FOR NO KEY UPDATE"
        ).format(_scope(access, "i"))
        cur = await conn.execute(query, (item_id,))
        row = await cur.fetchone()
        if row is None:
            raise NotFoundError("knowledge item")
        if row[0] != "active":
            raise NotActiveError(item_id, row[0])
        # The database sets invalidated_at to the time of this transaction.
        await conn.execute("UPDATE knowledge_item SET status = %s WHERE id = %s", (status, item_id))
        return await load_item(conn, access, item_id)


async def close_item(conn: Conn, access: WriteAccess, item_id: UUID) -> Item:
    """Close an active item without a replacement: an open question no longer asked, a decision
    no longer in force. It leaves the default view and stays in the history.

    Raises:
        NotFoundError: no such item that ``access`` may change.
        NotActiveError: the item is not active.
    """
    return await _end(conn, access, item_id, "closed")


async def retract_item(conn: Conn, access: WriteAccess, item_id: UUID) -> Item:
    """Retract an active item that was wrong: a misread extraction, a mistake. It leaves the
    default view and stays in the history, marked retracted.

    Raises:
        NotFoundError: no such item that ``access`` may change.
        NotActiveError: the item is not active.
    """
    return await _end(conn, access, item_id, "retracted")
