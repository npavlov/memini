"""``GET /metrics``: the service's metrics for Prometheus, and the HTTP metrics behind them.

**Access.** The route exists only when ``metrics_token`` is set, and answers only a request whose
bearer token is that value (compared in constant time); without it 401 ``unauthorized``, with
another value 401 ``invalid_token``, before anything is read. A Memini token is not accepted, an
admin one included: a scraper's credential sits in a monitoring configuration, and a Memini token
that may read metrics would read content as well. The metrics token reads counts only. The route is
outside ``/v1`` and not rate-limited, like the other public routes: the token is 32 or more random
characters, and a wrong one costs a comparison, no database work.

**What it answers** (:data:`memini.metrics.CONTENT_TYPE`): the counters and histograms the code
keeps (:mod:`memini.metrics`), and gauges read now:

- freshness, from :func:`memini.status.read_status` over every workspace, so
  ``memini_base_current`` is what ``GET /v1/status`` and the gate say: whether the database is
  reachable, streams by ``current`` and by each reason they are behind, the largest lag of a
  scheduled stream in seconds and in schedules, streams that fail, are missing or still load their
  history, workspaces by index state, the service's index state, live records, records not
  indexed, chunks;
- the job queue: queued jobs by kind, those already due, the longest a due job has waited, running
  jobs by kind;
- the connection pool: its size, idle connections, requests waiting, and its counters.

None of them is labelled by a workspace, source, stream or person: names can be private, and
metrics often go to a monitoring service outside the store (principle 11). ``memini status`` says
which stream is behind. A scrape runs one status read, as one ``GET /v1/status`` does.

**HTTP metrics.** :class:`HttpMetricsMiddleware` wraps the whole application, inside the request-id
middleware, so it sees every answer, a 429 of the rate limits and a 500 included. It counts each
request by route template (``/v1/workspaces/{workspace}``), never by its path, which holds names and
ids; a request that matched no route is ``unmatched``. Methods outside the standard ones are
``other``. The templates are the application's own (:class:`RouteTemplates`, filled by
:func:`memini.api.create_app` with each router's prefix): the route the router chose, which it
leaves in the scope, or, for a request answered before routing, the first template whose pattern
matches the path, in the order the routes were added.
"""

import hmac
import re
import time
import uuid
from collections.abc import Iterable, Mapping
from typing import Final, cast, get_args

from fastapi import APIRouter, Request, Response
from psycopg import sql
from pydantic import SecretStr
from starlette.routing import BaseRoute, compile_path
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from memini import __version__, metrics, status
from memini.api.auth import unauthorized_error
from memini.auth import Principal, ReadFilter, authorize_read
from memini.db import Pool
from memini.metrics import HTTP_DURATION, HTTP_REQUESTS, Family, family, gauge
from memini.query import QueryServices

__all__ = [
    "METRICS_PATH",
    "SERVICE_READER",
    "UNMATCHED",
    "HttpMetricsMiddleware",
    "RouteTemplates",
    "build_metrics_router",
    "read_gauges",
]

METRICS_PATH: Final = "/metrics"

UNMATCHED: Final = "unmatched"
"""The route label of a request that matched no route."""

_METHODS: Final = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})

SERVICE_READER: Final[ReadFilter] = authorize_read(
    Principal(principal_id=uuid.UUID(int=0), token_id=uuid.UUID(int=0), scopes=frozenset({"read"}))
)
"""How the metrics read the base: every workspace, as the service itself, for counts only. In v1
the filter narrows by a token's workspace and nothing else; with several users (requirements,
section 8) metrics stay counts across the whole base."""

# --- HTTP requests -------------------------------------------------------------------------------


