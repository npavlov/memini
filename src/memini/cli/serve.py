"""``memini serve``: run the service until SIGINT or SIGTERM.

Order of start: logging, migrations (refuse to start while any is pending, unless ``--migrate``
applies them), the connection pool, the application, the HTTP server. Order of stop: the HTTP
server finishes open requests (up to :data:`SHUTDOWN_GRACE` seconds) and runs the application's
shutdown, then the pool closes. A stop by signal exits 0.

uvicorn runs without its ``[standard]`` extras: its pure-Python HTTP parser (h11) is enough for a
service bound to localhost, and the extras (uvloop, httptools, websockets, watchfiles) serve nothing
Memini uses. Its access log is off: a request line carries the query string, which can hold private
text (principle 11).
"""

import argparse
import logging
import signal
import threading
from collections.abc import Generator
from contextlib import contextmanager
from types import FrameType
from typing import Final

import uvicorn
from starlette.types import ASGIApp

from memini.api import create_app
from memini.cli.common import EXIT_OK, CommandError, require_migrated
from memini.config import Settings
from memini.db import connect, database_check, migrate, open_pool

__all__ = ["SHUTDOWN_GRACE", "configure_logging", "run_http_server", "serve_command"]

SHUTDOWN_GRACE: Final = 10
"""Seconds open requests and streams get to finish after a stop is asked for."""

_STOP_SIGNALS: Final = (signal.SIGINT, signal.SIGTERM)

log = logging.getLogger(__name__)


def configure_logging(settings: Settings) -> None:
    """Send log records to stderr at ``settings.log_level``. The one place ``serve`` sets up
    logging; replace the body to change how the service logs."""
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


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

    async with open_pool(url) as pool:
        app = create_app(settings, pool=pool, readiness_checks={"database": database_check(pool)})
        # Background work (the job worker, the scheduler) starts here, beside the HTTP server, on
        # the same pool, and is stopped and awaited once run_http_server returns.
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
