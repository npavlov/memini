"""The MCP endpoint in the HTTP application: :data:`~memini.mcp.MCP_PATH`, behind a bearer token.

Every request to the endpoint passes :class:`RequireReadToken` before the MCP server reads a byte of
it. The check is the one the ``/v1`` routes make, with the same code and the same responses:

- no bearer token: 401 ``unauthorized``; a malformed, unknown, revoked or expired one: 401
  ``invalid_token``; both with the ``WWW-Authenticate`` challenge of :mod:`memini.api.auth`;
- a valid token without the ``read`` scope: 403 ``insufficient_scope``;
- no authenticator (no database and none given): 503 ``unavailable``.

The authenticated principal is attached to the request (``request.state.principal``); the MCP server
reads it from there for each message. Tools then check their own scope and read through
``authorize_read``.
"""

from starlette.datastructures import Headers
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from memini.api.auth import insufficient_scope_error, unauthorized_error
from memini.api.errors import ApiError, error_response
from memini.auth import (
    AuthenticationError,
    Authenticator,
    InsufficientScopeError,
    Principal,
    bearer_token,
    ensure_scope,
)
from memini.mcp import MCP_PATH, McpEndpoint, bind_principal

__all__ = ["RequireReadToken", "mcp_route"]


class RequireReadToken:
    """ASGI wrapper: let a request through only with a valid token that has the ``read`` scope."""

    def __init__(self, app: ASGIApp, authenticator: Authenticator | None) -> None:
        self.app = app
        self.authenticator = authenticator

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            raise RuntimeError("the MCP endpoint serves HTTP only")
        try:
            principal = await self._authenticate(Headers(scope=scope))
        except ApiError as error:
            response = error_response(
                error.status_code, error.code, error.detail, headers=error.headers
            )
            await response(scope, receive, send)
            return
        bind_principal(scope, principal)
        await self.app(scope, receive, send)

    async def _authenticate(self, headers: Headers) -> Principal:
        # The same order as memini.api.auth.authenticate_request: a request without a token is
        # told so even when authentication is unavailable.
        try:
            token = bearer_token(headers.get("Authorization"))
            if self.authenticator is None:
                raise ApiError(503, "unavailable", "Authentication is not available.")
            principal = await self.authenticator(token)
        except AuthenticationError as exc:
            raise unauthorized_error(exc.reason) from None
        try:
            ensure_scope(principal, "read")
        except InsufficientScopeError as exc:
            raise insufficient_scope_error(exc.scope) from None
        return principal


def mcp_route(endpoint: McpEndpoint, authenticator: Authenticator | None) -> Route:
    """The route that serves ``endpoint`` at :data:`~memini.mcp.MCP_PATH`, behind the token check.

    A route, not a mount: a mount matches only below its path, so ``/mcp`` itself would be
    redirected to ``/mcp/``.
    """
    return Route(
        MCP_PATH,
        RequireReadToken(endpoint, authenticator),
        name="mcp",
        include_in_schema=False,
    )
