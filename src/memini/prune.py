"""Prune: apply a source's current filters to the records it has stored.

Ingest applies a source's filters (:mod:`memini.filters`) to every batch it is sent from the moment
they are set; records stored before a rule was added stay as they were until a prune. A prune reads
every record of the source that has a payload, live or a tombstone, and applies the current rules
to it, as ingest would to the same payload, in its stored form (:mod:`memini.ingest.filtering`):

- a record the rules exclude whole (a channel or a title, or a thread none of whose messages is
  kept) is **deleted** with its chunks and symbols: the row is gone, not turned into a tombstone.
  The rules keep it out at ingest, so a resend or a sync of an older window does not bring it
  back; a tombstone would only keep the excluded payload in the store. Knowledge items that cite
  it keep their reference and show it as gone (requirements, section 3); should a rule be lifted,
  the next batch that holds the record stores it again under the same id.
- a thread some of whose messages are left out is **trimmed**: its payload is written again
  without them, with the content hash and normalized text of that form, and its chunks are
  derived again by the indexing hook in the same transaction, as ingest would; the writer embeds
  only the texts it has no vector for. Without a hook (no embedding provider), the record's
  chunks are deleted, since they quote the messages left out, and the record is counted as not
  indexed: ``memini reindex`` indexes it. A tombstone has no chunks; only its payload changes.
- every other record is left as it is; nothing is written.

So a prune run again over the same rules changes nothing. Records are read in pages by id, each
page in a transaction of its own that locks its records in id order, as an ingest batch does, so
a prune and a batch take turns on a record and never deadlock. A page that fails rolls back alone;
the pages before it stay pruned, and the run report counts them. Each prune writes a run of kind
``prune`` with ``records_scanned``, ``records_pruned`` (deleted), ``records_trimmed``,
``messages_pruned``, ``records_unchanged``, ``records_unreadable`` (a payload the parser of its
kind no longer reads: left alone), ``records_not_indexed`` and the index counts.

A git source is refused (:class:`NotPrunableError`): its include and exclude rules select files,
and its next sync applies a change of them.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from psycopg import AsyncConnection, errors, pq, sql
from psycopg.rows import TupleRow
from psycopg_pool import AsyncConnectionPool

from memini.auth import WriteAccess
from memini.filters import SourceFilters
from memini.index import IndexCounts, delete_record_chunks
from memini.index.writer import RUN_COUNTS
from memini.ingest.filtering import stored_form
from memini.ingest.model import INGESTIBLE_SOURCE_KINDS, IngestKind, RecordIndexer, StoredRecord
from memini.payloads import PAYLOAD_KINDS
from memini.runs import Run, current_run, record_run
from memini.workspaces import NotFoundError, get_writable_workspace

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "RUN_KIND",
    "NotPrunableError",
    "PruneConflictError",
    "PruneResult",
    "prune_source",
]

type Conn = AsyncConnection[TupleRow]

RUN_KIND: Final = "prune"
"""The kind of the run row each prune writes."""
DEFAULT_PAGE_SIZE: Final = 100
"""Records read, locked and pruned in one transaction."""


class NotPrunableError(Exception):
    """The source's kind has no filters to prune by: a git source selects files by its path
    rules, and its next sync applies them."""

    def __init__(self, kind: str) -> None:
        super().__init__(f"{kind} sources have no filters; their next sync applies their rules")
        self.kind = kind


class PruneConflictError(Exception):
    """Another writer held the same records in another order, and a page was rolled back. The
    pages before it stay pruned; running the prune again is safe."""


@dataclass(slots=True)
class PruneResult:
    """What a prune did. Each record scanned is counted once: in ``deleted``, ``trimmed``,
    ``unchanged`` or ``unreadable``."""

    workspace_id: UUID
    source_id: UUID
    run_id: UUID | None = None
    rules: bool = False
    """Whether the source has any filter rule; without one a prune reads nothing."""
    scanned: int = 0
    """Records with a payload read, live ones and tombstones."""
    deleted: int = 0
    """Records the rules exclude: deleted with their chunks."""
    trimmed: int = 0
    """Threads written again without the messages the rules leave out."""
    messages_removed: int = 0
    """Messages taken out of the threads, trimmed or deleted."""
    unchanged: int = 0
    unreadable: int = 0
    """Payloads the parser of their kind does not read: left as they are."""
    not_indexed: int = 0
    """Trimmed live records whose chunks were deleted rather than derived again (no indexing
    hook): ``memini reindex`` indexes them."""
    index: IndexCounts = field(default_factory=IndexCounts)

    def add(self, page: "PruneResult") -> None:
        self.scanned += page.scanned
        self.deleted += page.deleted
        self.trimmed += page.trimmed
        self.messages_removed += page.messages_removed
        self.unchanged += page.unchanged
        self.unreadable += page.unreadable
        self.not_indexed += page.not_indexed
        self.index += page.index


async def _find_source(
    conn: Conn, access: WriteAccess, workspace: str, name: str
) -> tuple[UUID, UUID, str, SourceFilters]:
    """The workspace id, source id, kind and filters of a source ``access`` may change."""
    async with conn.transaction():
        found = await get_writable_workspace(conn, access, workspace)
        query = sql.SQL(
            "SELECT s.id, s.kind, s.config FROM source s"
            " WHERE s.workspace_id = %s AND s.name = %s AND {}"
        ).format(
            access.where(
                workspace_id=sql.Identifier("s", "workspace_id"),
                owner_id=sql.Identifier("s", "owner_id"),
                source_id=sql.Identifier("s", "id"),
            )
        )
        cur = await conn.execute(query, (found.id, name))
        row = await cur.fetchone()
    if row is None:
        raise NotFoundError("source")
    source_id: UUID = row[0]
    kind: str = row[1]
    config: dict[str, Any] = row[2]
    if kind not in INGESTIBLE_SOURCE_KINDS:
        raise NotPrunableError(kind)
    return found.id, source_id, kind, SourceFilters.from_json(config.get("filters"))


# The next page of the source's records with a payload, by id, locked in that order as an ingest
# batch locks its records.
_PAGE: Final = """
SELECT id, kind, external_id, occurred_at, payload, deleted_at IS NOT NULL
FROM record
WHERE source_id = %s AND kind = ANY(%s::text[]) AND payload IS NOT NULL AND id > %s
ORDER BY id
LIMIT %s
FOR NO KEY UPDATE
"""

_REWRITE: Final = (
    "UPDATE record SET payload = %s, content_hash = %s, normalized_text = %s, updated_at = now()"
    " WHERE id = %s"
)


async def _delete(conn: Conn, record_id: str) -> IndexCounts:
    """Delete a record with its chunks and symbols, in the caller's transaction.

    A local deletion: the chunks first, for their counts, then the row; whatever else refers to
    the record goes with it by its foreign keys. Switch to the shared deletion core of forget and
    retention once it exists, so that every deletion of a record goes one way.
    """
    counts = await delete_record_chunks(conn, record_id=record_id)
    await conn.execute("DELETE FROM record WHERE id = %s", (record_id,))
    return counts


async def _page(
    conn: Conn,
    filters: SourceFilters,
    source_id: UUID,
    after: str,
    page_size: int,
    indexer: RecordIndexer | None,
    tally: PruneResult,
) -> str | None:
    """Prune the next page of records after the id ``after``, in one transaction; return the
    last id read, or ``None`` when there was none. The page's counts go into ``tally``."""
    async with conn.transaction():
        cur = await conn.execute(_PAGE, (source_id, list(PAYLOAD_KINDS), after, page_size))
        rows = await cur.fetchall()
        if not rows:
            return None
        to_index: list[StoredRecord] = []
        for record_id, kind, external_id, occurred_at, payload, deleted in rows:
            tally.scanned += 1
            try:
                verdict = filters.apply(kind, json.loads(payload))
            except ValueError:  # PayloadError included: not what the parser reads (any more)
                tally.unreadable += 1
                continue
            tally.messages_removed += verdict.messages_excluded
            if not verdict.keep:
                tally.index += await _delete(conn, record_id)
                tally.deleted += 1
            elif verdict.payload is None:
                tally.unchanged += 1
            else:
                data, digest, text = stored_form(kind, verdict.payload)
                await conn.execute(_REWRITE, (data, digest, text, record_id))
                tally.trimmed += 1
                if deleted:
                    continue  # a tombstone has no chunks
                if indexer is None:
                    tally.index += await delete_record_chunks(conn, record_id=record_id)
                    tally.not_indexed += 1
                else:
                    to_index.append(
                        _stored(record_id, kind, external_id, occurred_at, data, digest)
                    )
        if indexer is not None and to_index:
            counts = await indexer(conn, to_index)
            if counts is not None:
                tally.index += counts
        last: str = rows[-1][0]
        return last


