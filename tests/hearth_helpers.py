"""Hearth, the invented chat server of docs/recipes.md, for the tests of the raw response store and
replay: a fake server with a channel of threads, a client of it that counts its calls, a minimal
executor of the hearth recipe, and a trivial parser for it, registered through the item parser
interface. Every name, id and text is made up.

The executors here are test doubles, not the service executor (memini-bz1). :func:`sync_window`
runs one window of the recipe's list call with cursor paging and a detail call per item with
replies, stores each result with :func:`memini.responses.store_response`, and ends with
:func:`memini.responses.ingest_window`, as both executors do. :func:`run_window` and, without a
database, :func:`drive` make whatever :func:`memini.recipes.missing_calls` names until nothing is
missing or their budget is spent: the loop both executors run.
"""

import json
from collections.abc import Awaitable, Callable, Mapping
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
    IncompleteItem,
    Item,
    MissingCall,
    RawResponse,
    Recipe,
    RecipeParser,
    WindowInput,
    WindowParser,
    item_parser,
    load_recipe,
    missing_calls,
    parse_timestamp,
    recipe_digest,
    render_arguments,
)
from memini.responses import (
    ServerInfo,
    WindowIngest,
    begin_window,
    ingest_window,
    load_window,
    store_response,
)
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


def rewrite(result: dict[str, Any], change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    """A Hearth result with its JSON document changed by ``change``."""
    document = json.loads(result["content"][0]["text"])
    change(document)
    return _result(document)


@dataclass
class TruncatedHearth(FakeHearth):
    """A server whose first page says more pages follow but names no cursor."""

    def channel_history(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        def cut(document: dict[str, Any]) -> None:
            document["next_cursor"] = None

        return rewrite(super().channel_history(arguments), cut)


@dataclass
class LossyHearth(FakeHearth):
    """A server that loses a reply: Cy's thread has a third reply, which the listing counts and
    ``thread_replies`` leaves out."""

    def __post_init__(self) -> None:
        self.threads[2].replies.append(
            Message(ts(T0 + DAY + 12 * HOUR), "U0002", "Bo Sample", "Spare set in the shed.")
        )

    def thread_replies(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        def drop(document: dict[str, Any]) -> None:
            if len(document["messages"]) == 4:
                document["messages"].pop()

        return rewrite(super().thread_replies(arguments), drop)


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


# --- Executors driven by the missing calls -------------------------------------------------------


def _identity(call: MissingCall) -> str:
    return json.dumps([call.call, call.tool, call.arguments], sort_keys=True)


def _next_calls(
    window: WindowInput, repeats: dict[str, int], max_repeats: int
) -> list[MissingCall]:
    """The missing calls of ``window`` an executor makes now: not past ``max_pages``, and a call
    already made (a retry) at most ``max_repeats`` times."""
    todo: list[MissingCall] = []
    for call in missing_calls(window):
        if call.check == "max_pages":
            continue
        if call.repeat:
            key = _identity(call)
            if repeats.get(key, 0) >= max_repeats:
                continue
            repeats[key] = repeats.get(key, 0) + 1
        todo.append(call)
    return todo


def drive(
    recipe: Recipe,
    window: PlannedWindow,
    call_tool: Callable[[str, Mapping[str, Any]], dict[str, Any]],
    *,
    stream: str = CHANNEL,
    max_calls: int = 1000,
    max_repeats: int = 0,
) -> WindowInput:
    """Run one window of ``recipe`` in memory: make the missing calls, round by round, until none
    is missing or ``max_calls`` are made. A call made again replaces its result, as the store
    does. Returns the window as a parser reads it."""
    responses: list[RawResponse] = []
    repeats: dict[str, int] = {}
    made = 0
    while True:
        current = WindowInput(recipe, stream, window.start, window.end, tuple(responses))
        todo = _next_calls(current, repeats, max_repeats)
        if not todo or made >= max_calls:
            return current
        for call in todo[: max_calls - made]:
            made += 1
            result = call_tool(call.tool, call.arguments)
            identity = _identity(call)
            index = next(
                (
                    i
                    for i, r in enumerate(responses)
                    if json.dumps([r.call, r.tool, r.arguments], sort_keys=True) == identity
                ),
                None,
            )
            position = len(responses) + 1 if index is None else responses[index].position
            response = RawResponse(
                position=position,
                call=call.call,
                tool=call.tool,
                arguments=dict(call.arguments),
                page=call.page,
                item_id=call.item_id,
                result=result,
            )
            if index is None:
                responses.append(response)
            else:
                responses[index] = response


async def run_window(
    conn: Conn,
    call_tool: Callable[[str, Mapping[str, Any]], Awaitable[dict[str, Any]]],
    target: Target,
    window: PlannedWindow,
    *,
    recipe: Recipe,
    parser: WindowParser,
    access: WriteAccess,
    stream: str = CHANNEL,
    max_calls: int = 1000,
    max_repeats: int = 0,
) -> WindowIngest:
    """Run one window of ``recipe`` as both executors do: store each call, read the stored
    window for its missing calls, make them, until none is missing or ``max_calls`` are made;
    then parse and ingest the window."""
    raw = await begin_window(
        conn,
        workspace_id=target.workspace_id,
        owner_id=target.owner_id,
        source_id=target.source_id,
        stream_id=target.stream_id,
        window=window,
        recipe_digest=recipe_digest(recipe),
        server=SERVER,
    )
    repeats: dict[str, int] = {}
    made = 0
    while made < max_calls:
        _, responses = await load_window(conn, raw.id)
        current = WindowInput(recipe, stream, window.start, window.end, responses)
        todo = _next_calls(current, repeats, max_repeats)
        if not todo:
            break
        for call in todo[: max_calls - made]:
            made += 1
            await store_response(
                conn,
                raw.id,
                call=call.call,
                tool=call.tool,
                arguments=call.arguments,
                result=await call_tool(call.tool, call.arguments),
                page=call.page,
                item_id=call.item_id,
            )
    return await ingest_window(conn, access, raw.id, recipe=recipe, parser=parser)


# --- The parser ----------------------------------------------------------------------------------


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def hearth_parser(*, names: bool = False, count_root: bool = False) -> RecipeParser:
    """A trivial parser for the hearth recipe, written as an item parser
    (:func:`memini.recipes.item_parser`): a thread record per item of the window, keyed
    ``{channel}/{ts}``, with the root and its replies. The recipe's selectors and checks do the
    rest: the listing's pages, the detail calls, the reply counts.

    ``names``: authors as display names (``user_name``); else as user ids (``user``): the
    "parser fix" of the tests. ``count_root``: a bug that counts the root among the replies, so
    the parser finds a thread with replies short of its ``reply_count`` and the window is never
    complete.
    """

    def parse(item: Item, window: WindowInput) -> RecordIn | IncompleteItem:
        url = str(window.recipe.parser.options["message_url"])
        root_ts = item.key
        fetched: list[dict[str, Any]] = list(item.detail_items)
        root = item.listed.value if item.listed else next(m for m in fetched if m["ts"] == root_ts)
        replies = [m for m in fetched if m["ts"] != root_ts]
        messages = [root, *replies]
        times = [parse_timestamp(m["ts"], "unix") for m in messages]
        record = RecordIn(
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
        )
        expected = item.listed.reply_count if item.listed else None
        if count_root and expected and item.details and len(fetched) != expected:
            return IncompleteItem("its replies differ from its reply count", record)
        return record

    return item_parser(parse)
