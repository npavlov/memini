"""The query tools: ``search``, ``search_code``, ``get_file`` and ``list_commits``.

The library behind the MCP tools (:mod:`memini.mcp.query`) and their HTTP routes
(:mod:`memini.api.query`), independent of both:

- :mod:`memini.query.model`: the arguments and results, the same for MCP and HTTP.
- :mod:`memini.query.service`: one function per tool over :func:`memini.search.search`,
  :func:`~memini.query.reader.get_file` and :func:`~memini.query.commits.list_commits`;
  :class:`QueryServices`, what they need from the service; :class:`QueryError`, every refusal
  with an HTTP status, a code and a safe message.
- :mod:`memini.query.reader`: :func:`~memini.query.reader.get_file`, a file, a line range or a
  directory from a repository's mirror, at a pinned commit, a ref or a commit.
- :mod:`memini.query.commits`: :func:`~memini.query.commits.list_commits`, the history of
  repositories read from their mirrors on demand; nothing of it is stored.
- :mod:`memini.query.context`: the ``context`` of a code hit, a definition and a read: the
  project, service and domains a file belongs to, computed from the catalog at query time.

Every read goes through the caller's :class:`~memini.auth.ReadFilter`. Results carry their
source kind, locator and time; their text is data, never instructions (principles 3 and 11).
"""

from memini.query.commits import (
    DEFAULT_COMMIT_LIMITS,
    CommitAuthor,
    CommitInfo,
    CommitLimits,
    CommitList,
    CommitScope,
    CommitsError,
    CommitsTimeoutError,
    CommitsTooLargeError,
    PathChange,
    SourceWalk,
    WalkHead,
    list_commits,
    repository_scopes,
)
from memini.query.context import CodeContext, DomainRef, code_contexts
from memini.query.model import (
    CodeSearchArguments,
    GetFileArguments,
    GetFileQuery,
    ListCommitsArguments,
    SearchArguments,
    SearchHit,
    SearchResult,
    SymbolInfo,
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
    get_file,
)
from memini.query.service import (
    QueryError,
    QueryServices,
    query_errors,
    run_get_file,
    run_list_commits,
    run_search,
    run_search_code,
)

__all__ = [
    "DEFAULT_COMMIT_LIMITS",
    "DEFAULT_READ_LIMITS",
    "CodeContext",
    "CodeSearchArguments",
    "CommitAuthor",
    "CommitInfo",
    "CommitLimits",
    "CommitList",
    "CommitScope",
    "CommitsError",
    "CommitsTimeoutError",
    "CommitsTooLargeError",
    "DirectoryEntry",
    "DomainRef",
    "FileTooLargeError",
    "GetFileArguments",
    "GetFileQuery",
    "InvalidReadError",
    "ListCommitsArguments",
    "NotSyncedError",
    "PathChange",
    "QueryError",
    "QueryServices",
    "ReadError",
    "ReadLimits",
    "SearchArguments",
    "SearchHit",
    "SearchResult",
    "SourceRead",
    "SourceWalk",
    "SymbolInfo",
    "SymbolSpan",
    "WalkHead",
    "code_contexts",
    "code_locator",
    "get_file",
    "list_commits",
    "query_errors",
    "repository_scopes",
    "run_get_file",
    "run_list_commits",
    "run_search",
    "run_search_code",
]
