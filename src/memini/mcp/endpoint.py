"""The MCP server as an ASGI application: streamable HTTP, stateless, JSON responses.

Choices, and why:

- **Stateless.** Every POST stands alone: no ``Mcp-Session-Id``, nothing kept between requests.
  Each request is authenticated on its own, so there is no session that a second token could
  reuse, and no session state to lose on restart. The tools are request and response; none needs
  to call back to the client. The 2026-07-28 protocol revision is stateless anyway.
- **JSON responses**, not SSE streams: one JSON-RPC response per POST. Nothing is streamed yet.
- **POST only.** A stateless server sends nothing unprompted, so ``GET`` (a stream for server
  messages) and ``DELETE`` (end a session) answer 405, as the transport allows.
- **No host or origin check** of the SDK. It guards against DNS rebinding, but every request needs
  a bearer token, which a page in a browser does not have, and the allowed host names of a
  deployment are not known here.

The SDK's session manager runs inside :meth:`McpEndpoint.run`, which the application's lifespan
enters: Starlette does not run the lifespan of an app it routes to. Authentication is not done here;
:mod:`memini.api.mcp` wraps this app, and the server refuses a message without a principal.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any, Final

from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.exceptions import HTTPException
from starlette.types import Receive, Scope, Send

__all__ = ["MCP_PATH", "McpEndpoint"]

MCP_PATH: Final = "/mcp"
"""Where the HTTP application serves the MCP endpoint."""


class McpEndpoint:
    """The ASGI app that serves ``server`` over streamable HTTP while :meth:`run` is entered."""

    def __init__(self, server: Server[Any]) -> None:
        self.server = server
        self._manager: StreamableHTTPSessionManager | None = None

    @asynccontextmanager
    async def run(self) -> AsyncGenerator[None]:
        """Serve requests until exit. Enter it in the lifespan of the application.

        A session manager runs only once, so each run gets a new one; the endpoint can run again
        after it stopped, as a test client may start an application twice.
        """
        if self._manager is not None:
            raise RuntimeError("the MCP endpoint is already running")
        manager = StreamableHTTPSessionManager(
            app=self.server, stateless=True, json_response=True, security_settings=None
        )
        async with manager.run():
            self._manager = manager
            try:
                yield
            finally:
                self._manager = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            # The application renders it in its error shape.
            raise HTTPException(405, headers={"Allow": "POST"})
        manager = self._manager
        if manager is None:
            raise RuntimeError("the MCP endpoint is not running; enter McpEndpoint.run() first")
        await manager.handle_request(scope, receive, send)
