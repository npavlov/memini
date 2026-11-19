"""The ``extract`` job: the decisions and open questions of one thread or meeting, extracted,
checked against similar active items, and stored (requirements, section 5).

Ingest enqueues one job per thread or meeting whose content a batch writes new or changed
(:mod:`memini.jobs.extraction`), when an LLM provider is configured; ``serve`` registers the handler
(:func:`extract_handler`) then. :func:`run_extraction` does, for the record as it is when the job
runs:

1. **Skip** a record that is gone or a tombstone (counted ``records_gone``), and one no LLM may
   read: the gate (:meth:`memini.llm.LlmGate.check`) refuses with ``policy_none`` when the
   workspace or the record's source has ``llm_policy`` ``none``, or ``no_provider`` when no
   provider that policy allows is configured. A skip is counted
   (``records_skipped_policy_none``, ``records_skipped_no_provider``) and the job succeeds;
   nothing is sent anywhere.
2. **Idempotency.** The extractor is the model the gate grants and the prompt version
   (:func:`memini.extraction.llm.extractor_name`). If that extractor has read this content of the
   record before, nothing is called and nothing is written (``records_unchanged``): a job run
   again, or enqueued twice for one content, creates no duplicates.
3. **Extraction**: one LLM call (:meth:`LlmDecisions.extract`). Items without a valid anchor are
   rejected (``items_rejected``).
4. **Supersession**, item by item, in the order extracted: the top ``candidates`` similar active
   items of the workspace (:func:`similar_active_items`), the items already taken from this
   record, and the record's earlier items that are no longer active
   (:func:`memini.extraction.reextract.earlier_items`), one LLM call that names the record's
   source and every candidate's sources, so the gate holds it to the strictest of their policies.
   A duplicate is not stored (``items_duplicate``), also one of an earlier item that is no longer
   active: a new reading of the record does not bring back what was replaced, answered or
   retracted. A link to a manual item, to an item that is not active, to an item of this record
   not stored yet, or to an item another new item already links, is dropped (``links_dropped``):
   extraction never supersedes or answers an item recorded by hand.
5. **Store** the items with their evidence (the record and the anchors), participants (the
   authors of the cited messages, or a meeting's participants, as ingest linked them) and links
   through :func:`memini.knowledge.record_extraction`, in one transaction, with the extraction
   row even when nothing was found. If another job retired a candidate in the meantime
   (:class:`~memini.knowledge.NotActiveError`), step 4 runs again on fresh candidates, up to
   :data:`STORE_ATTEMPTS` times.

Every count goes to the run report of the job (:func:`memini.runs.current_run`), with the LLM
tokens, which the gate's endpoint records.

**Candidates** (:mod:`memini.extraction.candidates`). Knowledge items are not in the search
index. The similar items are found by the words of the statements, as search's lexical leg finds
chunks, and, when the job has :class:`~memini.extraction.candidates.StatementVectors` (an
embedding provider is configured and ``extraction_candidate_retriever`` is ``hybrid``), also by
the vectors of the statements, the two rankings fused. Items with evidence in the same record come
first (an earlier extraction of a changed record). Before the checks of a record, the statements
of its new items are embedded in one batch (a statement stored before is reused), and so are the
active items of the workspace that have no vector yet; the vectors of the items stored are kept
with them. Without vectors a workspace with at most ``candidates`` eligible items shows them all,
so a duplicate in another language is found there; in a larger one it is found only when the two
statements share a word. Only items every source of whose evidence the granted provider may read
are candidates: the supersession call is then granted the same provider as the extraction. An
embedding provider that fails fails the job, which is retried, as an LLM that fails does: a check
on fewer candidates could store a duplicate for good.
"""

import json
import logging
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Final, Literal
from uuid import UUID

from psycopg import AsyncConnection

from memini.auth import WriteAccess
from memini.config import Settings
from memini.db import Pool
from memini.embeddings import Embedder
from memini.embeddings.reuse import EmbeddedTexts
from memini.extraction.candidates import (
    DEFAULT_CANDIDATES,
    StatementVectors,
    StoredCandidate,
    similar_active_items,
)
from memini.extraction.inputs import record_input
from memini.extraction.llm import LlmDecisions, extractor_name
from memini.extraction.model import (
    EXTRACTED_KINDS,
    Candidate,
    ProposedItem,
    RecordInput,
    RecordRef,
)
from memini.extraction.reextract import earlier_items
from memini.jobs import EXTRACT_KIND, Handler, Job
from memini.knowledge import (
    EvidenceRef,
    NewItem,
    NotActiveError,
    NotFoundError,
    SupersedesRef,
    record_extraction,
)
from memini.llm import Grant, LlmGate, LlmRefused, configured_providers
from memini.runs import current_run

