"""Authentication: from a bearer token to the :class:`Principal` a request acts as.

Independent of FastAPI, so the HTTP API and the MCP endpoint authenticate the same way:

1. :func:`bearer_token` takes the token out of an ``Authorization`` header value.
2. An :data:`Authenticator`, usually :func:`pool_authenticator`, resolves it with
   :func:`authenticate`.

Both raise :class:`AuthenticationError`, whose ``reason`` says why; its message never holds the
token. Missing, malformed, unknown, revoked and expired tokens are all refused alike towards the
client; the reason is for logs and for choosing the ``WWW-Authenticate`` challenge.
"""

import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Final, Literal
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import TupleRow

from memini.auth.principal import Principal, is_scope
from memini.auth.tokens import hash_token, is_well_formed_token
from memini.db import Pool

__all__ = [
    "LAST_USED_RESOLUTION",
    "AuthenticationError",
    "AuthenticationFailure",
    "Authenticator",
    "authenticate",
    "bearer_token",
    "pool_authenticator",
]

AuthenticationFailure = Literal["missing", "malformed", "unknown", "revoked", "expired"]
"""Why a request was not authenticated.

- ``missing``: no ``Authorization`` header, or one with another scheme than ``Bearer``.
- ``malformed``: bearer credentials that do not have the format of a token.
- ``unknown``: no token has this digest.
- ``revoked``, ``expired``: the token exists and is refused.
"""

Authenticator = Callable[[str], Awaitable[Principal]]
"""Resolves a token to its principal; raises :class:`AuthenticationError` if it is refused."""

LAST_USED_RESOLUTION: Final = timedelta(minutes=1)
"""``token.last_used_at`` is written only when it is older than this, so a busy token does not
turn every request into a write on its row. It is accurate to within this much."""

_MESSAGES: Final[dict[AuthenticationFailure, str]] = {
    "missing": "no bearer token",
    "malformed": "the bearer token is malformed",
    "unknown": "the bearer token is unknown",
    "revoked": "the bearer token is revoked",
    "expired": "the bearer token has expired",
}

_log = logging.getLogger(__name__)


class AuthenticationError(Exception):
    """A request is not authenticated. The message names the reason, never the token."""

    def __init__(self, reason: AuthenticationFailure, *, token_id: UUID | None = None) -> None:
        super().__init__(_MESSAGES[reason])
        self.reason: AuthenticationFailure = reason
        self.token_id = token_id
        """The refused token, when it exists (``revoked``, ``expired``)."""


def bearer_token(authorization: str | None) -> str:
    """The token in an ``Authorization`` header value of the form ``Bearer <token>``.

    The scheme is case-insensitive (RFC 9110). Does not check that the token exists.

    Raises:
        AuthenticationError: ``missing`` when there are no bearer credentials, ``malformed`` when
            the credentials do not have the format of a token.
    """
    if authorization is None:
        raise AuthenticationError("missing")
    scheme, _, credentials = authorization.strip().partition(" ")
    if scheme.lower() != "bearer":
        raise AuthenticationError("missing")
    token = credentials.strip()
    if not is_well_formed_token(token):
        raise AuthenticationError("malformed")
    return token


# One round trip decides everything. Times come from the database clock, as revoke_token and the
# expires_at check of the token table use it too.
_LOOKUP: Final = (
    "SELECT id, owner_id, workspace_id, scopes,"
    " revoked_at IS NOT NULL AND revoked_at <= now(),"
    " expires_at IS NOT NULL AND expires_at <= now(),"
    " last_used_at IS NULL OR last_used_at <= now() - %(resolution)s"
    " FROM token WHERE hash = %(hash)s"
)

# The condition is repeated so that of two requests that both saw a stale time, the second writes
# nothing once the first has committed.
_TOUCH: Final = (
    "UPDATE token SET last_used_at = now()"
    " WHERE id = %(id)s AND (last_used_at IS NULL OR last_used_at <= now() - %(resolution)s)"
)


async def authenticate(conn: AsyncConnection[TupleRow], token: str) -> Principal:
    """Resolve ``token`` to the principal it acts as.

    Looks the token up by its digest. A malformed token is refused without a query. On success,
    updates ``last_used_at`` if it is older than :data:`LAST_USED_RESOLUTION`; that write is part
    of the caller's transaction, so the caller commits.

    Raises:
        AuthenticationError: the token is malformed, unknown, revoked or expired.
    """
    if not is_well_formed_token(token):
        raise AuthenticationError("malformed")
    cur = await conn.execute(
        _LOOKUP, {"hash": hash_token(token), "resolution": LAST_USED_RESOLUTION}
    )
    row = await cur.fetchone()
    if row is None:
        _log.info("token refused: unknown")
        raise AuthenticationError("unknown")
    token_id, owner_id, workspace_id, scopes, revoked, expired, stale = row
    if revoked or expired:
        reason: AuthenticationFailure = "revoked" if revoked else "expired"
        _log.info("token %s refused: %s", token_id, reason)
        raise AuthenticationError(reason, token_id=token_id)
    if stale:
        await conn.execute(_TOUCH, {"id": token_id, "resolution": LAST_USED_RESOLUTION})
    return Principal(
        principal_id=owner_id,
        token_id=token_id,
        scopes=frozenset(scope for scope in scopes if is_scope(scope)),
        workspace_id=workspace_id,
    )


def pool_authenticator(pool: Pool) -> Authenticator:
    """An :data:`Authenticator` that borrows a connection from ``pool`` for each token.

    The connection is returned, and the ``last_used_at`` update committed, before it returns.
    """

    async def authenticate_with_pool(token: str) -> Principal:
        async with pool.connection() as conn:
            return await authenticate(conn, token)

    return authenticate_with_pool
