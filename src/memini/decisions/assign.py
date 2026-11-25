"""The ``assign_domains`` job: a model chooses the domains of the catalog an item belongs to, when
nothing else says (requirements, section 5, **Domain assignment**).

It is the last of the ways an item comes by domains (:mod:`memini.decisions.context`): what the
record was pushed with, a default of its source or stream, a person's link and code evidence come
first, and the model never changes what came by one of them. For each active item of a record (the
``extract`` job enqueues one job per record whose extraction stored items) or of a whole workspace
(after its catalog changed), :func:`run_assignment`:

1. **Skips** an item that already has a domain by another way (``items_settled``), and one whose
   workspace's catalog has no domain to offer (``items_no_catalog``). When the item's project came
   by another way, only that project's domains are offered.
2. **Gates** it (principle 8): the content of the request is the item's statement and the parts it
   cites, which come from the sources of its evidence, and the catalog, whose titles and
   descriptions were written from the repositories its services name. The gate holds the request
   to the strictest policy of all those sources and of the workspace: ``policy_none`` and
   ``no_provider`` skip the item and are counted (``items_skipped_policy_none``,
   ``items_skipped_no_provider``); nothing is sent. An item whose evidence names a source that is
   gone is skipped too (``items_skipped_source_gone``): the policy of its content is unknown.
3. **Answers once.** The assigner is the granted model and :data:`PROMPT_VERSION`. Its last answer
   for an item, "none" included, is kept with a hash of the domains it was shown
   (``knowledge_domain_assignment``): asked again by the same assigner about the same domains, it
   calls nothing (``items_unchanged``). A change of the model, of the prompt or of the domains
   offered asks again.
4. **Asks** in one call: the item (kind, time, statement, and the parts of its records it cites,
   each cut to :data:`CITED_PART_LIMIT` characters; an item whose records are gone shows its
   statement only) and the domains, numbered, with their project, address, title and description.
   It answers what the item is about, then the numbers of one to three domains, or none
   (``items_assigned``, ``items_none``). A subdomain already implies its domain, so when the
   answer names a domain and one of its own subdomains, only the subdomain is kept
   (:func:`most_specific`); the prompt says so too.
5. **Writes** the model's links in one transaction with its answer: its earlier links of the item
   go, the new ones come (``links_written``), and a link a person made to the same domain stays a
   person's.

**Catalog text is data** (principle 11), as record text is: everything shown is inside the data
block of :func:`memini.extraction.llm.data_block`, with the same markers, escapes and reminder as
an extraction; the system message holds the instructions and no content. Titles and descriptions
were redacted when ``put_project`` stored them.
"""

import hashlib
import json
import logging
import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Protocol
from uuid import UUID

from psycopg import AsyncConnection
from pydantic import BaseModel

from memini.auth import WriteAccess
from memini.db import Pool
from memini.decisions.context import ItemContext, add_links, load_contexts
from memini.extraction.inputs import record_input
from memini.extraction.llm import UNTRUSTED_NOTICE, UNTRUSTED_REMINDER, data_block
from memini.extraction.model import EXTRACTED_KINDS, RecordRef
from memini.jobs import ASSIGN_DOMAINS_KIND, Handler, Job
from memini.llm import ChatRequest, Grant, LlmGate, LlmRefused, Message
from memini.payloads import PayloadError
from memini.runs import current_run

__all__ = [
    "ASSIGN_DOMAINS_KIND",
    "CITED_PART_LIMIT",
    "DESCRIPTION_LIMIT",
    "MAX_PICKS",
    "PROMPT_VERSION",
    "AssignmentOutcome",
    "DomainAssigner",
    "DomainChoice",
    "DomainPicks",
    "DomainQuestion",
    "LlmAssigner",
    "SaidPart",
    "assign_handler",
    "assigner_name",
    "assignment_request",
    "catalog_hash",
    "most_specific",
    "picked",
    "run_assignment",
]

