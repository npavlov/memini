"""Sensitive records: marked by hand or at push, kept and indexed, and left out of answers unless
asked for (requirements, section 8, Sensitive records).

A record marked sensitive (a salary, a diagnosis, a date of birth) stays where it is: its payload,
its chunks and its vectors are not touched, so marking and unmarking need no reindex. What changes
is what a reader gets. The read filter (:meth:`memini.auth.ReadFilter.where`) leaves out every
record marked sensitive, and what is derived from it, unless the request asks for it with
``include_sensitive`` and a token with the ``sensitive`` scope. A knowledge item whose every piece
of evidence is a sensitive record is left out too (:func:`hidden_items`). Model steps never read a
sensitive record (:data:`NOT_SENSITIVE`).

Marking is an admin write: :func:`mark_record` sets or clears the mark of one record and its reason,
:func:`list_sensitive` lists the marked records of a workspace for an admin, with their reasons and
no content. A push can mark a record as it stores it (``"sensitive": true`` on the record); a push
never clears a mark.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from psycopg import AsyncConnection, sql
from psycopg.rows import TupleRow

from memini.auth import ReadFilter, WriteAccess
from memini.ids import RecordKind
from memini.redaction import redact_text
from memini.removal import AmbiguousRecordError, ForgetTarget
from memini.workspaces import NotFoundError, get_writable_workspace

__all__ = [
    "MAX_REASON_CHARS",
    "NOT_SENSITIVE",
    "Marked",
    "MarkedRecord",
    "hidden_items",
    "list_sensitive",
    "mark_record",
    "sensitive_message",
]

type Conn = AsyncConnection[TupleRow]

MAX_REASON_CHARS: Final = 200
"""The longest reason a mark keeps (``record_sensitive_reason_check``)."""

NOT_SENSITIVE: Final = sql.SQL("NOT r.sensitive")
"""The condition on ``record r`` of a step that sends records to a model: never a sensitive one."""


def sensitive_message(kind: str) -> str:
    """What a reader is told when the record it named is marked sensitive."""
    return (
        f"The {kind} is marked sensitive and is left out of answers. Ask with include_sensitive"
        " and a token with the sensitive scope to read it."
    )


def hidden_items(access: ReadFilter | WriteAccess, alias: str = "i") -> sql.Composable:
    """The condition on ``knowledge_item {alias}``: true for an item the reader may see. An item
    whose every piece of evidence is a stored record marked sensitive is hidden, unless the access
    includes sensitive records; an item with no evidence, or with evidence of a record that is
    not sensitive or is gone, is shown. Writes see every item."""
    if not isinstance(access, ReadFilter) or access.include_sensitive:
        return sql.SQL("TRUE")
    return sql.SQL(
        "NOT (EXISTS (SELECT 1 FROM knowledge_evidence he WHERE he.item_id = {item}.id)"
        " AND NOT EXISTS (SELECT 1 FROM knowledge_evidence he"
        " LEFT JOIN record hr ON hr.id = he.record_id AND hr.workspace_id = he.workspace_id"
        " WHERE he.item_id = {item}.id AND (hr.id IS NULL OR NOT hr.sensitive)))"
    ).format(item=sql.Identifier(alias))


@dataclass(frozen=True, slots=True)
class Marked:
    """A record whose mark was set or cleared."""

    workspace: str
    record_id: str
    source: str
    record_kind: RecordKind
    external_id: str
    sensitive: bool
    reason: str | None
    sensitive_at: datetime | None
    changed: bool
    """Whether the mark or its reason changed; the same mark again changes nothing."""


@dataclass(frozen=True, slots=True)
class MarkedRecord:
    """A record marked sensitive, as an admin lists it: what names it and why, no content."""

    record_id: str
    source: str
    record_kind: RecordKind
    external_id: str
    occurred_at: datetime
    reason: str | None
    sensitive_at: datetime


_BY_ID: Final = sql.SQL("""
SELECT r.id, s.name, r.kind, r.external_id, r.sensitive, r.sensitive_reason, r.sensitive_at
FROM record r
JOIN source s ON s.id = r.source_id AND s.workspace_id = r.workspace_id
WHERE r.workspace_id = %s AND r.id = %s AND {access}
FOR NO KEY UPDATE OF r
""")

_BY_REFERENCE: Final = sql.SQL("""
SELECT r.id, s.name, r.kind, r.external_id, r.sensitive, r.sensitive_reason, r.sensitive_at
FROM source s
JOIN record r ON r.workspace_id = s.workspace_id AND r.source_id = s.id
WHERE s.workspace_id = %s AND s.name = %s AND r.external_id = %s
  AND (%s::text IS NULL OR r.kind = %s::text) AND {access}
