"""The MCP endpoint: Memini's tools over streamable HTTP, in the same process as the HTTP API.

- :mod:`memini.mcp.server`: the server, its instructions and :data:`TOOLS`, the one list of tools.
- :mod:`memini.mcp.tools`: what a tool is: typed arguments, a typed result, a scope.
- :mod:`memini.mcp.text`: how a text content shows the sources' text: in fenced blocks nothing
  closes early, with one-line escaped labels (principle 11).
- :mod:`memini.mcp.query`: ``search``, ``search_code``, ``get_symbol``, ``read_source`` and
  ``list_commits``, over :mod:`memini.query`, the library the HTTP routes call too.
- :mod:`memini.mcp.comms`: ``list_meetings``, ``get_meeting``, ``list_messages``, ``get_thread``
  and the write ``add_document``, over :mod:`memini.comms`, the library the HTTP routes call too.
- :mod:`memini.mcp.catalog`: ``get_catalog``, ``get_domain``, ``catalog_guide`` and the write
  ``put_project``, over :mod:`memini.catalog`, the library the HTTP routes call too.
- :mod:`memini.mcp.status`: the ``status`` tool.
- :mod:`memini.mcp.decisions`: ``record_decision``, ``supersede_decision``, ``close_decision`` and
  ``retract_decision``, over :mod:`memini.decisions`; writes, with the ``ingest`` scope.
- :mod:`memini.mcp.endpoint`: the server as an ASGI app, stateless with JSON responses.

The HTTP application mounts the endpoint at :data:`MCP_PATH` behind bearer-token authentication
(:mod:`memini.api.mcp`). This package does not import :mod:`memini.api`, which imports it.
"""

from memini.mcp.endpoint import MCP_PATH, McpEndpoint
from memini.mcp.server import SERVER_INSTRUCTIONS, SERVER_NAME, TOOLS, bind_principal, build_server
from memini.mcp.tools import NoArguments, Tool, ToolCall, ToolError

__all__ = [
    "MCP_PATH",
    "SERVER_INSTRUCTIONS",
    "SERVER_NAME",
    "TOOLS",
    "McpEndpoint",
    "NoArguments",
    "Tool",
    "ToolCall",
    "ToolError",
    "bind_principal",
    "build_server",
]