class RouteTemplates:
    """The full template of each route of the application, and how to find a request's."""

    def __init__(self) -> None:
        self._by_route: dict[int, str] = {}
        self._patterns: list[tuple[re.Pattern[str], str]] = []

    def add(self, routes: Iterable[BaseRoute], prefix: str = "") -> None:
        """Add ``routes``, as included under ``prefix``. A route without a path (an included
        router) is skipped; add its routes with their prefix instead."""
        for route in routes:
            path = getattr(route, "path", None)
            if not isinstance(path, str):
                continue
            template = prefix + path
            self._by_route[id(route)] = template
            pattern, _, _ = compile_path(template)
            self._patterns.append((pattern, template))

    def of(self, scope: Scope) -> str:
        """The template of the request of ``scope``, or :data:`UNMATCHED`."""
        template = self._by_route.get(id(scope.get("route")))
        if template is not None:
            return template
        path: str = scope["path"]
        root: str = scope.get("root_path", "")
        if root and path.startswith(root):
            path = path[len(root) :]
        for pattern, candidate in self._patterns:
            if pattern.match(path):
                return candidate
        return UNMATCHED


class HttpMetricsMiddleware:
    """ASGI middleware that counts and times every HTTP request; see the module."""

    def __init__(self, app: ASGIApp, templates: RouteTemplates) -> None:
        self.app = app
        self.templates = templates

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        answered: int | None = None

        async def watched_send(message: Message) -> None:
            nonlocal answered
            if message["type"] == "http.response.start":
                answered = cast(int, message["status"])
            await send(message)

        try:
            await self.app(scope, receive, watched_send)
        finally:
            route = self.templates.of(scope)
            method = scope["method"] if scope["method"] in _METHODS else "other"
            HTTP_REQUESTS.inc(
                route=route, method=method, status="none" if answered is None else str(answered)
            )
            HTTP_DURATION.observe(time.perf_counter() - started, route=route, method=method)


# --- Gauges read at scrape time -------------------------------------------------------------------


def _flag(value: bool) -> float:
    return 1.0 if value else 0.0


def _freshness(answer: status.ServiceStatus) -> list[Family]:
    streams = [stream for workspace in answer.workspaces for stream in workspace.streams]
    by_current = {"true": 0, "false": 0, "unjudged": 0}
    behind = dict.fromkeys(get_args(status.BehindReason), 0)
    lags: list[float] = []
    ratios: list[float] = []
    for stream in streams:
        by_current["unjudged" if stream.current is None else str(stream.current).lower()] += 1
        for reason in stream.behind:
            behind[reason] += 1
        if stream.schedule_seconds is not None and stream.lag_seconds is not None:
            lags.append(stream.lag_seconds)
            ratios.append(stream.lag_seconds / stream.schedule_seconds)
    index_states = dict.fromkeys(get_args(status.WorkspaceIndexState), 0)
    for workspace in answer.workspaces:
        index_states[workspace.index.state] += 1
    service_states: tuple[str, ...] = get_args(status.ServiceIndexState)
    return [
        gauge(
            "memini_database_reachable",
            "1 when the service reaches its database, else 0.",
            [({}, _flag(answer.database.reachable))],
        ),
        gauge(
            "memini_base_current",
            "1 when the base is current (GET /v1/status?gate=true answers 200), else 0.",
            [({}, _flag(answer.current))],
        ),
        gauge(
            "memini_streams",
            "Streams by current: true, false (behind), unjudged (no schedule, not behind).",
            [({"current": key}, value) for key, value in by_current.items()],
        ),
        gauge(
            "memini_streams_behind",
            "Streams behind, by reason: never_checked, lag, not_indexed, index_stale. A stream"
            " can be behind for several.",
            [({"reason": key}, value) for key, value in behind.items()],
        ),
        gauge(
            "memini_streams_failing",
            "Streams whose runs failed since the last that succeeded or was partial.",
            [({}, sum(1 for stream in streams if stream.failures > 0))],
        ),
        gauge(
            "memini_streams_missing",
            "Streams whose ref or channel no longer exists at the source.",
            [({}, sum(1 for stream in streams if stream.missing))],
        ),
        gauge(
            "memini_streams_history_loading",
            "Streams whose history is not loaded back to its start yet.",
            [({}, sum(1 for stream in streams if stream.history_loaded is False))],
        ),
        gauge(
            "memini_stream_lag_max_seconds",
            "The largest lag of a scheduled stream that has stored a window; 0 with none.",
            [({}, max(lags, default=0.0))],
        ),
        gauge(
            "memini_stream_lag_schedules_max",
            "The largest lag of a scheduled stream, in schedules: over"
            f" {status.STALE_AFTER_SCHEDULES} it is behind. 0 with none.",
            [({}, max(ratios, default=0.0))],
        ),
        gauge(
            "memini_workspaces",
            "Workspaces by the state of their index: ok, stale, none, off.",
            [({"index": key}, value) for key, value in index_states.items()],
        ),
        gauge(
            "memini_workspaces_index_behind",
            "Workspaces whose index is behind: stale, or records not indexed.",
            [({}, sum(1 for workspace in answer.workspaces if workspace.index.behind))],
        ),
        gauge(
            "memini_index_state",
            "The service's index across workspaces: 1 for its state, 0 for the others.",
            [({"state": state}, _flag(answer.index.state == state)) for state in service_states],
        ),
        gauge(
            "memini_records",
            "Live records.",
            [({}, sum(workspace.index.records for workspace in answer.workspaces))],
        ),
        gauge(
            "memini_records_not_indexed",
            "Live records with a payload and no chunk.",
            [({}, sum(workspace.index.records_not_indexed for workspace in answer.workspaces))],
        ),
        gauge(
            "memini_chunks",
            "Chunks in the index.",
            [({}, sum(workspace.index.chunks for workspace in answer.workspaces))],
        ),
    ]


