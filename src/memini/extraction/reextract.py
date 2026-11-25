"""Re-extraction: the ``extract`` jobs of stored records that the current extractor has not read
(requirements, section 5, Re-extraction).

Ingest enqueues the extraction of a thread or meeting when a batch writes its content new or
changed, and only while an LLM provider is configured (:mod:`memini.jobs.extraction`). That leaves
two kinds of record without a reading by the current extractor: those stored before a provider was
configured, and every record once the extractor changes, a new
:data:`~memini.extraction.llm.PROMPT_VERSION` or another model. :func:`enqueue_reextraction` finds
them and enqueues their jobs; ``POST /v1/workspaces/{workspace}/extract``
(:mod:`memini.api.extract`) and ``memini extract`` call it.

- **The records**: live threads and meetings of the workspace (not tombstones, with a payload), of
  the sources named (every source by default), that occurred at or after ``since``.
- **The current extractor** of a source is the one its jobs would record now: the provider the
  ``llm_policy`` of the workspace and of the source allow among those configured
  (:func:`memini.llm.decide`, the gate's rule), and the prompt version
  (:func:`~memini.extraction.llm.extractor_name`). A source no LLM may read (``policy_none``), or
  that no configured provider may serve (``no_provider``), is skipped: its records are counted and
  nothing is enqueued, as its jobs would only skip.
- **Once**: a record whose content the current extractor has read (a ``knowledge_extraction`` row
  of its content hash and that extractor) is ``current`` and skipped. Every other record gets the
  job ingest would give it, with the same dedupe key (:func:`memini.jobs.enqueue_extraction`): a
  job of the same content that is queued or running already is not doubled (``queued``). So a
  second run enqueues nothing while the jobs wait, and nothing once they have run.
- **Order**: oldest first, by ``occurred_at`` and id, in pages of :data:`PAGE_SIZE` records, each
  in a transaction of its own. A worker takes the jobs of one source in the order they were
  enqueued, so a later record is read after the earlier ones and its supersession check sees what
  they gave: a link goes from a newer item to an older one, never the other way.

Nothing here calls an LLM, and nothing changes a knowledge item: the jobs do the reading, and the
journal stays append-only. What happens to the items of an earlier extractor version is the
supersession check's to decide (:mod:`memini.extraction.job`): the record's own items are its first
candidates, and :func:`earlier_items` adds those that are no longer active, so that a re-reading
of the record does not bring back an item that was replaced, answered or retracted.
"""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from psycopg import AsyncConnection, pq, sql
from psycopg.rows import TupleRow

from memini.auth import WriteAccess
from memini.extraction.llm import extractor_name
from memini.extraction.model import EXTRACTED_KINDS, Candidate
from memini.jobs.extraction import EXTRACT_KIND, enqueue_extraction, extraction_dedupe_key
from memini.llm import LlmRefused, ProviderClass, ProviderInfo, decide
from memini.workspaces import NotFoundError, get_writable_workspace

__all__ = [
    "PAGE_SIZE",
    "EarlierItem",
    "Reextraction",
    "ReextractionCounts",
    "SourceReextraction",
    "earlier_items",
    "enqueue_reextraction",
]

type Conn = AsyncConnection[TupleRow]

PAGE_SIZE: Final = 500
"""Records read, and their jobs enqueued, in one transaction."""


@dataclass(slots=True)
class ReextractionCounts:
    """Records by what became of them. ``records`` is ``current + enqueued + queued + skipped``."""

    records: int = 0
    """Live threads and meetings of the sources, since the time asked for."""
    current: int = 0
    """Read already by the current extractor, in their current content: nothing to do."""
    enqueued: int = 0
    """Given a new job; in a dry run, those that would be."""
    queued: int = 0
    """A job of their current content was queued or running already: not doubled."""
    skipped: int = 0
    """Of a source no LLM may read, or that no configured provider may serve."""

    def add(self, other: "ReextractionCounts") -> None:
        self.records += other.records
        self.current += other.current
        self.enqueued += other.enqueued
        self.queued += other.queued
        self.skipped += other.skipped


