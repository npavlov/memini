"""``GET /v1/status`` (``read`` scope): the MCP ``status`` tool over HTTP, and the freshness gate.

The answer is :class:`memini.status.ServiceStatus`: whether the base is current, each stream's lag,
last runs, history and index, each workspace's index, the database. Everything goes through the
caller's authorization filter, so a token limited to one workspace sees that workspace only. As
the tool does, it reports a database that cannot be reached instead of failing; the token lookup
before it needs the database, though, so such a request is refused first (503).

**The gate** (principle 10: one call says whether the base is current). With ``gate=true`` the
answer is the same, and its status code says the verdict: 200 when the base is current, 503 when
it is not, for a script or a health check that reads only the code. The body says why: the
streams' ``behind`` and the workspaces' ``index.behind`` and ``reindex``. A 503 in the API's error
shape instead means the service could not answer (its database is down, so the token could not
be looked up): not current either. ``memini status --gate`` reads the same answer.
"""

from typing import Annotated, Final, cast

from fastapi import APIRouter, Query, Request, Response

from memini import status
from memini.api.auth import ReadAccess
from memini.db import Pool
from memini.query import QueryServices

__all__ = ["NOT_CURRENT_STATUS", "build_status_router"]

NOT_CURRENT_STATUS: Final = 503
"""What the gate answers when the base is not current."""


def build_status_router() -> APIRouter:
    """The status route; see the module."""
    router = APIRouter(tags=["status"])

    @router.get(
        "/status",
        summary="Whether the base is current",
        responses={
            NOT_CURRENT_STATUS: {
                "model": status.ServiceStatus,
                "description": "With gate=true: the base is not current; the body says why.",
            }
        },
    )
    async def get_status(
        request: Request,
        response: Response,
        access: ReadAccess,
        gate: Annotated[
            bool,
            Query(
                description="Answer 503 instead of 200 when the base is not current, for scripts"
                " and health checks; the body is the same."
            ),
        ] = False,
    ) -> status.ServiceStatus:
        """Per stream: lag, why it is behind (lag, never checked, records not indexed, a stale
        index), last run, failed runs in a row with the first line of the last error, history
        loaded, git pin. Per workspace: index state, record and chunk counts, the command that
        brings it up to date. The definitions are those of the MCP ``status`` tool. With
        ``gate=true``, 503 when the base is not current."""
        pool = cast(Pool | None, getattr(request.app.state, "pool", None))
        services = cast(QueryServices, request.app.state.services)
        fingerprint = status.service_fingerprint(services.embedder)
        answer = await status.read_status(pool, access, fingerprint=fingerprint)
        if gate and not answer.current:
            response.status_code = NOT_CURRENT_STATUS
        return answer

    return router
