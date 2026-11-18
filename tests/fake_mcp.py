"""A fake Hearth MCP server for the tests of the service executor: the invented chat server of
docs/recipes.md served by the official MCP SDK, over streamable HTTP inside the test process (an
ASGI app that httpx2 reaches without a socket) or over stdio (``fake_mcp_stdio.py``). No test
reaches a real server or the network. Every name, id and text is made up.

The server lists ``channel_history`` and ``thread_replies`` with input schemas, and answers from a
:class:`hearth_helpers.FakeHearth`. Faults are queued per tool: ``"slow"`` sleeps past a short
timeout before it answers, ``"error"`` answers ``isError``; the ASGI app answers queued HTTP
statuses (``429`` with ``Retry-After``, ``500``) to the next ``tools/call`` requests. It records
the ``Authorization`` header of every request and every call it answered.
"""

import json
from collections import deque
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final, cast

import anyio
import mcp_types as types
from mcp.server import Server, ServerRequestContext
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.types import Message, Receive, Scope, Send

from hearth_helpers import FakeHearth, Thread, ts
from hearth_helpers import Message as HearthMessage

__all__ = ["FakeMcp", "Received", "recent_threads"]

SERVER_NAME: Final = "hearth-mcp"
SERVER_VERSION: Final = "2.3.1"
SLOW_SECONDS: Final = 5.0

_HISTORY_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "channel": {"type": "string"},
        "oldest": {"type": "integer"},
        "latest": {"type": "integer"},
        "limit": {"type": "integer"},
        "cursor": {"type": "string"},
    },
    "required": ["channel"],
}
_REPLIES_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "channel": {"type": "string"},
        "thread": {"type": "string"},
        "cursor": {"type": "string"},
    },
    "required": ["channel", "thread"],
}


def recent_threads(base: datetime) -> list[Thread]:
    """Three threads of a channel in the day before ``base``, two with replies."""
    hour = timedelta(hours=1)
    start = base - timedelta(days=1)
    return [
        Thread(
            HearthMessage(ts(start + 2 * hour), "U0001", "Ann Example", "Kiln on Friday?"),
            [HearthMessage(ts(start + 3 * hour), "U0002", "Bo Sample", "Friday works.")],
        ),
        Thread(HearthMessage(ts(start + 5 * hour), "U0002", "Bo Sample", "Glaze order arrived.")),
        Thread(
            HearthMessage(ts(start + 8 * hour), "U0003", "Cy Placeholder", "Who has the keys?"),
            [
                HearthMessage(ts(start + 9 * hour), "U0001", "Ann Example", "In the drawer."),
                HearthMessage(ts(start + 10 * hour), "U0003", "Cy Placeholder", "Found them."),
            ],
        ),
    ]


@dataclass(frozen=True)
class Received:
    """A call the server answered."""

    tool: str
    arguments: Mapping[str, Any]


@dataclass
class FakeMcp:
    """The fake server; see the module."""

    hearth: FakeHearth
    name: str = SERVER_NAME
    version: str = SERVER_VERSION
    tools: tuple[str, ...] = ("channel_history", "thread_replies")
    history_schema: Mapping[str, Any] = field(default_factory=lambda: dict(_HISTORY_SCHEMA))
    faults: dict[str, deque[str]] = field(default_factory=dict[str, deque[str]])
    http_faults: deque[tuple[int, str | None]] = field(
        default_factory=deque[tuple[int, str | None]]
    )
    received: list[Received] = field(default_factory=list[Received])
    authorizations: list[str | None] = field(default_factory=list[str | None])
    http_statuses: list[int] = field(default_factory=list[int])

    def fail(self, tool: str, *faults: str) -> None:
        """Queue ``faults`` (``"slow"``, ``"error"``) for the next calls of ``tool``."""
        self.faults.setdefault(tool, deque()).extend(faults)

    def answer_http(self, status: int, retry_after: str | None = None) -> None:
        """Answer the next ``tools/call`` request with ``status`` instead of calling the tool."""
        self.http_faults.append((status, retry_after))

    def calls(self, tool: str | None = None) -> list[Received]:
        return [call for call in self.received if tool is None or call.tool == tool]

    # --- The MCP server --------------------------------------------------------------------------

    def server(self) -> Server[Any]:
        async def list_tools(
            ctx: ServerRequestContext[Any, Any], params: types.PaginatedRequestParams | None
        ) -> types.ListToolsResult:
            schemas = {
                "channel_history": dict(self.history_schema),
                "thread_replies": dict(_REPLIES_SCHEMA),
            }
            return types.ListToolsResult(
                tools=[types.Tool(name=name, input_schema=schemas[name]) for name in self.tools]
            )

        async def call_tool(
            ctx: ServerRequestContext[Any, Any], params: types.CallToolRequestParams
        ) -> types.CallToolResult:
            arguments = dict(params.arguments or {})
            queue = self.faults.get(params.name)
            fault = queue.popleft() if queue else None
            if fault == "slow":
                await anyio.sleep(SLOW_SECONDS)
            if fault == "error":
                self.received.append(Received(params.name, arguments))
                return types.CallToolResult(
                    content=[types.TextContent(type="text", text="synthetic: try again later")],
                    is_error=True,
                )
            if params.name == "channel_history":
                result = self.hearth.channel_history(arguments)
            elif params.name == "thread_replies":
                result = self.hearth.thread_replies(arguments)
            else:  # pragma: no cover - the recipe calls these two
                raise AssertionError(params.name)
            self.received.append(Received(params.name, arguments))
            return types.CallToolResult.model_validate(result)

        return Server(
            self.name, version=self.version, on_list_tools=list_tools, on_call_tool=call_tool
        )

    @asynccontextmanager
    async def asgi(self) -> AsyncGenerator[Any]:
        """The server as an ASGI app, running for the duration of the block."""
        manager = StreamableHTTPSessionManager(
            app=self.server(), stateless=True, json_response=True, security_settings=None
        )

        async def app(scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] != "http":  # pragma: no cover - httpx2 sends HTTP only
                return
            headers = dict(scope["headers"])
            auth = headers.get(b"authorization")
            self.authorizations.append(None if auth is None else auth.decode())
            body = b""
            more = True
            while more:
                message = await receive()
                body += message.get("body", b"")
                more = message.get("more_body", False)
            if self.http_faults and _is_tool_call(body):
                status, retry_after = self.http_faults.popleft()
                extra = [] if retry_after is None else [(b"retry-after", retry_after.encode())]
                self.http_statuses.append(status)
                await send({"type": "http.response.start", "status": status, "headers": extra})
                await send({"type": "http.response.body", "body": b"synthetic"})
                return
            sent = False

            async def replay() -> Message:
                nonlocal sent
                if sent:
                    await anyio.sleep_forever()
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}

            await manager.handle_request(scope, replay, send)

        async with manager.run():
            yield app


def _is_tool_call(body: bytes) -> bool:
    try:
        message: object = json.loads(body)
    except ValueError:
        return False
    return isinstance(message, dict) and cast(dict[str, Any], message).get("method") == "tools/call"
