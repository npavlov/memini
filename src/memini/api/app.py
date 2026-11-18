"""The FastAPI application factory.

Layout of the application :func:`create_app` builds:

- Public, no token: ``GET /healthz``, ``GET /readyz``, ``GET /version`` (see
  :mod:`memini.api.health`) and the OpenAPI schema at ``GET /openapi.json``.
- Resource routes live under :data:`V1_PREFIX`. The bearer-token dependency
  (:func:`memini.api.auth.authenticate_request`) is on that one router, so every resource route
  needs a valid token and the public routes above do not. A route asks for a scope, a read route
  for the authorization filter and a write route for its write access, through
  :mod:`memini.api.auth`. Administration: workspaces (:mod:`memini.api.workspaces`), sources and
  streams (:mod:`memini.api.sources`), runs (:mod:`memini.api.runs`).
- The MCP endpoint is at :data:`~memini.mcp.MCP_PATH` (see :mod:`memini.api.mcp`): the same
  bearer tokens, the ``read`` scope for every request. It is an ASGI app of its own: it gets request
  ids from the outer middleware, and its session manager runs in the application's lifespan.

The interactive documentation pages (Swagger UI and ReDoc) are off: they load scripts from a CDN,
so a browser that opens them on a local service would run third-party code in the service's
origin. Any OpenAPI viewer can read ``/openapi.json`` instead.
"""

from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Final

from fastapi import APIRouter, Depends, FastAPI
from fastapi.telemetry import TelemetryConfig
from starlette.types import ASGIApp

from memini import __version__
from memini.api.auth import authenticate_request
from memini.api.errors import ERROR_RESPONSES, install_error_handlers
from memini.api.health import DEFAULT_READINESS_TIMEOUT, ReadinessCheck, build_health_router
from memini.api.mcp import mcp_route
from memini.api.request_id import RequestIdMiddleware
from memini.api.runs import build_runs_router
from memini.api.sources import build_sources_router
from memini.api.workspaces import build_workspaces_router
from memini.auth import Authenticator, pool_authenticator
from memini.config import Settings
from memini.db import Pool
from memini.mcp import McpEndpoint, build_server

__all__ = ["OPENAPI_URL", "V1_PREFIX", "create_app"]

V1_PREFIX: Final = "/v1"
OPENAPI_URL: Final = "/openapi.json"

# FastAPI's built-in OpenTelemetry instrumentation is off. Once an OpenTelemetry provider is set, it
# records exception messages, request validation input and query strings, any of which can carry
# private text or a secret. An environment variable cannot turn its exporters on either.
_TELEMETRY_OFF: Final[TelemetryConfig] = {
    "tracing": False,
    "metrics": False,
    "logs": False,
    "operation_spans": False,
    "auto_configure": False,
}


class _MeminiApp(FastAPI):
    def build_middleware_stack(self) -> ASGIApp:
        # Outside Starlette's ServerErrorMiddleware, which turns unhandled exceptions into a 500:
        # that response gets the request id too, and the 500 handler can read it.
        return RequestIdMiddleware(super().build_middleware_stack())


def _lifespan(mcp: McpEndpoint) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        async with mcp.run():
            yield

    return lifespan


def create_app(
    settings: Settings,
    *,
    pool: Pool | None = None,
    authenticator: Authenticator | None = None,
    readiness_checks: Mapping[str, ReadinessCheck] | None = None,
    readiness_timeout: float = DEFAULT_READINESS_TIMEOUT,
) -> FastAPI:
    """Build the HTTP application.

    The MCP endpoint serves requests only while the application's lifespan runs, as it does under
    an ASGI server or ``with TestClient(app)``.

    Args:
        settings: the validated settings; kept in ``app.state.settings``.
        pool: the database pool, opened and closed by the caller; kept in ``app.state.pool``.
            Routes borrow connections from it, and tokens are looked up in it unless
            ``authenticator`` is given. Without it, routes that need the database answer 503.
        authenticator: resolves a bearer token to its principal; kept in
            ``app.state.authenticator``. Defaults to looking tokens up in ``pool``. Without
            either, every ``/v1`` and MCP request with a token answers 503.
        readiness_checks: checks that ``GET /readyz`` runs, by name, for example ``"database"``.
            Each returns when its dependency is usable and raises when it is not. With none,
            the service reports ready.
        readiness_timeout: seconds each check may take before it counts as failed.

    Raises:
        ValueError: ``readiness_timeout`` is not a finite number above zero.
    """
    mcp = McpEndpoint(build_server(pool=pool))
    app = _MeminiApp(
        title="Memini",
        summary="Self-hosted knowledge service: code, messages and meeting notes in; "
        "MCP and HTTP out.",
        version=__version__,
        debug=False,  # debug responses would carry tracebacks
        openapi_url=OPENAPI_URL,
        docs_url=None,
        redoc_url=None,
        responses=ERROR_RESPONSES,
        telemetry=_TELEMETRY_OFF,
        lifespan=_lifespan(mcp),
    )
    app.state.settings = settings
    app.state.pool = pool
    if authenticator is None and pool is not None:
        authenticator = pool_authenticator(pool)
    app.state.authenticator = authenticator
    install_error_handlers(app)

    app.include_router(build_health_router(readiness_checks or {}, readiness_timeout))

    # Resource routers are included into this router, before it is included into the app:
    # include_router copies the routes it finds at that moment.
    v1 = APIRouter(prefix=V1_PREFIX, dependencies=[Depends(authenticate_request)])
    v1.include_router(build_workspaces_router())
    v1.include_router(build_sources_router())
    v1.include_router(build_runs_router())
    app.include_router(v1)

    app.router.routes.append(mcp_route(mcp, authenticator))

    return app
