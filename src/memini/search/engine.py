"""Running a search: the fingerprint check, the transaction, the retrievers, fusion and reranking.

:func:`search` is the one entry point. In order:

1. A ``persons`` filter is refused (:class:`UnsupportedFilterError`): persons are linked to
   records in milestone M2, and an empty answer would read as "this person said nothing".
2. For ``hybrid`` and ``dense``, a first check of the index fingerprint, so that a stale index is
   refused before the query costs an embedding call; then the query is embedded (``kind="query"``,
   with the query prefix), outside any transaction.
3. One read-only transaction at ``REPEATABLE READ``, so that the check and every statement after it
   see one snapshot: a rebuild that finishes meanwhile cannot change the index under the search.
   ``statement_timeout`` bounds each statement; ``hnsw.ef_search`` and ``hnsw.iterative_scan``
   are set for the dense retriever.
4. The scope: the workspaces the caller may read (the :class:`~memini.auth.ReadFilter`, narrowed
   by the ``workspaces`` filter), each checked with :func:`~memini.index.check_index` against the
   fingerprint the embedder and this chunker make. Any mismatch raises
   :class:`~memini.index.IndexMismatchError` for the whole search, in every mode (below); a
   workspace with no index yet has nothing to search and is left out. Source and repository
   names are resolved to ids within those workspaces.
5. The retrievers of the mode (:mod:`memini.search.retrieval`); for ``hybrid``, reciprocal rank
   fusion of the dense and lexical candidates (:mod:`memini.search.fusion`).
6. One read of the hits' chunks, records and sources, under the same conditions.
7. For a ranked mode with a :class:`~memini.search.model.Reranker`, the reranker reorders the top
   :attr:`~memini.search.model.SearchOptions.rerank_depth` hits, after the transaction.

Every mode refuses a stale index, not only those that embed the query. Principle 9 blocks search
on an index built with another fingerprint, and the fingerprint covers the chunker as well as the
model: after a change of the chunker, the stored chunks, their lines and the symbol table are the
old chunker's, and while a rebuild runs, records already rebuilt and records not yet rebuilt are
cut by different rules. ``memini reindex`` reuses every vector whose text is unchanged, so a
rebuild after a chunker change is short. For the same reason every retriever reads only chunks
of the embedding key the check approved: during a rebuild by another process the rows of the new
key are not part of the index that was checked.
"""

from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from typing import Any, Final
from uuid import UUID

from psycopg import AsyncConnection, errors, sql
from psycopg.pq import TransactionStatus
from psycopg.rows import TupleRow

from memini.auth import ReadFilter
from memini.embeddings import Embedder, Vector
from memini.index import IndexFingerprint, VectorIndex, check_index, current_fingerprint
from memini.search.errors import InvalidSearchError, SearchTimeoutError, UnsupportedFilterError
from memini.search.fusion import reciprocal_rank_fusion
from memini.search.model import (
    RANKED_MODES,
    Hit,
    Reranker,
    SearchFilters,
    SearchOptions,
    SearchQuery,
)
from memini.search.retrieval import (
    ChunkScope,
    LexicalStatement,
    Statement,
    chunk_rows,
    dense_statement,
    lexical_statement,
    match_statement,
    symbol_definition,
    symbol_statement,
)

__all__ = ["HNSW_MAX_EF_SEARCH", "HNSW_MIN_EF_SEARCH", "search"]

type Conn = AsyncConnection[TupleRow]

HNSW_MIN_EF_SEARCH: Final = 40
"""pgvector's default ``hnsw.ef_search``; the dense retriever never asks for less."""
HNSW_MAX_EF_SEARCH: Final = 1000
"""The most ``hnsw.ef_search`` pgvector takes."""