@dataclass(slots=True)
class SourceReextraction:
    """What a re-extraction did with the records of one source."""

    source_id: UUID
    name: str
    extractor: str | None
    """The current extractor of the source; ``None`` when it is skipped."""
    skipped: str | None
    """Why the source is skipped: ``policy_none``, ``no_provider`` (a refusal reason of the
    gate); ``None`` when it is not."""
    counts: ReextractionCounts = field(default_factory=ReextractionCounts)


@dataclass(slots=True)
class Reextraction:
    """What :func:`enqueue_reextraction` did, by source."""

    workspace_id: UUID
    workspace: str
    dry_run: bool
    sources: list[SourceReextraction]
    """By name."""

    @property
    def totals(self) -> ReextractionCounts:
        totals = ReextractionCounts()
        for source in self.sources:
            totals.add(source.counts)
        return totals


# --- The sources ---------------------------------------------------------------------------------


async def _sources(
    conn: Conn, access: WriteAccess, workspace_id: UUID, names: Collection[str] | None
) -> tuple[str, list[tuple[UUID, str, str]]]:
    """The workspace's ``llm_policy``, and the id, name and ``llm_policy`` of each source named
    (every source of the workspace for ``None``), by name."""
    cur = await conn.execute("SELECT llm_policy FROM workspace WHERE id = %s", (workspace_id,))
    row = await cur.fetchone()
    if row is None:
        raise NotFoundError("workspace")
    policy: str = row[0]
    query = sql.SQL(
        "SELECT s.id, s.name, s.llm_policy FROM source s WHERE s.workspace_id = %s AND {} {}"
        " ORDER BY s.name"
    ).format(
        access.where(
            workspace_id=sql.Identifier("s", "workspace_id"),
            owner_id=sql.Identifier("s", "owner_id"),
            source_id=sql.Identifier("s", "id"),
        ),
        sql.SQL("") if names is None else sql.SQL("AND s.name = ANY(%s)"),
    )
    params: list[Any] = [workspace_id]
    if names is not None:
        params.append(sorted(set(names)))
    cur = await conn.execute(query, params)
    found = [(row[0], row[1], row[2]) for row in await cur.fetchall()]
    if names is not None and len(found) < len(set(names)):
        raise NotFoundError("source")
    return policy, found


def _current_extractor(
    workspace: tuple[str, str],
    source: tuple[str, str],
    providers: Mapping[ProviderClass, ProviderInfo],
) -> tuple[str | None, str | None]:
    """The extractor of the source's jobs now and ``None``, or ``None`` and why there is none."""
    try:
        _, provider_class = decide(workspace, [source], providers.keys())
    except LlmRefused as refusal:
        return None, refusal.reason
    return extractor_name(providers[provider_class].model), None


# --- The records ---------------------------------------------------------------------------------

_SCOPE: Final = sql.SQL(
    "r.workspace_id = {workspace} AND r.kind = ANY({kinds}::text[])"
    " AND r.deleted_at IS NULL AND r.payload IS NOT NULL"
    # A record marked sensitive is never sent to a model (memini.sensitivity).
    " AND NOT r.sensitive"
)

_COUNT: Final = sql.SQL("""
SELECT r.source_id, count(*) FROM record r
WHERE {scope} AND r.source_id = ANY({sources}::uuid[]) {since}
GROUP BY r.source_id
""")

_PAGE: Final = sql.SQL("""
SELECT r.id, r.source_id, r.content_hash, r.occurred_at,
       EXISTS (
           SELECT 1 FROM knowledge_extraction x
           WHERE x.workspace_id = r.workspace_id AND x.record_id = r.id
             AND x.content_hash = r.content_hash AND x.extractor = e.extractor
       )
FROM record r
JOIN unnest({sources}::uuid[], {extractors}::text[]) AS e (source_id, extractor)
  ON e.source_id = r.source_id
WHERE {scope} {since} {after}
ORDER BY r.occurred_at, r.id
LIMIT {limit}
""")

_QUEUED: Final = """
SELECT dedupe_key FROM job
WHERE workspace_id = %s AND kind = %s AND status IN ('queued', 'running')
  AND dedupe_key = ANY(%s::text[])
"""


