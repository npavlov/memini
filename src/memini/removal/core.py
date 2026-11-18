"""Deleting raw records with everything derived from them: what forget and retention share."""

from collections.abc import Sequence
from dataclasses import dataclass, fields
from typing import Final
from uuid import UUID

from psycopg import AsyncConnection, pq
from psycopg.rows import TupleRow

__all__ = ["RemovalCounts", "delete_raw_responses", "delete_records"]

type Conn = AsyncConnection[TupleRow]


@dataclass(frozen=True, slots=True)
class RemovalCounts:
    """What one deletion removed. Add two with ``+``."""

    records: int = 0
    chunks: int = 0
    symbols: int = 0
    raw_responses: int = 0
    """Stored raw responses the records were parsed from; 0 until the raw response store
    exists (:func:`delete_raw_responses`)."""

    def __add__(self, other: "RemovalCounts") -> "RemovalCounts":
        return RemovalCounts(
            **{f.name: getattr(self, f.name) + getattr(other, f.name) for f in fields(self)}
        )


_DELETE_SYMBOLS: Final = (
    "DELETE FROM symbol WHERE workspace_id = %s AND source_id = %s AND record_id = ANY(%s::text[])"
)
_DELETE_CHUNKS: Final = (
    "DELETE FROM chunk WHERE workspace_id = %s AND source_id = %s AND record_id = ANY(%s::text[])"
)
_DELETE_RECORDS: Final = (
    "DELETE FROM record WHERE workspace_id = %s AND source_id = %s AND id = ANY(%s::text[])"
)


async def delete_raw_responses(
    conn: Conn, *, workspace_id: UUID, source_id: UUID, record_ids: Sequence[str]
) -> int:
    """Remove the stored raw responses that hold content of the records; return how many lost
    their body (:func:`memini.responses.delete_raw_responses_for_records`), in the transaction
    that deletes the records. The forget test (tests/test_forget_db.py) scans every text column of
    every table, the raw response store's included.
    """
    del workspace_id  # the store is keyed by source
    # Imported here: memini.responses imports memini.ingest, which imports this package.
    from memini.responses import delete_raw_responses_for_records

    deletion = await delete_raw_responses_for_records(
        conn, source_id=source_id, record_ids=record_ids
    )
    return deletion.pruned + deletion.responses


async def delete_records(
    conn: Conn,
    *,
    workspace_id: UUID,
    source_id: UUID,
    record_ids: Sequence[str],
    raw_responses: bool = True,
) -> RemovalCounts:
    """Delete the records ``record_ids`` of one source with everything that holds their text:
    their symbols and chunks (counted), their stored raw responses (:func:`delete_raw_responses`)
    and, by the foreign keys' cascade, their links to persons. Knowledge evidence that cites them
    stays (see :mod:`memini.removal`).

    Runs in the caller's transaction, which must be open, so that a record and what derives from
    it go together. Ids that name no record of the source are passed over. ``raw_responses=False``
    leaves the raw response store alone: retention removes it by age instead
    (:func:`memini.responses.delete_raw_responses_before`), since removing a record's responses
    also drops the source's windows that were never parsed, which a running sync may be filling.
    """
    if conn.info.transaction_status == pq.TransactionStatus.IDLE:
        raise ValueError("delete_records runs in the caller's transaction: open one first")
    if not record_ids:
        return RemovalCounts()
    ids = sorted(set(record_ids))
    params = (workspace_id, source_id, ids)
    symbols = await conn.execute(_DELETE_SYMBOLS, params)
    chunks = await conn.execute(_DELETE_CHUNKS, params)
    raw = (
        await delete_raw_responses(
            conn, workspace_id=workspace_id, source_id=source_id, record_ids=ids
        )
        if raw_responses
        else 0
    )
    records = await conn.execute(_DELETE_RECORDS, params)
    return RemovalCounts(
        records=records.rowcount,
        chunks=chunks.rowcount,
        symbols=symbols.rowcount,
        raw_responses=raw,
    )