async def search(
    conn: Conn,
    embedder: Embedder,
    access: ReadFilter,
    query: SearchQuery,
    *,
    options: SearchOptions | None = None,
    reranker: Reranker | None = None,
    lexical: LexicalStatement = lexical_statement,
) -> list[Hit]:
    """Run ``query`` over what ``access`` lets the caller read. Best hits first, at most
    ``query.limit``.

    ``conn`` must have no transaction open: the search runs in one of its own and leaves the
    connection idle. ``embedder`` is the service's: its spec is what the index fingerprint is
    checked against in every mode, and it embeds the query in ``hybrid`` and ``dense``.
    ``lexical`` makes the statement of the lexical retriever (``hybrid`` and ``lexical``); the
    service always uses :func:`~memini.search.retrieval.lexical_statement`, the retrieval eval
    passes others to compare them (open question 1).

    Raises:
        UnsupportedFilterError: the ``persons`` filter is set.
        InvalidSearchError: the regular expression of a ``regex`` query is not valid.
        IndexMismatchError: the index of a workspace searched was built with another fingerprint;
            nothing in any workspace is searched until ``memini reindex`` rebuilds it.
        SearchTimeoutError: a statement ran longer than ``options.timeout``.
        EmbeddingError: the query could not be embedded.
    """
    options = options or SearchOptions()
    if conn.info.transaction_status != TransactionStatus.IDLE:
        raise RuntimeError("search runs in a transaction of its own; the connection is in one")
    if query.filters.persons is not None:
        raise UnsupportedFilterError(
            "the persons filter is not supported yet: records are linked to persons in milestone M2"
        )
    expected = current_fingerprint(embedder.spec)
    index = VectorIndex.of(embedder.spec)
    ranked = query.mode in RANKED_MODES
    depth = max(query.limit, options.rerank_depth) if ranked and reranker else query.limit
    vector: Vector | None = None
    try:
        if query.mode in ("hybrid", "dense"):
            async with _transaction(conn, options, ef_search=None):
                if await _scope(conn, access, query.filters, expected, index) is None:
                    return []
            (vector,) = await embedder.embed([query.text], kind="query")
        ef_search = None if vector is None else max(options.candidates, depth)
        async with _transaction(conn, options, ef_search=ef_search):
            scope = await _scope(conn, access, query.filters, expected, index)
            if scope is None:
                return []
            hits = await _retrieve(conn, scope, query, options, vector, depth, lexical)
    except errors.QueryCanceled as error:
        raise SearchTimeoutError(options.timeout) from error
    except errors.InvalidRegularExpression as error:
        reason = error.diag.message_primary or "invalid regular expression"
        raise InvalidSearchError(f"the regular expression is not usable: {reason}") from error
    if reranker is not None and ranked and hits:
        hits = await reranker.rerank(query.text, hits)
    return hits[: query.limit]


@asynccontextmanager
async def _transaction(
    conn: Conn, options: SearchOptions, *, ef_search: int | None
) -> AsyncGenerator[None]:
    """A read-only transaction at ``REPEATABLE READ`` with the search's limits."""
    settings: list[sql.Composable] = [
        sql.SQL("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"),
        sql.SQL("SET LOCAL statement_timeout = {}").format(
            sql.Literal(max(1, round(options.timeout * 1000)))
        ),
    ]
    if ef_search is not None:
        settings += [
            sql.SQL("SET LOCAL hnsw.ef_search = {}").format(
                sql.Literal(min(max(ef_search, HNSW_MIN_EF_SEARCH), HNSW_MAX_EF_SEARCH))
            ),
            sql.SQL("SET LOCAL hnsw.iterative_scan = relaxed_order"),
        ]
    async with conn.transaction():
        await conn.execute(sql.SQL("; ").join(settings))
        yield


async def _scope(
    conn: Conn,
    access: ReadFilter,
    filters: SearchFilters,
    expected: IndexFingerprint,
    index: VectorIndex,
) -> ChunkScope | None:
    """The chunks to search, or ``None`` if there are none. Checks every workspace's index."""
    workspaces = await _workspaces(conn, access, filters)
    indexed: dict[UUID, str] = {}
    for workspace_id, name in workspaces.items():
        if await check_index(conn, workspace_id, expected) is not None:
            indexed[workspace_id] = name
    if not indexed:
        return None
    source_ids = await _sources(conn, access, list(indexed), filters)
    if source_ids == ():
        return None
    return ChunkScope(
        access=access, workspaces=indexed, index=index, source_ids=source_ids, filters=filters
    )


def _names_and_ids(refs: Sequence[str | UUID]) -> tuple[list[str], list[UUID]]:
    return [ref for ref in refs if isinstance(ref, str)], [
        ref for ref in refs if isinstance(ref, UUID)
    ]


async def _workspaces(conn: Conn, access: ReadFilter, filters: SearchFilters) -> dict[UUID, str]:
    """The workspaces the caller may read and the filter names, by name."""
    params: dict[str, Any] = {}
    named = sql.SQL("")
    if filters.workspaces is not None:
        params["names"], params["ids"] = _names_and_ids(filters.workspaces)
        named = sql.SQL(" AND (w.name = ANY(%(names)s) OR w.id = ANY(%(ids)s))")
    query = sql.SQL("SELECT w.id, w.name FROM workspace w WHERE {access}{named} ORDER BY w.name")
    rows = await Statement(
        query.format(
            access=access.where(
                workspace_id=sql.Identifier("w", "id"), owner_id=sql.Identifier("w", "owner_id")
            ),
            named=named,
        ),
        params,
    ).rows(conn)
    return {row[0]: row[1] for row in rows}