@dataclass(frozen=True, slots=True)
class _Row:
    record_id: str
    source_id: UUID
    content_hash: str
    occurred_at: datetime
    current: bool


def _since(since: datetime | None, params: list[Any]) -> sql.Composable:
    if since is None:
        return sql.SQL("")
    params.append(since)
    return sql.SQL("AND r.occurred_at >= %s")


async def _skipped_counts(
    conn: Conn, workspace_id: UUID, source_ids: Sequence[UUID], since: datetime | None
) -> dict[UUID, int]:
    if not source_ids:
        return {}
    params: list[Any] = [workspace_id, list(EXTRACTED_KINDS), list(source_ids)]
    query = _COUNT.format(
        scope=_SCOPE.format(workspace=sql.Placeholder(), kinds=sql.Placeholder()),
        sources=sql.Placeholder(),
        since=_since(since, params),
    )
    cur = await conn.execute(query, params)
    return {row[0]: row[1] for row in await cur.fetchall()}


async def _page(
    conn: Conn,
    workspace_id: UUID,
    extractors: Mapping[UUID, str],
    since: datetime | None,
    after: tuple[datetime, str] | None,
    limit: int,
) -> list[_Row]:
    params: list[Any] = [
        sorted(extractors),
        [extractors[source_id] for source_id in sorted(extractors)],
        workspace_id,
        list(EXTRACTED_KINDS),
    ]
    since_clause = _since(since, params)
    after_clause = sql.SQL("")
    if after is not None:
        params.extend(after)
        after_clause = sql.SQL("AND (r.occurred_at, r.id) > (%s, %s)")
    query = _PAGE.format(
        sources=sql.Placeholder(),
        extractors=sql.Placeholder(),
        scope=_SCOPE.format(workspace=sql.Placeholder(), kinds=sql.Placeholder()),
        since=since_clause,
        after=after_clause,
        limit=sql.Literal(limit),
    )
    cur = await conn.execute(query, params)
    return [_Row(*row) for row in await cur.fetchall()]


async def _queued_keys(conn: Conn, workspace_id: UUID, keys: Sequence[str]) -> set[str]:
    if not keys:
        return set()
    cur = await conn.execute(_QUEUED, (workspace_id, EXTRACT_KIND, list(keys)))
    return {row[0] for row in await cur.fetchall()}


async def enqueue_reextraction(
    conn: Conn,
    access: WriteAccess,
    workspace: str,
    providers: Mapping[ProviderClass, ProviderInfo],
    *,
    sources: Collection[str] | None = None,
    since: datetime | None = None,
    dry_run: bool = False,
    page_size: int = PAGE_SIZE,
) -> Reextraction:
    """Enqueue an ``extract`` job for each live thread and meeting of ``workspace`` whose content
    the current extractor of its source has not read (see the module); with ``dry_run``, count
    them without enqueuing.

    ``providers`` are the LLM providers the service's settings configure
    (:func:`memini.llm.configured_providers`): the ones its extraction jobs are granted.
    ``sources`` names the sources to look at; ``None`` is every source of the workspace.
    ``conn`` must be idle: each page of records is enqueued in a transaction of its own, so a
    failure keeps the pages before it, and a second run completes the rest.

    Raises:
        NotFoundError: no such workspace, or source, that ``access`` may change.
    """
    if conn.info.transaction_status != pq.TransactionStatus.IDLE:
        raise ValueError("a re-extraction commits its own transactions: pass an idle connection")
    if page_size < 1:
        raise ValueError("page_size is at least 1")
    extractors: dict[UUID, str] = {}
    by_id: dict[UUID, SourceReextraction] = {}
    async with conn.transaction():
        found = await get_writable_workspace(conn, access, workspace)
        policy, rows = await _sources(conn, access, found.id, sources)
        result = Reextraction(found.id, found.name, dry_run, [])
        for source_id, name, source_policy in rows:
            extractor, skipped = _current_extractor(
                (f"workspace {found.name}", policy), (f"source {name}", source_policy), providers
            )
            entry = SourceReextraction(source_id, name, extractor, skipped)
            result.sources.append(entry)
            by_id[source_id] = entry
            if extractor is not None:
                extractors[source_id] = extractor
        unread = [entry.source_id for entry in result.sources if entry.extractor is None]
        for source_id, count in (await _skipped_counts(conn, found.id, unread, since)).items():
            by_id[source_id].counts.records += count
            by_id[source_id].counts.skipped += count
    if not extractors:
        return result

    after: tuple[datetime, str] | None = None
    while True:
        async with conn.transaction():
            page = await _page(conn, found.id, extractors, since, after, page_size)
            await _enqueue(conn, access, found.id, page, by_id, dry_run=dry_run)
        if len(page) < page_size:
            return result
        after = (page[-1].occurred_at, page[-1].record_id)


