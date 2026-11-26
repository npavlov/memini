"""The query tools as functions: from validated arguments to a result, or a :class:`QueryError`.

One function per tool, for the MCP tools (:mod:`memini.mcp.query`) and the HTTP routes
(:mod:`memini.api.query`) alike: :func:`run_search`, :func:`run_search_code`,
:func:`run_get_symbol`, :func:`run_get_file`, :func:`run_list_commits`. Each takes a connection
with no transaction open, the service's :class:`QueryServices`, the caller's
:class:`~memini.auth.ReadFilter` and the arguments, and calls the library:
:func:`memini.search.search`, :func:`memini.query.reader.get_file` or
:func:`memini.query.commits.list_commits`.

Every refusal of the libraries becomes a :class:`QueryError` with an HTTP status, a stable code and
a message that never repeats an argument or stored content:

| Error | Status | Code |
|---|---|---|
| a query, filter or regular expression that is not valid | 422 | ``invalid_search`` |
| a filter not supported yet | 422 | ``unsupported_filter`` |
| a ``projects`` value the catalog does not hold | 422 | ``unknown_project`` |
| a ``domains`` value it does not hold (in the projects named) | 422 | ``unknown_domain`` |
| a ``services`` value it does not hold (in the projects named) | 422 | ``unknown_service`` |
| a statement ran past ``search_timeout`` | 422 | ``search_timeout`` |
| the history walks of ``list_commits`` ran past ``git_log_timeout`` | 422 | ``commits_timeout`` |
| a history walk printed more than is read | 422 | ``commits_too_large`` |
| a path that is refused (absolute, ``..``, control characters) | 422 | ``invalid_path`` |
| a ref that is refused (a revision expression) | 422 | ``invalid_ref`` |
| a line range that is not valid | 422 | ``invalid_range`` |
| a repository name that several workspaces have | 422 | ``ambiguous_repository`` |
| a file over ``git_max_blob_size`` | 422 | ``file_too_large`` |
| no such workspace, repository, ref, commit or path the caller may read | 404 | ``not_found`` |
| the repository has no mirror yet | 409 | ``not_synced`` |
| a stale index whose rebuild waits or runs | 503 | ``index_rebuilding`` |
| an index built with another fingerprint (principle 9) | 503 | ``index_mismatch`` |
| no embedding provider configured | 503 | ``embeddings_not_configured`` |
| the query could not be embedded | 503 | ``embedding_failed`` |
| no mirror directory (code and history cannot be read) | 503 | ``unavailable`` |

A timeout is the query's to fix (narrow it, or a more specific pattern), so it is a 422, not a
server error. A stale index and a missing provider are the operator's: the message of a stale index
names ``memini reindex --workspace NAME``. A stale index whose rebuild waits or runs (a service with
``index_auto_rebuild`` after an upgrade) says how far the rebuild is instead: it passes by itself.

Embeddings: every search mode checks the index fingerprint, and the fingerprint is made from the
embedder's spec, so ``search``, ``search_code`` and ``get_symbol`` need the configured embedder in
every mode, also exact, regex and symbol. Without an embedding provider no index is built either
(code is synced only into an index), so there would be nothing to find. ``get_file`` and
``list_commits`` read the mirror and need none.
"""

import logging
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Final
from uuid import UUID

from psycopg import AsyncConnection, errors, sql
from psycopg.rows import TupleRow

from memini import knowledge
from memini.auth import ReadFilter
from memini.config import Settings
from memini.decisions import context as decision_context
from memini.decisions.model import DecisionItem
from memini.embeddings import Embedder, EmbeddingError, EmbeddingsNotConfiguredError
from memini.git import GitOptions, MirrorManager
from memini.ids import RecordKind
from memini.index import IndexRebuildingError
from memini.ingest import DEFAULT_LIMITS, IngestLimits
from memini.persons import ME, PersonRef, member_ids, parse_person_ref, resolve_persons
from memini.query.commits import (
    DEFAULT_COMMIT_LIMITS,
    CommitLimits,
    CommitList,
    CommitsTimeoutError,
    CommitsTooLargeError,
    list_commits,
    repository_scopes,
)
from memini.query.context import CodeContext, code_contexts
from memini.query.model import (
    CodeSearchArguments,
    GeneralSearchResult,
    GetFileArguments,
    ListCommitsArguments,
    SearchArguments,
    SearchHit,
    SearchResult,
)
from memini.query.reader import (
    DEFAULT_READ_LIMITS,
    FileTooLargeError,
    InvalidReadError,
    NotSyncedError,
    ReadLimits,
    SensitiveFileError,
    SourceRead,
    get_file,
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
    UnknownNameError,
    UnsupportedFilterError,
    search,
)
from memini.search.engine import search_transaction
from memini.sensitivity import sensitive_records
from memini.workspaces import NotFoundError, list_workspaces

