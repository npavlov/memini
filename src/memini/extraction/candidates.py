"""The candidates of a supersession check: the active items a new item is shown (requirements,
section 5, Decision extraction).

A check sees at most ``limit`` stored items (``extraction_candidates``), so which ones it sees
decides what it can find. Two legs rank the eligible active items of the workspace:

- **lexical**: each statement parsed as search's lexical leg parses a chunk (the ``russian``
  configuration after :func:`memini.search.retrieval.split_identifiers`) and ranked with
  ``ts_rank`` against any word of the new statement (:func:`lexical_rank`). It needs nothing
  but the database, and finds an item only through a shared word: a statement in another
  language is found when it shares a name or a number.
- **dense**: the cosine similarity of the statements' vectors under the configured embedding
  model, embedded as documents, so that both sides are embedded alike and the new item's vector
  is the one stored with it (:class:`StatementVectors`). Only with an embedding provider.

:func:`rank_candidates` puts the two together, in one function the ``extract`` job and the
decision eval share: items with evidence in the record being extracted first (an earlier
extraction of a changed record), then the reciprocal rank fusion of the two legs
(:func:`memini.search.fusion.reciprocal_rank_fusion`, with search's default k and weights, which
the retrieval eval chose: :data:`RRF_K`, :data:`DENSE_WEIGHT`), then the rest by time, latest
first. Only items with a word in common are in the lexical ranking, and only items with a
vector in the dense one. Without vectors this is the lexical order the job used before there
was a dense leg: same record, then rank, then the latest.

Eligible means active, in the workspace, and with evidence only from sources the granted LLM
provider may read (:func:`similar_active_items`): the check is then granted the provider of the
extraction.
"""

import logging
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from psycopg import AsyncConnection, sql

from memini.embeddings import Embedder, Vector
from memini.embeddings.reuse import EmbeddedTexts, embed_missing, text_hash
from memini.extraction.model import Candidate
from memini.index.fingerprint import embedding_key
from memini.search.fusion import reciprocal_rank_fusion
from memini.search.patterns import lexical_terms
from memini.search.retrieval import LEXICAL_CONFIG

__all__ = [
    "DEFAULT_CANDIDATES",
    "DENSE_WEIGHT",
    "LEXICAL_WEIGHT",
    "RRF_K",
    "Scored",
    "StatementVectors",
    "StoredCandidate",
    "lexical_rank",
    "rank_candidates",
    "similar_active_items",
]

log = logging.getLogger(__name__)

DEFAULT_CANDIDATES: Final = 8
"""Similar active items shown to a supersession check, unless the settings say otherwise."""

RRF_K: Final = 1
"""The k of the fusion: search's default (``search_rrf_k``), chosen by the retrieval eval."""
DENSE_WEIGHT: Final = 3.0
"""The weight of the dense ranks: search's default (``search_dense_weight``)."""
LEXICAL_WEIGHT: Final = 1.0


# --- Ranking -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Scored:
    """An eligible item with what each leg says of it."""

    id: str
    decided_at: datetime
    lexical: float
    """``ts_rank`` against the new statement; 0 when they share no word."""
    similarity: float | None = None
    """Cosine similarity of the statements' vectors; ``None`` without a vector."""
    same_record: bool = False
    """It has evidence in the record being extracted."""


def _recency(item: Scored) -> tuple[float, str]:
    return (-item.decided_at.timestamp(), item.id)


def rank_candidates(scored: Sequence[Scored], limit: int) -> list[str]:
    """The ids of the ``limit`` best of ``scored``, best first (see the module)."""
    lexical = sorted((s for s in scored if s.lexical > 0), key=lambda s: (-s.lexical, *_recency(s)))
    dense = sorted(
        (s for s in scored if s.similarity is not None),
        key=lambda s: (-(s.similarity or 0.0), *_recency(s)),
    )
    rankings: list[list[str]] = [[s.id for s in lexical]]
    weights = [LEXICAL_WEIGHT]
    if dense:
        rankings.append([s.id for s in dense])
        weights.append(DENSE_WEIGHT)
    fused = reciprocal_rank_fusion(rankings, k=RRF_K, weights=weights)
    position = {item.id: n for n, item in enumerate(fused)}
    ordered = sorted(
        scored, key=lambda s: (not s.same_record, position.get(s.id, len(position)), *_recency(s))
    )
    return [s.id for s in ordered[:limit]]


