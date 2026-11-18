"""What the commands share: exit codes, expected errors, keeping the database URL out of output, and
a database session that acts as the configured principal."""

import sys
import traceback
from collections.abc import AsyncGenerator, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from typing import Final
from urllib.parse import urlsplit
from uuid import UUID

import psycopg
from psycopg import AsyncConnection
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import TupleRow

from memini.config import Settings
from memini.db import connect, pending_migrations
from memini.workspaces import ensure_principal

__all__ = [
    "EXIT_FAILURE",
    "EXIT_INTERRUPTED",
    "EXIT_OK",
    "EXIT_UNAVAILABLE",
    "EXIT_USAGE",
    "CommandError",
    "Conn",
    "DatabaseUrl",
    "format_time",
    "message",
    "principal_session",
    "print_table",
    "require_migrated",
]

EXIT_OK: Final = 0
EXIT_FAILURE: Final = 1
"""The command could not do what was asked: no such workspace or token, a name already taken, a
failed or pending migration, a server that did not start."""
EXIT_USAGE: Final = 2
"""Wrong arguments, or invalid configuration. argparse exits with 2 as well."""
EXIT_UNAVAILABLE: Final = 3
"""The database cannot be reached, or was lost while the command ran."""
EXIT_INTERRUPTED: Final = 130
"""Interrupted by Ctrl-C (128 + SIGINT), as shells report it. ``serve`` exits 0 instead."""

type Conn = AsyncConnection[TupleRow]


class CommandError(Exception):
    """An expected failure. Printed as one line, without a traceback, then the command exits."""

    def __init__(self, text: str, exit_code: int = EXIT_FAILURE) -> None:
        super().__init__(text)
        self.text = text
        self.exit_code = exit_code


def message(text: str) -> None:
    """Print ``memini: text`` to stderr, where everything but a command's result goes."""
    print(f"memini: {text}", file=sys.stderr, flush=True)


class DatabaseUrl:
    """The database URL of the settings, with what is needed to keep it out of every message.

    The URL is a secret: it carries the password. Messages name the server as ``host:port``.
    Text that comes from elsewhere, such as a psycopg error, goes through :meth:`redact` before it
    is printed, which removes the URL and the password. The CLI's own messages are built from known
    parts and are not redacted: a common word for a password would blank it out of them.
    """

    def __init__(self, settings: Settings) -> None:
        self._url: str = settings.database_url.get_secret_value()
        self._params: Mapping[str, object] | None = None
        with suppress(psycopg.Error):  # check() reports it; redact() still removes the whole URL
            self._params = conninfo_to_dict(self._url)
        secrets = {self._url, str((self._params or {}).get("password") or "")}
        with suppress(ValueError):
            secrets.add(urlsplit(self._url).password or "")  # still percent-encoded
        # Longest first, so a password inside the URL does not leave the rest of the URL behind.
        self._secrets: tuple[str, ...] = tuple(
            sorted((s for s in secrets if s), key=len, reverse=True)
        )

    def check(self) -> None:
        """Raise :class:`CommandError` (usage) if the URL cannot be parsed.

        libpq's own message is dropped: it can quote the part that failed, password included.
        """
        if self._params is None:
            raise CommandError(
                "the database URL (MEMINI_DATABASE_URL or MEMINI_DATABASE_URL_FILE) is not a"
                " valid Postgres connection URL",
                EXIT_USAGE,
            )

    @property
    def target(self) -> str:
        """``host:port`` of the server, for messages. Holds no credentials."""
        params = self._params or {}
        host = params.get("host") or "localhost"
        port = params.get("port") or "5432"
        return f"{host}:{port}"

    def redact(self, text: str) -> str:
        """``text`` with the URL and the password replaced by ``***``."""
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text

    def unavailable(self, error: psycopg.OperationalError) -> str:
        """One line on why the database cannot be used, from a psycopg error. Redacted."""
        return f"cannot use the database at {self.target}: {self.redact(_reason(error))}"

    def format_traceback(self, error: BaseException) -> str:
        """The traceback of ``error`` and the errors it chains, with each error's text redacted.

        Frames show file names and source lines, never values, so they are kept as they are.
        """
        chain: list[BaseException] = []
        current: BaseException | None = error
        while current is not None and all(current is not seen for seen in chain):
            chain.append(current)
            current = current.__cause__ or (
                None if current.__suppress_context__ else current.__context__
            )
        parts: list[str] = []
        for link in reversed(chain):
            if parts:
                parts.append("\nThe error above led to the one below:\n\n")
            parts.append("Traceback (most recent call last):\n")
            parts.extend(traceback.format_tb(link.__traceback__))
            parts.extend(self.redact(line) for line in traceback.format_exception_only(link))
        return "".join(parts)


def _reason(error: psycopg.OperationalError) -> str:
    """The cause in a libpq message, without the parts that repeat the host and port.

    ``connection failed: connection to server at "127.0.0.1", port 1 failed: Connection refused``
    becomes ``Connection refused``. Only the first line is kept: further lines repeat each address
    that was tried.
    """
    lines = str(error).strip().splitlines()
    text = lines[0] if lines else ""
    for prefix in ("connection failed: ", "connection is bad: "):
        text = text.removeprefix(prefix)
    if " failed: " in text:
        text = text.rsplit(" failed: ", 1)[1]
    text = text.removeprefix("FATAL:").strip()
    return text or type(error).__name__


async def require_migrated(conn: Conn, hint: str) -> None:
    """Raise :class:`CommandError` if migrations are pending, naming them and what to run."""
    pending = await pending_migrations(conn)
    if pending:
        names = ", ".join(migration.file_name for migration in pending)
        raise CommandError(f"the database has pending migrations ({names}); {hint}")


@asynccontextmanager
async def principal_session(settings: Settings) -> AsyncGenerator[tuple[Conn, UUID]]:
    """A connection in one transaction, and the id of the principal the CLI acts as.

    The database must be migrated. The principal named by ``settings.principal`` is created if it
    does not exist. The transaction commits when the block ends and rolls back if it raises.
    """
    async with connect(settings.database_url.get_secret_value()) as conn:
        await require_migrated(conn, "run `memini migrate` first")
        owner_id = await ensure_principal(conn, settings.principal)
        yield conn, owner_id


def format_time(value: datetime | None, none: str = "never") -> str:
    """A time in UTC to the minute, e.g. ``2026-11-18T16:02Z``; ``none`` for ``None``."""
    if value is None:
        return none
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%MZ")


def print_table(header: Sequence[str], rows: Iterable[Sequence[str]]) -> None:
    """Print rows to stdout in columns aligned with two spaces, under an upper-case header."""
    table = [list(header), *(list(row) for row in rows)]
    widths = [max(len(cell) for cell in column) for column in zip(*table, strict=True)]
    for row in table:
        line = "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True))
        print(line.rstrip())
