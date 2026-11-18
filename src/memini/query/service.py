"""The query tools as functions: from validated arguments to a result, or a :class:`QueryError`.

One function per tool, for the MCP tools (:mod:`memini.mcp.query`) and the HTTP routes
(:mod:`memini.api.query`) alike: :func:`run_search`, :func:`run_search_code`,
:func:`run_get_symbol`, :func:`run_read_source`. Each takes a connection with no transaction open,
the service's :class:`QueryServices`, the caller's :class:`~memini.auth.ReadFilter` and the
arguments, and calls the library: :func:`memini.search.search` or
:func:`memini.query.reader.read_source`.

Every refusal of the libraries becomes a :class:`QueryError` with an HTTP status, a stable code and
a message that never repeats an argument or stored content:

| Error | Status | Code |
|---|---|---|
| a query, filter or regular expression that is not valid | 422 | ``invalid_search`` |
| a filter not supported yet | 422 | ``unsupported_filter`` |
| a statement ran past ``search_timeout`` | 422 | ``search_timeout`` |
| a path that is refused (absolute, ``..``, control characters) | 422 | ``invalid_path`` |
| a ref that is refused (a revision expression) | 422 | ``invalid_ref`` |
| a line range that is not valid | 422 | ``invalid_range`` |
| a repository name that several workspaces have | 422 | ``ambiguous_repository`` |
| a file over ``git_max_blob_size`` | 422 | ``file_too_large`` |
| no such workspace, repository, ref, commit or path the caller may read | 404 | ``not_found`` |
| the repository has no mirror yet | 409 | ``not_synced`` |
| an index built with another fingerprint (principle 9) | 503 | ``index_mismatch`` |
| no embedding provider configured | 503 | ``embeddings_not_configured`` |
| the query could not be embedded | 503 | ``embedding_failed`` |
| no mirror directory (code cannot be read) | 503 | ``unavailable`` |

A timeout is the query's to fix (narrow it, or a more specific pattern), so it is a 422, not a
server error. A stale index and a missing provider are the operator's: the message of a stale index
names ``memini reindex --workspace NAME``.

Embeddings: every search mode checks the index fingerprint, and the fingerprint is made from the
embedder's spec, so ``search``, ``search_code`` and ``get_symbol`` need the configured embedder in
every mode, also exact, regex and symbol. Without an embedding provider no index is built either
(code is synced only into an index), so there would be nothing to find. ``read_source`` reads the
mirror and needs none.
"""

import logging
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field

from psycopg import AsyncConnection
from psycopg.rows import TupleRow

from memini.auth import ReadFilter
from memini.config import Settings
from memini.embeddings import Embedder, EmbeddingError, EmbeddingsNotConfiguredError
from memini.git import GitOptions, MirrorManager
from memini.ingest import DEFAULT_LIMITS, IngestLimits
from memini.persons import parse_person_ref
from memini.query.model import (
    CodeSearchArguments,
    Definition,
    ReadSourceArguments,
    SearchArguments,
    SearchHit,
    SearchResult,
    SymbolArguments,
    SymbolResult,
)
from memini.query.reader import (
    DEFAULT_READ_LIMITS,
    FileTooLargeError,
    InvalidReadError,
    NotSyncedError,
    ReadLimits,
    SourceRead,
    read_source,
)
from memini.search import (
    Hit,
    IndexMismatchError,
    InvalidSearchError,
    Mode,
    Reranker,
    SearchFilters,
    SearchOptions,
    SearchQuery,
    SearchTimeoutError,
    UnsupportedFilterError,
    search,
)
from memini.workspaces import NotFoundError

__all__ = [
    "QueryError",
    "QueryServices",
    "query_errors",
    "run_get_symbol",
    "run_read_source",
    "run_search",
    "run_search_code",
]

type Conn = AsyncConnection[TupleRow]

log = logging.getLogger(__name__)


@dataclass(slots=True)
class QueryServices:
    """What the query tools need besides a connection and the caller: the search options, the
    repository mirrors to read code from, the embedder and the reranker.

    The application builds one at start (:meth:`from_settings`) and sets :attr:`embedder` and
    :attr:`reranker` while it runs; tests build one directly.
    """

    search_options: SearchOptions = field(default_factory=SearchOptions)
    mirrors: MirrorManager | None = None
    """``None``: code cannot be read (no mirror directory)."""
    embedder: Embedder | None = None
    """The service's embedder; ``None`` when no embedding provider is configured, or the
    application is not running."""
    read_limits: ReadLimits = DEFAULT_READ_LIMITS
    ingest_limits: IngestLimits = DEFAULT_LIMITS
    """The limits of a batch, for the ``add_document`` write (:mod:`memini.comms`)."""
    reranker: Reranker | None = None
    """Reorders the best hits of a ranked search; ``None``: the fused order (the default), or the
    reranker is not loaded yet."""

    @classmethod
    def from_settings(cls, settings: Settings) -> "QueryServices":
        """Options, mirrors and limits from ``settings``; the embedder is set later. Reads
        nothing yet."""
        return cls(
            search_options=SearchOptions.from_settings(settings),
            mirrors=MirrorManager(GitOptions.from_settings(settings)),
            ingest_limits=IngestLimits.from_settings(settings),
        )