log = logging.getLogger(__name__)

PROMPT_VERSION: Final = "domains-v2"
"""The version of the assignment prompt and of the reading of its answer. Change it with either:
the answers kept name it, and a new version asks again about every item."""

CITED_PART_LIMIT: Final = 800
"""Characters of each cited part shown with the item."""
DESCRIPTION_LIMIT: Final = 600
"""Characters of a domain's description shown."""
MAX_PICKS: Final = 3
"""Domains one answer may give; the first ones are kept."""
_CITED_PARTS: Final = 4
_MAX_TOKENS: Final = 512
_ASSIGNER_CHARS: Final = re.compile(r"[^A-Za-z0-9._:@/+=-]")
_EXPECTED_REFUSALS: Final = frozenset({"policy_none", "no_provider"})


def assigner_name(model: str) -> str:
    """The assigner kept with each answer and model link: ``{model}@{PROMPT_VERSION}``."""
    cleaned = _ASSIGNER_CHARS.sub("-", model).lstrip("._:@/+=-") or "model"
    suffix = f"@{PROMPT_VERSION}"
    return cleaned[: 200 - len(suffix)] + suffix


# --- The question --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DomainChoice:
    """A domain or subdomain the model may choose."""

    project: str
    domain: str
    """The address: ``domain`` or ``domain/subdomain``."""
    title: str = ""
    description: str = ""
    project_title: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return (self.project, self.domain)


@dataclass(frozen=True, slots=True)
class SaidPart:
    """A part of a record an item cites: a message, a segment, a summary."""

    text: str
    who: str | None = None
    at: datetime | None = None


@dataclass(frozen=True, slots=True)
class DomainQuestion:
    """What the model is shown about one item, and the domains it may choose."""

    kind: str
    statement: str
    decided_at: datetime
    said: tuple[SaidPart, ...]
    choices: tuple[DomainChoice, ...]


def catalog_hash(choices: Sequence[DomainChoice]) -> str:
    """SHA-256 of the domains offered, as shown: a change of any of them asks again."""
    shown = [
        [c.project, c.project_title, c.domain, c.title, c.description[:DESCRIPTION_LIMIT]]
        for c in choices
    ]
    return hashlib.sha256(json.dumps(shown, ensure_ascii=False).encode()).hexdigest()


class DomainPicks(BaseModel):
    """The answer as the model returns it; the fields are in the order of their names."""

    about: str
    """The business matter of the item, in a few words: written before the choice."""
    picks: list[int]
    """The numbers of the domains chosen; empty for none."""


_SYSTEM: Final = """\
You file the decisions and open questions of a team's log under the business areas of its \
catalog. The first line of the data is one item: its kind, the time it was said, its statement, \
and the parts of the record it was taken from, when they are still stored. Each other line is a \
domain of the catalog: its number ("n"), its project, its address ("domain"; a subdomain is \
written domain/subdomain), its title and its description.

{untrusted}

Work in two steps:
1. "about": in a few words, the business matter the item decides or asks about.
2. "picks": the numbers of the domains that matter belongs to, usually one and at most three. \
Pick a subdomain when the item concerns that part only, its domain when it concerns the area as \
a whole. A subdomain already implies its domain: never pick a domain together with one of \
its own subdomains. Pick nothing when no domain fits: an item about the team's own way of \
working, a meeting, people, tools, or an area the catalog does not list belongs nowhere, and an \
empty list is then the right answer. A word shared with a title is not enough.

Reply with one JSON object: {{"about": ..., "picks": [...]}}."""

_TASK: Final = "Choose the domains of the item."


