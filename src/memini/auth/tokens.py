"""Bearer tokens: their format, what is stored of them, and creating, revoking and listing them.

Format: ``memini_`` followed by 43 base64url characters, the encoding of 32 random bytes from
:mod:`secrets` (256 bits). The fixed prefix lets secret scanners and log redaction recognize a
token (the logs redact ``memini_[A-Za-z0-9_-]{20,}``).

What is stored: never the token. ``token.hash`` holds the SHA-256 digest of the token's UTF-8 bytes,
and ``token.prefix`` its first 15 characters (``memini_`` and 8 more), so a listing can tell tokens
apart. Those 8 characters carry 48 of the 256 bits; the other 208 still cannot be guessed.

Why plain SHA-256, without a slow hash or a server-side pepper: both protect secrets that can be
guessed, such as passwords, from someone who has stolen the digests. No one can guess 256 random
bits, with a fast hash or a slow one, so a digest from a database dump gives no way back to a
token. A pepper would be one more secret to keep and rotate, and losing it would void every token.
A fast hash also keeps the lookup that every request makes cheap.

Why no constant-time comparison: a token is found by its digest through the unique index on
``token.hash``, never compared with a stored token. Timing can reveal at most how much of the digest
of a guess matched stored digests in the index, and a partial SHA-256 digest gives no way to a
token that produces it.

A principal must exist before it can own a token, and a workspace before a token can be limited to
it: see :func:`memini.workspaces.ensure_principal` and :func:`memini.workspaces.create_workspace`.
"""

import hashlib
import re
import secrets
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal
from uuid import UUID

from psycopg import AsyncConnection, sql
from psycopg.rows import TupleRow
from pydantic import SecretStr

from memini.auth.principal import SCOPES, Scope, is_scope

__all__ = [
    "DISPLAY_PREFIX_LENGTH",
    "TOKEN_PREFIX",
    "NewToken",
    "TokenInfo",
    "TokenNotFoundError",
    "TokenStatus",
    "create_token",
    "display_prefix",
    "generate_token",
    "hash_token",
    "is_well_formed_token",
    "list_tokens",
    "revoke_token",
]

TOKEN_PREFIX: Final = "memini_"  # noqa: S105 - the public marker of a token, not a secret
_TOKEN_BYTES: Final = 32
_TOKEN_PATTERN: Final = re.compile(r"memini_[A-Za-z0-9_-]{43}")

DISPLAY_PREFIX_LENGTH: Final = len(TOKEN_PREFIX) + 8
"""Characters of a token kept in ``token.prefix``: ``memini_`` and 8 more (48 bits)."""

TokenStatus = Literal["active", "revoked", "expired"]


def generate_token() -> str:
    """A new token: ``memini_`` and 43 base64url characters (32 random bytes)."""
    return TOKEN_PREFIX + secrets.token_urlsafe(_TOKEN_BYTES)


def is_well_formed_token(value: str) -> bool:
    """Whether ``value`` has the format of a token. Says nothing about whether it exists."""
    return _TOKEN_PATTERN.fullmatch(value) is not None


def hash_token(token: str) -> bytes:
    """The digest stored for ``token``: SHA-256 of its UTF-8 bytes, 32 bytes."""
    return hashlib.sha256(token.encode()).digest()


def display_prefix(token: str) -> str:
    """The part of ``token`` kept in ``token.prefix`` and shown in listings; not a secret."""
    return token[:DISPLAY_PREFIX_LENGTH]


@dataclass(frozen=True, slots=True)
class TokenInfo:
    """A stored token, without its digest. Safe to show and to log."""

    id: UUID
    owner_id: UUID
    workspace_id: UUID | None
    """The one workspace the token is limited to, or ``None`` for every workspace."""
    name: str
    prefix: str
    """The first characters of the token, as shown to tell tokens apart."""
    scopes: tuple[Scope, ...]
    created_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    last_used_at: datetime | None
    """Updated on use, at most once per :data:`memini.auth.LAST_USED_RESOLUTION`."""

    def status(self, at: datetime) -> TokenStatus:
        """``revoked`` or ``expired`` if the token is refused at ``at``, else ``active``."""
        if self.revoked_at is not None and self.revoked_at <= at:
            return "revoked"
        if self.expires_at is not None and self.expires_at <= at:
            return "expired"
        return "active"