class QueryError(Exception):
    """A query that cannot be answered: an HTTP status, a stable code and a message for the
    caller. The message never holds an argument, stored content or a secret."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _sentence(text: str) -> str:
    """A library message as a sentence: capital first letter, a full stop at the end."""
    text = text[:1].upper() + text[1:]
    return text if text.endswith(".") else text + "."


@contextmanager
def query_errors() -> Generator[None]:
    """Turn a refusal of the search or read library into a :class:`QueryError`; see the module."""
    try:
        yield
    except UnsupportedFilterError as error:
        raise QueryError(422, "unsupported_filter", _sentence(str(error))) from None
    except InvalidSearchError as error:
        raise QueryError(422, "invalid_search", _sentence(str(error))) from None
    except SearchTimeoutError as error:
        raise QueryError(422, "search_timeout", _sentence(str(error))) from None
    except IndexMismatchError as error:
        raise QueryError(503, "index_mismatch", _sentence(str(error))) from None
    except EmbeddingsNotConfiguredError:
        raise QueryError(
            503,
            "embeddings_not_configured",
            "Search needs an embedding provider, and none is configured: set"
            " MEMINI_EMBEDDING_PROVIDER. read_source works without one.",
        ) from None
    except EmbeddingError as error:
        log.warning("a search query could not be embedded: %s", error)
        raise QueryError(
            503, "embedding_failed", "The query could not be embedded; the server log says why."
        ) from None
    except InvalidReadError as error:
        raise QueryError(422, error.code, _sentence(str(error))) from None
    except FileTooLargeError as error:
        raise QueryError(422, "file_too_large", _sentence(str(error))) from None
    except NotSyncedError as error:
        raise QueryError(409, "not_synced", _sentence(str(error))) from None
    except NotFoundError as error:
        raise QueryError(404, "not_found", f"No such {error.what}.") from None


def _names(values: Sequence[str] | None) -> tuple[str, ...] | None:
    return None if values is None else tuple(values)


async def _search(
    conn: Conn, services: QueryServices, access: ReadFilter, query: SearchQuery
) -> list[Hit]:
    embedder = services.embedder
    if embedder is None:
        raise EmbeddingsNotConfiguredError
    return await search(
        conn,
        embedder,
        access,
        query,
        options=services.search_options,
        reranker=services.reranker,
    )


async def run_search(
    conn: Conn, services: QueryServices, access: ReadFilter, arguments: SearchArguments
) -> SearchResult:
    """``search``: ranked search over every kind of record the caller may read."""
    with query_errors():
        query = SearchQuery(
            text=arguments.query,
            mode=arguments.mode,
            limit=arguments.limit,
            filters=SearchFilters(
                workspaces=_names(arguments.workspaces),
                sources=_names(arguments.sources),
                repositories=_names(arguments.repositories),
                kinds=None if arguments.kinds is None else tuple(arguments.kinds),
                since=arguments.since,
                until=arguments.until,
                persons=None
                if arguments.persons is None
                else tuple(parse_person_ref(person) for person in arguments.persons),
                roles=None if arguments.roles is None else tuple(arguments.roles),
            ),
        )
        hits = await _search(conn, services, access, query)
    return SearchResult(hits=[SearchHit.of(hit) for hit in hits])


_CODE_MODES: dict[str, Mode] = {
    "semantic": "hybrid",
    "exact": "exact",
    "regex": "regex",
    "symbol": "symbol",
}


async def run_search_code(
    conn: Conn, services: QueryServices, access: ReadFilter, arguments: CodeSearchArguments
) -> SearchResult:
    """``search_code``: code records only; ``semantic`` is the hybrid mode."""
    with query_errors():
        query = SearchQuery(
            text=arguments.query,
            mode=_CODE_MODES[arguments.mode],
            limit=arguments.limit,
            case_sensitive=arguments.case_sensitive,
            filters=SearchFilters(
                workspaces=_names(arguments.workspaces),
                repositories=_names(arguments.repositories),
                kinds=("code_file",),
                languages=None if arguments.languages is None else tuple(arguments.languages),
                paths=tuple(arguments.paths or ()),
            ),
        )
        hits = await _search(conn, services, access, query)
    return SearchResult(hits=[SearchHit.of(hit) for hit in hits])


async def run_get_symbol(
    conn: Conn, services: QueryServices, access: ReadFilter, arguments: SymbolArguments
) -> SymbolResult:
    """``get_symbol``: definitions from the symbol table, with the chunk that holds each."""
    with query_errors():
        query = SearchQuery(
            text=arguments.name,
            mode="symbol",
            limit=arguments.limit,
            case_sensitive=arguments.case_sensitive,
            symbol_match=arguments.match,
            filters=SearchFilters(
                workspaces=_names(arguments.workspaces),
                repositories=_names(arguments.repositories),
                kinds=("code_file",),
                languages=None if arguments.languages is None else tuple(arguments.languages),
                paths=tuple(arguments.paths or ()),
            ),
        )
        hits = await _search(conn, services, access, query)
    return SymbolResult(
        definitions=[Definition.of(hit) for hit in hits if hit.definition is not None]
    )


async def run_read_source(
    conn: Conn, services: QueryServices, access: ReadFilter, arguments: ReadSourceArguments
) -> SourceRead:
    """``read_source``: a file, a line range or a directory from a repository's mirror."""
    mirrors = services.mirrors
    if mirrors is None:
        raise QueryError(503, "unavailable", "Reading code is not available: there is no mirror.")
    with query_errors():
        return await read_source(
            conn,
            access,
            mirrors,
            repository=arguments.repository,
            path=arguments.path,
            workspace=arguments.workspace,
            ref=arguments.ref,
            start_line=arguments.start_line,
            end_line=arguments.end_line,
            limits=services.read_limits,
        )
