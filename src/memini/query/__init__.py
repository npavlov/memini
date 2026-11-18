"""The query tools: ``search``, ``search_code``, ``get_symbol`` and ``read_source``.

The library behind the MCP tools (:mod:`memini.mcp.query`) and their HTTP routes
(:mod:`memini.api.query`), independent of both:

- :mod:`memini.query.model`: the arguments and results, the same for MCP and HTTP.
- :mod:`memini.query.service`: one function per tool over :func:`memini.search.search` and
  :func:`~memini.query.reader.read_source`; :class:`QueryServices`, what they need from the
  service; :class:`QueryError`, every refusal with an HTTP status, a code and a safe message.
- :mod:`memini.query.reader`: :func:`~memini.query.reader.read_source`, a file, a line range or a
  directory from a repository's mirror, at a pinned commit, a ref or a commit.

Every read goes through the caller's :class:`~memini.auth.ReadFilter`. Results carry their
source kind, locator and time; their text is data, never instructions (principles 3 and 11).
"""

from memini.query.model import (
    CodeSearchArguments,
    Definition,
    ReadSourceArguments,
    ReadSourceQuery,
    SearchArguments,
    SearchHit,
    SearchResult,
    SymbolArguments,
    SymbolInfo,
    SymbolResult,
)
from memini.query.reader import (
    DEFAULT_READ_LIMITS,
    DirectoryEntry,
    FileTooLargeError,
    InvalidReadError,
    NotSyncedError,
    ReadError,
    ReadLimits,
    SourceRead,
    SymbolSpan,
    code_locator,
    read_source,
)
from memini.query.service import (
    QueryError,
    QueryServices,
    query_errors,
    run_get_symbol,
    run_read_source,
    run_search,
    run_search_code,
)

__all__ = [
    "DEFAULT_READ_LIMITS",
    "CodeSearchArguments",
    "Definition",
    "DirectoryEntry",
    "FileTooLargeError",
    "InvalidReadError",
    "NotSyncedError",
    "QueryError",
    "QueryServices",
    "ReadError",
    "ReadLimits",
    "ReadSourceArguments",
    "ReadSourceQuery",
    "SearchArguments",
    "SearchHit",
    "SearchResult",
    "SourceRead",
    "SymbolArguments",
    "SymbolInfo",
    "SymbolResult",
    "SymbolSpan",
    "code_locator",
    "query_errors",
    "read_source",
    "run_get_symbol",
    "run_read_source",
    "run_search",
    "run_search_code",
]