__all__ = [
    "DEFAULT_CANDIDATES",
    "EXTRACT_KIND",
    "STORE_ATTEMPTS",
    "ExtractionOutcome",
    "StoredCandidate",
    "extract_handler",
    "extraction_enabled",
    "run_extraction",
    "similar_active_items",
]

log = logging.getLogger(__name__)

STORE_ATTEMPTS: Final = 3
"""Times step 4 and 5 run when other jobs keep retiring the candidates in between."""

_EXPECTED_REFUSALS: Final = frozenset({"policy_none", "no_provider"})

type Status = Literal["extracted", "unchanged", "skipped", "gone"]


def extraction_enabled(settings: Settings) -> bool:
    """Whether decisions are extracted: an LLM provider is configured. Ingest enqueues extraction
    jobs, and ``serve`` runs them, only then."""
    return bool(configured_providers(settings))


@dataclass(slots=True)
class ExtractionOutcome:
    """What one extraction job did."""

    status: Status
    reason: str | None = None
    """Why it skipped (``policy_none``, ``no_provider``) or found the record gone."""
    extractor: str | None = None
    item_ids: tuple[UUID, ...] = ()
    """The items stored (``extracted``), or stored by the earlier extraction (``unchanged``)."""
    proposed: int = 0
    rejected: int = 0
    duplicates: int = 0
    replaces: int = 0
    answers: int = 0
    dropped: int = 0
    embedded: int = 0
    """Statements that went to the embedding model for the candidates: the new items', and those
    of active items without a vector."""

    def report(self) -> None:
        """Add the counts to the run being recorded, if any."""
        run = current_run()
        if run is None:
            return
        key = {
            "extracted": "records_extracted",
            "unchanged": "records_unchanged",
            "gone": "records_gone",
            "skipped": f"records_skipped_{self.reason}",
        }[self.status]
        run.count(key)
        for name, value in (
            ("items_proposed", self.proposed),
            ("items_rejected", self.rejected),
            ("items_duplicate", self.duplicates),
            ("items_stored", len(self.item_ids) if self.status == "extracted" else 0),
            ("links_replaces", self.replaces),
            ("links_answers", self.answers),
            ("links_dropped", self.dropped),
            ("statements_embedded", self.embedded),
        ):
            run.count(name, value)
        if self.extractor is not None:
            run.detail("extractor", self.extractor)


# --- The record ----------------------------------------------------------------------------------

_RECORD: Final = """
SELECT r.source_id, r.kind, r.external_id, r.external_url, r.content_hash, r.occurred_at,
       r.payload, r.deleted_at IS NOT NULL, w.name, s.name
FROM record r
JOIN workspace w ON w.id = r.workspace_id
JOIN source s ON s.id = r.source_id
WHERE r.id = %s AND r.workspace_id = %s
"""


@dataclass(frozen=True, slots=True)
class _Record:
    source_id: UUID
    content_hash: str
    input: RecordInput


async def _load(conn: AsyncConnection[Any], workspace_id: UUID, record_id: str) -> _Record | None:
    cur = await conn.execute(_RECORD, (record_id, workspace_id))
    row = await cur.fetchone()
    if row is None:
        return None
    source_id, kind, external_id, url, content_hash, occurred_at, payload, deleted = row[:8]
    if deleted or payload is None or kind not in EXTRACTED_KINDS:
        return None
    workspace, source = row[8], row[9]
    data = json.loads(bytes(payload))
    return _Record(
        source_id=source_id,
        content_hash=content_hash,
        input=record_input(
            workspace=workspace,
            ref=RecordRef(source, kind, external_id),
            occurred_at=occurred_at,
            payload=data,
            external_url=url,
            record_id=record_id,
        ),
    )


async def _extracted_before(
    conn: AsyncConnection[Any], workspace_id: UUID, record_id: str, content_hash: str, name: str
) -> bool:
    cur = await conn.execute(
        "SELECT 1 FROM knowledge_extraction WHERE workspace_id = %s AND record_id = %s"
        " AND content_hash = %s AND extractor = %s",
        (workspace_id, record_id, content_hash, name),
    )
    return await cur.fetchone() is not None


