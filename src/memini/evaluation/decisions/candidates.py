"""The candidates the decision eval shows a supersession check: every active item, or the top k
as the ``extract`` job finds them (``memini eval decisions --candidates``).

By default the replay shows every active item of the set: the sets are small, and the classifier
is measured on its own. :class:`RankedCandidates` shows what the job would: the items already
taken from the record being extracted (the job's pending items), and the ``limit`` best of the
others by :func:`memini.extraction.candidates.rank_candidates`, the job's own ranking:

- ``lexical``: ``ts_rank`` of each statement against the words of the new one, computed by
  Postgres with the job's expression (:func:`memini.extraction.candidates.lexical_rank`) in a
  scratch database of its own on the server of the settings' ``database_url`` (the role needs
  ``CREATEDB``), dropped at the end;
- ``dense``: the cosine similarity of the statements' vectors under the embedding provider of
  the settings, embedded as documents as the job embeds them, each statement once;
- ``hybrid``: both, fused as the job fuses them.

The report's ``reached`` counts, per case, whether the items its expected verdict names were
among the candidates shown: with ``--candidates`` it measures the retrieval as well.
"""

from collections.abc import Mapping, Sequence
from contextlib import AsyncExitStack
from typing import Any, Final, Literal, get_args

import anyio
from psycopg import AsyncConnection, sql

from memini.config import Settings
from memini.db import connect, migrate
from memini.embeddings import Embedder, EmbeddingsNotConfiguredError, Vector, create_embedder
from memini.embeddings.reuse import embed_missing
from memini.evaluation.decisions.runner import DecisionEvalError
from memini.evaluation.runner import scratch_database
from memini.extraction import Candidate, ProposedItem, RecordInput
from memini.extraction.candidates import Scored, lexical_rank, rank_candidates

__all__ = ["SELECTIONS", "RankedCandidates", "Selection", "create_candidates"]

Selection = Literal["all", "lexical", "dense", "hybrid"]
SELECTIONS: Final[tuple[Selection, ...]] = get_args(Selection)


class RankedCandidates:
    """The top ``limit`` active items by ``retriever`` (see the module)."""

    def __init__(
        self,
        retriever: Literal["lexical", "dense", "hybrid"],
        limit: int,
        *,
        server_url: str,
        embedder: Embedder | None = None,
    ) -> None:
        if limit < 1:
            raise DecisionEvalError("the candidate limit is at least 1")
        if retriever != "lexical" and embedder is None:
            raise DecisionEvalError(f"{retriever} candidates need an embedding provider")
        self.retriever = retriever
        self.limit = limit
        self._server_url = server_url
        self._embedder = embedder if retriever != "lexical" else None
        self._vectors: dict[str, Vector] = {}
        self._stack = AsyncExitStack()
        self._lock = anyio.Lock()
        self._conn: AsyncConnection[Any] | None = None

    def describe(self) -> dict[str, Any]:
        """What the report's setup records."""
        found: dict[str, Any] = {"retriever": self.retriever, "limit": self.limit}
        if self._embedder is not None:
            found["embedding"] = self._embedder.spec.describe()
        return found

    async def _db(self) -> AsyncConnection[Any]:
        async with self._lock:
            if self._conn is None:
                _, url = await self._stack.enter_async_context(scratch_database(self._server_url))
                await migrate(url)
                self._conn = await self._stack.enter_async_context(connect(url, autocommit=True))
            return self._conn

    async def _lexical(self, statement: str, others: Sequence[str]) -> list[float]:
        if self.retriever == "dense":
            return [0.0] * len(others)
        conn = await self._db()
        params: list[Any] = []
        rank = lexical_rank(sql.SQL("t.statement"), statement, params)
        query = sql.SQL(
            "SELECT {} FROM unnest(%s::text[]) WITH ORDINALITY AS t(statement, n) ORDER BY t.n"
        ).format(rank)
        cur = await conn.execute(query, [*params, list(others)])
        return [float(row[0]) for row in await cur.fetchall()]

    async def _similarity(self, statement: str, others: Sequence[str]) -> list[float | None]:
        if self._embedder is None:
            return [None] * len(others)

        async def lookup(hashes: Sequence[str]) -> Mapping[str, Vector]:
            return {h: self._vectors[h] for h in hashes if h in self._vectors}

        embedded = await embed_missing(self._embedder, [statement, *others], lookup)
        self._vectors.update(embedded.vectors)
        mine = embedded.vector(statement)
        return [
            sum(a * b for a, b in zip(mine, embedded.vector(text), strict=True)) for text in others
        ]

    async def select(
        self, item: ProposedItem, record: RecordInput, others: Sequence[Candidate]
    ) -> list[Candidate]:
        """The ``limit`` of ``others`` the job would show with ``item``, best first."""
        del record  # the items of its record are shown anyway, as the job's pending items
        if not others:
            return []
        statements = [each.statement for each in others]
        lexical = await self._lexical(item.statement, statements)
        similarity = await self._similarity(item.statement, statements)
        scored = [
            Scored(id=each.id, decided_at=each.decided_at, lexical=lex, similarity=sim)
            for each, lex, sim in zip(others, lexical, similarity, strict=True)
        ]
        by_id = {each.id: each for each in others}
        return [by_id[each] for each in rank_candidates(scored, self.limit)]

    async def aclose(self) -> None:
        try:
            await self._stack.aclose()
        finally:
            if self._embedder is not None:
                await self._embedder.aclose()


def create_candidates(
    selection: Selection, limit: int, settings: Settings | None
) -> RankedCandidates | None:
    """The candidates of ``selection``: ``None`` for every active item.

    Raises:
        DecisionEvalError: no settings, or no embedding provider for ``dense`` or ``hybrid``.
    """
    if selection == "all":
        return None
    if settings is None:
        raise DecisionEvalError("ranked candidates need the service's settings")
    embedder: Embedder | None = None
    if selection != "lexical":
        try:
            embedder = create_embedder(settings)
        except EmbeddingsNotConfiguredError as error:
            raise DecisionEvalError(f"{selection} candidates: {error}") from None
    return RankedCandidates(
        selection,
        limit,
        server_url=settings.database_url.get_secret_value(),
        embedder=embedder,
    )
