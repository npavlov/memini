"""The fake Hearth MCP server (tests/fake_mcp.py) over stdio, for the stdio test of the service
executor: ``python fake_mcp_stdio.py BASE`` serves the threads of :func:`fake_mcp.recent_threads`
before the unix time ``BASE``. When ``HEARTH_TOKEN`` is not the token the test set, every call
answers ``isError``. Everything here is made up."""

import os
import sys
from datetime import UTC, datetime

import anyio
from mcp.server.stdio import stdio_server

from fake_mcp import FakeMcp, recent_threads
from hearth_helpers import FakeHearth

EXPECTED_TOKEN = "synthetic-stdio-token-0001"  # noqa: S105 - fake, checked by the test


async def main() -> None:
    base = datetime.fromtimestamp(int(sys.argv[1]), tz=UTC)
    fake = FakeMcp(FakeHearth(threads=recent_threads(base)))
    if os.environ.get("HEARTH_TOKEN") != EXPECTED_TOKEN:
        fake.fail("channel_history", *["error"] * 100)
    server = fake.server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    anyio.run(main)