async def _enqueue(
    conn: Conn,
    access: WriteAccess,
    workspace_id: UUID,
    page: Sequence[_Row],
    by_id: Mapping[UUID, SourceReextraction],
    *,
    dry_run: bool,
) -> None:
    """Count one page, and enqueue the jobs of its records that need one, in its order."""
    needed = [row for row in page if not row.current]
    for row in page:
        counts = by_id[row.source_id].counts
        counts.records += 1
        if row.current:
            counts.current += 1
    if dry_run:
        keys = [extraction_dedupe_key(row.record_id, row.content_hash) for row in needed]
        queued = await _queued_keys(conn, workspace_id, keys)
        for row, key in zip(needed, keys, strict=True):
            counts = by_id[row.source_id].counts
            if key in queued:
                counts.queued += 1
            else:
                counts.enqueued += 1
        return
    for row in needed:
        enqueued = await enqueue_extraction(
            conn,
            workspace_id=workspace_id,
            owner_id=access.owner_id,
            source_id=row.source_id,
            record_id=row.record_id,
            content_hash=row.content_hash,
        )
        counts = by_id[row.source_id].counts
        if enqueued.created:
            counts.enqueued += 1
        else:
            counts.queued += 1


# --- What a re-reading may not bring back --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EarlierItem:
    """An item with evidence in a record that is no longer active: superseded, closed or
    retracted. A new reading of the record that says the same is a duplicate of it."""

    candidate: Candidate
    """``id`` is the item's id as text."""
    source_ids: frozenset[UUID]
    """The sources of its evidence: their content is in the supersession request."""


_EARLIER: Final = """
SELECT i.id, i.kind, i.statement, i.decided_at,
       array(SELECT DISTINCT e.source_id FROM knowledge_evidence e WHERE e.item_id = i.id)
FROM knowledge_item i
WHERE i.workspace_id = %(workspace)s AND i.status <> 'active'
  AND i.id IN (SELECT e.item_id FROM knowledge_evidence e WHERE e.record_id = %(record)s)
  AND NOT EXISTS (
      SELECT 1 FROM knowledge_evidence e LEFT JOIN source s ON s.id = e.source_id
      WHERE e.item_id = i.id AND (s.id IS NULL OR s.llm_policy <> ALL(%(policies)s::text[]))
  )
ORDER BY i.recorded_at DESC, i.id DESC
LIMIT %(limit)s
"""


async def earlier_items(
    conn: AsyncConnection[Any],
    *,
    workspace_id: UUID,
    record_id: str,
    policies: Collection[str],
    limit: int,
) -> list[EarlierItem]:
    """The ``limit`` latest items with evidence in ``record_id`` that are no longer active, of
    any extractor or by hand, whose every evidence source has a policy in ``policies`` (what the
    granted provider may read).

    The supersession check of a new reading of the record shows them beside the active
    candidates: a new item that says the same as one of them is a duplicate and is not stored,
    so a decision that was replaced, a question that was answered or an item a person retracted
    does not come back as an active item. They are never linked: only an active item is
    superseded.
    """
    cur = await conn.execute(
        _EARLIER,
        {
            "workspace": workspace_id,
            "record": record_id,
            "policies": sorted(policies),
            "limit": limit,
        },
    )
    return [
        EarlierItem(
            candidate=Candidate(id=str(item_id), kind=kind, statement=text, decided_at=decided),
            source_ids=frozenset(sources),
        )
        for item_id, kind, text, decided, sources in await cur.fetchall()
    ]
