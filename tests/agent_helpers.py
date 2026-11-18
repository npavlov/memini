"""The agent executor's tests: an mcp source of the invented Hearth chat server (docs/recipes.md;
tests/hearth_helpers.py) with threads in the two windows a first run plans, the parser registered
under the recipe's name, the scripted calls of a fake agent, and the service under uvicorn for the
capture hook. Every name, id and text is made up.

**The mcp source** is inserted with plain SQL: the admin library adds mcp sources in memini-bz1.
Its recipe is the TOML text of tests/fixtures/recipes/hearth.toml under ``source.config.recipe``
(:data:`memini.executors.agent.RECIPE_KEY`), its stream the channel ``C0042``.

**The windows.** A run plans from the database clock: a new stream gets ``initial`` (14 days)
back from it, split at ``max_span`` (7 days) into two windows. The threads are placed days away
from both bounds, so the plan's windows hold them whatever the moment the test runs.
"""

import asyncio
import json
import socket
import threading
import time
import uuid
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import uvicorn
from psycopg import AsyncConnection
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from hearth_helpers import (
    CHANNEL,
    FAKE_TOKEN,
    HEARTH_RECIPE_PATH,
    FakeHearth,
    Message,
    Thread,
    hearth_parser,
    ts,
)
from memini.api import create_app
from memini.config import Settings
from memini.db import open_pool
from memini.embeddings import FakeEmbedder
from memini.executors.agent import RECIPE_KEY
from memini.recipes import register_parser, unregister_parser

type Conn = AsyncConnection[TupleRow]
type Json = dict[str, Any]

PARSER: Final = "hearth.thread"
"""The parser the hearth recipe names."""
SOURCE: Final = "hearth"
DAY: Final = timedelta(days=1)
HOUR: Final = timedelta(hours=1)


def recipe_text() -> str:
    return HEARTH_RECIPE_PATH.read_text(encoding="utf-8")


def recent_threads(now: datetime) -> list[Thread]:
    """Five threads of channel C0042: two in the older window of a first run (14 to 7 days back),
    three in the newer one (7 days back to now), with replies to three of them. One reply holds a
    fake token: redaction keeps it out of every stored row."""

    def at(days: float, hours: int = 0) -> datetime:
        return now - days * DAY + hours * HOUR

    return [
        Thread(
            Message(ts(at(10)), "U0001", "Ann Example", "Shall we fire the kiln on Friday?"),
            [Message(ts(at(10, 1)), "U0002", "Bo Sample", "Friday works for me.")],
        ),
        Thread(Message(ts(at(9)), "U0002", "Bo Sample", "The glaze order arrived.")),
        Thread(
            Message(ts(at(3)), "U0003", "Cy Placeholder", "Who has the shelf keys?"),
            [
                Message(ts(at(3, 1)), "U0001", "Ann Example", "They are in the drawer."),
                Message(
                    ts(at(3, 2)),
                    "U0003",
                    "Cy Placeholder",
                    f"Found them. The shop login token is {FAKE_TOKEN}",
                ),
            ],
        ),
        Thread(Message(ts(at(2)), "U0004", "Dee Sample", "Kiln at 1220 degrees.")),
        Thread(
            Message(ts(at(1)), "U0001", "Ann Example", "Unloading tomorrow."),
            [Message(ts(at(1, 1)), "U0002", "Bo Sample", "Cones look right.")],
        ),
    ]


def every_text(threads: list[Thread]) -> list[str]:
    return [m.text for t in threads for m in (t.root, *t.replies)]


def fake_hearth() -> FakeHearth:
    return FakeHearth(threads=recent_threads(datetime.now(UTC)))


def call_hearth(server: FakeHearth, tool: str, arguments: Mapping[str, Any]) -> Json:
    """The server's MCP result of one call."""
    if tool == "channel_history":
        return server.channel_history(arguments)
    if tool == "thread_replies":
        return server.thread_replies(arguments)
    raise AssertionError(f"the recipe calls no tool {tool}")


@contextmanager
def hearth_parser_registered() -> Generator[None]:
    register_parser(PARSER, hearth_parser(names=True), replace=True)
    try:
        yield
    finally:
        unregister_parser(PARSER)


@dataclass(frozen=True)
class McpSource:
    source_id: uuid.UUID
    stream_id: uuid.UUID


async def add_mcp_source(
    db: Conn,
    workspace_id: uuid.UUID,
    owner_id: uuid.UUID,
    *,
    name: str = SOURCE,
    config: Mapping[str, Any] | None = None,
    stream: str = CHANNEL,
) -> McpSource:
    """An mcp source with the hearth recipe and one stream, inserted with plain SQL."""
    cur = await db.execute(
        "INSERT INTO source (workspace_id, owner_id, name, kind, config)"
        " VALUES (%s, %s, %s, 'mcp', %s) RETURNING id",
        (
            workspace_id,
            owner_id,
            name,
            Jsonb(dict(config) if config is not None else {RECIPE_KEY: recipe_text()}),
        ),
    )
    source = await cur.fetchone()
    assert source is not None
    cur = await db.execute(
        "INSERT INTO stream (workspace_id, owner_id, source_id, key) VALUES (%s, %s, %s, %s)"
        " RETURNING id",
        (workspace_id, owner_id, source[0], stream),
    )
    found = await cur.fetchone()
    assert found is not None
    return McpSource(source[0], found[0])


def response_body(call: Mapping[str, Any], result: Mapping[str, Any]) -> Json:
    """What a capture hook posts for ``call``: its content blocks, as Claude Code hands them to a
    PostToolUse hook."""
    return {
        "call_id": call["id"],
        "tool": call["tool"],
        "arguments": call["arguments"],
        "result": result["content"],
    }


def payloads_of(rows: list[tuple[Any, ...]]) -> dict[str, Any]:
    """external_id -> (payload, occurred_at, external_url) of record rows."""
    return {row[0]: (json.loads(bytes(row[1])), row[2], row[3]) for row in rows}


# --- The service under uvicorn -------------------------------------------------------------------


@contextmanager
def running_service(settings: Settings) -> Generator[str]:
    """Serve the application on a free port in a thread of its own until the block ends, as
    tests/test_cli_persons_db.py does: the capture hook is a process of its own and reaches the
    service over HTTP."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    state: dict[str, Any] = {}

    async def serve() -> None:
        async with open_pool(
            settings.database_url.get_secret_value(), min_size=1, max_size=4, timeout=10
        ) as pool:
            app = create_app(settings, pool=pool, embedder=FakeEmbedder())
            config = uvicorn.Config(
                app, host="127.0.0.1", port=port, lifespan="on", log_config=None, access_log=False
            )
            server = uvicorn.Server(config)
            state["server"] = server
            await server.serve()

    def run() -> None:
        try:
            asyncio.run(serve())
        except BaseException as error:  # reported by the test thread
            state["error"] = error

    thread = threading.Thread(target=run, name="memini-test-service", daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while True:
        server: uvicorn.Server | None = state.get("server")
        if server is not None and server.started:
            break
        if not thread.is_alive() or time.monotonic() > deadline:
            raise AssertionError(f"the service did not start: {state.get('error')!r}")
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=60)
        assert not thread.is_alive(), "the service did not stop"
    if "error" in state:
        raise state["error"]
