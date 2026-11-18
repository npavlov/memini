"""What the eval can swap: the lexical retriever and the reranker, each by name.

The service has one lexical retriever (:func:`memini.search.retrieval.lexical_statement`) and no
reranker yet. Open question 1 (which BM25 in Postgres) and the optional reranker (memini-2ee) are
settled by measuring alternatives on the same suite, so the eval takes them by name
(``memini eval --lexical NAME --reranker NAME``) from the registries here, and records the name
in its report.

Adding a lexical variant: write a function of the shape of
:data:`~memini.search.retrieval.LexicalStatement` (the scope, the query text and a limit; rows of
``(chunk id, score)``, best first, under :func:`~memini.search.retrieval.chunk_conditions`) and
add a :class:`LexicalVariant` to :data:`LEXICAL_VARIANTS`. A variant that needs its own index or
column (a ``tsvector`` per language, a BM25 index of ``pg_textsearch``) creates it in ``prepare``,
which runs once on the eval's scratch database after the fixtures are indexed and before the first
question; the service's own schema is not touched until the variant wins and becomes a migration.

Adding a reranker: an object with the :class:`~memini.search.Reranker` protocol, made from the
settings by ``create``, in a :class:`RerankerVariant` in :data:`RERANKERS`.
"""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Final

from psycopg import AsyncConnection
from psycopg.rows import TupleRow

from memini.config import Settings
from memini.search import Reranker
from memini.search.retrieval import LexicalStatement, lexical_statement

__all__ = [
    "DEFAULT_LEXICAL",
    "DEFAULT_RERANKER",
    "LEXICAL_VARIANTS",
    "RERANKERS",
    "LexicalVariant",
    "RerankerVariant",
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


@dataclass(frozen=True, slots=True)
class RerankerVariant:
    """A reranker the eval can measure; ``create`` is ``None`` for no reranking."""

    name: str
    description: str
    create: Callable[[Settings], Reranker] | None = None


DEFAULT_LEXICAL: Final = "tsvector-simple"
DEFAULT_RERANKER: Final = "none"

LEXICAL_VARIANTS: Final[dict[str, LexicalVariant]] = {
    DEFAULT_LEXICAL: LexicalVariant(
        name=DEFAULT_LEXICAL,
        description="the service's: any word of the query in the 'simple' tsvector of the chunk,"
        " ranked by ts_rank_cd (the baseline of open question 1)",
        statement=lexical_statement,
    ),
}
"""Lexical retrievers by name. The default is the service's."""

RERANKERS: Final[dict[str, RerankerVariant]] = {
    DEFAULT_RERANKER: RerankerVariant(name=DEFAULT_RERANKER, description="fused order, no rerank"),
}
"""Rerankers by name. The default is the service's: none."""


def names(registry: Mapping[str, object]) -> str:
    """The names of a registry, for messages."""
    return ", ".join(sorted(registry))
