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

Then :class:`CountWriteCalls` reads the body, which the request limits keep to
``http_max_body_size``, and hands it on unchanged. A ``tools/call`` of a tool that is not
read-only (:data:`WRITE_TOOLS`), alone or in a batch, is counted against the write budget of the
token instead of the read budget every MCP request is counted against first
(:func:`memini.api.limits.count_as_write`); over that budget it answers 429 ``rate_limited`` and the
server never sees it. Which tool a request calls is known only from its body, so this is the first
place it can be told. A body that is not JSON-RPC calls no tool here, and the server answers it.

With ``mcp_enabled`` false there is no MCP server behind the path: :class:`McpSwitchedOff` answers
every request 404 :data:`MCP_DISABLED`, before a token is looked at, so the answer is the same
with a token and without, and no tool runs. The request limits count such a request as any other
to the path. The HTTP API, which serves the same calls, is not affected.
"""

import json
from collections.abc import Collection
from typing import Final, cast

from starlette.datastructures import Headers
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from memini.api.auth import insufficient_scope_error, unauthorized_error
from memini.api.errors import ApiError, error_response
from memini.api.limits import count_as_write
from memini.auth import (
    AuthenticationError,
    Authenticator,
    InsufficientScopeError,
    Principal,
    bearer_token,
    ensure_scope,
)
from memini.mcp import MCP_PATH, TOOLS, McpEndpoint, bind_principal

__all__ = [
    "MCP_DISABLED",
    "WRITE_TOOLS",
    "CountWriteCalls",
    "McpSwitchedOff",
    "RequireReadToken",
    "calls_a_tool",
    "mcp_route",
]

WRITE_TOOLS: Final = frozenset(tool.name for tool in TOOLS if not tool.read_only)
"""The tools a call of which is counted as a write: those that are not read-only."""

MCP_DISABLED: Final = "mcp_disabled"
"""The code of the 404 every request to the endpoint gets while ``mcp_enabled`` is false."""


class McpSwitchedOff:
    """ASGI app: the endpoint of a service whose ``mcp_enabled`` is false; see the module."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = error_response(
            404, MCP_DISABLED, "The MCP endpoint is switched off on this server."
        )
        await response(scope, receive, send)


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


class CountWriteCalls:
    """ASGI wrapper: count a request that calls a write tool as a write; see the module."""

    def __init__(self, app: ASGIApp, write_tools: Collection[str] = WRITE_TOOLS) -> None:
        self.app = app
        self.write_tools = frozenset(write_tools)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        messages = await _read_body(receive)
        body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.request")
        if calls_a_tool(body, self.write_tools):
            refusal = count_as_write(scope)
            if refusal is not None:
                response = error_response(
                    refusal.status_code, refusal.code, refusal.detail, headers=refusal.headers
                )
                await response(scope, _replay(messages, receive), send)
                return
        await self.app(scope, _replay(messages, receive), send)


async def _read_body(receive: Receive) -> list[Message]:
    """The messages of the request up to the end of its body, or up to a disconnect."""
    messages: list[Message] = []
    while True:
        message = await receive()
        messages.append(message)
        if message["type"] != "http.request" or not message.get("more_body", False):
            return messages


def _replay(messages: list[Message], receive: Receive) -> Receive:
    """``receive`` with ``messages`` first, as if they had not been read yet."""
    pending = list(messages)

    async def replayed() -> Message:
        return pending.pop(0) if pending else await receive()

    return replayed


def calls_a_tool(body: bytes, names: Collection[str]) -> bool:
    """Whether the JSON-RPC request in ``body``, or one in a batch, is a ``tools/call`` of a tool
    named in ``names``. A body that is not JSON calls none."""
    try:
        parsed: object = json.loads(body)
    except (ValueError, RecursionError):  # JSONDecodeError and UnicodeDecodeError are ValueErrors
        return False
    messages = cast(list[object], parsed) if isinstance(parsed, list) else [parsed]
    return any(_tool_called(message) in names for message in messages)


def _tool_called(message: object) -> str | None:
    if not isinstance(message, dict):
        return None
    request = cast(dict[str, object], message)
    params = request.get("params")
    if request.get("method") != "tools/call" or not isinstance(params, dict):
        return None
    name = cast(dict[str, object], params).get("name")
    return name if isinstance(name, str) else None


def mcp_route(
    endpoint: McpEndpoint | None,
    authenticator: Authenticator | None,
    *,
    write_tools: Collection[str] = WRITE_TOOLS,
) -> Route:
    """The route that serves ``endpoint`` at :data:`~memini.mcp.MCP_PATH`, behind the token check;
    a call of one of ``write_tools`` is counted as a write. Without an endpoint (``mcp_enabled``
    is false), the route that answers :data:`MCP_DISABLED` instead.

    A route, not a mount: a mount matches only below its path, so ``/mcp`` itself would be
    redirected to ``/mcp/``.
    """
    app: ASGIApp = (
        McpSwitchedOff()
        if endpoint is None
        else RequireReadToken(CountWriteCalls(endpoint, write_tools), authenticator)
    )
    return Route(MCP_PATH, app, name="mcp", include_in_schema=False)
