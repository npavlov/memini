"""Shared fixtures. Every value here is synthetic.

Database fixtures are for tests marked `integration`. They need a Postgres server with pgvector:
`make db` starts one, or `MEMINI_TEST_DATABASE_URL` points to another. The role in the URL must be
allowed to create databases. Each test gets a new database, which is dropped afterwards.
`make test-integration` runs them in parallel worker processes (pytest-xdist) against the same
server, so a test may look only at databases it created itself, never at the server as a whole.

A migrated database is a copy of a template: each worker process migrates one database of its own
the first time a test asks for one, marks it a template, and creates every further migrated
database from it with `CREATE DATABASE ... TEMPLATE`. The template's name carries the worker's id
and a random part, so neither workers nor concurrent runs share one; it is dropped when the worker
ends. Tests of the migrations themselves start from `empty_database_url` and migrate it.
"""

import io
import logging
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from psycopg import sql
from pydantic import SecretStr

from memini.config import Settings
from memini.db import load_migrations, migrate
from memini.logs import (
    ROUTED_LOGGERS,
    JsonFormatter,
    TextFormatter,
    clear_secrets,
    configure_logging,
)

TEST_DATABASE_URL_ENV = "MEMINI_TEST_DATABASE_URL"
DEFAULT_TEST_DATABASE_URL = "postgresql://memini:memini@127.0.0.1:55432/memini"


@pytest.fixture
def settings() -> Settings:
    """Valid settings whose database URL points nowhere; for tests that never connect."""
    return Settings(database_url=SecretStr("postgresql://fake:fake@db.invalid:5432/memini"))


@pytest.fixture
def log_output() -> Iterator[io.StringIO]:
    """What `configure_logging` writes during the test, JSON at level DEBUG.

    The logging setup of the process, and the registered secrets, are restored afterwards.
    """
    root = logging.getLogger()
    root_level = root.level
    routed = {
        name: (list(logger.handlers), logger.propagate, logger.level)
        for name in ROUTED_LOGGERS
        for logger in [logging.getLogger(name)]
    }
    stream = io.StringIO()
    configure_logging("DEBUG", stream=stream)
    try:
        yield stream
    finally:
        for handler in list(root.handlers):
            if isinstance(handler.formatter, JsonFormatter | TextFormatter):
                root.removeHandler(handler)
        root.setLevel(root_level)
        for name, (handlers, propagate, level) in routed.items():
            logger = logging.getLogger(name)
            logger.handlers[:] = handlers
            logger.propagate = propagate
            logger.setLevel(level)
        clear_secrets()


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    """Async tests (`pytest.mark.anyio`) run on asyncio, like the service."""
    return "asyncio"


@pytest.fixture(scope="session")
def postgres_url() -> str:
    """URL of the server the test databases are created on. Tests never write to its database."""
    return os.environ.get(TEST_DATABASE_URL_ENV) or DEFAULT_TEST_DATABASE_URL


@pytest.fixture
def empty_database_url(postgres_url: str) -> Iterator[str]:
    """URL of a new database with nothing in it: created from template0, dropped after the test."""
    name = _new_database_name()
    with _admin(postgres_url) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
    try:
        yield _with_database(postgres_url, name)
    finally:
        _drop_database(postgres_url, name)


@pytest.fixture(scope="session")
def migrated_templates(postgres_url: str) -> Iterator[list[str]]:
    """Names of this worker's migrated template databases (at most one); dropped at the end."""
    templates: list[str] = []
    try:
        yield templates
    finally:
        if templates:
            with psycopg.connect(postgres_url, autocommit=True, connect_timeout=5) as admin:
                for name in templates:
                    # A template cannot be dropped; it accepts no connections, so no FORCE needed.
                    admin.execute(
                        sql.SQL("ALTER DATABASE {} WITH IS_TEMPLATE false").format(
                            sql.Identifier(name)
                        )
                    )
                    admin.execute(
                        sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name))
                    )


@pytest.fixture
async def migrated_database_url(
    postgres_url: str, migrated_templates: list[str]
) -> AsyncIterator[str]:
    """URL of a new database with every migration applied: a copy of this worker's template,
    which the first such test migrates. Needs an async (anyio) test.

    With `MEMINI_TEST_PREVIOUS_MIGRATIONS` set (scripts/test-upgrade.sh), the template gets the
    previous release's migrations first and then the current ones, so every test runs on an
    upgraded schema.
    """
    if not migrated_templates:
        migrated_templates.append(await _migrated_template(postgres_url))
    name = _new_database_name()
    with _admin(postgres_url) as admin:
        admin.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
                sql.Identifier(name), sql.Identifier(migrated_templates[0])
            )
        )
    try:
        yield _with_database(postgres_url, name)
    finally:
        _drop_database(postgres_url, name)


async def _migrated_template(postgres_url: str) -> str:
    """Create a database, migrate it and make it a template that accepts no connections."""
    # gw0, gw1 ... under pytest-xdist; the random part keeps concurrent runs apart.
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    name = f"memini_template_{worker}_{uuid.uuid4().hex[:8]}"
    with _admin(postgres_url) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
    url = _with_database(postgres_url, name)
    try:
        if previous := os.environ.get("MEMINI_TEST_PREVIOUS_MIGRATIONS"):
            migrations = load_migrations(Path(previous))
            if not migrations:
                pytest.fail(f"no previous migrations in {previous}")
            await migrate(url, migrations=migrations)
        await migrate(url)
        with _admin(postgres_url) as admin:
            # Copying needs a template nobody is connected to; refusing connections keeps it so.
            admin.execute(
                sql.SQL("ALTER DATABASE {} WITH IS_TEMPLATE true ALLOW_CONNECTIONS false").format(
                    sql.Identifier(name)
                )
            )
    except BaseException:
        _drop_database(postgres_url, name)
        raise
    return name


def _new_database_name() -> str:
    return f"memini_test_{uuid.uuid4().hex[:12]}"


def _admin(postgres_url: str) -> psycopg.Connection[tuple[object, ...]]:
    """An autocommit connection to the server's own database, for CREATE and DROP DATABASE."""
    try:
        return psycopg.connect(postgres_url, autocommit=True, connect_timeout=5)
    except psycopg.OperationalError as error:
        raise pytest.fail.Exception(
            f"Postgres is not reachable: {error}\n"
            f"Start it with `make db`, or set {TEST_DATABASE_URL_ENV}.",
            pytrace=False,
        ) from None


def _drop_database(postgres_url: str, name: str) -> None:
    with _admin(postgres_url) as admin:
        admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )


def _with_database(url: str, name: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("postgresql", "postgres"):
        raise ValueError(f"{TEST_DATABASE_URL_ENV} must be a postgresql:// URL")
    return urlunsplit(parts._replace(path=f"/{name}"))
