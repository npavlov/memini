"""Time limits of the database sessions that serve a request.

The pool checks every connection before it lends it out (:func:`prepare_connection`, the ``check``
of :func:`~memini.db.open_pool`). The same round trip sets the connection's time limits for the
borrower:

- inside :func:`session_limits`, which the HTTP application enters for every request
  (:mod:`memini.api.limits`), the request's: ``statement_timeout`` (how long one statement may run,
  waiting for locks included) and ``idle_in_transaction_session_timeout`` (how long a transaction
  may stay open with nothing running; past it the database closes the session);
- outside it, the database's own defaults (``RESET``): the job worker, the scheduler and the CLI
  never inherit a request's limits from a connection a request used before, so a long sync keeps
  running as it did.

A context variable carries the limits, so they reach every connection borrowed while a request is
handled: by the route, by the authenticator, by an MCP tool, in whichever task, since a task copies
the context of the code that starts it. A statement may still set a shorter or longer limit of its
own for its transaction (``SET LOCAL``), as search does with ``search_timeout``.

A statement stopped by its limit raises :class:`psycopg.errors.QueryCanceled`; a session closed for
an idle transaction raises :class:`psycopg.errors.IdleInTransactionSessionTimeout` on its next use.
Both are in :data:`TIMEOUT_ERRORS`.
"""

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Final

from psycopg import AsyncConnection, errors, sql

__all__ = [
    "TIMEOUT_ERRORS",
    "SessionLimits",
    "current_session_limits",
    "prepare_connection",
    "session_limits",
]

TIMEOUT_ERRORS: Final = (errors.QueryCanceled, errors.IdleInTransactionSessionTimeout)
"""What a statement or a session stopped by its time limit raises."""


@dataclass(frozen=True, slots=True)
class SessionLimits:
    """Time limits of a session, in seconds. 0: no limit of its own, the database's default."""

    statement_timeout: float
    idle_in_transaction_timeout: float

    def __post_init__(self) -> None:
        if self.statement_timeout < 0 or self.idle_in_transaction_timeout < 0:
            raise ValueError("a time limit is 0 or more seconds")


_limits: ContextVar[SessionLimits | None] = ContextVar("memini_session_limits", default=None)


@contextmanager
def session_limits(limits: SessionLimits | None) -> Generator[None]:
    """Lend pooled connections with ``limits`` while the block runs; ``None``: with none."""
    token = _limits.set(limits)
    try:
        yield
    finally:
        _limits.reset(token)


def current_session_limits() -> SessionLimits | None:
    """The limits a connection borrowed now gets, or ``None`` outside :func:`session_limits`."""
    return _limits.get()


def _milliseconds(seconds: float) -> int:
    return max(1, round(seconds * 1000))


def _setting(name: sql.SQL, seconds: float) -> sql.Composed:
    if seconds == 0:
        return sql.SQL("RESET {}").format(name)
    return sql.SQL("SET {} = {}").format(name, sql.Literal(_milliseconds(seconds)))


def _limit_statement(limits: SessionLimits | None) -> sql.Composed:
    return sql.SQL("; ").join(
        [
            _setting(
                sql.SQL("statement_timeout"), 0 if limits is None else limits.statement_timeout
            ),
            _setting(
                sql.SQL("idle_in_transaction_session_timeout"),
                0 if limits is None else limits.idle_in_transaction_timeout,
            ),
        ]
    )


async def prepare_connection(conn: AsyncConnection[Any]) -> None:
    """Check that ``conn`` works and set its time limits for the borrower; the pool's ``check``.

    One round trip, as the pool's own check (``AsyncConnectionPool.check_connection``) makes. The
    settings are made outside a transaction, so they hold for the session until the next borrow
    sets them again. Raises what the connection raises; the pool then discards it and tries another.
    """
    statement = _limit_statement(_limits.get())
    if conn.autocommit:
        await conn.execute(statement)
        return
    await conn.set_autocommit(True)
    try:
        await conn.execute(statement)
    finally:
        # A closed connection cannot change mode; the error that closed it is the one to raise.
        if not conn.closed:
            await conn.set_autocommit(False)
