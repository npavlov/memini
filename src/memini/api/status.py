"""``GET /v1/status`` (``read`` scope): the MCP ``status`` tool over HTTP.

The answer is :class:`memini.status.ServiceStatus`: whether the base is current, each stream's lag,
last runs, history and index, each workspace's index, the database. Everything goes through the
caller's authorization filter, so a token limited to one workspace sees that workspace only. As
the tool does, it reports a database that cannot be reached instead of failing; the token lookup
before it needs the database, though, so such a request is refused first (503).
"""

from typing import cast

from fastapi import APIRouter, Request

from memini import status
from memini.api.auth import ReadAccess
from memini.db import Pool
from memini.query import QueryServices

__all__ = ["build_status_router"]


def build_status_router() -> APIRouter:
    """The status route; see the module."""
    router = APIRouter(tags=["status"])

    @router.get("/status", summary="Whether the base is current")
    async def get_status(request: Request, access: ReadAccess) -> status.ServiceStatus:
        """Per stream: lag, why it is behind (lag, never checked, records not indexed, a stale
        index), last run, failed runs in a row with the first line of the last error, history
        loaded, git pin. Per workspace: index state, record and chunk counts, the command that
        brings it up to date. The definitions are those of the MCP ``status`` tool."""
        pool = cast(Pool | None, getattr(request.app.state, "pool", None))
        services = cast(QueryServices, request.app.state.services)
        fingerprint = status.service_fingerprint(services.embedder)
        return await status.read_status(pool, access, fingerprint=fingerprint)

    return router
