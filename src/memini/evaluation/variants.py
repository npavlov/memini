"""What the eval can swap: the lexical retriever and the reranker, each by name.

The service has one lexical retriever (:data:`memini.search.retrieval.lexical_statement`) and an
optional reranker (:mod:`memini.rerank`, on in the image, off by default elsewhere). Open question
1 (which lexical index in Postgres) and the reranker's default were settled by measuring
alternatives on the same suite, so the eval takes them by name (``memini eval --lexical NAME
--reranker NAME``) from the registries here, and records the name in its report.

Adding a lexical variant: write a function of the shape of
:data:`~memini.search.retrieval.LexicalStatement` (the scope, the query text and a limit; rows of
``(chunk id, score)``, best first, under :func:`~memini.search.retrieval.chunk_conditions`) and
add a :class:`LexicalVariant` to :data:`LEXICAL_VARIANTS`. A variant that needs its own index or
column (a ``tsvector`` of another configuration, a BM25 index of ``pg_textsearch``) creates it in
``prepare``, which runs once on the eval's scratch database after the fixtures are indexed and
before the first question; the service's own schema is not touched until the variant wins and
becomes a migration.

The variants here are those open question 1 measured (docs/requirements.md, section 9, Lexical
search), kept so that a change of the suite, the chunker or the model can measure them again. The
``bm25-textsearch-*`` ones need a server that loads the ``pg_textsearch`` extension
(``shared_preload_libraries``), which the stock pgvector image does not have.

Adding a reranker: an object with the :class:`ModelReranker` protocol, made from the settings by
``create``, in a :class:`RerankerVariant` in :data:`RERANKERS`. The eval loads it before the
first question, so a missing model stops the run instead of falling back to the fused order, and
gives the local one :data:`EVAL_RERANK_TIMEOUT` per search rather than the service's
``search_reranker_timeout``, so a slow machine does not fall back either.
"""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Final, Protocol

from psycopg import AsyncConnection, errors, sql
from psycopg.rows import TupleRow

from memini.config import Settings
from memini.rerank import local_reranker
from memini.search import Reranker
from memini.search.patterns import lexical_terms
from memini.search.retrieval import (
    ChunkScope,
    LexicalStatement,
    Params,
    Statement,
    TsvectorRetriever,
    chunk_conditions,
    lexical_statement,
    split_identifiers,
)

__all__ = [
    "DEFAULT_LEXICAL",
    "DEFAULT_RERANKER",
    "EVAL_RERANK_TIMEOUT",
    "LEXICAL_VARIANTS",
    "RERANKERS",
    "LexicalVariant",
    "ModelReranker",
    "RerankerVariant",
    "TextsearchRetriever",
]

type Conn = AsyncConnection[TupleRow]


@dataclass(frozen=True, slots=True)
class LexicalVariant:
    """A lexical retriever the eval can measure."""

    name: str
    description: str
    statement: LexicalStatement
    prepare: Callable[[Conn], Awaitable[None]] | None = None
    """Runs once on the scratch database (autocommit) after indexing: indexes or columns the
    variant reads that the service's schema does not have."""


class ModelReranker(Reranker, Protocol):
    """A :class:`~memini.search.Reranker` with a model to load first and close after."""

    def describe(self) -> str:
        """The model and what else decides its scores, in one line, for the report."""
        ...

    async def load(self) -> None: ...

    async def aclose(self) -> None: ...


@dataclass(frozen=True, slots=True)
class RerankerVariant:
    """A reranker the eval can measure; ``create`` is ``None`` for no reranking."""

    name: str
    description: str
    create: Callable[[Settings], ModelReranker] | None = None


DEFAULT_LEXICAL: Final = "tsvector-russian-split"
DEFAULT_RERANKER: Final = "none"