def _stored(
    record_id: str,
    kind: IngestKind,
    external_id: str,
    occurred_at: datetime,
    data: bytes,
    digest: str,
) -> StoredRecord:
    return StoredRecord(
        id=record_id,
        kind=kind,
        external_id=external_id,
        occurred_at=occurred_at,
        content_hash=digest,
        payload=data,
        content_changed=True,
    )


def _count(run: Run | None, page: PruneResult) -> None:
    if run is None:
        return
    for name, value in (
        ("records_scanned", page.scanned),
        ("records_pruned", page.deleted),
        ("records_trimmed", page.trimmed),
        ("messages_pruned", page.messages_removed),
        ("records_unchanged", page.unchanged),
        ("records_unreadable", page.unreadable),
        ("records_not_indexed", page.not_indexed),
    ):
        run.count(name, value)
    for attribute, name in RUN_COUNTS.items():
        run.count(name, getattr(page.index, attribute))


async def _prune(
    conn: Conn,
    filters: SourceFilters,
    result: PruneResult,
    *,
    indexer: RecordIndexer | None,
    page_size: int,
    run: Run | None,
) -> None:
    after = ""
    while True:
        page = PruneResult(result.workspace_id, result.source_id)
        try:
            last = await _page(conn, filters, result.source_id, after, page_size, indexer, page)
        except errors.DeadlockDetected:
            raise PruneConflictError(
                "another writer held the same records at the same time; run the prune again"
            ) from None
        if last is None:
            return
        # Counted once the page has committed, so a page rolled back reports nothing.
        result.add(page)
        _count(run, page)
        after = last