def _time(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def assignment_request(question: DomainQuestion) -> ChatRequest[DomainPicks]:
    """The request: the item and the domains numbered from 1, all inside the data block."""
    item: dict[str, Any] = {
        "item": question.kind,
        "at": _time(question.decided_at),
        "statement": question.statement,
        "said": [
            {
                key: value
                for key, value in (
                    ("from", part.who),
                    ("at", _time(part.at)),
                    ("text", _cut(part.text, CITED_PART_LIMIT)),
                )
                if value is not None
            }
            for part in question.said[:_CITED_PARTS]
        ],
    }
    domains = [
        {
            "n": n,
            "project": choice.project,
            "project_title": choice.project_title or None,
            "domain": choice.domain,
            "title": choice.title or None,
            "description": _cut(choice.description, DESCRIPTION_LIMIT) or None,
        }
        for n, choice in enumerate(question.choices, start=1)
    ]
    nonce, block = data_block([item, *domains])
    system = _SYSTEM.format(untrusted=UNTRUSTED_NOTICE.format(nonce=nonce))
    user = f"{_TASK}\n\n{block}\n\n{UNTRUSTED_REMINDER}"
    return ChatRequest(
        messages=[Message("system", system), Message("user", user)],
        output=DomainPicks,
        max_tokens=_MAX_TOKENS,
        output_name="domain_assignment",
    )


def most_specific(chosen: Sequence[DomainChoice]) -> tuple[DomainChoice, ...]:
    """``chosen`` without a domain whose own subdomain is chosen too: a subdomain already
    implies its domain. The order is kept."""
    return tuple(
        choice
        for choice in chosen
        if not any(
            other.project == choice.project and other.domain.startswith(f"{choice.domain}/")
            for other in chosen
        )
    )


def picked(output: DomainPicks, question: DomainQuestion) -> tuple[DomainChoice, ...]:
    """The domains the answer names: a number not shown is ignored, a repeat counts once, a
    domain named with one of its subdomains gives way to it (:func:`most_specific`), and at
    most :data:`MAX_PICKS` are kept."""
    found: dict[int, DomainChoice] = {}
    for n in output.picks:
        if 1 <= n <= len(question.choices) and n not in found:
            found[n] = question.choices[n - 1]
    return most_specific(list(found.values()))[:MAX_PICKS]


class DomainAssigner(Protocol):
    """Chooses the domains of one item: what the eval measures (:mod:`memini.evaluation.domains`),
    the stub and the model alike."""

    @property
    def name(self) -> str: ...

    async def assign(self, question: DomainQuestion) -> tuple[DomainChoice, ...]:
        """The domains of ``question.choices`` the item belongs to; none at all is an answer."""
        ...


@dataclass(slots=True)
class LlmAssigner:
    """The assignment call through ``gate``."""

    gate: LlmGate
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    async def check(
        self, conn: AsyncConnection[Any], *, workspace_id: UUID, source_ids: Collection[UUID]
    ) -> Grant:
        return await self.gate.check(conn, workspace_id=workspace_id, source_ids=source_ids)

    async def assign(
        self,
        conn: AsyncConnection[Any],
        *,
        workspace_id: UUID,
        source_ids: Collection[UUID],
        question: DomainQuestion,
    ) -> tuple[tuple[DomainChoice, ...], Grant]:
        """The domains the model chooses, and the grant it answered under.

        Raises what :meth:`LlmGate.complete` raises: :class:`~memini.llm.LlmRefused` before
        anything is sent, :class:`~memini.llm.LlmError`, :class:`~memini.llm.LlmOutputError`.
        """
        completion = await self.gate.complete(
            conn,
            workspace_id=workspace_id,
            source_ids=source_ids,
            request=assignment_request(question),
        )
        self.calls += 1
        self.input_tokens += completion.usage.input_tokens
        self.output_tokens += completion.usage.output_tokens
        return picked(completion.output, question), completion.grant


# --- The job -------------------------------------------------------------------------------------


@dataclass(slots=True)
class AssignmentOutcome:
    """What one run did, by item."""

    seen: int = 0
    settled: int = 0
    no_catalog: int = 0
    unchanged: int = 0
    assigned: int = 0
    none: int = 0
    skipped: dict[str, int] = field(default_factory=dict[str, int])
    links: int = 0
    assigner: str | None = None

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    def report(self) -> None:
        """Add the counts to the run being recorded, if any."""
        run = current_run()
        if run is None:
            return
        for name, value in (
            ("items_seen", self.seen),
            ("items_settled", self.settled),
            ("items_no_catalog", self.no_catalog),
            ("items_unchanged", self.unchanged),
            ("items_assigned", self.assigned),
            ("items_none", self.none),
            ("links_written", self.links),
        ):
            run.count(name, value)
        for reason, value in sorted(self.skipped.items()):
            run.count(f"items_skipped_{reason}", value)
        if self.assigner is not None:
            run.detail("assigner", self.assigner)


_ITEMS: Final = """
SELECT i.id, i.kind, i.statement, i.decided_at FROM knowledge_item i
WHERE i.workspace_id = %(workspace)s AND i.status = 'active'
  AND (%(record)s::text IS NULL OR EXISTS (
      SELECT 1 FROM knowledge_evidence e WHERE e.item_id = i.id AND e.record_id = %(record)s))
ORDER BY i.decided_at, i.id
"""

_EVIDENCE: Final = """
SELECT e.item_id, e.record_id, e.source_id, e.anchor, s.id IS NOT NULL
FROM knowledge_evidence e
LEFT JOIN source s ON s.id = e.source_id AND s.workspace_id = e.workspace_id
WHERE e.item_id = ANY(%s) ORDER BY e.item_id, e.id
"""

_CHOICES: Final = """
SELECT p.name, p.title, CASE WHEN up.id IS NULL THEN d.name ELSE up.name || '/' || d.name END,
       d.title, d.description
FROM domain d JOIN project p ON p.id = d.project_id
LEFT JOIN domain up ON up.id = d.parent_id
WHERE d.workspace_id = %s
ORDER BY 1, 3
"""

# The repositories the catalog's text was written from.
_CATALOG_SOURCES: Final = "SELECT DISTINCT source_id FROM service WHERE workspace_id = %s"

_ANSWERS: Final = """
SELECT item_id, assigner, catalog_hash FROM knowledge_domain_assignment WHERE item_id = ANY(%s)
"""

_RECORDS: Final = """
SELECT r.id, r.kind, r.external_id, r.occurred_at, r.payload, s.name, w.name
FROM record r JOIN source s ON s.id = r.source_id JOIN workspace w ON w.id = r.workspace_id
WHERE r.id = ANY(%s) AND r.workspace_id = %s AND r.deleted_at IS NULL AND r.payload IS NOT NULL
"""

_ANSWER: Final = """
INSERT INTO knowledge_domain_assignment (item_id, workspace_id, owner_id, assigner, catalog_hash,
                                         answer)
SELECT i.id, i.workspace_id, i.owner_id, %(assigner)s, %(hash)s, %(answer)s
FROM knowledge_item i WHERE i.id = %(item)s AND i.workspace_id = %(workspace)s
ON CONFLICT (item_id) DO UPDATE
    SET assigner = excluded.assigner, catalog_hash = excluded.catalog_hash,
        answer = excluded.answer, answered_at = now()
"""


@dataclass(frozen=True, slots=True)
class _Item:
    id: UUID
    kind: str
    statement: str
    decided_at: datetime
    evidence: tuple[tuple[str, UUID, str | None, bool], ...]
    """``(record_id, source_id, anchor, source exists)``."""


async def _items(
    conn: AsyncConnection[Any], workspace_id: UUID, record_id: str | None
) -> list[_Item]:
    cur = await conn.execute(_ITEMS, {"workspace": workspace_id, "record": record_id})
    rows = await cur.fetchall()
    if not rows:
        return []
    evidence: dict[UUID, list[tuple[str, UUID, str | None, bool]]] = {}
    cur = await conn.execute(_EVIDENCE, ([row[0] for row in rows],))
    for item_id, record, source_id, anchor, exists in await cur.fetchall():
        evidence.setdefault(item_id, []).append((record, source_id, anchor, bool(exists)))
    return [_Item(row[0], row[1], row[2], row[3], tuple(evidence.get(row[0], ()))) for row in rows]


async def _choices(conn: AsyncConnection[Any], workspace_id: UUID) -> list[DomainChoice]:
    cur = await conn.execute(_CHOICES, (workspace_id,))
    return [
        DomainChoice(
            project=row[0], project_title=row[1], domain=row[2], title=row[3], description=row[4]
        )
        for row in await cur.fetchall()
    ]


async def _said(
    conn: AsyncConnection[Any], workspace_id: UUID, items: Sequence[_Item]
) -> dict[tuple[str, str], SaidPart]:
    """The parts the items cite, by record and anchor, from the records still stored."""
    wanted = sorted({record for item in items for record, _, _, _ in item.evidence})
    if not wanted:
        return {}
    cur = await conn.execute(_RECORDS, (wanted, workspace_id))
    found: dict[tuple[str, str], SaidPart] = {}
    for (
        record_id,
        kind,
        external_id,
        occurred_at,
        payload,
        source,
        workspace,
    ) in await cur.fetchall():
        if kind not in EXTRACTED_KINDS:
            continue
        try:
            parsed = record_input(
                workspace=workspace,
                ref=RecordRef(source, kind, external_id),
                occurred_at=occurred_at,
                payload=json.loads(bytes(payload)),
                record_id=record_id,
            )
        except (PayloadError, ValueError):
            log.warning("record %s: its payload does not parse; its parts are not shown", record_id)
            continue
        for part in parsed.parts:
            found[(record_id, part.anchor)] = SaidPart(part.text, part.who, part.at)
    return found


async def _answers(
    conn: AsyncConnection[Any], item_ids: Sequence[UUID]
) -> dict[UUID, tuple[str, str]]:
    cur = await conn.execute(_ANSWERS, (list(item_ids),))
    return {row[0]: (row[1], row[2]) for row in await cur.fetchall()}


async def _store(
    conn: AsyncConnection[Any],
    access: WriteAccess,
    *,
    workspace_id: UUID,
    item_id: UUID,
    assigner: str,
    digest: str,
    chosen: Sequence[DomainChoice],
) -> int:
    async with conn.transaction():
        await conn.execute(
            "DELETE FROM knowledge_item_domain WHERE item_id = %s AND workspace_id = %s"
            " AND origin = 'model'",
            (item_id, workspace_id),
        )
        written = await add_links(
            conn,
            access,
            workspace_id=workspace_id,
            item_id=item_id,
            places=[choice.key for choice in chosen],
            origin="model",
            assigner=assigner,
        )
        await conn.execute(
            _ANSWER,
            {
                "assigner": assigner,
                "hash": digest,
                "answer": "domains" if chosen else "none",
                "item": item_id,
                "workspace": workspace_id,
            },
        )
    return written


def _offered(context: ItemContext, choices: Sequence[DomainChoice]) -> list[DomainChoice]:
    project = context.fixed_project
    return [c for c in choices if project is None or c.project == project]


async def run_assignment(
    conn: AsyncConnection[Any],
    assigner: LlmAssigner,
    access: WriteAccess,
    *,
    workspace_id: UUID,
    record_id: str | None = None,
) -> AssignmentOutcome:
    """Assign the domains of the active items of ``record_id``, or of the whole workspace
    (see the module), and report the counts to the run being recorded.

    ``conn`` is in autocommit mode: the calls are made outside any transaction, and each item's
    links and answer are written in one of their own.

    Raises:
        LlmError: a provider failed, or answered with something that does not match the schema;
            the job is retried, and the items answered before stay answered.
        LlmRefused: a refusal other than ``policy_none`` and ``no_provider``.
    """
    outcome = AssignmentOutcome()
    try:
        await _run(conn, assigner, access, workspace_id, record_id, outcome)
    finally:
        outcome.report()
    return outcome


async def _run(
    conn: AsyncConnection[Any],
    assigner: LlmAssigner,
    access: WriteAccess,
    workspace_id: UUID,
    record_id: str | None,
    outcome: AssignmentOutcome,
) -> None:
    items = await _items(conn, workspace_id, record_id)
    if not items:
        return
    contexts = await load_contexts(conn, access, [workspace_id], item_ids=[i.id for i in items])
    choices = await _choices(conn, workspace_id)
    cur = await conn.execute(_CATALOG_SOURCES, (workspace_id,))
    catalog_sources = {row[0] for row in await cur.fetchall()}
    answers = await _answers(conn, [item.id for item in items])
    said = await _said(conn, workspace_id, items)
    for item in items:
        outcome.seen += 1
        context = contexts.context(item.id)
        if context.settled:
            outcome.settled += 1
            continue
        offered = _offered(context, choices)
        if not offered:
            outcome.no_catalog += 1
            continue
        if not item.evidence or not all(exists for *_, exists in item.evidence):
            outcome.skip("source_gone")
            continue
        sources = {source_id for _, source_id, _, _ in item.evidence} | catalog_sources
        try:
            grant = await assigner.check(conn, workspace_id=workspace_id, source_ids=sources)
        except LlmRefused as refusal:
            if refusal.reason not in _EXPECTED_REFUSALS:
                raise
            outcome.skip(refusal.reason)
            continue
        name = assigner_name(grant.model)
        outcome.assigner = name
        digest = catalog_hash(offered)
        if answers.get(item.id) == (name, digest):
            outcome.unchanged += 1
            continue
        question = DomainQuestion(
            kind=item.kind,
            statement=item.statement,
            decided_at=item.decided_at,
            said=tuple(
                part
                for record, _, anchor, _ in item.evidence
                if anchor is not None and (part := said.get((record, anchor))) is not None
            ),
            choices=tuple(offered),
        )
        try:
            chosen, grant = await assigner.assign(
                conn, workspace_id=workspace_id, source_ids=sources, question=question
            )
        except LlmRefused as refusal:
            # The policies changed since the check.
            if refusal.reason not in _EXPECTED_REFUSALS:
                raise
            outcome.skip(refusal.reason)
            continue
        name = assigner_name(grant.model)
        # Whatever the assigner, the answer kept names a subdomain without its domain.
        chosen = most_specific(chosen)
        outcome.links += await _store(
            conn,
            access,
            workspace_id=workspace_id,
            item_id=item.id,
            assigner=name,
            digest=digest,
            chosen=chosen,
        )
        if chosen:
            outcome.assigned += 1
        else:
            outcome.none += 1


def assign_handler(pool: Pool, gate: LlmGate) -> Handler:
    """The handler of ``assign_domains`` jobs: :func:`run_assignment` of the job's record, or of
    its workspace, as the principal that enqueued it. ``gate`` is the service's; the caller
    closes it."""
    assigner = LlmAssigner(gate)

    async def handle(job: Job) -> None:
        record_id = job.args.get("record_id")
        if record_id is not None and not isinstance(record_id, str):
            raise ValueError(f"{ASSIGN_DOMAINS_KIND} job {job.id} names a record that is not one")
        async with pool.connection() as conn:
            await conn.set_autocommit(True)
            try:
                await run_assignment(
                    conn,
                    assigner,
                    WriteAccess.for_owner(job.owner_id),
                    workspace_id=job.workspace_id,
                    record_id=record_id,
                )
            finally:
                if not conn.closed:
                    await conn.set_autocommit(False)

    return handle