@dataclass(frozen=True, slots=True)
class NewToken:
    """A token just created. ``secret`` is the token itself; it is not stored and cannot be shown
    again, so hand it to the user once and drop it."""

    secret: SecretStr
    info: TokenInfo


class TokenNotFoundError(LookupError):
    """No token with this id belongs to this owner."""


_COLUMNS: Final = sql.SQL(
    "id, owner_id, workspace_id, name, prefix, scopes,"
    " created_at, expires_at, revoked_at, last_used_at"
)


def _token_info(row: Sequence[Any]) -> TokenInfo:
    return TokenInfo(
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


def _canonical_scopes(scopes: Iterable[str]) -> list[Scope]:
    wanted = set(scopes)
    unknown = sorted(wanted.difference(SCOPES))
    if unknown:
        raise ValueError(f"unknown scopes {unknown}; expected some of {list(SCOPES)}")
    if not wanted:
        raise ValueError("a token needs at least one scope")
    return [scope for scope in SCOPES if scope in wanted]


async def create_token(
    conn: AsyncConnection[TupleRow],
    *,
    owner_id: UUID,
    name: str,
    scopes: Iterable[Scope],
    workspace_id: UUID | None = None,
    expires_at: datetime | None = None,
) -> NewToken:
    """Create a token for ``owner_id`` and return it with its stored row.

    The returned ``secret`` is the only copy of the token. Only its digest and display prefix are
    stored. Runs in the caller's transaction; the caller commits.

    Args:
        name: what the token is for, e.g. ``"laptop agent"``.
        scopes: one or more of ``read``, ``ingest``, ``admin``.
        workspace_id: limit the token to this workspace; ``None`` for every workspace.
        expires_at: when the token stops working (timezone-aware); ``None`` for never. It must be
            later than the database's current time.

    Raises:
        ValueError: an empty name, no scope or an unknown one, or a naive ``expires_at``.
        psycopg.errors.ForeignKeyViolation: the owner or the workspace does not exist.
        psycopg.errors.CheckViolation: ``expires_at`` is not in the future.
    """
    if not name.strip():
        raise ValueError("a token needs a name")
    if expires_at is not None and expires_at.utcoffset() is None:
        raise ValueError("expires_at must be timezone-aware")
    canonical = _canonical_scopes(scopes)
    token = generate_token()
    cur = await conn.execute(
        sql.SQL(
            "INSERT INTO token (owner_id, workspace_id, name, hash, prefix, scopes, expires_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING {}"
        ).format(_COLUMNS),
        (
            owner_id,
            workspace_id,
            name,
            hash_token(token),
            display_prefix(token),
            canonical,
            expires_at,
        ),
    )
    row = await cur.fetchone()
    if row is None:  # INSERT ... RETURNING always returns the row
        raise RuntimeError("INSERT INTO token returned no row")
    return NewToken(secret=SecretStr(token), info=_token_info(row))


async def revoke_token(
    conn: AsyncConnection[TupleRow], *, owner_id: UUID, token_id: UUID
) -> TokenInfo:
    """Revoke a token of ``owner_id`` from now on and return its row.

    Revoking a revoked token keeps the time it was first revoked. Runs in the caller's transaction.

    Raises:
        TokenNotFoundError: ``owner_id`` has no token ``token_id``.
    """
    # LEAST ignores NULL: an unrevoked token gets now(), a revoked one keeps the earlier time.
    cur = await conn.execute(
        sql.SQL(
            "UPDATE token SET revoked_at = LEAST(revoked_at, now())"
            " WHERE id = %s AND owner_id = %s RETURNING {}"
        ).format(_COLUMNS),
        (token_id, owner_id),
    )
    row = await cur.fetchone()
    if row is None:
        raise TokenNotFoundError(f"no token {token_id} of this owner")
    return _token_info(row)


async def list_tokens(conn: AsyncConnection[TupleRow], *, owner_id: UUID) -> list[TokenInfo]:
    """Every token of ``owner_id``, revoked and expired ones included, oldest first."""
    cur = await conn.execute(
        sql.SQL("SELECT {} FROM token WHERE owner_id = %s ORDER BY created_at, id").format(
            _COLUMNS
        ),
        (owner_id,),
    )
    return [_token_info(row) for row in await cur.fetchall()]
