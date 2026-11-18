"""Bearer tokens, authentication and the single authorization filter. Independent of FastAPI.

- :mod:`memini.auth.tokens`: the token format and digest; create, revoke and list tokens.
- :mod:`memini.auth.authentication`: from an ``Authorization`` header to a :class:`Principal`.
- :mod:`memini.auth.authorization`: scopes, and :func:`authorize_read`, which every read goes
  through.
"""

from memini.auth.authentication import (
    LAST_USED_RESOLUTION,
    AuthenticationError,
    AuthenticationFailure,
    Authenticator,
    authenticate,
    bearer_token,
    pool_authenticator,
)
from memini.auth.authorization import (
    InsufficientScopeError,
    ReadFilter,
    authorize_read,
    ensure_scope,
)
from memini.auth.principal import SCOPES, Principal, Scope, is_scope
from memini.auth.tokens import (
    TOKEN_PREFIX,
    NewToken,
    TokenInfo,
    TokenNotFoundError,
    TokenStatus,
    create_token,
    generate_token,
    hash_token,
    is_well_formed_token,
    list_tokens,
    revoke_token,
)

__all__ = [
    "LAST_USED_RESOLUTION",
    "SCOPES",
    "TOKEN_PREFIX",
    "AuthenticationError",
    "AuthenticationFailure",
    "Authenticator",
    "InsufficientScopeError",
    "NewToken",
    "Principal",
    "ReadFilter",
    "Scope",
    "TokenInfo",
    "TokenNotFoundError",
    "TokenStatus",
    "authenticate",
    "authorize_read",
    "bearer_token",
    "create_token",
    "ensure_scope",
    "generate_token",
    "hash_token",
    "is_scope",
    "is_well_formed_token",
    "list_tokens",
    "pool_authenticator",
    "revoke_token",
]
