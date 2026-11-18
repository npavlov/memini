"""The FastAPI application factory.

Layout of the application :func:`create_app` builds:

- Public, no token: ``GET /healthz``, ``GET /readyz``, ``GET /version`` (see
  :mod:`memini.api.health`) and the OpenAPI schema at ``GET /openapi.json``.
- Resource routes live under :data:`V1_PREFIX`. The bearer-token dependency goes on that one
  router, so every resource route needs a token and the public routes above do not.
- Other ASGI apps, such as the MCP endpoint, are mounted with ``app.mount(path, asgi_app)``. They
  get request ids from the outer middleware and handle their own errors.

The interactive documentation pages (Swagger UI and ReDoc) are off: they load scripts from a CDN,
so a browser that opens them on a local service would run third-party code in the service's
origin. Any OpenAPI viewer can read ``/openapi.json`` instead.
"""

from collections.abc import Mapping
from typing import Final

from fastapi import APIRouter, FastAPI
from fastapi.telemetry import TelemetryConfig
from starlette.types import ASGIApp

from memini import __version__
from memini.api.errors import ERROR_RESPONSES, install_error_handlers
from memini.api.health import DEFAULT_READINESS_TIMEOUT, ReadinessCheck, build_health_router
from memini.api.request_id import RequestIdMiddleware
from memini.config import Settings

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


def create_app(
    settings: Settings,
    *,
    readiness_checks: Mapping[str, ReadinessCheck] | None = None,
    readiness_timeout: float = DEFAULT_READINESS_TIMEOUT,
) -> FastAPI:
    """Build the HTTP application.

    Args:
        settings: the validated settings; kept in ``app.state.settings``.
        readiness_checks: checks that ``GET /readyz`` runs, by name, for example ``"database"``.
            Each returns when its dependency is usable and raises when it is not. With none,
            the service reports ready.
        readiness_timeout: seconds each check may take before it counts as failed.

    Raises:
        ValueError: ``readiness_timeout`` is not a finite number above zero.
    """
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
    )
    app.state.settings = settings
    install_error_handlers(app)

    app.include_router(build_health_router(readiness_checks or {}, readiness_timeout))

    # Resource routers are included into this router; the token dependency goes on it.
    v1 = APIRouter(prefix=V1_PREFIX)
    app.include_router(v1)

    return app
