"""``memini serve``: run the service until SIGINT or SIGTERM.

Order of start: logging, migrations (refuse to start while any is pending, unless ``--migrate``
applies them), the connection pool, the embedder (:func:`service_embedder`), the application, the
job handlers (:func:`job_handlers`), the job worker and scheduler, the HTTP server. Order of stop:
the HTTP server finishes open requests (up to :data:`SHUTDOWN_GRACE` seconds) and runs the
application's shutdown; the worker and scheduler stop (a running job finishes within the worker's
grace period or goes back to the queue); pending run reports are written; the embedder is closed;
then the pool closes. A stop by signal exits 0.

One embedder serves the search queries of the application and the ``git_sync`` and ``mcp_sync``
jobs: a local model is loaded once. ``retention`` jobs need no embedder and always run: the
scheduler enqueues one for each source with a retention every ``retention_interval``. ``mcp_sync``
jobs always run too (:mod:`memini.executors.service`).

Logs are JSON lines on stderr through :func:`memini.logs.configure_logging`, which masks tokens and
secrets; the password of the database URL and the metrics token are registered as secrets at start.
A statement of the pool slower than ``log_slow_statement_threshold`` is logged
(:mod:`memini.db.statements`).

uvicorn runs without its ``[standard]`` extras: its pure-Python HTTP parser (h11) is enough for a
service bound to localhost, and the extras (uvloop, httptools, websockets, watchfiles) serve nothing
Memini uses. Its access log is off: a request line carries the query string, which can hold private
text (principle 11).
"""

import argparse
import logging
import signal
import threading
from collections.abc import AsyncGenerator, Generator, Mapping
from contextlib import asynccontextmanager, contextmanager
from datetime import timedelta
from types import FrameType
from typing import Final

import anyio
import uvicorn
from starlette.types import ASGIApp

from memini import logs
from memini.api import create_app
from memini.cli.common import EXIT_OK, CommandError, require_migrated
from memini.config import Settings
from memini.credentials import Keyring
from memini.db import Pool, connect, database_check, migrate, open_pool
from memini.embeddings import Embedder, EmbeddingsNotConfiguredError, create_embedder
from memini.executors.service import MCP_SYNC, ServiceOptions, mcp_sync_handler
from memini.extraction.job import extract_handler, extraction_enabled
from memini.git import GitOptions, MirrorManager
from memini.gitsync import GIT_SYNC, git_sync_handler, mirrors_unusable
from memini.ingest import PayloadIndexer
from memini.jobs import (
    DEFAULT_RETENTION_INTERVAL,
    EXTRACT_KIND,
    SYNC_KINDS,
    Handler,
    Scheduler,
    Worker,
)
from memini.llm import LlmGate, create_llm_gate
from memini.removal import RETENTION, retention_handler
from memini.runs import wait_for_run_writes

__all__ = [
    "SHUTDOWN_GRACE",
    "background_jobs",
    "configure_logging",
    "job_handlers",
    "run_http_server",
    "serve_command",
    "service_embedder",
]

SHUTDOWN_GRACE: Final = 10
"""Seconds open requests and streams get to finish after a stop is asked for."""

_STOP_SIGNALS: Final = (signal.SIGINT, signal.SIGTERM)

log = logging.getLogger(__name__)


def configure_logging(settings: Settings) -> None:
    """Send every log record to stderr as a JSON line at ``settings.log_level``, redacted.

    The one place ``serve`` sets up logging. The password of the database URL and the metrics
    token are registered as secrets, so they are masked wherever they would appear.
    """
    logs.configure_logging(settings.log_level)
    logs.register_url_secrets(settings.database_url.get_secret_value())
    if settings.metrics_token is not None:
        logs.register_secret(settings.metrics_token.get_secret_value())


