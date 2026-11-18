"""What an MCP tool is in Memini: typed arguments, a typed result, the scope it needs.

A :class:`Tool` validates its arguments with a pydantic model, runs, and returns a pydantic model.
The client gets that model as ``structuredContent``, described by the tool's ``outputSchema``, and a
short text summary as ``content``. Errors stay inside the result (``isError``) so a model can see
them; their text never holds argument values or exception text, which can carry private content or
secrets. The exception goes to the server log. A tool that refuses for a reason the caller can act
on (a query that is not valid, nothing found) raises :class:`ToolError`, whose message is sent as
it is.

A tool receives a :class:`ToolCall`: the authenticated principal and the service's resources. A tool
that reads data calls ``authorization.authorize_read(call.principal)`` and passes the filter to its
read functions, like a read route does.
"""

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from mcp_types import CallToolResult, TextContent, ToolAnnotations
from mcp_types import Tool as ToolDefinition
from pydantic import BaseModel, ConfigDict, ValidationError

from memini.auth import Principal, Scope
from memini.db import TIMEOUT_ERRORS, Pool
from memini.query import QueryServices

__all__ = ["NoArguments", "Tool", "ToolCall", "ToolDefinition", "ToolError"]

_log = logging.getLogger(__name__)


class ToolError(Exception):
    """A refusal the caller can act on. The message is sent to the client as it is: it must never
    hold an argument value, stored content, a secret or exception text."""


class NoArguments(BaseModel):
    """The arguments of a tool that takes none. Unknown arguments are refused."""

    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True, slots=True)
class ToolCall:
    """What a tool gets for one call."""

    principal: Principal
    """The caller. Reads go through ``authorize_read(principal)``."""
    pool: Pool | None
    """The database pool; ``None`` when the service runs without a database."""
    services: QueryServices = field(default_factory=QueryServices)
    """What the query tools need: search options, the repository mirrors, the embedder."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Tool[ArgumentsT: BaseModel, ResultT: BaseModel]:
    """One MCP tool. Register it in :data:`memini.mcp.server.TOOLS`."""

    name: str
    title: str
    description: str
    arguments: type[ArgumentsT]
    result: type[ResultT]
    run: Callable[[ToolCall, ArgumentsT], Awaitable[ResultT]]
    """The tool itself. Raises on failure; the caller turns that into an error result."""
    summarize: Callable[[ResultT], str]
    """The text content sent next to the structured result: short, for people and for clients
    that read only text."""
    scope: Scope = "read"
    """The scope a token needs to see and call the tool."""
    read_only: bool = True
    """False for a tool that changes data: ``tools/list`` says so, and a call is counted against
    the write budget of the token (:mod:`memini.api.mcp`, :mod:`memini.api.limits`)."""

    def definition(self) -> ToolDefinition:
        """The tool as ``tools/list`` describes it."""
        return ToolDefinition(
            name=self.name,
            title=self.title,
            description=self.description,
            input_schema=self.arguments.model_json_schema(mode="validation"),
            output_schema=self.result.model_json_schema(mode="serialization"),
            # Memini's tools work on its own store only, never on the outside world.
            annotations=ToolAnnotations(read_only_hint=self.read_only, open_world_hint=False),
        )

    async def call(self, call: ToolCall, arguments: Mapping[str, Any]) -> CallToolResult:
        """Validate ``arguments``, run the tool and shape its result for the client."""
        try:
            parsed = self.arguments.model_validate(arguments)
        except ValidationError as exc:
            # Locations and error types only: the values are the caller's input.
            problems = ", ".join(
                f"{'.'.join(str(part) for part in error['loc']) or '(arguments)'}: {error['type']}"
                for error in exc.errors()
            )
            return _error_result(f"Invalid arguments: {problems}.")
        try:
            result = await self.run(call, parsed)
        except ToolError as error:
            return _error_result(str(error))
        except TIMEOUT_ERRORS as exc:
            _log.warning(
                "tool %s stopped by a database time limit (%s)", self.name, type(exc).__name__
            )
            return _error_result(
                f"The {self.name} tool was stopped: a database statement ran past its time limit."
                " Try again later, or ask for less."
            )
        except Exception:
            _log.exception("tool %s failed", self.name)
            return _error_result(f"The {self.name} tool failed; the server log has the details.")
        return CallToolResult(
            content=[TextContent(type="text", text=self.summarize(result))],
            structured_content=result.model_dump(mode="json"),
        )


def _error_result(message: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=message)], is_error=True)