__all__ = [
    "QueryError",
    "QueryServices",
    "query_errors",
    "run_get_file",
    "run_list_commits",
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
    commit_limits: CommitLimits = DEFAULT_COMMIT_LIMITS
    """The bounds of a ``list_commits`` call (the ``git_log_*`` settings)."""
    ingest_limits: IngestLimits = DEFAULT_LIMITS
    """The limits of a batch, for the ``put_document`` write (:mod:`memini.comms`)."""
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
            commit_limits=CommitLimits.from_settings(settings),
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
    except UnknownNameError as error:
        raise QueryError(422, f"unknown_{error.kind}", _sentence(str(error))) from None
    except InvalidSearchError as error:
        raise QueryError(422, "invalid_search", _sentence(str(error))) from None
    except SearchTimeoutError as error:
        raise QueryError(422, "search_timeout", _sentence(str(error))) from None
    except CommitsTimeoutError as error:
        raise QueryError(422, "commits_timeout", _sentence(str(error))) from None
    except CommitsTooLargeError as error:
        raise QueryError(422, "commits_too_large", _sentence(str(error))) from None
    except IndexRebuildingError as error:
        raise QueryError(503, "index_rebuilding", _sentence(str(error))) from None
    except IndexMismatchError as error:
        raise QueryError(503, "index_mismatch", _sentence(str(error))) from None
    except EmbeddingsNotConfiguredError:
        raise QueryError(
            503,
            "embeddings_not_configured",
            "Search needs an embedding provider, and none is configured: set"
            " MEMINI_EMBEDDING_PROVIDER. get_file works without one.",
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
    except SensitiveFileError as error:
        raise QueryError(403, "sensitive_record", str(error)) from None
    except NotFoundError as error:
        raise QueryError(404, "not_found", f"No such {error.what}.") from None


def _names(values: Sequence[str] | None) -> tuple[str, ...] | None:
    return None if values is None else tuple(values)


async def _search(
    conn: Conn, services: QueryServices, access: ReadFilter, query: SearchQuery
) -> tuple[list[Hit], int]:
    """The hits of ``query``, and how many of the best were left out as marked sensitive.

    A caller that did not ask for sensitive records gets the best hits without them: the search
    runs over every record, and the hits of a sensitive one are taken out after it and counted
    (``left_out``), so the count says how many of the best answers there were, and nothing of
    them leaves this function."""
    embedder = services.embedder
    if embedder is None:
        raise EmbeddingsNotConfiguredError
    wide = access if access.include_sensitive else replace(access, include_sensitive=True)
    hits = await search(
        conn,
        embedder,
        wide,
        query,
        options=services.search_options,
        reranker=services.reranker,
    )
    if access.include_sensitive or not hits:
        return hits, 0
    hidden = await sensitive_records(conn, [hit.record_id for hit in hits])
    kept = [hit for hit in hits if hit.record_id not in hidden]
    return kept, len(hits) - len(kept)


async def _contexts(
    conn: Conn, access: ReadFilter, hits: Sequence[Hit]
) -> list[CodeContext | None]:
    """What each hit's file belongs to, by the catalog; ``None`` for a hit that is no file of a
    repository. Read after the search, from the catalog as it is then."""
    return await code_contexts(
        conn,
        access,
        [
            (hit.workspace_id, hit.source_id, hit.path)
            if hit.source_kind == "repository" and hit.path is not None
            else None
            for hit in hits
        ],
    )


async def run_search(
    conn: Conn, services: QueryServices, access: ReadFilter, arguments: SearchArguments
) -> GeneralSearchResult:
    """``search``: ranked search over every kind of record the caller may read."""
    record_kinds: list[RecordKind] = []
    item_kinds: list[knowledge.ItemKind] = []
    for kind in arguments.kinds or ():
        if kind == "decision" or kind == "open_question":
            item_kinds.append(kind)
        else:
            record_kinds.append(kind)
    with query_errors():
        query = SearchQuery(
            text=arguments.query,
            mode=arguments.mode,
            limit=arguments.limit,
            filters=SearchFilters(
                workspaces=_names(arguments.workspaces),
                sources=_names(arguments.sources),
                kinds=tuple(record_kinds) or None,
                since=arguments.since,
                until=arguments.until,
                persons=None
                if arguments.persons is None
                else tuple(parse_person_ref(person) for person in arguments.persons),
                roles=None if arguments.roles is None else tuple(arguments.roles),
                projects=_names(arguments.projects),
                domains=_names(arguments.domains),
                services=_names(arguments.services),
            ),
        )
        hits, left_out = await _search(conn, services, access, query)
        contexts = await _contexts(conn, access, hits)
        decisions = []
        if item_kinds or not arguments.kinds:
            filters = knowledge.ItemFilters(
                workspaces=query.filters.workspaces,
                kinds=tuple(item_kinds) or None,
                sources=tuple(arguments.sources) if arguments.sources else None,
                decided_from=arguments.since,
                decided_until=arguments.until,
                limit=arguments.limit,
            )
            try:
                async with search_transaction(conn, services.search_options, ef_search=None):
                    decisions = await _decision_matches(conn, access, query, filters, hits)
            except errors.QueryCanceled as error:
                raise SearchTimeoutError(services.search_options.timeout) from error
    records = bool(record_kinds or not arguments.kinds)
    return GeneralSearchResult(
        items=[SearchHit.of(hit, context) for hit, context in zip(hits, contexts, strict=True)]
        if records
        else [],
        left_out=left_out if records else 0,
        decisions=[DecisionItem.of(item) for item in decisions],
    )


async def _decision_matches(
    conn: Conn,
    access: ReadFilter,
    query: SearchQuery,
    filters: knowledge.ItemFilters,
    hits: list[Hit],
) -> list[knowledge.Item]:
    matches: dict[UUID, knowledge.Item] = {}
    chosen = query.filters
    if chosen.projects is not None or chosen.domains is not None:
        # As list_decisions reads them (memini.decisions.context): an item under one of the
        # projects and in one of the domains, a domain with its subdomains, by any way it came.
        within = await _decisions_in_catalog(conn, access, chosen)
        if not within:
            return []
        filters = replace(filters, item_ids=within)
    # A service names code, not decisions: with services, as with roles, items match only through
    # the records retrieved, which the filter did narrow.
    by_statement = chosen.roles is None and chosen.services is None
    if query.mode != "dense" and by_statement:
        statements = filters
        if query.filters.persons is not None:
            spaces = await list_workspaces(conn, access)
            people = await resolve_persons(
                conn, access, [space.id for space in spaces], query.filters.persons
            )
            statements = replace(
                filters, person_ids=tuple(person for group in people.values() for person in group)
            )
        for item in await knowledge.list_items(conn, access, replace(statements, query=query.text)):
            matches[item.id] = item
    if hits and len(matches) < filters.limit:
        records = tuple(dict.fromkeys(hit.record_id for hit in hits))
        cited = await knowledge.list_items(conn, access, replace(filters, record_ids=records))
        for item in cited:
            matches.setdefault(item.id, item)
    return list(matches.values())[: filters.limit]


async def _decisions_in_catalog(
    conn: Conn, access: ReadFilter, chosen: SearchFilters
) -> tuple[UUID, ...]:
    """The active items of the workspaces searched under ``chosen.projects`` and in
    ``chosen.domains``. The record search has refused a name no catalog holds already."""
    spaces = await list_workspaces(conn, access)
    workspace_ids = [
        space.id for space in spaces if chosen.workspaces is None or space.name in chosen.workspaces
    ]
    found = await decision_context.load_contexts(conn, access, workspace_ids)
    try:
        return tuple(
            found.select(projects=chosen.projects, domains=chosen.domains, statuses=("active",))
        )
    except decision_context.UnknownPlaceError:
        return ()


_CODE_MODES: dict[str, Mode] = {
    "semantic": "hybrid",
    "exact": "exact",
    "regex": "regex",
    "symbol": "symbol",
    "symbol_prefix": "symbol",
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
            symbol_match="prefix" if arguments.mode == "symbol_prefix" else "exact",
            filters=SearchFilters(
                workspaces=_names(arguments.workspaces),
                repositories=_names(arguments.sources),
                kinds=("code_file",),
                languages=None if arguments.languages is None else tuple(arguments.languages),
                paths=tuple(arguments.paths or ()),
                projects=_names(arguments.projects),
                domains=_names(arguments.domains),
                services=_names(arguments.services),
            ),
        )
        hits, left_out = await _search(conn, services, access, query)
        contexts = await _contexts(conn, access, hits)
    return SearchResult(
        items=[SearchHit.of(hit, context) for hit, context in zip(hits, contexts, strict=True)],
        left_out=left_out,
    )


async def run_get_file(
    conn: Conn, services: QueryServices, access: ReadFilter, arguments: GetFileArguments
) -> SourceRead:
    """``get_file``: a file, a line range or a directory from a repository's mirror."""
    mirrors = services.mirrors
    if mirrors is None:
        raise QueryError(503, "unavailable", "Reading code is not available: there is no mirror.")
    with query_errors():
        return await get_file(
            conn,
            access,
            mirrors,
            repository=arguments.source,
            path=arguments.path,
            workspace=arguments.workspace,
            ref=arguments.ref,
            start_line=arguments.start_line,
            end_line=arguments.end_line,
            limits=services.read_limits,
        )


async def run_list_commits(
    conn: Conn, services: QueryServices, access: ReadFilter, arguments: ListCommitsArguments
) -> CommitList:
    """``list_commits``: the history of repositories, read from their mirrors on demand."""
    mirrors = services.mirrors
    if mirrors is None:
        raise QueryError(
            503, "unavailable", "Reading history is not available: there is no mirror."
        )
    with query_errors():
        until = arguments.until
        if arguments.cursor is not None:
            if until is not None:
                raise InvalidSearchError("cursor continues a list: send it without until")
            until = _cursor_time(arguments.cursor)
        scopes = await repository_scopes(
            conn,
            access,
            sources=arguments.sources,
            workspaces=arguments.workspaces,
            paths=tuple(arguments.paths or ()),
        )
        authors = (
            None
            if arguments.persons is None
            else await _author_texts(conn, access, arguments.persons)
        )
        return await list_commits(
            conn,
            access,
            mirrors,
            scopes,
            authors=authors,
            since=arguments.since,
            until=until,
            query=arguments.query,
            limit=arguments.limit,
            limits=services.commit_limits,
        )


def _cursor_time(cursor: str) -> datetime:
    """The time a ``list_commits`` cursor stands for: the commits before it follow."""
    try:
        moment = datetime.fromisoformat(cursor)
    except ValueError:
        raise InvalidSearchError(
            "cursor is not one a list gave: pass next_cursor as it is"
        ) from None
    if moment.utcoffset() is None:
        raise InvalidSearchError("cursor is not one a list gave: pass next_cursor as it is")
    return moment


_AUTHOR_ALIASES: Final = sql.SQL("""
SELECT DISTINCT a.value FROM alias a
WHERE a.person_id = ANY(%s) AND a.kind IN ('email', 'git_author')
""")


async def _author_texts(conn: Conn, access: ReadFilter, persons: Sequence[str]) -> list[str]:
    """What the ``persons`` of ``list_commits`` look for in an author's name and address: for
    'me' and a person id, the mail addresses and author names of that person and everyone merged
    into it; any other value as it is. A person that names no one adds nothing, so a list of such
    persons only matches no commit."""
    texts: list[str] = []
    named: list[PersonRef] = []
    for person in persons:
        ref = parse_person_ref(person)
        if isinstance(ref, UUID) or ref == ME:
            named.append(ref)
        else:
            texts.append(person)
    if named:
        spaces = await list_workspaces(conn, access)
        found = await resolve_persons(conn, access, [space.id for space in spaces], named)
        roots = {root for group in found.values() for root in group}
        if roots:
            members = await member_ids(conn, roots)
            cur = await conn.execute(_AUTHOR_ALIASES, (list(members),))
            texts += [row[0] for row in await cur.fetchall()]
    # Nothing to look for: an author no name or address holds.
    return texts or ["\x00"]
