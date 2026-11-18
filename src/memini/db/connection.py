"""Connections and the connection pool.

Memini talks to Postgres through psycopg 3, asynchronously, in plain SQL. A URL is a libpq
connection string, usually `postgresql://user:password@host:port/dbname`.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from pgvector.psycopg import register_vector_async
from psycopg import AsyncConnection
from psycopg.rows import TupleRow
from psycopg_pool import AsyncConnectionPool

APPLICATION_NAME = "memini"
"""Shown in `pg_stat_activity`, so a lock holder or a slow query can be traced to the service."""

type Pool = AsyncConnectionPool[AsyncConnection[TupleRow]]


@asynccontextmanager
async def connect(
    url: str, *, autocommit: bool = False
) -> AsyncGenerator[AsyncConnection[TupleRow], None]:
    """Open one connection outside the pool and close it on exit.

    For the migration runner and for commands that run once. It does not register the pgvector
    types, so it works on a database where the `vector` extension does not exist yet.
    """
    async with await AsyncConnection.connect(
        url, autocommit=autocommit, application_name=APPLICATION_NAME
    ) as conn:
        yield conn


@asynccontextmanager
async def open_pool(
    url: str, *, min_size: int = 1, max_size: int = 10, timeout: float = 30.0
) -> AsyncGenerator[Pool, None]:
    """Open the connection pool of the service and close it on exit.

    Waits until `min_size` connections are open, so a wrong URL fails here and not on the first
    request. Every connection knows the pgvector types, so migrate the database first. A connection
    is checked before it is handed out, so a database restart does not reach the caller as an error.
    """
    pool: Pool = AsyncConnectionPool(
        url,
        min_size=min_size,
        max_size=max_size,
        timeout=timeout,
        kwargs={"application_name": APPLICATION_NAME},
        configure=register_vector_async,
        check=AsyncConnectionPool.check_connection,
        name=APPLICATION_NAME,
        open=False,
    )
    await pool.open(wait=True, timeout=timeout)
    try:
        yield pool
    finally:
        await pool.close()
