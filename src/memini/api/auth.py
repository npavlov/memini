"""Bearer-token authentication and scopes for the HTTP API, as FastAPI dependencies.

``create_app`` puts :func:`authenticate_request` on the ``/v1`` router, so every resource route
needs a valid token and the public routes do not. A route then asks for what it needs:

- a scope: a parameter typed :data:`ReadPrincipal`, :data:`IngestPrincipal` or
  :data:`AdminPrincipal`, or ``dependencies=[Depends(require_scope("admin"))]`` on the route;
- for a read, a parameter typed :data:`ReadAccess`: the ``read`` scope plus the authorization
  filter, to pass to the read function. Declare it before a database connection, so a refused
  request never borrows one.
- for an administrative write, a parameter typed :data:`AdminAccess`: the ``admin`` scope plus the
  :class:`~memini.auth.WriteAccess` to pass to the write function, which keeps a token limited to
  one workspace inside it. Declare it before a database connection too.
- for an ingest write, a parameter typed :data:`IngestAccess`: the same with the ``ingest`` scope.

Responses follow RFC 6750:

- no bearer token: 401, code ``unauthorized``, ``WWW-Authenticate: Bearer realm="memini"``;
- a malformed, unknown, revoked or expired token: 401, code ``invalid_token``, the same challenge
  with ``error="invalid_token"``; which of these it was goes to the log only;
- a valid token without the scope: 403, code ``insufficient_scope``, with
  ``error="insufficient_scope"`` and the scope.

The challenges and the errors that carry them are plain functions, so the MCP endpoint
(:mod:`memini.api.mcp`) answers the same way.
"""

import functools
from collections.abc import Awaitable, Callable
from typing import Annotated, Final, cast

from fastapi import Depends, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from memini.api.errors import ApiError
from memini.auth import (
    AuthenticationError,
    AuthenticationFailure,
    Authenticator,
    InsufficientScopeError,
    Principal,
    ReadFilter,
    Scope,
    WriteAccess,
    authorization,
    bearer_token,
    ensure_scope,
    is_scope,
)

__all__ = [
    "AdminAccess",
    "AdminPrincipal",
    "CurrentPrincipal",
    "IngestAccess",
    "IngestPrincipal",
    "ReadAccess",
    "ReadPrincipal",
    "admin_access",
    "authenticate_request",
    "ingest_access",
    "insufficient_scope_challenge",
    "insufficient_scope_error",
    "read_access",
    "require_scope",
    "unauthorized_challenge",
    "unauthorized_error",
]

REALM: Final = "memini"

# Documents the scheme in the OpenAPI schema. It parses nothing that is used: the header is parsed
# by memini.auth.bearer_token, the same parser the MCP endpoint uses.
_BEARER_SCHEME: Final = HTTPBearer(
    scheme_name="bearer",
    description="A Memini token: memini_ followed by 43 characters. The first comes from"
    " `memini bootstrap`; others from POST /v1/tokens (`memini token create`).",
    auto_error=False,
)


def unauthorized_challenge(reason: AuthenticationFailure) -> str:
    """The ``WWW-Authenticate`` value for a request refused with ``reason`` (RFC 6750, 3.1)."""
    if reason == "missing":
        # A request without credentials gets no error code.
        return f'Bearer realm="{REALM}"'
    return f'Bearer realm="{REALM}", error="invalid_token"'


def insufficient_scope_challenge(scope: Scope) -> str:
    """The ``WWW-Authenticate`` value for a token that lacks ``scope`` (RFC 6750, 3.1)."""
    return f'Bearer realm="{REALM}", error="insufficient_scope", scope="{scope}"'


def unauthorized_error(reason: AuthenticationFailure) -> ApiError:
    """The 401 for a request refused with ``reason``, with its challenge."""
    headers = {"WWW-Authenticate": unauthorized_challenge(reason)}
    if reason == "missing":
        return ApiError(401, "unauthorized", "A bearer token is required.", headers=headers)
    return ApiError(401, "invalid_token", "The bearer token is not valid.", headers=headers)


def insufficient_scope_error(scope: Scope) -> ApiError:
    """The 403 for a token that lacks ``scope``, with its challenge."""
    return ApiError(
        403,
        "insufficient_scope",
        f"The token lacks the {scope} scope.",
        headers={"WWW-Authenticate": insufficient_scope_challenge(scope)},
    )


async def authenticate_request(
    request: Request,
    _documented: Annotated[HTTPAuthorizationCredentials | None, Security(_BEARER_SCHEME)],
) -> Principal:
    """Authenticate the request by its bearer token; 401 if it has no valid one.

    The principal is also kept in ``request.state.principal`` for logging.
    """
    try:
        token = bearer_token(request.headers.get("Authorization"))
        authenticator = cast(
            Authenticator | None, getattr(request.app.state, "authenticator", None)
        )
        if authenticator is None:
            raise ApiError(503, "unavailable", "Authentication is not available.")
        principal = await authenticator(token)
    except AuthenticationError as exc:
        raise unauthorized_error(exc.reason) from None
    request.state.principal = principal
    return principal


CurrentPrincipal = Annotated[Principal, Depends(authenticate_request)]
"""The authenticated caller, whatever its scopes."""


@functools.cache
def require_scope(scope: Scope) -> Callable[[Principal], Awaitable[Principal]]:
    """A dependency that returns the caller if its token has ``scope``, and answers 403 if not.

    Use it as a parameter type (``Annotated[Principal, Depends(require_scope("ingest"))]``) or in
    a route's ``dependencies``. The same scope always gives the same dependency, so FastAPI runs
    it once per request.
    """
    if not is_scope(scope):
        raise ValueError(f"unknown scope {scope!r}")

    async def scoped(principal: CurrentPrincipal) -> Principal:
        try:
            ensure_scope(principal, scope)
        except InsufficientScopeError as exc:
            raise insufficient_scope_error(exc.scope) from None
        return principal

    return scoped


ReadPrincipal = Annotated[Principal, Depends(require_scope("read"))]
IngestPrincipal = Annotated[Principal, Depends(require_scope("ingest"))]
AdminPrincipal = Annotated[Principal, Depends(require_scope("admin"))]


async def read_access(principal: ReadPrincipal) -> ReadFilter:
    """The ``read`` scope and the authorization filter for the caller, for a read route."""
    # Called through its module, so a test can watch the read path call the filter.
    return authorization.authorize_read(principal)


ReadAccess = Annotated[ReadFilter, Depends(read_access)]
"""What a read route takes: the caller's :class:`~memini.auth.ReadFilter`, after the read scope."""


async def admin_access(principal: AdminPrincipal) -> WriteAccess:
    """The ``admin`` scope and what the caller may change, for an administrative write route."""
    return authorization.authorize_write(principal, "admin")


AdminAccess = Annotated[WriteAccess, Depends(admin_access)]
"""What an administrative write route takes: the caller's :class:`~memini.auth.WriteAccess`, after
the admin scope."""


async def ingest_access(principal: IngestPrincipal) -> WriteAccess:
    """The ``ingest`` scope and what the caller may change, for a route that stores records."""
    return authorization.authorize_write(principal, "ingest")


IngestAccess = Annotated[WriteAccess, Depends(ingest_access)]
"""What an ingest route takes: the caller's :class:`~memini.auth.WriteAccess`, after the ingest
scope."""