async def prune_source(
    conn: Conn,
    access: WriteAccess,
    workspace: str,
    source: str,
    *,
    runs: AsyncConnectionPool[Any] | AsyncConnection[Any] | None = None,
    indexer: RecordIndexer | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> PruneResult:
    """Apply the current filters of the push or mcp source ``source`` of the workspace
    ``workspace`` to the records it has stored; see the module. ``conn`` is idle: each page
    commits its own transaction.

    Args:
        runs: where the prune's run row is written (kind ``prune``): the pool, or a connection
            used for nothing else. ``None``: no run row.
        indexer: the indexing hook that derives the chunks of a trimmed thread again, the
            service's being :class:`~memini.ingest.PayloadIndexer`; ``None``: their chunks are
            deleted, and ``memini reindex`` indexes them.

    Raises:
        ValueError: ``conn`` has a transaction open, or ``page_size`` is not positive.
        NotFoundError: no such workspace or source that ``access`` may change.
        NotPrunableError: the source is a git source.
        PruneConflictError: another writer got in the way; the pages before stay pruned, and
            running the prune again is safe.
        Exception: whatever ``indexer`` raises, the page it was called for rolled back: the
            service's hook raises :class:`~memini.index.IndexMismatchError` for a workspace whose
            index must be rebuilt first, and :class:`~memini.embeddings.EmbeddingError` when the
            model fails.
    """
    if conn.info.transaction_status != pq.TransactionStatus.IDLE:
        raise ValueError("a prune commits its own transactions: pass an idle connection")
    if page_size < 1:
        raise ValueError("page_size is at least 1")
    workspace_id, source_id, _, filters = await _find_source(conn, access, workspace, source)
    result = PruneResult(workspace_id, source_id, rules=not filters.empty)

    async def prune(run: Run | None) -> None:
        if filters.empty:
            return  # nothing to exclude: no record needs reading
        await _prune(conn, filters, result, indexer=indexer, page_size=page_size, run=run)

    if runs is None:
        await prune(current_run())
        return result
    async with record_run(
        runs,
        workspace_id=workspace_id,
        owner_id=access.owner_id,
        kind=RUN_KIND,
        source_id=source_id,
        progress_interval=None,
    ) as run:
        result.run_id = run.id
        async with run.step("prune"):
            await prune(run)
    return result
