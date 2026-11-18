"""Database access for routes: a pooled connection per request, as a FastAPI dependency."""

from collections.abc import AsyncIterator
from typing import Annotated, cast

from fastapi import Depends, Request
from psycopg import AsyncConnection
from psycopg.rows import TupleRow

from memini.api.errors import ApiError
from memini.db import Pool

__all__ = ["Connection", "database_connection"]


async def database_connection(request: Request) -> AsyncIterator[AsyncConnection[TupleRow]]:
    """A connection from the application's pool, held for the request.

    The transaction commits when the route returns and rolls back when it raises. Without a pool
    (``create_app`` got none) the route answers 503.
    """
    pool = cast(Pool | None, getattr(request.app.state, "pool", None))
    if pool is None:
        raise ApiError(503, "unavailable", "The database is not available.")
    async with pool.connection() as conn:
        yield conn


Connection = Annotated[AsyncConnection[TupleRow], Depends(database_connection)]
"""A route parameter that borrows a pooled connection for the request."""
