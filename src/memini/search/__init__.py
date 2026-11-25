"""Search over the index: hybrid retrieval for questions, exact, regex and symbol search for code.

One entry point, transport-free, for the MCP tools and the HTTP API::

    access = authorize_read(principal)          # the token's limits; every read goes through it
    hits = await search(conn, embedder, access, SearchQuery(text="...", mode="hybrid"))

- :func:`search` (:mod:`memini.search.engine`): checks the index fingerprint of every workspace
  searched (principle 9), runs the retrievers of the mode in one read-only snapshot under a
  statement timeout, fuses, and returns :class:`Hit` objects.
- :mod:`memini.search.model`: :class:`SearchQuery` (text, :data:`Mode`, limit, options of the
  mode), :class:`SearchFilters`, :class:`SearchOptions` (from the ``search_*`` settings),
  :class:`Hit`, and the :class:`Reranker` hook.
- :mod:`memini.search.retrieval`: the SQL of each retriever. :mod:`memini.search.fusion`:
  reciprocal rank fusion. :mod:`memini.search.patterns`: globs, LIKE patterns, line-mode regexes.
- :mod:`memini.search.catalog`: the ``projects``, ``domains`` and ``services`` filters as sources
  and path masks, and what a file belongs to; both computed from the catalog at query time.

Modes: ``hybrid`` (dense and lexical, fused; the default), ``dense``, ``lexical``, ``exact``,
``regex``, ``symbol``. The ``search_code`` tool's "semantic" mode is ``hybrid`` limited to code
records: identifiers in a question are found by the lexical retriever, meaning by the dense one.
"""

from memini.index import IndexMismatchError
from memini.search.engine import search
from memini.search.errors import (
    InvalidSearchError,
    SearchError,
    SearchTimeoutError,
    UnknownNameError,
    UnsupportedFilterError,
)
from memini.search.fusion import Fused, reciprocal_rank_fusion
from memini.search.model import (
    MAX_LIMIT,
    MODES,
    RANKED_MODES,
    Hit,
    Mode,
    Reranker,
    SearchFilters,
    SearchOptions,
    SearchQuery,
    SourceKind,
    SymbolDefinition,
    SymbolMatch,
)
from memini.search.patterns import glob_regex
from memini.search.retrieval import pinned_commit

__all__ = [
    "MAX_LIMIT",
    "MODES",
    "RANKED_MODES",
    "Fused",
    "Hit",
    "IndexMismatchError",
    "InvalidSearchError",
    "Mode",
    "Reranker",
    "SearchError",
    "SearchFilters",
    "SearchOptions",
    "SearchQuery",
    "SearchTimeoutError",
    "SourceKind",
    "SymbolDefinition",
    "SymbolMatch",
    "UnknownNameError",
    "UnsupportedFilterError",
    "glob_regex",
    "pinned_commit",
    "reciprocal_rank_fusion",
    "search",
]
