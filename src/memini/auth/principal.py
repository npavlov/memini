"""Who a request acts as: the principal behind a bearer token, with what the token allows."""

from dataclasses import dataclass
from typing import Final, Literal, TypeGuard, get_args
from uuid import UUID

__all__ = ["SCOPES", "Principal", "Scope", "is_scope"]

Scope = Literal["read", "ingest", "admin"]
"""What a token may do (requirements, section 3).

- ``read``: query routes and tools.
- ``ingest``: write tools and the push API.
- ``admin``: workspaces, sources, tokens.

Scopes do not imply each other: a token needs each scope it uses.
"""

SCOPES: Final[tuple[Scope, ...]] = get_args(Scope)
"""Every scope, in canonical order. The database checks the same list (``token_scopes_check``)."""


def is_scope(value: object) -> TypeGuard[Scope]:
    """Whether ``value`` names a scope."""
    return value in SCOPES


@dataclass(frozen=True, slots=True)
class Principal:
    """An authenticated caller: the principal a token resolves to, limited by the token.

    Built by :func:`memini.auth.authenticate`; nothing else should make one outside tests.
    """

    principal_id: UUID
    """The owner of the token. Requests act as this principal."""
    token_id: UUID
    """The token the request came with; for logs and audit, never a secret."""
    scopes: frozenset[Scope]
    """What the token may do."""
    workspace_id: UUID | None = None
    """The one workspace the token is limited to, or ``None`` for no limit."""

    def has_scope(self, scope: Scope) -> bool:
        return scope in self.scopes