@asynccontextmanager
async def service_embedder(settings: Settings) -> AsyncGenerator[Embedder | None]:
    """The embedder the settings describe, for the duration of the block, then closed; ``None``
    without an embedding provider. ``serve`` shares it between the application and the jobs.

    Raises:
        EmbeddingError: a setting the provider needs is missing (settings built by hand only).
    """
    try:
        embedder = create_embedder(settings)
    except EmbeddingsNotConfiguredError:
        yield None
        return
    try:
        yield embedder
    finally:
        await embedder.aclose()


@asynccontextmanager
async def job_handlers(
    settings: Settings, pool: Pool, *, embedder: Embedder | None = None
) -> AsyncGenerator[Mapping[str, Handler]]:
    """The job handlers ``serve`` runs, by job kind, for the duration of the block.

    ``retention`` (:mod:`memini.removal`) is always there, and so is ``mcp_sync``
    (:mod:`memini.executors.service`): an mcp sync without an embedding provider stores its
    records without chunks, as the push API does, and ``memini reindex`` indexes them later; a
    source whose credential needs ``credential_key`` fails its runs with a message naming it,
    while sources without a credential sync. ``extract`` (:mod:`memini.extraction.job`) is there
    when an LLM provider is configured, with an LLM gate of its own that is closed at the end;
    ingest enqueues its jobs only then. ``git_sync`` (:mod:`memini.gitsync`) is
    there when an embedding provider is configured and
    mirrors can be kept: the git executable is on ``PATH`` and the mirror directory can be created
    and written. Code is synced only into an index: without embeddings there is none to write, and
    records synced without their chunks would be left out of the index once embeddings are set,
    since a sync writes only what changed. Otherwise a warning says why git sources are not
    synced, and the scheduler leaves their streams alone.

    ``embedder`` is the service's, which the caller closes; without it, one is created from the
    settings and closed at the end.
    """
    handlers: dict[str, Handler] = {
        RETENTION: retention_handler(pool, batch_size=settings.retention_batch_size)
    }
    owned = embedder is None
    if embedder is None:
        try:
            embedder = create_embedder(settings)
        except EmbeddingsNotConfiguredError as error:
            log.warning("git sources are not synced: %s", error)
    gate: LlmGate | None = None
    try:
        handlers[MCP_SYNC] = _mcp_handler(settings, pool, embedder)
        if extraction_enabled(settings):
            gate = create_llm_gate(settings)
            handlers[EXTRACT_KIND] = extract_handler(
                pool, gate, candidates=settings.extraction_candidates
            )
        else:
            log.info("decisions are not extracted: no LLM provider is configured")
        if embedder is not None:
            options = GitOptions.from_settings(settings)
            problem = mirrors_unusable(options)
            if problem is None:
                handlers[GIT_SYNC] = git_sync_handler(
                    pool,
                    MirrorManager(options),
                    embedder,
                    retention_batch=settings.retention_batch_size,
                )
            else:
                log.warning("git sources are not synced: %s", problem)
        yield handlers
    finally:
        if gate is not None:
            await gate.aclose()
        if owned and embedder is not None:
            await embedder.aclose()


def _mcp_handler(settings: Settings, pool: Pool, embedder: Embedder | None) -> Handler:
    options = ServiceOptions.from_settings(settings)
    if not options.policy.endpoints and not options.policy.commands:
        log.info(
            "mcp sources reach nothing until MEMINI_MCP_ALLOWED_ENDPOINTS or"
            " MEMINI_MCP_ALLOWED_COMMANDS lists their servers"
        )
    if embedder is None:
        log.info("mcp sources are synced without embeddings: their records are not indexed")
    return mcp_sync_handler(
        pool,
        keyring=Keyring.from_settings(settings),
        indexer=None if embedder is None else PayloadIndexer(embedder),
        options=options,
    )


