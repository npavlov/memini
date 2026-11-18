"""Tokens managed through the API: create, list and revoke them for the caller of a request.

The token routes (:mod:`memini.api.tokens`) and the ``memini token`` commands are thin layers over
these functions, as the source routes are over :mod:`memini.sources`. The first token cannot come
from here, since every request needs one: ``memini bootstrap`` creates it on the database directly
(:func:`memini.auth.create_token`).

Each function takes the caller's :class:`~memini.auth.WriteAccess`, which the ``admin`` scope
gives, and its rules:

- Tokens belong to the caller's principal. A token creates tokens for that principal and sees and
  revokes only that principal's tokens.
- A token limited to one workspace creates only tokens limited to that same workspace: one for
  every workspace is refused (:class:`~memini.auth.WorkspaceLimitError`), and another workspace is
  not found (:class:`~memini.workspaces.NotFoundError`), as for every other write. It sees and
  revokes only tokens limited to its workspace; the others look like tokens that do not exist.
- Scopes are not narrowed by the caller's. The ``admin`` scope is the one that manages tokens, so
  it may give a new token any scope, ``admin`` included; what bounds the new token is the caller's
  workspace limit.
- Revoking is idempotent, and a token may revoke itself: the request that does it still answers.
  A principal left without an admin token gets a new one from ``memini bootstrap``.

Every write runs in a transaction of its own, or a savepoint of the caller's, so a refused write
leaves the caller's connection usable.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final
from uuid import UUID

from psycopg import AsyncConnection, sql
from psycopg.rows import TupleRow

from memini.auth import (
    NewToken,
    Scope,
    TokenInfo,
    TokenStatus,
    WriteAccess,
    create_token,
    is_scope,
)
from memini.workspaces import NotFoundError, get_writable_workspace

__all__ = [
    "MAX_TOKEN_LIFETIME",
    "TokenEntry",
    "create_token_as",
    "list_tokens_as",
    "revoke_token_as",
]

MAX_TOKEN_LIFETIME: Final = timedelta(days=36525)
"""About a hundred years; for longer, create a token that does not expire."""

type Conn = AsyncConnection[TupleRow]


@dataclass(frozen=True, slots=True)
class TokenEntry:
    """A stored token with the name of the workspace it is limited to. Holds no secret."""

    info: TokenInfo
    workspace: str | None
    """The name of the one workspace the token is limited to; ``None`` for every workspace."""
    as_of: datetime
    """The database's time when the token was read: the clock that decides whether it is
    expired or revoked."""

    @property
    def status(self) -> TokenStatus:
        """``active``, ``revoked`` or ``expired`` at :attr:`as_of`."""
        return self.info.status(self.as_of)


_COLUMNS: Final = sql.SQL(
    "t.id, t.owner_id, t.workspace_id, t.name, t.prefix, t.scopes, t.created_at, t.expires_at,"
    " t.revoked_at, t.last_used_at, (SELECT w.name FROM workspace w WHERE w.id = t.workspace_id),"
    " now()"
)


def _entry(row: Sequence[Any]) -> TokenEntry:
    info = TokenInfo(
        id=row[0],
        owner_id=row[1],
        workspace_id=row[2],
        name=row[3],
        prefix=row[4],
        scopes=tuple(scope for scope in row[5] if is_scope(scope)),
        created_at=row[6],
        expires_at=row[7],
        revoked_at=row[8],
        last_used_at=row[9],
    )
    return TokenEntry(info=info, workspace=row[10], as_of=row[11])


def _visible(access: WriteAccess) -> sql.Composable:
    """The tokens ``access`` may see and revoke: its principal's, within its workspace limit."""
    owned = sql.SQL("t.owner_id = {}").format(sql.Literal(access.owner_id))
    reach = access.where(
        workspace_id=sql.Identifier("t", "workspace_id"),
        owner_id=sql.Identifier("t", "owner_id"),
    )
    return sql.SQL("{} AND {}").format(owned, reach)


async def create_token_as(
    conn: Conn,
    access: WriteAccess,
    *,
    name: str,
    scopes: Iterable[Scope],
    workspace: str | None = None,
    expires_in: timedelta | None = None,
) -> tuple[NewToken, TokenEntry]:
    """Create a token for the principal of ``access``; return it, and its entry for listings.

    The returned secret is the only copy of the token: hand it to the caller once.

    Args:
        name: what the token is for, e.g. ``"laptop agent"``.
        scopes: one or more of ``read``, ``ingest``, ``admin``.
        workspace: limit the token to the workspace of this name; ``None`` for every workspace.
        expires_in: the token stops working this long after it is created, by the database's
            clock; ``None`` for never. At most :data:`MAX_TOKEN_LIFETIME`.

    Raises:
        WorkspaceLimitError: ``access`` is limited to a workspace and ``workspace`` is ``None``.
        NotFoundError: no workspace ``workspace`` that ``access`` may change.
        ValueError: an empty name, no scope or an unknown one, or a lifetime out of range.
    """
    if expires_in is not None and not timedelta() < expires_in <= MAX_TOKEN_LIFETIME:
        raise ValueError("a token's lifetime is longer than zero and at most 36525 days")
    async with conn.transaction():
        workspace_id: UUID | None = None
        if workspace is None:
            access.ensure_unlimited()
        else:
            workspace_id = (await get_writable_workspace(conn, access, workspace)).id
        # now() is the start of the transaction, which is the token's created_at too: the expiry
        # is after it by the whole lifetime, whatever the clocks of client and service.
        cur = await conn.execute("SELECT now()")
        row = await cur.fetchone()
        if row is None:  # SELECT now() always returns a row
            raise RuntimeError("SELECT now() returned no row")
        started: datetime = row[0]
        new = await create_token(
            conn,
            owner_id=access.owner_id,
            name=name,
            scopes=scopes,
            workspace_id=workspace_id,
            expires_at=None if expires_in is None else started + expires_in,
        )
    return new, TokenEntry(info=new.info, workspace=workspace, as_of=started)


async def list_tokens_as(conn: Conn, access: WriteAccess) -> list[TokenEntry]:
    """The tokens ``access`` may see, revoked and expired ones included, oldest first."""
    query = sql.SQL("SELECT {} FROM token t WHERE {} ORDER BY t.created_at, t.id").format(
        _COLUMNS, _visible(access)
    )
    cur = await conn.execute(query)
    return [_entry(row) for row in await cur.fetchall()]


async def revoke_token_as(conn: Conn, access: WriteAccess, token_id: UUID) -> TokenEntry:
    """Revoke the token ``token_id`` from now on, if ``access`` may see it; return it.

    Revoking a revoked token keeps the time it was first revoked.

    Raises:
        NotFoundError: no such token, or one ``access`` may not see.
    """
    # LEAST ignores NULL: an unrevoked token gets now(), a revoked one keeps the earlier time.
    query = sql.SQL(
        "UPDATE token AS t SET revoked_at = LEAST(t.revoked_at, now())"
        " WHERE t.id = %s AND {} RETURNING {}"
    ).format(_visible(access), _COLUMNS)
    async with conn.transaction():
        cur = await conn.execute(query, (token_id,))
        row = await cur.fetchone()
    if row is None:
        raise NotFoundError("token")
    return _entry(row)
