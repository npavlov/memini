"""Query routes under ``/v1``: the HTTP face of the MCP query tools, with the same arguments and
results (:mod:`memini.query`).

| Route | MCP tool | Does |
|---|---|---|
| ``GET /search`` | ``search`` | ranked search over every kind of record |
| ``GET /search/code`` | ``search_code`` | code search: semantic, exact, regex, symbol, prefix |
| ``GET …/files/{path}`` | ``get_file`` | a file, lines of it, or a directory |
| ``GET /commits`` | ``list_commits`` | commit history, read from the mirrors on demand |

``…`` is ``/workspaces/{workspace}/sources/{source}``: the source is a repository.

Every route needs the ``read`` scope and reads through the caller's
:class:`~memini.auth.ReadFilter`. Arguments are query parameters with the names of the tool's
arguments; a list is the parameter repeated (``?kinds=thread&kinds=meeting``). A time takes a zone
(``2026-03-01T00:00:00Z``; a ``+`` in an offset is sent as ``%2B``). Unknown parameters are
refused (422 ``validation_error``). The root of a repository is ``…/files/``; a directory
listing comes back for a directory path.

Errors are those of :mod:`memini.query.service`, in the API's error shape: 422 for a query, path,
ref or range that is not valid, a regular expression that runs past ``search_timeout``
(``search_timeout``), a history walk that runs past ``git_log_timeout`` (``commits_timeout``) or
a file over the size limit; 404 ``not_found`` for anything the token may
not read or that does not exist; 409 ``not_synced`` for a repository without a mirror yet; 503 for
a stale index (``index_mismatch``, the message names ``memini reindex``; ``index_rebuilding`` while
its rebuild waits or runs, with how far it is) or missing embeddings.
"""

from typing import Annotated, cast

from fastapi import APIRouter, Depends, Query, Request

from memini.api.auth import ReadAccess
from memini.api.database import Connection
from memini.api.errors import ApiError
from memini.query import (
    CodeSearchArguments,
    CommitList,
    GetFileArguments,
    GetFileQuery,
    ListCommitsArguments,
    QueryError,
    QueryServices,
    SearchArguments,
    SearchResult,
    SourceRead,
    run_get_file,
    run_list_commits,
    run_search,
    run_search_code,
)
from memini.query.model import GeneralSearchResult

__all__ = ["Services", "build_query_router", "query_services"]


def query_services(request: Request) -> QueryServices:
    """The application's :class:`~memini.query.QueryServices`."""
    return cast(QueryServices, request.app.state.services)


Services = Annotated[QueryServices, Depends(query_services)]


def _api_error(error: QueryError) -> ApiError:
    return ApiError(error.status, error.code, error.message)


def build_query_router() -> APIRouter:
    """The query routes; see the module."""
    router = APIRouter(tags=["search"])

    @router.get("/search", summary="Search everything")
    async def search_everything(
        arguments: Annotated[SearchArguments, Query()],
        access: ReadAccess,
        services: Services,
        conn: Connection,
    ) -> GeneralSearchResult:
        """Ranked search over code, threads, meetings and documents the token may read: the MCP
        ``search`` tool. Each hit carries its source kind, locator, time and snippet; a hit of a
        thread, a meeting or a document also its ``anchor`` in the record, a ``link`` to that
        place and the chunk's time ``chunk_occurred_at``. ``record_id`` with ``anchor`` is a
        knowledge evidence
        reference, as ``POST …/decisions`` takes it."""
        try:
            return await run_search(conn, services, access, arguments)
        except QueryError as error:
            raise _api_error(error) from None

    @router.get("/search/code", summary="Search code")
    async def search_code(
        arguments: Annotated[CodeSearchArguments, Query()],
        access: ReadAccess,
        services: Services,
        conn: Connection,
    ) -> SearchResult:
        """Code search, the MCP ``search_code`` tool: ``semantic`` (the default), ``exact``,
        ``regex``, ``symbol`` or ``symbol_prefix``. Hits carry repo@sha:path#lines."""
        try:
            return await run_search_code(conn, services, access, arguments)
        except QueryError as error:
            raise _api_error(error) from None

    @router.get(
        "/workspaces/{workspace}/sources/{source}/files/{path:path}",
        summary="Read a file or directory of a repository",
    )
    async def read_file(
        workspace: str,
        source: str,
        path: str,
        arguments: Annotated[GetFileQuery, Query()],
        access: ReadAccess,
        services: Services,
        conn: Connection,
    ) -> SourceRead:
        """A file, a line range or a directory listing from the mirror of a repository, the MCP
        ``get_file`` tool. ``path`` is relative to the repository root (empty for the root).
        By default at the pinned commit of the default tracked branch; ``ref`` takes a branch or
        tag name, a full ref or a full commit id. 422 ``invalid_path`` for an absolute path or
        one with ``..``; 404 when there is nothing there."""
        # The query string is validated; the path is checked by the reader (invalid_path), and a
        # name of any length is looked up and not found, as on the other routes.
        read = GetFileArguments.model_construct(
            source=source,
            path=path,
            workspace=workspace,
            ref=arguments.ref,
            start_line=arguments.start_line,
            end_line=arguments.end_line,
            include_sensitive=arguments.include_sensitive,
        )
        try:
            return await run_get_file(conn, services, access, read)
        except QueryError as error:
            raise _api_error(error) from None

    @router.get("/commits", summary="List commits")
    async def list_commits(
        arguments: Annotated[ListCommitsArguments, Query()],
        access: ReadAccess,
        services: Services,
        conn: Connection,
    ) -> CommitList:
        """Who changed what, the MCP ``list_commits`` tool: the history of the repositories the
        token may read, newest first, read from their mirrors when asked and never stored. A list
        cut by a bound is ``truncated`` and gives ``next_cursor``, the ``cursor`` of the call that
        continues it. 422 ``commits_timeout`` for walks past the time limit: narrow the
        question."""
        try:
            return await run_list_commits(conn, services, access, arguments)
        except QueryError as error:
            raise _api_error(error) from None

    return router
