"""The FastAPI application factory.

Layout of the application :func:`create_app` builds:

- Public, no token: ``GET /healthz``, ``GET /readyz``, ``GET /version`` (see
  :mod:`memini.api.health`) and the OpenAPI schema at ``GET /openapi.json``. ``GET /metrics``
  (:mod:`memini.api.metrics`), when ``metrics_token`` is set, needs that token instead of a Memini
  one.
- Resource routes live under :data:`V1_PREFIX`. The bearer-token dependency
  (:func:`memini.api.auth.authenticate_request`) is on that one router, so every resource route
  needs a valid token and the public routes above do not. A route asks for a scope, a read route
  for the authorization filter and a write route for its write access, through
  :mod:`memini.api.auth`. Administration: workspaces (:mod:`memini.api.workspaces`), sources and
  streams (:mod:`memini.api.sources`), runs (:mod:`memini.api.runs`), tokens
  (:mod:`memini.api.tokens`), forgetting a record (:mod:`memini.api.forget`), persons and
  aliases (:mod:`memini.api.persons`). Freshness:
  ``GET /v1/status`` (:mod:`memini.api.status`), the same answer as the MCP ``status`` tool.
  Intake: batches of records for push sources (:mod:`memini.api.ingest`), and the agent
  executor's runs of mcp sources (:mod:`memini.api.agent_runs`). Queries: search, code
  search, symbols and reading files (:mod:`memini.api.query`), the same calls as the MCP tools;
  meetings, messages, threads and adding a document (:mod:`memini.api.comms`), the same calls as
  the MCP comms tools. The catalog: projects, services and domains, and writing a project
  (:mod:`memini.api.catalog`), the same calls as the MCP catalog tools. Knowledge: recording,
  superseding, closing and retracting decisions by hand
  (:mod:`memini.api.decisions`), the same calls as the MCP write tools; enqueuing the extraction of
  stored records the current extractor has not read (:mod:`memini.api.extract`).
- The MCP endpoint is at :data:`~memini.mcp.MCP_PATH` (see :mod:`memini.api.mcp`): the same
  bearer tokens, the ``read`` scope for every request. It is an ASGI app of its own: it gets request
  ids from the outer middleware, and its session manager runs in the application's lifespan.
  With ``mcp_enabled`` false no MCP server is built: the path answers 404 ``mcp_disabled``.

The query tools and routes share one :class:`~memini.query.QueryServices` (``app.state.services``):
the search options, the repository mirrors, and the embedder that embeds queries. The embedder is
created from the settings when the application starts and closed when it stops, unless one is
given to :func:`create_app`, which its caller then owns and closes. Without an embedding provider
there is none: search answers 503 ``embeddings_not_configured``, and reading files still works.

The reranker (:mod:`memini.rerank`; ``search_reranker`` is ``local`` in the image, ``none`` by
default elsewhere) is created when the application starts and loaded in the background, so a model
being fetched or loaded does not hold up the start: searches rerank once it is loaded. If it cannot
load (no optional dependencies, no model files, files that do not match their digest), one warning
says why and search goes on without it: the service never fails for it. It is closed when the
application stops, unless it was given to :func:`create_app`.

The interactive documentation pages (Swagger UI and ReDoc) are off: they load scripts from a CDN,
so a browser that opens them on a local service would run third-party code in the service's
origin. Any OpenAPI viewer can read ``/openapi.json`` instead.
"""

import logging
from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager, nullcontext
from typing import Final

import anyio
from fastapi import APIRouter, Depends, FastAPI
from fastapi.telemetry import TelemetryConfig
from starlette.types import ASGIApp

from memini import __version__
from memini.api.agent_runs import build_agent_runs_router
from memini.api.auth import authenticate_request
from memini.api.catalog import build_catalog_router
from memini.api.comms import build_comms_router
from memini.api.decisions import build_decisions_router
from memini.api.errors import ERROR_RESPONSES, install_error_handlers
from memini.api.extract import build_extract_router
from memini.api.forget import build_forget_router
from memini.api.health import DEFAULT_READINESS_TIMEOUT, ReadinessCheck, build_health_router
from memini.api.ingest import build_ingest_router
from memini.api.limits import RequestLimitsMiddleware
from memini.api.mcp import mcp_route
from memini.api.metrics import HttpMetricsMiddleware, RouteTemplates, build_metrics_router
from memini.api.persons import build_persons_router
from memini.api.query import build_query_router
from memini.api.request_id import RequestIdMiddleware
from memini.api.runs import build_runs_router
from memini.api.sources import build_sources_router
from memini.api.status import build_status_router
from memini.api.tokens import build_tokens_router
from memini.api.workspaces import build_workspaces_router
from memini.auth import Authenticator, pool_authenticator
from memini.config import Settings
from memini.db import Pool
from memini.embeddings import Embedder, EmbeddingsNotConfiguredError, create_embedder
from memini.mcp import McpEndpoint, build_server
from memini.query import QueryServices
from memini.rerank import LocalReranker, RerankError, create_reranker
from memini.search import Reranker

