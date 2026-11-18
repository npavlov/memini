"""Schema migrations: numbered SQL files, applied forward only.

A migration is a file `migrations/NNNN_name.sql` in this package. Versions start at 0001 and go up
by one, without gaps. Each file runs in one transaction together with its row in `schema_migration`,
so it applies whole or not at all. Write a new file to change the schema; never edit an applied one.

`migrate` takes a session advisory lock first. A second process that starts at the same time waits
for the lock and then finds nothing to do. The lock uses the single 64-bit key space of Postgres
advisory locks; locks with two 32-bit keys (for example per source) never collide with it.
"""

import hashlib
import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from typing import Any, Final

import psycopg
from psycopg import AsyncConnection
from psycopg.rows import tuple_row

from memini.db.connection import connect

log = logging.getLogger(__name__)

MIGRATION_LOCK_KEY: Final = 0x6D656D696E69
"""Advisory lock held while migrating: "memini" in ASCII."""

_FILE_NAME: Final = re.compile(r"(\d{4})_([a-z0-9_]+)\.sql")

_CREATE_BOOKKEEPING: Final = """
CREATE TABLE IF NOT EXISTS schema_migration (
    version    integer     PRIMARY KEY,
    name       text        NOT NULL,
    checksum   text        NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""


class MigrationError(Exception):
    """The migrations on disk and in the database do not fit together, or one failed."""


@dataclass(frozen=True)
class Migration:
    """One migration file."""

    version: int
    name: str
    sql: bytes

    @property
    def checksum(self) -> str:
        """SHA-256 of the file. An applied migration whose file changed stops the runner."""
        return hashlib.sha256(self.sql).hexdigest()

    @property
    def file_name(self) -> str:
        return f"{self.version:04d}_{self.name}.sql"


def load_migrations(directory: Traversable | None = None) -> list[Migration]:
    """Read the migration files, ordered by version.

    `directory` defaults to the `migrations` directory of this package. Files that do not end in
    `.sql` are ignored; a `.sql` file with a name other than `NNNN_name.sql` is an error.
    """
    root = directory if directory is not None else resources.files("memini.db") / "migrations"
    found: list[Migration] = []
    for entry in root.iterdir():
        if not entry.name.endswith(".sql"):
            continue
        match = _FILE_NAME.fullmatch(entry.name)
        if match is None:
            raise MigrationError(f"{entry.name}: a migration file is named NNNN_name.sql")
        found.append(Migration(int(match[1]), match[2], entry.read_bytes()))
    return check_sequence(found)


def check_sequence(migrations: Iterable[Migration]) -> list[Migration]:
    """Order migrations by version; fail unless the versions are 1, 2, 3 ... without gaps."""
    ordered = sorted(migrations, key=lambda m: m.version)
    for expected, migration in enumerate(ordered, start=1):
        if migration.version != expected:
            raise MigrationError(
                f"{migration.file_name}: expected version {expected:04d}; "
                "migrations are numbered from 0001 without gaps or repeats"
            )
    return ordered


async def pending_migrations(
    conn: AsyncConnection[Any], *, migrations: Sequence[Migration] | None = None
) -> list[Migration]:
    """Return the migrations not yet applied to the database of `conn`, in order.

    Raises `MigrationError` if an applied migration has changed on disk, or if the database has a
    migration this build does not know (it was migrated by a newer version). The service can call
    this at start and refuse to run while anything is pending.
    """
    known = load_migrations() if migrations is None else check_sequence(migrations)
    applied = await _applied_checksums(conn)

    unknown = sorted(set(applied) - {m.version for m in known})
    if unknown:
        raise MigrationError(
            f"the database has migrations {unknown} that this version of memini does not know"
        )
    changed = [
        m.file_name for m in known if m.version in applied and applied[m.version] != m.checksum
    ]
    if changed:
        raise MigrationError(f"applied migrations changed on disk: {changed}; add a new migration")
    return [m for m in known if m.version not in applied]


async def migrate(url: str, *, migrations: Sequence[Migration] | None = None) -> list[Migration]:
    """Apply every pending migration to the database at `url`; return the ones applied now.

    Safe to run from several processes at once, and a no-op when nothing is pending.
    """
    async with connect(url, autocommit=True) as conn:
        # Held by this session; released when the connection closes, also on error.
        await conn.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK_KEY,))
        await conn.execute(_CREATE_BOOKKEEPING)
        pending = await pending_migrations(conn, migrations=migrations)
        for migration in pending:
            try:
                async with conn.transaction():
                    await conn.execute(migration.sql)
                    await conn.execute(
                        "INSERT INTO schema_migration (version, name, checksum)"
                        " VALUES (%s, %s, %s)",
                        (migration.version, migration.name, migration.checksum),
                    )
            except psycopg.Error as error:
                raise MigrationError(f"{migration.file_name} failed: {error}") from error
            log.info("applied migration %s", migration.file_name)
        return pending


async def _applied_checksums(conn: AsyncConnection[Any]) -> dict[int, str]:
    async with conn.cursor(row_factory=tuple_row) as cur:
        await cur.execute("SELECT to_regclass('schema_migration') IS NOT NULL")
        exists = await cur.fetchone()
        if exists is None or not exists[0]:
            return {}
        await cur.execute("SELECT version, checksum FROM schema_migration")
        return {int(version): str(checksum) for version, checksum in await cur.fetchall()}
