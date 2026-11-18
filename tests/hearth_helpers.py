"""Hearth, the invented chat server of docs/recipes.md, for the tests of the raw response store and
replay: a fake server with a channel of threads, a client of it that counts its calls, a minimal
executor of the hearth recipe, and a trivial parser for it. Every name, id and text is made up.

The executor here is a test double, not the service executor (memini-bz1): it runs one window of
the recipe's list call with cursor paging and a detail call per item with replies, stores each
result with :func:`memini.responses.store_response`, and ends with
:func:`memini.responses.ingest_window`, as both executors do.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import TupleRow

from memini.auth import WriteAccess
from memini.ingest import RecordIn
from memini.intake import PlannedWindow
from memini.recipes import (
    ParsedRecord,
    ParsedWindow,
    Recipe,
    WindowInput,
    WindowParser,
    load_recipe,
    parse_timestamp,
    recipe_digest,
    render_arguments,
)
from memini.responses import ServerInfo, WindowIngest, begin_window, ingest_window, store_response
from redaction_helpers import GITHUB

type Conn = AsyncConnection[TupleRow]

HEARTH_RECIPE_PATH: Final = Path(__file__).parent / "fixtures" / "recipes" / "hearth.toml"
CHANNEL: Final = "C0042"
SERVER: Final = ServerInfo(name="hearth-mcp", version="2.3.1")

T0: Final = datetime(2026, 3, 2, tzinfo=UTC)
DAY: Final = timedelta(days=1)
HOUR: Final = timedelta(hours=1)
WINDOW_1: Final = PlannedWindow(kind="initial", start=T0, end=T0 + 3 * DAY)
WINDOW_2: Final = PlannedWindow(kind="forward", start=T0 + 2 * DAY, end=T0 + 4 * DAY)

FAKE_TOKEN: Final = GITHUB
"""Shaped like a GitHub token, fake: redaction must keep it out of every stored row."""


def hearth_recipe() -> Recipe:
    return load_recipe(HEARTH_RECIPE_PATH)


def ts(moment: datetime) -> str:
    """A Hearth message id: the time in unix seconds, as a string with a fraction."""
    return f"{int(moment.timestamp())}.000100"


@dataclass
class Message:
    ts: str
    user: str
    user_name: str
    text: str

    def wire(self, reply_count: int | None = None) -> dict[str, Any]:
        found: dict[str, Any] = {
            "ts": self.ts,
            "user": self.user,
            "user_name": self.user_name,
            "text": self.text,
        }
        if reply_count is not None:
            found["reply_count"] = reply_count
        return found


@dataclass
class Thread:
    root: Message
    replies: list[Message] = field(default_factory=list[Message])


def initial_threads() -> list[Thread]:
    """Four threads of channel C0042, 2 to 4 March 2026; the last one is in both windows."""
    return [
        Thread(
            Message(ts(T0 + 9 * HOUR), "U0001", "Ann Example", "Shall we fire the kiln on Friday?"),
            [Message(ts(T0 + 10 * HOUR), "U0002", "Bo Sample", "Friday works for me.")],
        ),
        Thread(Message(ts(T0 + 13 * HOUR), "U0002", "Bo Sample", "The glaze order arrived.")),
        Thread(
            Message(ts(T0 + DAY + 8 * HOUR), "U0003", "Cy Placeholder", "Who has the shelf keys?"),
            [
                Message(ts(T0 + DAY + 9 * HOUR), "U0001", "Ann Example", "They are in the drawer."),
                Message(
                    ts(T0 + DAY + 11 * HOUR),
                    "U0003",
                    "Cy Placeholder",
                    f"Found them. The shop login token is {FAKE_TOKEN}",
                ),
            ],
        ),
        Thread(
            Message(ts(T0 + 2 * DAY + 6 * HOUR), "U0004", "Dee Sample", "Kiln at 1220 degrees."),
            [Message(ts(T0 + 2 * DAY + 7 * HOUR), "U0001", "Ann Example", "Cones look right.")],
        ),
    ]


def late_reply() -> Message:
    """A reply to the last thread that arrives after the first window was synced."""
    return Message(ts(T0 + 3 * DAY + 2 * HOUR), "U0002", "Bo Sample", "Unloading tomorrow.")


def _result(document: Mapping[str, Any]) -> dict[str, Any]:
    """An MCP tool result as the wire carries it: a JSON document in a text block."""
    return {
        "content": [{"type": "text", "text": json.dumps(document)}],
        "isError": False,
    }


@dataclass
class FakeHearth:
    """The server's data and its two tools, ``channel_history`` and ``thread_replies``."""

    threads: list[Thread] = field(default_factory=initial_threads)
    page_size: int = 2

    def channel_history(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        oldest, latest = int(arguments["oldest"]), int(arguments["latest"])
        listed = sorted(
            (t for t in self.threads if oldest <= float(t.root.ts) < latest),
            key=lambda t: float(t.root.ts),
            reverse=True,  # newest_first, as the recipe says
        )
        start = int(str(arguments.get("cursor", "cur-0")).removeprefix("cur-"))
        page = listed[start : start + self.page_size]
        more = start + self.page_size < len(listed)
        return _result(
            {
                "messages": [t.root.wire(len(t.replies)) for t in page],
                "has_more": more,
                "next_cursor": f"cur-{start + self.page_size}" if more else None,
            }
        )

    def thread_replies(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        thread = next(t for t in self.threads if t.root.ts == arguments["thread"])
        messages = [thread.root.wire(), *(reply.wire() for reply in thread.replies)]
        return _result({"messages": messages, "next_cursor": None})


@dataclass
class FakeClient:
    """A client of :class:`FakeHearth` that counts every call: replay must make none."""

    server: FakeHearth
    calls: int = 0

    async def call_tool(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        self.calls += 1
        if name == "channel_history":
            return self.server.channel_history(arguments)
        if name == "thread_replies":
            return self.server.thread_replies(arguments)
        raise AssertionError(f"the recipe calls no tool {name}")


@dataclass(frozen=True)
class Target:
    workspace_id: UUID
    owner_id: UUID
    source_id: UUID
    stream_id: UUID


async def sync_window(
    conn: Conn,
    client: FakeClient,
    target: Target,
    window: PlannedWindow,
    *,
    recipe: Recipe,
    parser: WindowParser,
    access: WriteAccess,
    stream: str = CHANNEL,
    run_id: UUID | None = None,
) -> WindowIngest:
    """Run one window of the hearth recipe against ``client``: list pages, then the detail call
    of each item with replies; store each result; then parse and ingest the window."""
    raw = await begin_window(
        conn,
        workspace_id=target.workspace_id,
        owner_id=target.owner_id,
        source_id=target.source_id,
        stream_id=target.stream_id,
        window=window,
        recipe_digest=recipe_digest(recipe),
        server=SERVER,
        run_id=run_id,
    )
    listing = recipe.listing
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    page = 0
    while True:
        arguments = render_arguments(
            listing.arguments, stream=stream, window=(window.start, window.end)
        )
        if cursor is not None:
            arguments["cursor"] = cursor
        result = await client.call_tool(listing.tool, arguments)
        await store_response(
            conn,
            raw.id,
            call="list",
            tool=listing.tool,
            arguments=arguments,
            result=result,
            page=page,
        )
        document = json.loads(result["content"][0]["text"])
        items.extend(document["messages"])
        cursor = document.get("next_cursor")
        if not cursor or not document.get("has_more"):
            break
        page += 1
    detail = recipe.detail
    assert detail is not None
    for item in items:
        if not item.get("reply_count"):
            continue
        arguments = render_arguments(detail.arguments, stream=stream, item={"id": item["ts"]})
        result = await client.call_tool(detail.tool, arguments)
        await store_response(
            conn,
            raw.id,
            call="detail",
            tool=detail.tool,
            arguments=arguments,
            result=result,
            item_id=item["ts"],
        )
    return await ingest_window(conn, access, raw.id, recipe=recipe, parser=parser)


# --- The parser ----------------------------------------------------------------------------------


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def hearth_parser(*, names: bool = False, count_root: bool = False) -> WindowParser:
    """A trivial parser for the hearth recipe. A thread record per root message listed or fetched
    in the window, keyed ``{channel}/{ts}``, with the root and its replies.

    ``names``: authors as display names (``user_name``); else as user ids (``user``): the
    "parser fix" of the tests. ``count_root``: a bug that counts the root among the replies, so a
    thread with replies never matches its ``reply_count`` and the window is never complete.
    """

    def parse(window: WindowInput) -> ParsedWindow:
        url = str(window.recipe.parser.options["message_url"])
        roots: dict[str, tuple[dict[str, Any], int]] = {}
        details: dict[str, list[tuple[int, list[dict[str, Any]]]]] = {}
        incomplete: list[str] = []
        listed_last_page = False
        for response in window.responses:
            if response.is_error:
                continue
            document = response.decode("json")
            if response.call == "list":
                for message in document["messages"]:
                    roots[message["ts"]] = (message, response.position)
                listed_last_page = listed_last_page or not document.get("has_more")
            else:
                assert response.item_id is not None
                details.setdefault(response.item_id, []).append(
                    (response.position, document["messages"])
                )
        if not listed_last_page:
            incomplete.append("the listing did not reach its last page")

        records: list[ParsedRecord] = []
        for root_ts in sorted(set(roots) | set(details)):
            positions: list[int] = []
            root: dict[str, Any] | None = None
            if root_ts in roots:
                root, position = roots[root_ts]
                positions.append(position)
            replies: list[dict[str, Any]] = []
            for position, messages in details.get(root_ts, []):
                positions.append(position)
                for message in messages:
                    if message["ts"] == root_ts:
                        root = root or message
                    if message["ts"] != root_ts or count_root:
                        replies.append(message)
            assert root is not None
            expected = root.get("reply_count")
            if expected is not None and expected > 0:
                if root_ts not in details:
                    incomplete.append(f"no detail call for the item of call {positions[0]}")
                elif len(replies) != expected:
                    incomplete.append(f"the replies of the item of call {positions[0]} differ")
            messages = [root, *(m for m in replies if m["ts"] != root_ts)]
            times = [parse_timestamp(m["ts"], "unix") for m in messages]
            records.append(
                ParsedRecord(
                    record=RecordIn(
                        kind="thread",
                        external_id=f"{window.stream}/{root_ts}",
                        occurred_at=max(times),
                        payload={
                            "channel": window.stream,
                            "messages": [
                                {
                                    "id": m["ts"],
                                    "author": m["user_name"] if names else m["user"],
                                    "sent_at": _iso(when),
                                    "text": m["text"],
                                }
                                for m, when in zip(messages, times, strict=True)
                            ],
                        },
                        external_url=url.format(stream=window.stream, id=root_ts),
                    ),
                    responses=tuple(sorted(set(positions))),
                )
            )
        return ParsedWindow(
            records=tuple(records), complete=not incomplete, incomplete=tuple(incomplete)
        )

    return parse