def lexical_rank(text: sql.Composable, statement: str, params: list[Any]) -> sql.Composable:
    """``ts_rank`` of ``text`` (an SQL expression: a column) against any word of ``statement``,
    as search's lexical leg ranks chunks; 0 for a statement without words. The words are
    appended to ``params``, in the order of their placeholders."""
    terms = lexical_terms(statement)
    if not terms:
        return sql.SQL("0::real")
    config = sql.Literal(LEXICAL_CONFIG)
    queries: list[sql.Composable] = []
    for term in terms:
        params.append(term)
        queries.append(sql.SQL("plainto_tsquery({}::regconfig, lexical_text(%s))").format(config))
    return sql.SQL("ts_rank(to_tsvector({}::regconfig, lexical_text({})), {})").format(
        config, text, sql.SQL(" || ").join(queries)
    )


# --- Statement vectors ---------------------------------------------------------------------------


class StatementVectors:
    """The vectors of item statements under one embedder, stored per item
    (``knowledge_item_vector``) and reused by the hash of the statement, so that a statement is
    embedded once (principle 6). Statements are embedded as documents: an item is compared with
    items, and the vector computed for a new item's check is the one stored with it."""

    def __init__(self, embedder: Embedder) -> None:
        self.embedder = embedder
        self.key = embedding_key(embedder.spec)

    async def vectors(
        self, conn: AsyncConnection[Any], workspace_id: UUID, statements: Sequence[str]
    ) -> EmbeddedTexts:
        """Vectors of ``statements``: those stored in the workspace under the key, the rest from
        the model."""

        async def lookup(hashes: Sequence[str]) -> Mapping[str, Vector]:
            cur = await conn.execute(
                "SELECT DISTINCT ON (text_hash) text_hash, embedding::real[]"
                " FROM knowledge_item_vector"
                " WHERE workspace_id = %s AND embedding_key = %s AND text_hash = ANY(%s)",
                (workspace_id, self.key, list(hashes)),
                # Binary: each float4 arrives exactly, as the index writer reads chunk vectors.
                binary=True,
            )
            return {row[0]: list(row[1]) for row in await cur.fetchall()}

        if not statements:
            return EmbeddedTexts(vectors={}, reused=0, embedded=0)
        return await embed_missing(self.embedder, statements, lookup)

    async def store(
        self,
        conn: AsyncConnection[Any],
        workspace_id: UUID,
        items: Sequence[tuple[UUID, str]],
        embedded: EmbeddedTexts,
    ) -> int:
        """Store the vector of each ``(item id, statement)`` from ``embedded``; one the item
        already has, or for a statement the item does not have, is skipped. Returns the rows
        written."""
        written = 0
        for item_id, statement in items:
            digest = text_hash(statement)
            vector = embedded.vectors.get(digest)
            if vector is None:
                continue
            cur = await conn.execute(
                "INSERT INTO knowledge_item_vector"
                " (workspace_id, owner_id, item_id, embedding_key, text_hash, embedding)"
                " SELECT i.workspace_id, i.owner_id, i.id, %s, %s, %s::vector FROM knowledge_item i"
                " WHERE i.id = %s AND i.workspace_id = %s"
                " AND encode(sha256(convert_to(i.statement, 'UTF8')), 'hex') = %s"
                " ON CONFLICT (item_id, embedding_key) DO NOTHING",
                (self.key, digest, vector, item_id, workspace_id, digest),
            )
            written += cur.rowcount
        return written

    async def fill(self, conn: AsyncConnection[Any], workspace_id: UUID) -> int:
        """Embed the active items of the workspace that have no vector under the key, and drop
        the vectors of other keys there. Returns the statements that went to the model."""
        await conn.execute(
            "DELETE FROM knowledge_item_vector WHERE workspace_id = %s AND embedding_key <> %s",
            (workspace_id, self.key),
        )
        cur = await conn.execute(
            "SELECT i.id, i.statement FROM knowledge_item i"
            " WHERE i.workspace_id = %s AND i.status = 'active' AND NOT EXISTS ("
            "   SELECT 1 FROM knowledge_item_vector v"
            "   WHERE v.item_id = i.id AND v.embedding_key = %s)"
            " ORDER BY i.id",
            (workspace_id, self.key),
        )
        missing: list[tuple[UUID, str]] = [(row[0], row[1]) for row in await cur.fetchall()]
        if not missing:
            return 0
        embedded = await self.vectors(conn, workspace_id, [text for _, text in missing])
        written = await self.store(conn, workspace_id, missing, embedded)
        log.info("stored the vectors of %d active items (%d reused)", written, embedded.reused)
        return embedded.embedded


