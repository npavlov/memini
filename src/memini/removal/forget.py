"""Forget: the hard delete of one record by hand, with everything derived from it. See
:mod:`memini.removal`."""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from typing import Final, get_args
from uuid import UUID

from psycopg import AsyncConnection, sql
from psycopg.rows import TupleRow

from memini.auth import WriteAccess
from memini.ids import RecordKind
from memini.jobs import source_lock_key
from memini.removal.core import RemovalCounts, delete_records
from memini.sources import SourceBusyError
from memini.workspaces import NotFoundError, get_writable_workspace

__all__ = [
    "AmbiguousRecordError",
    "ForgetTarget",
    "Forgotten",
    "forget_record",
    "forgotten_record_ids",
]

type Conn = AsyncConnection[TupleRow]

_RECORD_ID: Final = frozenset("0123456789abcdef")


class AmbiguousRecordError(ValueError):
    """The source has records of more than one kind under the external id, and none was named."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ForgetTarget:
    """The record to forget: by its id, or by its source's name and external id, with its kind
    when the source has records of two kinds under that id (as evidence names a record)."""

    record_id: str | None = None
    source: str | None = None
    external_id: str | None = None
    kind: RecordKind | None = None

    def __post_init__(self) -> None:
        by_reference = self.source is not None or self.external_id is not None
        if self.record_id is not None:
            if by_reference or self.kind is not None:
                raise ValueError("name a record by record_id or by source and external_id")
            if len(self.record_id) != 64 or not set(self.record_id) <= _RECORD_ID:
                raise ValueError("a record id is 64 lowercase hex characters")
        elif not (self.source and self.external_id):
            raise ValueError("name a record by record_id, or by source and external_id")
        if self.kind is not None and self.kind not in get_args(RecordKind):
            raise ValueError("a record kind is code_file, thread, meeting or document")


@dataclass(frozen=True, slots=True)
class Forgotten:
    """A record forgotten: its audit entry and what was deleted with it."""

    id: UUID
    """The audit entry (``forgotten_record``)."""
    workspace_id: UUID
    workspace: str
    record_id: str
    source_id: UUID
    source: str
    record_kind: RecordKind
    forgotten_at: datetime
    removed: RemovalCounts
    citing_items: int
    """Knowledge items whose evidence cites the record. They stay, with the reference; their
    evidence reads as ``forgotten``."""


_BY_ID: Final = sql.SQL("""
SELECT r.id, r.source_id, s.name, r.kind
FROM record r
JOIN source s ON s.id = r.source_id AND s.workspace_id = r.workspace_id
WHERE r.workspace_id = %s AND r.id = %s AND {access}
""")

_BY_REFERENCE: Final = sql.SQL("""
SELECT r.id, r.source_id, s.name, r.kind
FROM source s
JOIN record r ON r.workspace_id = s.workspace_id AND r.source_id = s.id
WHERE s.workspace_id = %s AND s.name = %s AND r.external_id = %s
  AND (%s::text IS NULL OR r.kind = %s::text) AND {access}
ORDER BY r.kind
""")

_CITING: Final = (
    "SELECT count(DISTINCT item_id) FROM knowledge_evidence WHERE workspace_id = %s"
    " AND record_id = %s"
)

_AUDIT: Final = (
    "INSERT INTO forgotten_record (workspace_id, owner_id, token_id, record_id, source_id,"
    " record_kind) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id, forgotten_at"
)


async def forget_record(
    conn: Conn,
    access: WriteAccess,
    workspace: str,
    target: ForgetTarget,
    *,
    token_id: UUID | None = None,
) -> Forgotten:
    """Forget one record of the workspace ``workspace``: delete it, its chunks, symbols, person
    links and raw responses (:func:`~memini.removal.delete_records`), and write the audit entry,
    in one transaction (a savepoint when the caller has one open). A live record and a tombstone
    alike. Knowledge items that cite it stay and keep their reference; it does not come back on a
    later sync of its source (:func:`forgotten_record_ids`).

    Takes the advisory lock of the record's source for the transaction, as deleting a source does
    (:func:`memini.jobs.source_lock_key`): no sync of the source runs between its plan and its
    writes while the record goes.

    Args:
        token_id: the token of the request, for the audit; ``None`` for an operator without one.

    Raises:
        NotFoundError: no such workspace that ``access`` may change, or no such record in it.
        AmbiguousRecordError: by source and external id, records of two kinds and no kind given.
        SourceBusyError: a job of the record's source runs; nothing was deleted. Try again.
    """
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
        record_id: str = rows[0][0]
        source_id: UUID = rows[0][1]
        source: str = rows[0][2]
        kind: RecordKind = rows[0][3]

        cur = await conn.execute(
            "SELECT pg_try_advisory_xact_lock(%s, %s)", source_lock_key(source_id)
        )
        locked = await cur.fetchone()
        if locked is None or not locked[0]:
            raise SourceBusyError(f"a job of source {source} is running")

        cur = await conn.execute(_CITING, (found.id, record_id))
        citing = await cur.fetchone()
        removed = await delete_records(
            conn, workspace_id=found.id, source_id=source_id, record_ids=[record_id]
        )
        if removed.records != 1:  # deleted by another transaction since it was found
            raise NotFoundError("record")
        cur = await conn.execute(
            _AUDIT, (found.id, access.owner_id, token_id, record_id, source_id, kind)
        )
        audit = await cur.fetchone()
        if audit is None:  # INSERT ... RETURNING returns the row
            raise RuntimeError("the audit entry of forget was not written")
    return Forgotten(
        id=audit[0],
        workspace_id=found.id,
        workspace=found.name,
        record_id=record_id,
        source_id=source_id,
        source=source,
        record_kind=kind,
        forgotten_at=audit[1],
        removed=removed,
        citing_items=0 if citing is None else int(citing[0]),
    )


async def forgotten_record_ids(conn: Conn, record_ids: Collection[str]) -> set[str]:
    """The ids among ``record_ids`` of records that were forgotten: a writer of records leaves
    them out, so that a forgotten record does not come back.

    A writer that may race with :func:`forget_record` calls it after it has locked the rows of
    the records it writes (``FOR NO KEY UPDATE``, or the source's advisory lock): forget deletes
    the row and writes its entry in one transaction, so a writer that waited for the row sees the
    entry in its next statement.
    """
    if not record_ids:
        return set()
    cur = await conn.execute(
        "SELECT record_id FROM forgotten_record WHERE record_id = ANY(%s::text[])",
        (sorted(set(record_ids)),),
    )
    return {row[0] for row in await cur.fetchall()}