async def _participants(
    conn: AsyncConnection[Any], record_id: str, kind: str, anchors: Sequence[str]
) -> list[UUID]:
    """The persons of an item: the authors of the messages it cites, or the participants of the
    meeting, as ingest linked them (:mod:`memini.persons`)."""
    if kind == "thread":
        cur = await conn.execute(
            "SELECT DISTINCT person_id FROM record_person WHERE record_id = %s"
            " AND role = 'author' AND anchor = ANY(%s) ORDER BY person_id",
            (record_id, list(anchors)),
        )
    else:
        cur = await conn.execute(
            "SELECT DISTINCT person_id FROM record_person WHERE record_id = %s"
            " AND role = 'participant' ORDER BY person_id",
            (record_id,),
        )
    return [row[0] for row in await cur.fetchall()]


# --- Candidates ----------------------------------------------------------------------------------


def _readable_by(grant: Grant) -> tuple[str, ...]:
    """The source policies whose content the granted provider may read: a cloud provider only
    ``cloud``; the local one ``local`` and ``cloud``."""
    return ("cloud",) if grant.provider_class == "cloud" else ("local", "cloud")


# --- The run -------------------------------------------------------------------------------------


@dataclass(slots=True)
class _Plan:
    items: list[NewItem] = field(default_factory=list[NewItem])
    duplicates: int = 0
    replaces: int = 0
    answers: int = 0
    dropped: int = 0


@dataclass(frozen=True, slots=True)
class _Vectors:
    """The statement vectors of a run: the store, and the vectors of the record's new items."""

    store: StatementVectors
    embedded: EmbeddedTexts

    def of(self, statement: str) -> tuple[str, list[float]]:
        return self.store.key, self.embedded.vector(statement)


async def _supersession(
    conn: AsyncConnection[Any],
    decisions: LlmDecisions,
    record: _Record,
    workspace_id: UUID,
    items: Sequence[ProposedItem],
    grant: Grant,
    limit: int,
    vectors: _Vectors | None,
) -> _Plan:
    """Step 4 of the module: the items to store, with their links."""
    plan = _Plan()
    policies = _readable_by(grant)
    record_id = record.input.record_id or ""
    pending: list[Candidate] = []
    taken: set[UUID] = set()
    # Duplicate targets only: never in by_id, so a link to one is dropped.
    earlier = await earlier_items(
        conn, workspace_id=workspace_id, record_id=record_id, policies=policies, limit=limit
    )
    for item in items:
        stored = await similar_active_items(
            conn,
            workspace_id=workspace_id,
            record_id=record_id,
            statement=item.statement,
            policies=policies,
            limit=limit,
            vector=None if vectors is None else vectors.of(item.statement),
        )
        by_id = {each.candidate.id: each for each in stored}
        sources = {record.source_id}
        for each in (*stored, *earlier):
            sources |= each.source_ids
        verdict = await decisions.classify(
            conn,
            workspace_id=workspace_id,
            source_ids=sources,
            item=item,
            record=record.input,
            candidates=[
                *pending,
                *(each.candidate for each in stored),
                *(each.candidate for each in earlier),
            ],
        )
        if verdict.duplicate_of is not None:
            plan.duplicates += 1
            continue
        links: list[SupersedesRef] = []
        for link in verdict.supersedes:
            target = by_id.get(link.candidate_id)
            if target is None or target.manual or target.item_id in taken:
                plan.dropped += 1
                continue
            taken.add(target.item_id)
            links.append(SupersedesRef(target.item_id, link.relation))
            if link.relation == "replaces":
                plan.replaces += 1
            else:
                plan.answers += 1
        plan.items.append(
            NewItem(
                kind=item.kind,
                statement=item.statement,
                decided_at=item.decided_at,
                evidence=[EvidenceRef(record_id, anchor) for anchor in item.anchors],
                participants=await _participants(
                    conn, record_id, record.input.ref.kind, item.anchors
                ),
                supersedes=links,
            )
        )
        pending.append(
            Candidate(
                id=f"new-{len(pending) + 1}",
                kind=item.kind,
                statement=item.statement,
                decided_at=item.decided_at,
            )
        )
    return plan


async def run_extraction(
    conn: AsyncConnection[Any],
    decisions: LlmDecisions,
    access: WriteAccess,
    *,
    workspace_id: UUID,
    record_id: str,
    candidates: int = DEFAULT_CANDIDATES,
    vectors: StatementVectors | None = None,
) -> ExtractionOutcome:
    """Extract, check and store the items of the record ``record_id`` (see the module), and
    report the counts to the run being recorded. With ``vectors``, candidates are found by the
    statement vectors too.

    ``conn`` is in autocommit mode: the LLM calls are made outside any transaction, and
    :func:`~memini.knowledge.record_extraction` commits its own.

    Raises:
        LlmError: a provider failed, or answered with something that does not match the schema;
            the job is retried.
        LlmRefused: a refusal other than ``policy_none`` and ``no_provider``.
        EmbeddingError: the embedding provider of ``vectors`` failed; the job is retried.
    """
    outcome = await _run(conn, decisions, access, workspace_id, record_id, candidates, vectors)
    outcome.report()
    return outcome