# --- Stored candidates ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StoredCandidate:
    """An active item a new one is checked against."""

    candidate: Candidate
    """``id`` is the item's id as text."""
    manual: bool
    source_ids: frozenset[UUID]
    """The sources of its evidence: their content is in the supersession request."""

    @property
    def item_id(self) -> UUID:
        return UUID(self.candidate.id)


_SCORES: Final = sql.SQL("""
SELECT i.id, i.decided_at,
       EXISTS (
           SELECT 1 FROM knowledge_evidence e WHERE e.item_id = i.id AND e.record_id = {record}
       ),
       {rank},
       {similarity}
FROM knowledge_item i {vectors}
WHERE i.workspace_id = {workspace} AND i.status = 'active'
  AND NOT EXISTS (
      SELECT 1 FROM knowledge_evidence e LEFT JOIN source s ON s.id = e.source_id
      WHERE e.item_id = i.id AND (s.id IS NULL OR s.llm_policy <> ALL({policies}::text[]))
  )
""")

_DETAILS: Final = """
SELECT i.id, i.kind, i.statement, i.decided_at, i.extractor = 'manual',
       array(SELECT DISTINCT e.source_id FROM knowledge_evidence e WHERE e.item_id = i.id)
FROM knowledge_item i
WHERE i.id = ANY(%s)
"""


async def similar_active_items(
    conn: AsyncConnection[Any],
    *,
    workspace_id: UUID,
    record_id: str,
    statement: str,
    policies: Collection[str],
    limit: int = DEFAULT_CANDIDATES,
    vector: tuple[str, Vector] | None = None,
) -> list[StoredCandidate]:
    """The ``limit`` eligible active items most like ``statement``, best first
    (:func:`rank_candidates`). Eligible: of the workspace, and with evidence only from sources
    whose policy is in ``policies``, what the provider may read. ``vector`` is the embedding key
    and the statement's vector: with it, the dense leg ranks the items stored with a vector
    under that key."""
    params: list[Any] = [record_id]
    rank = lexical_rank(sql.SQL("i.statement"), statement, params)
    if vector is None:
        similarity: sql.Composable = sql.SQL("NULL::float8")
        join: sql.Composable = sql.SQL("")
    else:
        key, values = vector
        similarity = sql.SQL("1 - (v.embedding <=> %s::vector)")
        join = sql.SQL(
            "LEFT JOIN knowledge_item_vector v ON v.item_id = i.id AND v.embedding_key = %s"
        )
        params.extend([values, key])
    params.extend([workspace_id, sorted(policies)])
    query = _SCORES.format(
        record=sql.Placeholder(),
        rank=rank,
        similarity=similarity,
        vectors=join,
        workspace=sql.Placeholder(),
        policies=sql.Placeholder(),
    )
    cur = await conn.execute(query, params)
    scored = [
        Scored(
            id=str(item_id),
            decided_at=decided,
            lexical=float(lexical),
            similarity=None if similar is None else float(similar),
            same_record=bool(same),
        )
        for item_id, decided, same, lexical, similar in await cur.fetchall()
    ]
    chosen = rank_candidates(scored, limit)
    if not chosen:
        return []
    cur = await conn.execute(_DETAILS, ([UUID(each) for each in chosen],))
    found: dict[str, StoredCandidate] = {}
    for item_id, kind, text, decided, manual, sources in await cur.fetchall():
        found[str(item_id)] = StoredCandidate(
            candidate=Candidate(id=str(item_id), kind=kind, statement=text, decided_at=decided),
            manual=bool(manual),
            source_ids=frozenset(sources),
        )
    return [found[each] for each in chosen if each in found]