EVAL_RERANK_TIMEOUT: Final = 600.0
"""Seconds the eval's local reranker may take per search. A reranking cut short by the service's
``search_reranker_timeout`` keeps the fused order with a warning, which would lower the metrics
without an error: the eval measures quality, the latency budget is the service's."""

_CHUNK_TEXT: Final = sql.SQL("header || E'\\n' || content")
"""What the lexical column is made of, as migration 0010 has it."""


def _tsvector_variant(name: str, description: str, retriever: TsvectorRetriever) -> LexicalVariant:
    """A variant over a column of its own: added to ``chunk``, generated from the header and
    content as ``retriever`` parses them, with a GIN index, on the scratch database."""

    async def prepare(conn: Conn) -> None:
        column = sql.Identifier(retriever.column)
        await conn.execute(
            sql.SQL(
                "ALTER TABLE chunk ADD COLUMN {} tsvector GENERATED ALWAYS AS ({}) STORED"
            ).format(column, retriever.document(_CHUNK_TEXT))
        )
        await conn.execute(sql.SQL("CREATE INDEX ON chunk USING gin ({})").format(column))
        await conn.execute("ANALYZE chunk")

    return LexicalVariant(name=name, description=description, statement=retriever, prepare=prepare)


@dataclass(frozen=True, slots=True)
class TextsearchRetriever:
    """BM25 through the ``pg_textsearch`` extension (Timescale, PostgreSQL License): an index
    ``USING bm25`` on the chunk's header and content, scored with ``<@>``.

    The extension must be installed on the server and loaded through
    ``shared_preload_libraries``; :meth:`prepare` creates it in the scratch database. Its
    standalone scoring gives 0 to a chunk without any word of the query, so those are left out.
    """

    index: str
    config: str
    split: bool
    """Whether identifiers are cut into words (:func:`split_identifiers`) in the indexed text and
    in the query."""

    def _text(self, text: sql.Composable) -> sql.Composable:
        return split_identifiers(text) if self.split else sql.SQL("({})").format(text)

    async def prepare(self, conn: Conn) -> None:
        try:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS pg_textsearch")
        except errors.FeatureNotSupported:  # not installed on the server
            raise RuntimeError(
                "the bm25-textsearch variants need a server with the pg_textsearch extension"
                " installed and in shared_preload_libraries"
            ) from None
        await conn.execute(
            sql.SQL("CREATE INDEX {} ON chunk USING bm25 (({})) WITH (text_config = {})").format(
                sql.Identifier(self.index), self._text(_CHUNK_TEXT), sql.Literal(self.config)
            )
        )

    def __call__(self, scope: ChunkScope, text: str, limit: int) -> Statement:
        param = Params()
        where = chunk_conditions(scope, param)
        score = sql.SQL("({}) <@> to_bm25query({}, {})").format(
            self._text(sql.SQL("c.header || E'\\n' || c.content")),
            self._text(param(" ".join(lexical_terms(text)))),
            sql.Literal(self.index),
        )
        query = sql.SQL(
            "SELECT id, -score FROM (SELECT c.id, {score} AS score FROM {from_} WHERE {where})"
            " AS scored WHERE score < 0 ORDER BY score, id LIMIT {limit}"
        ).format(
            score=score,
            from_=sql.SQL("chunk c JOIN record r ON r.id = c.record_id"),
            where=where,
            limit=sql.Literal(limit),
        )
        return Statement(query, param.values)


def _textsearch_variant(
    name: str, description: str, retriever: TextsearchRetriever
) -> LexicalVariant:
    return LexicalVariant(
        name=name, description=description, statement=retriever, prepare=retriever.prepare
    )


def _registry(*variants: LexicalVariant) -> dict[str, LexicalVariant]:
    return {variant.name: variant for variant in variants}