__all__ = ["OPENAPI_URL", "V1_PREFIX", "create_app"]

V1_PREFIX: Final = "/v1"
OPENAPI_URL: Final = "/openapi.json"

log = logging.getLogger(__name__)

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
    route_templates: RouteTemplates | None = None
    """Set by create_app, for the HTTP metrics."""

    def build_middleware_stack(self) -> ASGIApp:
        # Outside Starlette's ServerErrorMiddleware, which turns unhandled exceptions into a 500:
        # that response gets the request id too, and the 500 handler can read it. The HTTP metrics
        # count that 500 too, and the refusals of the request limits.
        return RequestIdMiddleware(
            HttpMetricsMiddleware(
                super().build_middleware_stack(), self.route_templates or RouteTemplates()
            )
        )


async def _load_reranker(reranker: LocalReranker, services: QueryServices) -> None:
    """Load the reranker, then let search use it; on failure, say why and leave it off."""
    try:
        await reranker.load()
    except RerankError as error:
        log.warning("reranking is off: %s", error)
        return
    services.reranker = reranker
    log.info("reranking with %s, %d threads", reranker.describe(), reranker.threads)


def _lifespan(
    mcp: McpEndpoint | None,
    services: QueryServices,
    settings: Settings,
    *,
    own_embedder: bool,
    own_reranker: bool,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        created: Embedder | None = None
        if own_embedder:
            try:
                created = create_embedder(settings)  # cheap: a model loads on first use
            except EmbeddingsNotConfiguredError as error:
                log.info("search is off: %s", error)
            services.embedder = created
        reranker = create_reranker(settings) if own_reranker else None
        try:
            async with anyio.create_task_group() as tasks:
                if reranker is not None:
                    tasks.start_soon(_load_reranker, reranker, services)
                try:
                    async with mcp.run() if mcp is not None else nullcontext():
                        yield
                finally:
                    tasks.cancel_scope.cancel()
        finally:
            if reranker is not None:
                services.reranker = None
                await reranker.aclose()
            if created is not None:
                services.embedder = None
                await created.aclose()

    return lifespan


def create_app(
    settings: Settings,
    *,
    pool: Pool | None = None,
    authenticator: Authenticator | None = None,
    readiness_checks: Mapping[str, ReadinessCheck] | None = None,
    readiness_timeout: float = DEFAULT_READINESS_TIMEOUT,
    embedder: Embedder | None = None,
    reranker: Reranker | None = None,
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
        embedder: the embedder that embeds search queries, owned by the caller, who closes it
            (``memini serve`` shares one with the sync jobs; tests pass a fake). Without it, one
            is created from the settings while the application runs, if a provider is
            configured.
        reranker: the reranker of ranked searches, owned by the caller (tests pass a fake).
            Without it, one is created from the settings while the application runs, unless
            ``search_reranker`` is ``none``.

    Raises:
        ValueError: ``readiness_timeout`` is not a finite number above zero.
        EmbeddingError: at start, embedding settings that cannot make an embedder.
    """
    services = QueryServices.from_settings(settings)
    services.embedder = embedder
    services.reranker = reranker
    mcp = McpEndpoint(build_server(pool=pool, services=services)) if settings.mcp_enabled else None
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
        lifespan=_lifespan(
            mcp,
            services,
            settings,
            own_embedder=embedder is None,
            own_reranker=reranker is None,
        ),
    )
    app.state.settings = settings
    app.state.pool = pool
    app.state.services = services
    if authenticator is None and pool is not None:
        authenticator = pool_authenticator(pool)
    app.state.authenticator = authenticator
    install_error_handlers(app)
    app.add_middleware(RequestLimitsMiddleware, settings=settings)

    # The HTTP metrics label a request by the template of its route, prefix included.
    templates = RouteTemplates()
    public = [build_health_router(readiness_checks or {}, readiness_timeout)]
    if settings.metrics_token is not None:
        public.append(build_metrics_router(settings.metrics_token))
    for router in public:
        app.include_router(router)
        templates.add(router.routes)

    # Resource routers are included into this router, before it is included into the app:
    # include_router copies the routes it finds at that moment.
    v1 = APIRouter(prefix=V1_PREFIX, dependencies=[Depends(authenticate_request)])
    for router in (
        build_workspaces_router(),
        build_sources_router(),
        build_ingest_router(),
        build_agent_runs_router(),
        build_runs_router(),
        build_status_router(),
        build_tokens_router(),
        build_persons_router(),
        build_query_router(),
        build_catalog_router(),
        build_comms_router(),
        build_decisions_router(),
        build_extract_router(),
        build_forget_router(),
    ):
        v1.include_router(router)
        templates.add(router.routes, V1_PREFIX)
    app.include_router(v1)

    app.router.routes.append(mcp_route(mcp, authenticator))
    templates.add(app.router.routes)  # the OpenAPI schema and MCP; included routers have no path
    app.route_templates = templates

    return app
