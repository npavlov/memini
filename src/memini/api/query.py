"""Query routes under ``/v1``: the HTTP face of the MCP query tools, with the same arguments and
results (:mod:`memini.query`).

| Route | MCP tool | Does |
|---|---|---|
| ``GET /search`` | ``search`` | ranked search over every kind of record |
| ``GET /search/code`` | ``search_code`` | code search: semantic, exact, regex, symbol |
| ``GET /symbols`` | ``get_symbol`` | definitions of a symbol, with location and code |
| ``GET …/files/{path}`` | ``read_source`` | a file, lines of it, or a directory |

``…`` is ``/workspaces/{workspace}/sources/{source}``: the repository is a git source.

Every route needs the ``read`` scope and reads through the caller's
:class:`~memini.auth.ReadFilter`. Arguments are query parameters with the names of the tool's
arguments; a list is the parameter repeated (``?kinds=thread&kinds=meeting``). A time takes a zone
(``2026-03-01T00:00:00Z``; a ``+`` in an offset is sent as ``%2B``). Unknown parameters are
refused (422 ``validation_error``). The root of a repository is ``…/files/``; a directory
listing comes back for a directory path.

Errors are those of :mod:`memini.query.service`, in the API's error shape: 422 for a query, path,
ref or range that is not valid, a regular expression that runs past ``search_timeout``
(``search_timeout``) or a file over the size limit; 404 ``not_found`` for anything the token may
not read or that does not exist; 409 ``not_synced`` for a repository without a mirror yet; 503 for
a stale index (``index_mismatch``, the message names ``memini reindex``) or missing embeddings.
"""

from typing import Annotated, cast

from fastapi import APIRouter, Depends, Query, Request

from memini.api.auth import ReadAccess
from memini.api.database import Connection
from memini.api.errors import ApiError
from memini.query import (
    CodeSearchArguments,
    QueryError,
    QueryServices,
    ReadSourceArguments,
    ReadSourceQuery,
    SearchArguments,
    SearchResult,
    SourceRead,
    SymbolArguments,
    SymbolResult,
    run_get_symbol,
    run_read_source,
    run_search,
    run_search_code,
)

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
    ) -> SearchResult:
        """Ranked search over code, threads, meetings and documents the token may read: the MCP
        ``search`` tool. Each hit carries its source kind, locator, time and snippet."""
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
        ``regex`` or ``symbol``. Hits carry repo@sha:path#lines."""
        try:
            return await run_search_code(conn, services, access, arguments)
        except QueryError as error:
            raise _api_error(error) from None

    @router.get("/symbols", summary="Find definitions of a symbol")
    async def get_symbol(
        arguments: Annotated[SymbolArguments, Query()],
        access: ReadAccess,
        services: Services,
        conn: Connection,
    ) -> SymbolResult:
        """Definitions of a symbol by name or qualified name, the MCP ``get_symbol`` tool."""
        try:
            return await run_get_symbol(conn, services, access, arguments)
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
        arguments: Annotated[ReadSourceQuery, Query()],
        access: ReadAccess,
        services: Services,
        conn: Connection,
    ) -> SourceRead:
        """A file, a line range or a directory listing from the mirror of a git source, the MCP
        ``read_source`` tool. ``path`` is relative to the repository root (empty for the root).
        By default at the pinned commit of the default tracked branch; ``ref`` takes a branch or
        tag name, a full ref or a full commit id. 422 ``invalid_path`` for an absolute path or
        one with ``..``; 404 when there is nothing there."""
        # The query string is validated; the path is checked by the reader (invalid_path), and a
        # name of any length is looked up and not found, as on the other routes.
        read = ReadSourceArguments.model_construct(
            repository=source,
            path=path,
            workspace=workspace,
            ref=arguments.ref,
            start_line=arguments.start_line,
            end_line=arguments.end_line,
        )
        try:
            return await run_read_source(conn, services, access, read)
        except QueryError as error:
            raise _api_error(error) from None

    return router
