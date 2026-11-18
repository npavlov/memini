"""Token routes under ``/v1/tokens``: the HTTP face of :mod:`memini.token_admin` (``admin`` scope).

| Route | Does |
|---|---|
| ``POST /tokens`` | create a token; the response holds it, this once (201) |
| ``GET /tokens`` | list tokens, without secrets |
| ``DELETE /tokens/{token_id}`` | revoke a token; answers with the revoked token |

The rules are those of :mod:`memini.token_admin`: a token limited to one workspace creates, sees
and revokes only tokens limited to that workspace (403 ``workspace_limited`` for a token for every
workspace, 404 for another workspace or token), and the ``admin`` scope may give any scope. The
first admin token comes from ``memini bootstrap``, inside the service's container.

The token is in the body of the 201 and nowhere else: only its digest is stored, so it cannot be
shown again. That response carries ``Cache-Control: no-store``. Errors are translated by
:mod:`memini.api.admin_errors`.
"""

from datetime import datetime, timedelta
from typing import Final
from uuid import UUID

from fastapi import APIRouter, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from memini import token_admin
from memini.api.admin_errors import admin_errors
from memini.api.auth import AdminAccess
from memini.api.database import Connection
from memini.auth import SCOPES, Scope, TokenStatus
from memini.config import HANDLE_REGEX

__all__ = ["CreatedToken", "NewTokenRequest", "TokenList", "TokenOut", "build_tokens_router"]

MAX_NAME_LENGTH: Final = 200
_MAX_LIFETIME_SECONDS: Final = int(token_admin.MAX_TOKEN_LIFETIME.total_seconds())


class NewTokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scopes: list[Scope] = Field(
        min_length=1,
        max_length=len(SCOPES),
        description="What the token may do: read (queries), ingest (writes of content), admin"
        " (workspaces, sources, tokens). Scopes do not imply each other.",
    )
    name: str = Field(
        min_length=1,
        max_length=MAX_NAME_LENGTH,
        description="What the token is for, e.g. 'laptop agent': printable text.",
    )
    workspace: str | None = Field(
        default=None,
        pattern=HANDLE_REGEX,
        description="Limit the token to this workspace; null: every workspace. A token limited to"
        " one workspace may only create tokens limited to it.",
    )
    expires_in_seconds: int | None = Field(
        default=None,
        ge=1,
        le=_MAX_LIFETIME_SECONDS,
        description="The token stops working this many seconds after it is created; null: never.",
    )

    @field_validator("name")
    @classmethod
    def _printable(cls, value: str) -> str:
        if not value.strip() or not value.isprintable():
            raise ValueError("a token name is printable text and not blank")
        return value


class TokenOut(BaseModel):
    id: UUID
    name: str
    prefix: str = Field(description="The first characters of the token, to tell tokens apart.")
    scopes: list[Scope]
    workspace: str | None = Field(
        description="The workspace the token is limited to; null: every workspace."
    )
    status: TokenStatus = Field(description="active, revoked or expired, as of the response.")
    created_at: datetime
    expires_at: datetime | None = Field(description="null: does not expire.")
    revoked_at: datetime | None
    last_used_at: datetime | None = Field(description="Updated at most once a minute.")

    @classmethod
    def of(cls, entry: token_admin.TokenEntry) -> "TokenOut":
        info = entry.info
        return cls(
            id=info.id,
            name=info.name,
            prefix=info.prefix,
            scopes=list(info.scopes),
            workspace=entry.workspace,
            status=entry.status,
            created_at=info.created_at,
            expires_at=info.expires_at,
            revoked_at=info.revoked_at,
            last_used_at=info.last_used_at,
        )


class CreatedToken(TokenOut):
    token: str = Field(
        description="The token itself, for the Authorization header. Shown this once: only its"
        " digest is stored."
    )


class TokenList(BaseModel):
    tokens: list[TokenOut] = Field(description="Oldest first.")


def build_tokens_router() -> APIRouter:
    """The token routes; see the module."""
    router = APIRouter(prefix="/tokens", tags=["tokens"])

    @router.post("", status_code=201, summary="Create a token")
    async def create_token(
        body: NewTokenRequest, access: AdminAccess, conn: Connection, response: Response
    ) -> CreatedToken:
        """Create a token for the caller's principal. The response holds the token, this once.
        A token limited to one workspace may only create tokens limited to it: 403
        ``workspace_limited`` without ``workspace``, 404 for another workspace."""
        expires_in = (
            None if body.expires_in_seconds is None else timedelta(seconds=body.expires_in_seconds)
        )
        with admin_errors():
            new, entry = await token_admin.create_token_as(
                conn,
                access,
                name=body.name,
                scopes=body.scopes,
                workspace=body.workspace,
                expires_in=expires_in,
            )
        response.headers["Cache-Control"] = "no-store"
        out = TokenOut.of(entry)
        return CreatedToken(**out.model_dump(), token=new.secret.get_secret_value())

    @router.get("", summary="List tokens")
    async def list_tokens(access: AdminAccess, conn: Connection) -> TokenList:
        """The caller's tokens, revoked and expired ones included, without secrets. A token
        limited to one workspace sees only the tokens limited to it."""
        found = await token_admin.list_tokens_as(conn, access)
        return TokenList(tokens=[TokenOut.of(entry) for entry in found])

    @router.delete("/{token_id}", summary="Revoke a token")
    async def revoke_token(token_id: UUID, access: AdminAccess, conn: Connection) -> TokenOut:
        """Revoke a token from now on and answer with it. Revoking it again keeps the first time.
        404 if there is no such token or the caller may not see it."""
        with admin_errors():
            entry = await token_admin.revoke_token_as(conn, access, token_id)
        return TokenOut.of(entry)

    return router
