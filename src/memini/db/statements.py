"""Timing of database statements, and the log of slow ones.

Every connection of the service's pool (:func:`~memini.db.open_pool`) makes its cursors from a
class :func:`timed_cursor` builds. Each ``execute`` and ``executemany`` is timed on the client, from
the call to the end of its result, waiting for the connection and for locks included: what the
caller waited. The time goes to the histogram ``memini_db_statement_duration_seconds``
(:mod:`memini.metrics`). A statement that took longer than the threshold
(``log_slow_statement_threshold``; 0 turns the log off) is logged at WARNING by the logger
``memini.db.statements``, failed ones included: a statement stopped by its time limit is the
slowest of all. The line carries the duration, the request id or the run id (:mod:`memini.logs`),
and the statement as :func:`statement_fingerprint` gives it:

- its text with every literal replaced by ``?``: quoted strings, dollar-quoted strings and numbers,
  so a value that :mod:`psycopg.sql` embedded (``sql.Literal``) is gone; comments are dropped and
  white space collapsed. Placeholders (``%s``, ``$1``) and identifiers stay;
- a fingerprint of that text, 16 hex digits, the same for every run of the statement, to group
  lines by.

Never the parameters of the statement (principle 11): they are sent apart from the text and never
read here. The text is the service's own SQL; psycopg types a query as a literal string, so no
value can be formatted into it but through ``sql.Literal``, which the fingerprint masks.

Timing on the client was chosen over the server's ``log_min_duration_statement``: that setting
needs a superuser or the database's own configuration, writes the statement to the database's
log with its parameters, and knows nothing of the request or the run the statement served.
"""

import hashlib
import logging
import re
import time
from collections.abc import Iterable
from typing import Any, ClassVar, Final, Self, cast

from psycopg import AsyncCursor, sql
from psycopg.abc import Params, Query, QueryNoTemplate

from memini.metrics import DB_SLOW_STATEMENTS, DB_STATEMENT_DURATION

__all__ = [
    "MAX_STATEMENT_TEXT",
    "TimedCursor",
    "statement_fingerprint",
    "timed_cursor",
]

MAX_STATEMENT_TEXT: Final = 500
"""Characters of a statement's text a slow-statement line keeps; a longer one is cut."""

_log = logging.getLogger(__name__)

# Comments are dropped; strings, dollar-quoted strings and numbers become "?"; quoted identifiers
# are matched only so that their content is left alone.
_TOKENS: Final = re.compile(
    r"""
      (?P<comment>--[^\n]*|/\*.*?\*/)
    | (?P<string>(?:[EeBbXxNn]|[Uu]&)?'(?:[^']|'')*'?)
    | (?P<dollar>\$\$.*?(?:\$\$|\Z)|\$(?P<tag>[A-Za-z_][A-Za-z0-9_]*)\$.*?(?:\$(?P=tag)\$|\Z))
    | (?P<identifier>"(?:[^"]|"")*"?)
    | (?P<number>(?<![\w$.])(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?(?!\w))
    """,
    re.DOTALL | re.VERBOSE,
)
_SPACE: Final = re.compile(r"\s+")


def _mask(match: re.Match[str]) -> str:
    if match.group("comment") is not None:
        return " "
    if match.group("identifier") is not None:
        return match.group(0)
    return "?"


def statement_fingerprint(text: str) -> tuple[str, str]:
    """``text`` without its literals and comments, and a fingerprint of that; see the module.

    The text is cut to :data:`MAX_STATEMENT_TEXT` characters; the fingerprint covers all of it.
    """
    masked = _SPACE.sub(" ", _TOKENS.sub(_mask, text)).strip()
    digest = hashlib.sha256(masked.encode("utf-8")).hexdigest()[:16]
    if len(masked) > MAX_STATEMENT_TEXT:
        masked = masked[: MAX_STATEMENT_TEXT - 1] + "…"
    return masked, digest


class TimedCursor[Row](AsyncCursor[Row]):
    """A cursor that times its statements; see the module. Make the class with
    :func:`timed_cursor`, which sets the threshold."""

    __slots__ = ()

    slow_after: ClassVar[float] = 0.0
    """Seconds after which a statement is logged as slow; 0: never."""

    async def execute(
        self,
        query: Query,
        params: Params | None = None,
        *,
        prepare: bool | None = None,
        binary: bool | None = None,
    ) -> Self:
        started = time.perf_counter()
        failed = True
        try:
            # Template queries (Python 3.14 t-strings) are not used: every query is a literal
            # string or composed with psycopg.sql.
            await super().execute(
                cast(QueryNoTemplate, query), params, prepare=prepare, binary=binary
            )
            failed = False
        finally:
            self._timed(query, time.perf_counter() - started, failed=failed, call="execute")
        return self

    async def executemany(
        self, query: Query, params_seq: Iterable[Params], *, returning: bool = False
    ) -> None:
        started = time.perf_counter()
        failed = True
        try:
            await super().executemany(query, params_seq, returning=returning)
            failed = False
        finally:
            self._timed(query, time.perf_counter() - started, failed=failed, call="executemany")

    def _timed(self, query: Query, seconds: float, *, failed: bool, call: str) -> None:
        DB_STATEMENT_DURATION.observe(seconds)
        if self.slow_after <= 0 or seconds <= self.slow_after:
            return
        DB_SLOW_STATEMENTS.inc()
        text, fingerprint = statement_fingerprint(self._text(query))
        _log.warning(
            "slow statement: %d ms",
            round(seconds * 1000),
            extra={
                "duration_ms": round(seconds * 1000, 1),
                "fingerprint": fingerprint,
                "statement": text,
                "call": call,
                "failed": failed,
            },
        )

    def _text(self, query: Query) -> str:
        """The text of ``query``; a composed one as it was sent, literals included."""
        try:
            if isinstance(query, str):
                return query
            if isinstance(query, bytes):
                return query.decode("utf-8", "replace")
            if isinstance(query, sql.Composable):
                return query.as_string(self)
        except Exception as exc:  # a closed connection, an encoding it cannot render
            return f"<{type(exc).__name__} while rendering the statement>"
        return f"<{type(cast(object, query)).__name__}>"


def timed_cursor(slow_after: float) -> type[TimedCursor[Any]]:
    """A cursor class for a connection: it times every statement and logs those over
    ``slow_after`` seconds; 0 logs none.

    Raises:
        ValueError: ``slow_after`` is below 0.
    """
    if not slow_after >= 0:
        raise ValueError("the slow-statement threshold is 0 or more seconds")

    class Timed(TimedCursor[Any]):
        __slots__ = ()

    Timed.slow_after = slow_after
    return Timed