# Two scans, each of one partial index (job_queued_idx, job_running_idx); finished jobs stay in
# the table and are never read here.
_JOBS: Final = sql.SQL("""
SELECT 'queued', j.kind, count(*), count(*) FILTER (WHERE j.run_after <= now()),
       extract(epoch FROM now() - min(j.run_after) FILTER (WHERE j.run_after <= now()))::float8
FROM job j
WHERE j.status = 'queued' AND {queued_readable}
GROUP BY j.kind
UNION ALL
SELECT 'running', j.kind, count(*), 0, NULL
FROM job j
WHERE j.status = 'running' AND {running_readable}
GROUP BY j.kind
""")


def _readable(access: ReadFilter) -> sql.Composable:
    return access.where(
        workspace_id=sql.Identifier("j", "workspace_id"),
        owner_id=sql.Identifier("j", "owner_id"),
        source_id=sql.Identifier("j", "source_id"),
    )


async def _jobs(pool: Pool, access: ReadFilter) -> list[Family]:
    query = _JOBS.format(queued_readable=_readable(access), running_readable=_readable(access))
    async with pool.connection() as conn:
        cur = await conn.execute(query)
        rows = await cur.fetchall()
    counts: list[tuple[Mapping[str, str], float]] = []
    due: list[tuple[Mapping[str, str], float]] = []
    waited = 0.0
    for state, kind, count, ready, oldest in rows:
        counts.append(({"kind": kind, "state": state}, count))
        if state == "queued":
            due.append(({"kind": kind}, ready))
            if oldest is not None:
                waited = max(waited, oldest)
    return [
        gauge("memini_jobs", "Jobs queued or running, by kind and state.", counts),
        gauge("memini_jobs_due", "Queued jobs whose time has come, by kind.", due),
        gauge(
            "memini_job_wait_max_seconds",
            "How long the queued job that is due longest has waited for a worker; 0 with none.",
            [({}, waited)],
        ),
    ]