async def _run(
    conn: AsyncConnection[Any],
    decisions: LlmDecisions,
    access: WriteAccess,
    workspace_id: UUID,
    record_id: str,
    candidates: int,
    store: StatementVectors | None,
) -> ExtractionOutcome:
    record = await _load(conn, workspace_id, record_id)
    if record is None:
        return ExtractionOutcome("gone", "the record is gone, deleted or not a thread or meeting")
    sources = {record.source_id}
    try:
        grant = await decisions.gate.check(conn, workspace_id=workspace_id, source_ids=sources)
        name = extractor_name(grant.model)
        if await _extracted_before(conn, workspace_id, record_id, record.content_hash, name):
            return ExtractionOutcome("unchanged", extractor=name)
        extracted = await decisions.extract(
            conn, workspace_id=workspace_id, source_ids=sources, record=record.input
        )
    except LlmRefused as refusal:
        if refusal.reason not in _EXPECTED_REFUSALS:
            raise
        log.info("extraction of record %s skipped: %s", record_id, refusal.reason)
        return ExtractionOutcome("skipped", refusal.reason)
    grant = extracted.grant
    name = extractor_name(grant.model)
    outcome = ExtractionOutcome(
        "extracted", extractor=name, proposed=len(extracted.items), rejected=extracted.rejected
    )
    vectors: _Vectors | None = None
    if store is not None and extracted.items:
        statements = [item.statement for item in extracted.items]
        vectors = _Vectors(store, await store.vectors(conn, workspace_id, statements))
        outcome.embedded = vectors.embedded.embedded + await store.fill(conn, workspace_id)
    for attempt in range(1, STORE_ATTEMPTS + 1):
        plan = await _supersession(
            conn, decisions, record, workspace_id, extracted.items, grant, candidates, vectors
        )
        try:
            stored = await record_extraction(
                conn,
                access,
                record_id=record_id,
                content_hash=record.content_hash,
                extractor=name,
                items=plan.items,
            )
        except NotActiveError:
            if attempt == STORE_ATTEMPTS:
                raise
            log.info("record %s: a candidate was retired meanwhile; checking again", record_id)
            continue
        except NotFoundError:
            return ExtractionOutcome("gone", "the record was deleted during the extraction")
        outcome.status = "extracted" if stored.created else "unchanged"
        outcome.item_ids = stored.item_ids
        if vectors is not None and stored.created:
            kept = zip(stored.item_ids, (item.statement for item in plan.items), strict=True)
            await vectors.store.store(conn, workspace_id, list(kept), vectors.embedded)
        outcome.duplicates = plan.duplicates
        outcome.replaces, outcome.answers = plan.replaces, plan.answers
        outcome.dropped = plan.dropped
        break
    return outcome


# --- The handler ---------------------------------------------------------------------------------


@asynccontextmanager
async def _autocommit(pool: Pool) -> AsyncGenerator[AsyncConnection[Any]]:
    """A pooled connection in autocommit mode, given back as the pool lends them."""
    async with pool.connection() as conn:
        await conn.set_autocommit(True)
        try:
            yield conn
        finally:
            if not conn.closed:
                await conn.set_autocommit(False)


def extract_handler(
    pool: Pool,
    gate: LlmGate,
    *,
    candidates: int = DEFAULT_CANDIDATES,
    embedder: Embedder | None = None,
) -> Handler:
    """The handler of ``extract`` jobs: :func:`run_extraction` of the job's record, as the
    principal that enqueued it. ``gate`` is the service's, and so is ``embedder``, which finds
    candidates by statement vectors too when given; the caller closes both."""
    decisions = LlmDecisions(gate)
    vectors = None if embedder is None else StatementVectors(embedder)

    async def handle(job: Job) -> None:
        record_id = job.args.get("record_id")
        if not isinstance(record_id, str):
            raise ValueError(f"{EXTRACT_KIND} job {job.id} names no record")
        async with _autocommit(pool) as conn:
            await run_extraction(
                conn,
                decisions,
                WriteAccess.for_owner(job.owner_id),
                workspace_id=job.workspace_id,
                record_id=record_id,
                candidates=candidates,
                vectors=vectors,
            )

    return handle