ORDER BY r.kind
FOR NO KEY UPDATE OF r
""")

_SET: Final = (
    "UPDATE record SET sensitive = %s, sensitive_reason = %s,"
    " sensitive_at = CASE WHEN %s THEN now() END WHERE id = %s RETURNING sensitive_at"
)


def clean_reason(reason: str | None) -> str | None:
    """The reason as a mark keeps it: trimmed, redacted, at most :data:`MAX_REASON_CHARS`
    characters; ``None`` for none."""
    if reason is None or not reason.strip():
        return None
    return redact_text(reason.strip()).value[:MAX_REASON_CHARS]


async def mark_record(
    conn: Conn,
    access: WriteAccess,
    workspace: str,
    target: ForgetTarget,
    *,
    sensitive: bool,
    reason: str | None = None,
) -> Marked:
    """Mark the record ``target`` of ``workspace`` sensitive, or clear its mark (``sensitive``
    false, which also drops the reason). Its content, chunks and vectors stay as they are.

    Raises:
        NotFoundError: no such workspace that ``access`` may change, or no such record in it.
        AmbiguousRecordError: by source and external id, records of two kinds and no kind given.
    """
    kept = clean_reason(reason) if sensitive else None
    async with conn.transaction():
        found = await get_writable_workspace(conn, access, workspace)
        condition = access.where(
            workspace_id=sql.Identifier("r", "workspace_id"),
            owner_id=sql.Identifier("r", "owner_id"),
            source_id=sql.Identifier("r", "source_id"),
        )
        if target.record_id is not None:
            cur = await conn.execute(_BY_ID.format(access=condition), (found.id, target.record_id))
        else:
            cur = await conn.execute(
                _BY_REFERENCE.format(access=condition),
                (found.id, target.source, target.external_id, target.kind, target.kind),
            )
        rows = await cur.fetchall()
        if not rows:
            raise NotFoundError("record")
        if len(rows) > 1:
            raise AmbiguousRecordError(
                "the source has records of more than one kind under that external id; name its kind"
            )
        record_id, source, kind, external_id, was, was_reason, was_at = rows[0]
        if was == sensitive and was_reason == kept:
            return Marked(
                found.name, record_id, source, kind, external_id, was, was_reason, was_at, False
            )
        cur = await conn.execute(_SET, (sensitive, kept, sensitive, record_id))
        row = await cur.fetchone()
        at: datetime | None = None if row is None else row[0]
    return Marked(found.name, record_id, source, kind, external_id, sensitive, kept, at, True)


_LIST: Final = sql.SQL("""
SELECT r.id, s.name, r.kind, r.external_id, r.occurred_at, r.sensitive_reason, r.sensitive_at
FROM record r
JOIN source s ON s.id = r.source_id AND s.workspace_id = r.workspace_id
WHERE r.workspace_id = %s AND r.sensitive AND {access}
ORDER BY r.sensitive_at DESC, r.id
LIMIT %s
""")


async def list_sensitive(
    conn: Conn, access: WriteAccess, workspace: str, *, limit: int = 200
) -> tuple[UUID, list[MarkedRecord]]:
    """The records of ``workspace`` marked sensitive, last marked first: for an admin, who may
    change the marks. Names and reasons only, never content.

    Raises:
        NotFoundError: no such workspace that ``access`` may change.
    """
    found = await get_writable_workspace(conn, access, workspace)
    condition = access.where(
        workspace_id=sql.Identifier("r", "workspace_id"),
        owner_id=sql.Identifier("r", "owner_id"),
        source_id=sql.Identifier("r", "source_id"),
    )
    cur = await conn.execute(_LIST.format(access=condition), (found.id, limit))
    return found.id, [
        MarkedRecord(
            record_id=row[0],
            source=row[1],
            record_kind=row[2],
            external_id=row[3],
            occurred_at=row[4],
            reason=row[5],
            sensitive_at=row[6],
        )
        for row in await cur.fetchall()
    ]