_POOL_GAUGES: Final = (
    ("pool_max", "memini_db_pool_max", "The most connections the pool opens."),
    ("pool_size", "memini_db_pool_connections", "Connections the pool holds, lent or idle."),
    ("pool_available", "memini_db_pool_idle", "Idle connections in the pool."),
    ("requests_waiting", "memini_db_pool_requests_waiting", "Requests waiting for a connection."),
)
_POOL_COUNTERS: Final = (
    ("requests_num", "memini_db_pool_requests_total", "Connections asked of the pool.", 1.0),
    (
        "requests_queued",
        "memini_db_pool_requests_queued_total",
        "Requests that had to wait for a connection.",
        1.0,
    ),
    (
        "requests_wait_ms",
        "memini_db_pool_requests_wait_seconds_total",
        "Time requests waited for a connection.",
        0.001,
    ),
    (
        "requests_errors",
        "memini_db_pool_requests_errors_total",
        "Requests that got no connection: a timeout, or the pool closed.",
        1.0,
    ),
    (
        "connections_errors",
        "memini_db_pool_connection_errors_total",
        "Failed attempts to open a connection.",
        1.0,
    ),
    (
        "connections_lost",
        "memini_db_pool_connections_lost_total",
        "Connections found broken when they were checked.",
        1.0,
    ),
)


def _pool(pool: Pool) -> list[Family]:
    stats = pool.get_stats()
    families = [
        gauge(name, help, [({}, float(stats.get(key, 0)))]) for key, name, help in _POOL_GAUGES
    ]
    families.extend(
        family(name, "counter", help, [({}, stats.get(key, 0) * scale)])
        for key, name, help, scale in _POOL_COUNTERS
    )
    return families


async def read_gauges(
    pool: Pool | None, services: QueryServices, *, started: float
) -> list[Family]:
    """The gauges a scrape reads now; see the module. ``started``: the process's start, as a Unix
    time."""
    families = [
        gauge("memini_info", "The Memini version; always 1.", [({"version": __version__}, 1.0)]),
        gauge(
            "memini_start_time_seconds",
            "When the application started, in Unix time.",
            [({}, started)],
        ),
    ]
    answer = await status.read_status(
        pool, SERVICE_READER, fingerprint=status.service_fingerprint(services.embedder)
    )
    families.extend(_freshness(answer))
    if pool is not None:
        families.extend(_pool(pool))
        if answer.database.reachable:
            families.extend(await _jobs(pool, SERVICE_READER))
    return families


# --- The route -----------------------------------------------------------------------------------


def _check_token(request: Request, expected: bytes) -> None:
    """Pass a request whose bearer token is the metrics token; 401 otherwise."""
    header = request.headers.get("Authorization")
    scheme, _, credentials = (header or "").strip().partition(" ")
    if header is None or scheme.lower() != "bearer":
        raise unauthorized_error("missing")
    if not hmac.compare_digest(credentials.strip().encode("utf-8"), expected):
        raise unauthorized_error("unknown")


def build_metrics_router(token: SecretStr) -> APIRouter:
    """``GET /metrics`` for a scraper that sends ``token``; see the module."""
    expected = token.get_secret_value().encode("utf-8")
    router = APIRouter(tags=["health"])
    started = time.time()

    @router.get(
        METRICS_PATH,
        summary="Metrics for Prometheus",
        response_class=Response,
        responses={200: {"content": {metrics.CONTENT_TYPE: {}}, "description": "The metrics."}},
    )
    async def get_metrics(request: Request) -> Response:
        """Counts and timings of the service in the Prometheus text format. Needs the metrics
        token (``metrics_token``) as the bearer token; a Memini token is not accepted."""
        _check_token(request, expected)
        pool = cast(Pool | None, getattr(request.app.state, "pool", None))
        services = cast(QueryServices, request.app.state.services)
        families: Iterable[Family] = [
            *metrics.REGISTRY.collect(),
            *await read_gauges(pool, services, started=started),
        ]
        return Response(
            metrics.render(families),
            media_type=metrics.CONTENT_TYPE,
            headers={"Cache-Control": "no-store"},
        )

    return router