@asynccontextmanager
async def background_jobs(
    pool: Pool,
    handlers: Mapping[str, Handler],
    *,
    retention_interval: timedelta = DEFAULT_RETENTION_INTERVAL,
) -> AsyncGenerator[None]:
    """Run the job worker and the scheduler on ``pool`` for the duration of the block.

    The worker runs the kinds in ``handlers``; the scheduler enqueues syncs only for the source
    kinds whose sync job has a handler, and retention passes (every ``retention_interval`` per
    source) only with a ``retention`` handler, so nothing piles up that no worker could run. With
    no handlers, neither starts. At the end of the block both are stopped and awaited, and pending
    run reports are written.
    """
    sync_kinds = {source: kind for source, kind in SYNC_KINDS.items() if kind in handlers}
    retention_every = retention_interval if RETENTION in handlers else None
    services: list[Worker | Scheduler] = []
    if handlers:
        services.append(Worker(pool, handlers))
    if sync_kinds or retention_every is not None:
        services.append(Scheduler(pool, sync_kinds=sync_kinds, retention_every=retention_every))
    if not services:
        log.info("no job handlers yet: the job worker and the scheduler are not started")
    try:
        async with anyio.create_task_group() as group:
            for service in services:
                group.start_soon(service.run)
            try:
                yield
            finally:
                for service in services:
                    service.stop()
    finally:
        await wait_for_run_writes()


async def serve_command(args: argparse.Namespace, settings: Settings) -> int:
    migrate_first: bool = args.migrate
    configure_logging(settings)
    url = settings.database_url.get_secret_value()

    if migrate_first:
        applied = await migrate(url)
        log.info("%d migrations applied", len(applied))
    else:
        async with connect(url) as conn:
            await require_migrated(
                conn, "run `memini migrate`, or start with `memini serve --migrate`"
            )

    async with (
        open_pool(url, slow_statement_threshold=settings.log_slow_statement_threshold) as pool,
        service_embedder(settings) as embedder,
    ):
        app = create_app(
            settings,
            pool=pool,
            readiness_checks={"database": database_check(pool)},
            embedder=embedder,
        )
        async with (
            job_handlers(settings, pool, embedder=embedder) as handlers,
            background_jobs(
                pool, handlers, retention_interval=timedelta(seconds=settings.retention_interval)
            ),
        ):
            started = await run_http_server(app, settings)
    if not started:
        raise CommandError("the HTTP server did not start; see the log above")
    log.info("memini stopped")
    return EXIT_OK


async def run_http_server(app: ASGIApp, settings: Settings) -> bool:
    """Serve ``app`` on ``settings.http_host:http_port`` until SIGINT or SIGTERM.

    Returns whether the server started. It does not when the address cannot be bound or the
    application's startup (lifespan) fails; uvicorn logs why.
    """
    config = uvicorn.Config(
        app,
        host=settings.http_host,
        port=settings.http_port,
        lifespan="on",  # a failing application startup stops the server, it is not skipped
        log_config=None,  # logging is configured by configure_logging
        access_log=False,
        server_header=False,
        # No X-Forwarded-For: the client address is the peer's. Trusting the header (uvicorn trusts
        # it from 127.0.0.1 by default) would let a local process pick the address its requests
        # are rate-limited under.
        proxy_headers=False,
        timeout_graceful_shutdown=SHUTDOWN_GRACE,
    )
    server = uvicorn.Server(config)
    with _stop_signals_end_normally():
        try:
            await server.serve()
        except SystemExit:  # uvicorn's way to fail a start, after it has logged the reason
            return False
    return server.started


@contextmanager
def _stop_signals_end_normally() -> Generator[None]:
    """Make a stop by SIGINT or SIGTERM end the command normally, with exit code 0.

    While it serves, uvicorn handles both signals: it stops gracefully. Afterwards it puts back the
    handlers it found and raises the signal again, so that the default action still happens: a
    ``KeyboardInterrupt``, or death by SIGTERM before the pool is closed. Here the stop was asked
    for and is done, so the handlers it finds ignore the signal. Signals can be handled in the main
    thread only; elsewhere this does nothing.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = {number: signal.signal(number, _ignore) for number in _STOP_SIGNALS}
    try:
        yield
    finally:
        for number, handler in previous.items():
            if handler is not None:
                signal.signal(number, handler)


def _ignore(number: int, frame: FrameType | None) -> None:
    del number, frame
