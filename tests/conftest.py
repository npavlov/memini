"""Shared fixtures.

Database fixtures are for tests marked `integration`. They need a Postgres server with pgvector:
`make db` starts one, or `MEMINI_TEST_DATABASE_URL` points to another. The role in the URL must be
allowed to create databases. Each test gets a new, empty database, which is dropped afterwards.
"""

import os
import uuid
from collections.abc import Iterator
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from psycopg import sql

from memini.db import migrate

TEST_DATABASE_URL_ENV = "MEMINI_TEST_DATABASE_URL"
DEFAULT_TEST_DATABASE_URL = "postgresql://memini:memini@127.0.0.1:55432/memini"


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
    name = f"memini_test_{uuid.uuid4().hex[:12]}"
    try:
        admin = psycopg.connect(postgres_url, autocommit=True, connect_timeout=5)
    except psycopg.OperationalError as error:
        raise pytest.fail.Exception(
            f"Postgres is not reachable: {error}\n"
            f"Start it with `make db`, or set {TEST_DATABASE_URL_ENV}.",
            pytrace=False,
        ) from None
    with admin:
        admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
    try:
        yield _with_database(postgres_url, name)
    finally:
        with psycopg.connect(postgres_url, autocommit=True, connect_timeout=5) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


@pytest.fixture
async def migrated_database_url(empty_database_url: str) -> str:
    """URL of a new database with every migration applied. Needs an async (anyio) test."""
    await migrate(empty_database_url)
    return empty_database_url


def _with_database(url: str, name: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("postgresql", "postgres"):
        raise ValueError(f"{TEST_DATABASE_URL_ENV} must be a postgresql:// URL")
    return urlunsplit(parts._replace(path=f"/{name}"))