async def _sources(
    conn: Conn, access: ReadFilter, workspace_ids: list[UUID], filters: SearchFilters
) -> tuple[UUID, ...] | None:
    """The ids of the sources and repositories the filters name; ``None`` without such filters.

    Both filters given: the sources that are in both.
    """
    chosen: list[set[UUID]] = []
    for refs, git_only in ((filters.sources, False), (filters.repositories, True)):
        if refs is None:
            continue
        names, ids = _names_and_ids(refs)
        query = sql.SQL(
            "SELECT s.id FROM source s WHERE {access} AND s.workspace_id = ANY(%(workspaces)s)"
            " AND (s.name = ANY(%(names)s) OR s.id = ANY(%(ids)s)){git}"
        ).format(
            access=access.where(
                workspace_id=sql.Identifier("s", "workspace_id"),
                owner_id=sql.Identifier("s", "owner_id"),
                source_id=sql.Identifier("s", "id"),
            ),
            git=sql.SQL(" AND s.kind = 'git'") if git_only else sql.SQL(""),
        )
        params = {"workspaces": workspace_ids, "names": names, "ids": ids}
        chosen.append({row[0] for row in await Statement(query, params).rows(conn)})
    if not chosen:
        return None
    common = chosen[0].intersection(*chosen[1:])
    return tuple(sorted(common))


async def _retrieve(
    conn: Conn,
    scope: ChunkScope,
    query: SearchQuery,
    options: SearchOptions,
    vector: Vector | None,
    depth: int,
    lexical_retriever: LexicalStatement,
) -> list[Hit]:
    """The hits of ``query`` in ``scope``, at most ``depth``, in the mode's order."""
    mode = query.mode
    if mode in ("exact", "regex"):
        statement = match_statement(
            scope, mode, query.text, case_sensitive=query.case_sensitive, limit=depth
        )
        found = await statement.rows(conn)
        rows = await chunk_rows(
            conn, scope, [row[0] for row in found], snippet_chars=options.snippet_chars
        )
        return [
            rows[chunk_id].hit(scope.workspaces[rows[chunk_id].workspace_id], match_line=line)
            for chunk_id, line in found
            if chunk_id in rows
        ]
    if mode == "symbol":
        statement = symbol_statement(
            scope,
            query.text,
            match=query.symbol_match,
            case_sensitive=query.case_sensitive,
            limit=depth,
        )
        found = await statement.rows(conn)
        rows = await chunk_rows(
            conn, scope, [row[0] for row in found], snippet_chars=options.snippet_chars
        )
        return [
            rows[row[0]].hit(
                scope.workspaces[rows[row[0]].workspace_id], definition=symbol_definition(row)
            )
            for row in found
            if row[0] in rows
        ]

    dense: list[tuple[Any, ...]] = []
    lexical: list[tuple[Any, ...]] = []
    candidates = depth if mode != "hybrid" else max(options.candidates, depth)
    if mode in ("hybrid", "dense"):
        if vector is None:
            raise RuntimeError(f"a {mode} search needs the query's vector")
        dense = await dense_statement(scope, vector, candidates).rows(conn)
    if mode in ("hybrid", "lexical"):
        lexical = await lexical_retriever(scope, query.text, candidates).rows(conn)

    fused = reciprocal_rank_fusion(
        [[row[0] for row in dense], [row[0] for row in lexical]],
        k=options.rrf_k,
        weights=(options.dense_weight, options.lexical_weight),
    )[:depth]
    distances = {row[0]: float(row[1]) for row in dense}
    lexical_scores = {row[0]: float(row[1]) for row in lexical}
    rows = await chunk_rows(conn, scope, [f.id for f in fused], snippet_chars=options.snippet_chars)
    hits: list[Hit] = []
    for item in fused:
        row = rows.get(item.id)
        if row is None:
            continue
        if mode == "hybrid":
            score = item.score
        elif mode == "dense":
            score = 1.0 - distances[item.id]  # cosine similarity
        else:
            score = lexical_scores[item.id]
        hits.append(
            row.hit(
                scope.workspaces[row.workspace_id],
                score=score,
                dense_rank=item.ranks[0],
                lexical_rank=item.ranks[1],
            )
        )
    return hits