LEXICAL_VARIANTS: Final[dict[str, LexicalVariant]] = _registry(
    LexicalVariant(
        name=DEFAULT_LEXICAL,
        description="the service's: any word of the query in the 'russian' tsvector of the chunk,"
        " identifiers split, ranked by ts_rank (migration 0010)",
        statement=lexical_statement,
    ),
    _tsvector_variant(
        "tsvector-simple",
        "the service's before open question 1 (migration 0005): the 'simple' configuration, no"
        " stemming, ranked by ts_rank_cd",
        TsvectorRetriever(
            column="lexical_simple", configs=("simple",), split=False, rank="ts_rank_cd"
        ),
    ),
    _tsvector_variant(
        "tsvector-simple-split",
        "'simple', identifiers split, ts_rank_cd",
        TsvectorRetriever(
            column="lexical_simple_split", configs=("simple",), split=True, rank="ts_rank_cd"
        ),
    ),
    _tsvector_variant(
        "tsvector-english",
        "'english' (Cyrillic words unstemmed), ts_rank_cd",
        TsvectorRetriever(
            column="lexical_english", configs=("english",), split=False, rank="ts_rank_cd"
        ),
    ),
    _tsvector_variant(
        "tsvector-russian",
        "'russian' (English stemmer for ASCII words), ts_rank_cd",
        TsvectorRetriever(
            column="lexical_russian", configs=("russian",), split=False, rank="ts_rank_cd"
        ),
    ),
    _tsvector_variant(
        "tsvector-english-russian",
        "'english' and 'russian' concatenated, ts_rank_cd",
        TsvectorRetriever(
            column="lexical_english_russian",
            configs=("english", "russian"),
            split=False,
            rank="ts_rank_cd",
        ),
    ),
    _tsvector_variant(
        "tsvector-simple-russian-split",
        "'simple' and 'russian' concatenated, identifiers split, ts_rank_cd",
        TsvectorRetriever(
            column="lexical_simple_russian_split",
            configs=("simple", "russian"),
            split=True,
            rank="ts_rank_cd",
        ),
    ),
    _tsvector_variant(
        "tsvector-russian-split-cd",
        "the service's column, ranked by ts_rank_cd",
        TsvectorRetriever(
            column="lexical_russian_split_cd", configs=("russian",), split=True, rank="ts_rank_cd"
        ),
    ),
    _tsvector_variant(
        "tsvector-russian-split-n1",
        "the service's column, ranked by ts_rank divided by 1 + the log of the chunk's length",
        TsvectorRetriever(
            column="lexical_russian_split_n1",
            configs=("russian",),
            split=True,
            rank="ts_rank",
            normalization=1,
        ),
    ),
    _textsearch_variant(
        "bm25-textsearch-simple",
        "BM25 of pg_textsearch, 'simple'",
        TextsearchRetriever(index="chunk_bm25_simple", config="simple", split=False),
    ),
    _textsearch_variant(
        "bm25-textsearch-russian",
        "BM25 of pg_textsearch, 'russian'",
        TextsearchRetriever(index="chunk_bm25_russian", config="russian", split=False),
    ),
    _textsearch_variant(
        "bm25-textsearch-russian-split",
        "BM25 of pg_textsearch, 'russian', identifiers split",
        TextsearchRetriever(index="chunk_bm25_russian_split", config="russian", split=True),
    ),
)
"""Lexical retrievers by name. The default is the service's."""

RERANKERS: Final[dict[str, RerankerVariant]] = {
    DEFAULT_RERANKER: RerankerVariant(name=DEFAULT_RERANKER, description="fused order, no rerank"),
    "local": RerankerVariant(
        name="local",
        description="the local cross-encoder of the search_reranker_* settings, by default"
        " Qwen3-Reranker 0.6B: the best search_rerank_depth hits in the order of its relevance",
        create=lambda settings: local_reranker(settings, timeout=EVAL_RERANK_TIMEOUT),
    ),
}
"""Rerankers by name. The default, none, is the fused order: the service's outside the image."""


def names(registry: Mapping[str, object]) -> str:
    """The names of a registry, for messages."""
    return ", ".join(sorted(registry))
