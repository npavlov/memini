"""The M3 check (docs/plan.md, M3 "Done when"), against real Postgres databases and fake MCP servers
served in the test process. This is the M3 check: acceptance of memini-2qe.

    One recipe run by both executors yields identical records; an incomplete window leaves the
    checkpoint in place and lists the missing calls; a parser fix replays from stored raw
    responses.

**Two deployments, one server.** Database A is where the service executor runs; database B, a copy
of A made before either ran (``CREATE DATABASE … TEMPLATE``), is where the agent executor runs. So
the workspace, the mcp source (made by the admin library, :func:`memini.sources.create_mcp_source`),
its stream and the hook's token have the same ids in both, and so do the records and chunks the
executors make: a record id derives from the source id and the record's kind and external id, a
chunk id from the record id. The two databases are then compared row by row. One fake MCP server
answers both executors: the service executor's ``mcp_sync`` job, run by a real
:class:`~memini.jobs.Worker` with the official SDK's client over streamable HTTP (an ASGI app that
httpx2 reaches without a socket); and a scripted agent that makes each call it is handed with the
same SDK's client against the same app and posts the result.

1. **Equivalence**, for each synthetic recipe of tests/fixtures/recipes: the agent run starts
   first and pins its ``as_of``; the service job is enqueued with that ``as_of`` in its args, so
   both plan the same windows and make the same calls. Compared: the calls the server answered;
   the run reports' plans and incomplete windows; records (every column but the times of the
   write: id, kind, external id, content hash, payload bytes, normalized text, times, url);
   chunks (every column but the times of the write and the vector: id, key, text hash,
   embedding key, place); persons and person links (by name and aliases: person ids are made
   in each database); the raw windows and the stored responses' call identities, attribution
   and results; and the stream's checkpoint.

   - ``hearth`` (``decode = "json"``, cursor paging, reply counts) and ``quillpost``
     (``decode = "text"``, offset paging, totals): the agent is the memini-intake skill's client
     and its real capture hook (integrations/claude-code), run as processes against the service
     under uvicorn, posting the content blocks as Claude Code hands them to a ``PostToolUse``
     hook.
   - ``millnotes`` (``decode = "structured"``, numbered pages, a listing without dates): Claude
     Code hands a hook an MCP tool's content blocks only, so a recipe that decodes
     ``structuredContent`` cannot go through the hook (requirements, Agent executor). The
     scripted agent drives the run API over HTTP itself and posts the whole MCP result object,
     as the API allows.

   The bodies of the stored responses are the same bytes for millnotes. For the hook recipes the
   agent's body is the content blocks in the envelope the service adds, and the service's is the
   result object as the server sent it, which holds the protocol's ``resultType`` and a ``_meta``
   naming the server besides: the bodies differ by exactly these two keys (:data:`ENVELOPE`).

2. **An incomplete window**, for both executors against one faulty Hearth: a page that says more
   follow and names no cursor, and a thread whose detail call loses a reply. Each executor tries
   the failing call as often as the other (the service's ``mcp_call_attempts`` set to the agent's
   :data:`~memini.executors.agent.MAX_ATTEMPTS`), stores what it got, and ends ``partial``; the
   checkpoint stays at the end of the window before; the run reports list exactly the call that
   would complete the window, as :func:`memini.recipes.missing_calls` over the stored window
   does; and the two databases are still identical.

3. **A parser fix**: both executors run Hearth with a parser that writes user ids as authors;
   then a fixed parser is registered under the same name, and database A is replayed with
   :func:`memini.responses.replay` and database B with ``memini replay``. The server is not called
   again; every record now has display names and new person links, and the databases are still
   identical.

No test reaches a real server or the network, an ollama or a model: embeddings come from the
FakeEmbedder, waits of the service executor's budget from a fake clock. Every name, id, address,
token and text is synthetic.
"""

import json
import math
import os
import re
import sys
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal, LiteralString, Protocol
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import anyio
import anyio.lowlevel
import httpx2
import mcp_types as types
import psycopg
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server import Server, ServerRequestContext
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from psycopg import AsyncConnection, sql
from psycopg.rows import TupleRow, dict_row
from pydantic import SecretStr
from starlette.types import ASGIApp

from agent_helpers import recent_threads, running_service
from hearth_helpers import (
    CHANNEL,
    FAKE_TOKEN,
    FakeHearth,
    LossyHearth,
    TruncatedHearth,
    hearth_parser,
)
from memini.api import create_app
from memini.auth import WriteAccess, create_token
from memini.cli import main
from memini.cli import replay as replay_cli
from memini.config import Settings, environment_variables
from memini.credentials import CredentialKey, Keyring
from memini.db import Pool, connect, open_pool
from memini.embeddings import Embedder, FakeEmbedder
from memini.executors.agent import MAX_ATTEMPTS
from memini.executors.budget import RetryPolicy
from memini.executors.egress import EgressPolicy, IPAddress
from memini.executors.service import MCP_SYNC, ServiceOptions, mcp_sync_handler
from memini.executors.source import HttpEndpoint
from memini.ingest import PayloadIndexer, RecordIn
from memini.intake import AS_OF_ARG
from memini.jobs import RetryPolicy as JobRetry
from memini.jobs import Worker, enqueue, stream_dedupe_key
from memini.recipes import (
    IncompleteItem,
    Item,
    Recipe,
    RecipeParser,
    WindowInput,
    WindowParser,
    get_parser,
    item_parser,
    load_recipe,
    missing_calls,
    register_parser,
    unregister_parser,
)
from memini.responses import load_window, replay
from memini.runs import wait_for_run_writes
from memini.sources import NewMcpSource, create_mcp_source
from memini.workspaces import create_workspace, ensure_principal

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

type Conn = AsyncConnection[TupleRow]
type Row = dict[str, Any]
type Json = dict[str, Any]

REPOSITORY: Final = Path(__file__).resolve().parent.parent
RECIPES: Final = Path(__file__).parent / "fixtures" / "recipes"
LAUNCHER: Final = (
    REPOSITORY / "integrations" / "claude-code" / "skills" / "memini-intake" / "scripts"
) / "memini-agent"
WORKSPACE: Final = "acme"
KEY: Final = b"k" * 32
MAX_STEPS: Final = 500
"""``next`` answers an agent asks for at most, in one run."""


# --- A fake MCP server ---------------------------------------------------------------------------

type ToolHandler = Callable[[Mapping[str, Any]], Json]


@dataclass
class FakeServer:
    """An MCP server of the official SDK with the given tools, each an input schema and a function
    from arguments to an MCP result object. It keeps every call it answered, in order."""

    name: str
    version: str
    tools: Mapping[str, tuple[Json, ToolHandler]]
    received: list[str] = field(default_factory=list[str])
    """Each call answered, as canonical JSON ``[tool, arguments]``."""

    def calls_since(self, mark: int) -> list[str]:
        """The calls answered after the first ``mark``, sorted: the service executor makes calls
        at once, the agent one at a time."""
        return sorted(self.received[mark:])

    @asynccontextmanager
    async def serving(self) -> AsyncGenerator[ASGIApp]:
        """The server as an ASGI app over streamable HTTP, for the duration of the block."""

        async def list_tools(
            ctx: ServerRequestContext[Any, Any], params: types.PaginatedRequestParams | None
        ) -> types.ListToolsResult:
            return types.ListToolsResult(
                tools=[
                    types.Tool(name=name, input_schema=schema)
                    for name, (schema, _) in self.tools.items()
                ]
            )

        async def call_tool(
            ctx: ServerRequestContext[Any, Any], params: types.CallToolRequestParams
        ) -> types.CallToolResult:
            arguments = dict(params.arguments or {})
            _, handler = self.tools[params.name]
            self.received.append(canonical([params.name, arguments]))
            return types.CallToolResult.model_validate(handler(arguments))

        server = Server(
            self.name, version=self.version, on_list_tools=list_tools, on_call_tool=call_tool
        )
        manager = StreamableHTTPSessionManager(
            app=server, stateless=True, json_response=True, security_settings=None
        )
        async with manager.run():
            yield manager.handle_request


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def text_result(text: str) -> Json:
    return {"content": [{"type": "text", "text": text}], "isError": False}


def schema(*properties: str, required: Sequence[str] = ()) -> Json:
    """An input schema that declares ``properties`` (the drift check reads their names)."""
    return {
        "type": "object",
        "properties": {name: {} for name in properties},
        "required": list(required),
    }


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def when(text: str) -> datetime:
    return datetime.fromisoformat(text)


ANN: Final = "Ann Example <ann@example.test>"
BO: Final = "Bo Sample <bo@example.test>"
CY: Final = "Cy Placeholder <cy@example.test>"
PEOPLE: Final = (
    ("Ann Example", "ann@example.test"),
    ("Bo Sample", "bo@example.test"),
    ("Cy Placeholder", "cy@example.test"),
)


# --- Hearth: a chat server, json -----------------------------------------------------------------


def hearth_server(hearth: FakeHearth) -> FakeServer:
    """Hearth (docs/recipes.md; tests/hearth_helpers.py) with its two tools."""
    return FakeServer(
        "hearth-mcp",
        "2.3.1",
        {
            "channel_history": (
                schema("channel", "oldest", "latest", "limit", "cursor", required=["channel"]),
                hearth.channel_history,
            ),
            "thread_replies": (
                schema("channel", "thread", "cursor", required=["channel", "thread"]),
                hearth.thread_replies,
            ),
        },
    )


def hearth_of(now: datetime) -> FakeServer:
    """Five threads in the two windows a first run plans, three with replies, one reply with a
    fake token (tests/agent_helpers.py)."""
    return hearth_server(FakeHearth(threads=recent_threads(now)))


# --- Quillpost: a mail server, text --------------------------------------------------------------


@dataclass(frozen=True)
class Mail:
    sender: str
    sent: datetime
    text: str


@dataclass(frozen=True)
class MailThread:
    id: str
    mailbox: str
    subject: str
    messages: tuple[Mail, ...]

    @property
    def last(self) -> datetime:
        return max(m.sent for m in self.messages)


@dataclass
class FakeQuillpost:
    """Mail threads in text meant for a model: two threads a call whatever the page size, newest
    activity first, the window's days read from the search query."""

    threads: list[MailThread]
    page_size: int = 2

    def search_threads(self, arguments: Mapping[str, Any]) -> Json:
        query = str(arguments["query"])
        after = re.search(r"after:(\d{4}/\d{2}/\d{2})", query)
        before = re.search(r"before:(\d{4}/\d{2}/\d{2})", query)
        assert after is not None
        assert before is not None
        start = datetime.strptime(after.group(1), "%Y/%m/%d").replace(tzinfo=UTC).date()
        end = datetime.strptime(before.group(1), "%Y/%m/%d").replace(tzinfo=UTC).date()
        found = sorted(
            (t for t in self.threads if start <= t.last.date() < end),
            key=lambda t: t.last,
            reverse=True,
        )
        offset = int(arguments.get("offset", 0))
        lines = ["Threads that match the query, newest activity first:", ""]
        lines += [
            f"Thread {t.id} in {t.mailbox}: {len(t.messages)} messages, last activity {iso(t.last)}"
            for t in found[offset : offset + self.page_size]
        ]
        lines += ["", f"Total: {len(found)} threads"]
        return text_result("\n".join(lines))

    def read_thread(self, arguments: Mapping[str, Any]) -> Json:
        thread = next(t for t in self.threads if t.id == arguments["thread_id"])
        assert arguments["mailbox"] == thread.mailbox
        parts = [
            f"--- message {n} ---\nFrom: {m.sender}\nDate: {iso(m.sent)}\n"
            f"Subject: {thread.subject}\n\n{m.text}"
            for n, m in enumerate(thread.messages, start=1)
        ]
        return text_result("\n".join(parts))


def mail_threads(now: datetime) -> list[MailThread]:
    """Five threads of the last five days, inside the seven days a first run plans."""

    def at(days: float, hours: int = 0) -> datetime:
        return (now - timedelta(days=days) + timedelta(hours=hours)).replace(microsecond=0)

    return [
        MailThread(
            "T-2001",
            "inbox",
            "Kiln schedule",
            (
                Mail(ANN, at(5), "Shall we fire the kiln on Friday?"),
                Mail(BO, at(5, 2), "Friday works for me."),
                Mail(CY, at(4), "I will bring the cones."),
            ),
        ),
        MailThread("T-2002", "inbox", "Glaze order", (Mail(BO, at(4, 3), "The order arrived."),)),
        MailThread(
            "T-2003",
            "archive",
            "Shelf keys",
            (
                Mail(CY, at(3), "Who has the shelf keys?"),
                Mail(ANN, at(3, 1), f"In the drawer. The shop login token is {FAKE_TOKEN}"),
            ),
        ),
        MailThread(
            "T-2004",
            "inbox",
            "Open studio",
            (
                Mail(ANN, at(2), "Open studio on Saturday?"),
                Mail(BO, at(2, 1), "I can host the morning."),
                Mail(CY, at(2, 2), "I take the afternoon."),
                Mail(ANN, at(1, 6), "Then it is settled."),
            ),
        ),
        MailThread("T-2005", "inbox", "Clay delivery", (Mail(CY, at(1), "Clay comes Monday."),)),
    ]


def quillpost_of(now: datetime) -> FakeServer:
    server = FakeQuillpost(mail_threads(now))
    return FakeServer(
        "quillpost-mail",
        "1.4.0",
        {
            "search_threads": (
                schema("query", "page_size", "offset", required=["query"]),
                server.search_threads,
            ),
            "read_thread": (
                schema("thread_id", "mailbox", required=["thread_id"]),
                server.read_thread,
            ),
        },
    )


_MESSAGE: Final = re.compile(
    r"^--- message \d+ ---\nFrom: (?P<sender>.+)\nDate: (?P<date>\S+)\nSubject: (?P<subject>.*)\n\n"
    r"(?P<text>.*?)(?=\n--- message \d+ ---$|\Z)",
    re.MULTILINE | re.DOTALL,
)


def quillpost_parser() -> RecipeParser:
    """A thread record per listed thread, its messages read from the text of its detail call."""

    def parse(item: Item, window: WindowInput) -> RecordIn | IncompleteItem:
        text = "\n".join(str(page.document) for page in item.details)
        found = list(_MESSAGE.finditer(text))
        return RecordIn(
            kind="thread",
            external_id=item.key,
            occurred_at=max(when(m["date"]) for m in found),
            payload={
                "title": found[0]["subject"],
                "channel": item.listed.fields["mailbox"] if item.listed else None,
                "messages": [
                    {"author": m["sender"], "occurred_at": m["date"], "text": m["text"]}
                    for m in found
                ],
            },
            external_url=f"https://quillpost.example/t/{item.key}",
        )

    return item_parser(parse)


# --- Millnotes: a meeting note-taker, structured -------------------------------------------------

LISBON: Final = ZoneInfo("Europe/Lisbon")


@dataclass(frozen=True)
class Meeting:
    id: str
    started: datetime
    title: str
    participants: tuple[str, ...]


@dataclass
class FakeMillnotes:
    """Meetings newest first, listed in numbered pages without dates; each meeting's notes as
    ``structuredContent``."""

    meetings: list[Meeting]

    def list_meetings(self, arguments: Mapping[str, Any]) -> Json:
        assert arguments["include_shared"] is True
        number, size = int(arguments["page"]), int(arguments["per_page"])
        chunk = self.meetings[(number - 1) * size : number * size]
        return {
            "content": [{"type": "text", "text": f"{len(chunk)} meetings"}],
            "structuredContent": {
                "meetings": [
                    {
                        "meeting_id": m.id,
                        "started at": m.started.astimezone(LISBON).strftime("%Y-%m-%d %H:%M"),
                        "title": m.title,
                    }
                    for m in chunk
                ],
                "page": number,
                "pages": math.ceil(len(self.meetings) / size),
            },
            "isError": False,
        }

    def get_meeting(self, arguments: Mapping[str, Any]) -> Json:
        assert arguments["sections"] == ["summary", "action_items", "transcript"]
        meeting = next(m for m in self.meetings if m.id == arguments["id"])
        first, *others = meeting.participants
        notes = {
            "id": meeting.id,
            "title": meeting.title,
            "started_at": iso(meeting.started),
            "ended_at": iso(meeting.started + timedelta(minutes=30)),
            "participants": list(meeting.participants),
            "summary": f"## Decisions\n\nThe kiln is fired after {meeting.title}.",
            "action_items": [
                {"text": "Order more cones.", "owner": others[0] if others else first}
            ],
            "transcript": [
                {"speaker": name, "start": 60 * n, "text": f"Point {n} of {meeting.title}."}
                for n, name in enumerate(meeting.participants)
            ],
            "people": [
                {"name": name, "email": email}
                for name, email in PEOPLE
                if name in meeting.participants
            ],
        }
        return {
            "content": [{"type": "text", "text": f"Meeting {meeting.id}: {meeting.title}"}],
            "structuredContent": notes,
            "isError": False,
        }


def meetings(now: datetime) -> list[Meeting]:
    """A meeting a day for 34 days, the newest three hours ago: 30 in the 30 days a first run
    plans, so the listing takes two pages of 25 and the second reaches past the window."""
    newest = now.replace(second=0, microsecond=0) - timedelta(hours=3)
    names = [name for name, _ in PEOPLE]
    return [
        Meeting(
            f"M-{n:04d}",
            newest - timedelta(days=n),
            f"Studio standup {n}",
            tuple(names[: 2 + n % 2]),
        )
        for n in range(34)
    ]


def millnotes_of(now: datetime) -> FakeServer:
    server = FakeMillnotes(meetings(now))
    return FakeServer(
        "millnotes",
        "5.0.2",
        {
            "list_meetings": (
                schema("include_shared", "page", "per_page"),
                server.list_meetings,
            ),
            "get_meeting": (schema("id", "sections", required=["id"]), server.get_meeting),
        },
    )


def millnotes_parser() -> RecipeParser:
    """A meeting record per meeting inside the window, from its notes."""

    def parse(item: Item, window: WindowInput) -> RecordIn | IncompleteItem:
        notes: Json = item.details[0].document
        payload = {
            key: notes[key]
            for key in (
                "title",
                "started_at",
                "ended_at",
                "participants",
                "summary",
                "action_items",
                "transcript",
                "people",
            )
        }
        return RecordIn(
            kind="meeting",
            external_id=item.key,
            occurred_at=when(notes["started_at"]),
            payload=payload,
            external_url=f"https://millnotes.example/m/{item.key}",
        )

    return item_parser(parse)


# --- The recipes ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Case:
    """A recipe of tests/fixtures/recipes, its fake server and its parser, and how the scripted
    agent posts what it gets."""

    recipe_file: str
    source: str
    """The source's name, and the MCP server's name in the agent's session (``mcp__NAME__…``)."""
    stream: str
    host: str
    capture: Literal["hook", "api"]
    records: int
    """Records a first run makes."""
    server: Callable[[datetime], FakeServer]
    parser: Callable[[], WindowParser]

    @property
    def recipe(self) -> Recipe:
        return load_recipe(RECIPES / self.recipe_file)

    @property
    def url(self) -> str:
        return f"https://{self.host}/mcp"

    def policy(self) -> EgressPolicy:
        return EgressPolicy.build(endpoints=[self.host])


HEARTH: Final = Case(
    "hearth.toml",
    "hearth",
    CHANNEL,
    "hearth.example",
    "hook",
    5,
    hearth_of,
    lambda: hearth_parser(names=True),
)
QUILLPOST: Final = Case(
    "quillpost.toml",
    "quillpost",
    "in:inbox -from:noreply",
    "quillpost.example",
    "hook",
    5,
    quillpost_of,
    quillpost_parser,
)
MILLNOTES: Final = Case(
    "millnotes.toml",
    "millnotes",
    "meetings",
    "millnotes.example",
    "api",
    30,
    millnotes_of,
    millnotes_parser,
)


# --- Two databases -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Deployment:
    """One database of the check."""

    url: str
    pool: Pool
    db: Conn


@dataclass
class World:
    """Database A, where the service executor runs, and its copy B, where the agent executor
    runs: the same workspace, source, stream and token in both."""

    case: Case
    service: Deployment
    agent: Deployment
    owner: uuid.UUID
    workspace: uuid.UUID
    source: uuid.UUID
    stream: uuid.UUID
    token: str
    now: datetime
    tmp: Path

    @property
    def access(self) -> WriteAccess:
        return WriteAccess.for_owner(self.owner)


def _database(url: str, name: str) -> str:
    return urlunsplit(urlsplit(url)._replace(path=f"/{name}"))


def _name(url: str) -> str:
    return urlsplit(url).path.lstrip("/")


@pytest.fixture
async def world(
    request: pytest.FixtureRequest,
    migrated_database_url: str,
    postgres_url: str,
    tmp_path: Path,
) -> AsyncIterator[World]:
    case: Case = request.param
    name = case.recipe.parser.name
    register_parser(name, case.parser(), replace=True)
    url_a = migrated_database_url
    url_b = _database(url_a, _name(url_a) + "_b")
    try:
        async with connect(url_a, autocommit=True) as db:
            owner = await ensure_principal(db, "owner")
            acme = await create_workspace(db, owner_id=owner, name=WORKSPACE)
            source = await create_mcp_source(
                db,
                WriteAccess.for_owner(owner),
                WORKSPACE,
                NewMcpSource(
                    name=case.source,
                    endpoint=HttpEndpoint(case.url),
                    streams=[case.stream],
                    recipe_toml=(RECIPES / case.recipe_file).read_text(encoding="utf-8"),
                ),
                policy=case.policy(),
            )
            token = await create_token(db, owner_id=owner, name="capture hook", scopes=["ingest"])
            stream = await scalar(db, "SELECT id FROM stream WHERE source_id = %s", source.id)
            now = await scalar(db, "SELECT now()")
        # B is A as it stands now: no session may be open on A while it is copied.
        with psycopg.connect(postgres_url, autocommit=True, connect_timeout=5) as admin:
            admin.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
                    sql.Identifier(_name(url_b)), sql.Identifier(_name(url_a))
                )
            )
        async with (
            open_pool(url_a, min_size=1, max_size=8, timeout=5) as pool_a,
            connect(url_a, autocommit=True) as db_a,
            open_pool(url_b, min_size=1, max_size=8, timeout=5) as pool_b,
            connect(url_b, autocommit=True) as db_b,
        ):
            yield World(
                case=case,
                service=Deployment(url_a, pool_a, db_a),
                agent=Deployment(url_b, pool_b, db_b),
                owner=owner,
                workspace=acme.id,
                source=source.id,
                stream=stream,
                token=token.secret.get_secret_value(),
                now=now,
                tmp=tmp_path,
            )
    finally:
        unregister_parser(name)
        with psycopg.connect(postgres_url, autocommit=True, connect_timeout=5) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(_name(url_b))
                )
            )


async def rows(db: Conn, query: LiteralString, *params: Any) -> list[Row]:
    async with db.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        return list(await cur.fetchall())


async def scalar(db: Conn, query: LiteralString, *params: Any) -> Any:
    cur = await db.execute(query, params)
    row = await cur.fetchone()
    assert row is not None
    return row[0]


# --- The agent executor --------------------------------------------------------------------------


@dataclass(frozen=True)
class HandedOut:
    """A call the agent was handed: the tool's name at the server."""

    tool: str
    arguments: Json
    id: str | None = None


type Step = HandedOut | Literal["window", "done"]


class Capture(Protocol):
    """How the scripted agent talks to Memini."""

    async def start(self, server: types.Implementation | None) -> None: ...
    async def next(self) -> Step: ...
    async def post(self, call: HandedOut, result: types.CallToolResult) -> None: ...
    async def finish(self) -> None: ...


@dataclass
class HookCapture:
    """The memini-intake skill's commands and its capture hook, run as Claude Code runs them."""

    world: World
    env: Mapping[str, str]
    said: list[str] = field(default_factory=list[str])
    """Every line ``next`` printed about a window that is done."""

    async def run(self, *args: str, stdin: str = "") -> str:
        done = await anyio.run_process(
            [str(LAUNCHER), *args], input=stdin.encode(), env=dict(self.env), check=False
        )
        assert done.returncode == 0, done.stderr.decode()
        return done.stdout.decode()

    async def start(self, server: types.Implementation | None) -> None:
        assert server is not None
        await self.run(
            "start",
            WORKSPACE,
            self.world.case.source,
            self.world.case.stream,
            "--server",
            self.world.case.source,
            "--server-name",
            server.name,
            "--server-version",
            server.version,
        )

    async def next(self) -> Step:
        printed = await self.run("next")
        if printed.startswith("The run is done"):
            return "done"
        if " is done (" in printed:
            self.said.append(printed.strip())
            return "window"
        lines = dict(
            line.split(": ", 1)
            for line in printed.splitlines()
            if line.startswith(("tool:", "arguments:"))
        )
        prefix = f"mcp__{self.world.case.source}__"
        assert lines["tool"].startswith(prefix)
        return HandedOut(lines["tool"].removeprefix(prefix), json.loads(lines["arguments"]))

    async def post(self, call: HandedOut, result: types.CallToolResult) -> None:
        assert not result.is_error
        blocks = [
            block.model_dump(mode="json", by_alias=True, exclude_none=True)
            for block in result.content
        ]
        event = {
            "session_id": "session-0001",
            "transcript_path": str(self.world.tmp / "transcript.jsonl"),
            "cwd": str(self.world.tmp),
            "permission_mode": "default",
            "hook_event_name": "PostToolUse",
            "tool_name": f"mcp__{self.world.case.source}__{call.tool}",
            "tool_input": call.arguments,
            "tool_response": blocks,  # what Claude Code hands a PostToolUse hook of an MCP tool
            "tool_use_id": "toolu_0001",
        }
        output = json.loads(await self.run("hook", stdin=json.dumps(event)))
        receipt = output["hookSpecificOutput"]["additionalContext"]
        assert receipt.startswith(
            ("memini: the response was stored", "memini: the same response was stored before")
        ), receipt

    async def finish(self) -> None:
        printed = await self.run("finish")
        assert printed.startswith("agent run finished"), printed


@asynccontextmanager
async def hook_capture(world: World) -> AsyncGenerator[HookCapture]:
    """The service on database B under uvicorn, and the hook's configuration."""
    token_file = world.tmp / "capture.token"
    token_file.write_text(f"{world.token}\n", encoding="utf-8")
    token_file.chmod(0o600)
    config = world.tmp / "capture.toml"
    settings = Settings(database_url=SecretStr(world.agent.url))
    with running_service(settings) as service:
        config.write_text(
            f'url = "{service}"\ntoken_file = "{token_file}"\nservers = ["{world.case.source}"]\n',
            encoding="utf-8",
        )
        yield HookCapture(
            world,
            {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": str(world.tmp),
                "MEMINI_CAPTURE_CONFIG": str(config),
                "MEMINI_CAPTURE_STATE": str(world.tmp / "state" / "agent-run.json"),
                "MEMINI_CAPTURE_PYTHON": sys.executable,
            },
        )


@dataclass
class ApiCapture:
    """The run API over HTTP, posting the whole MCP result object the agent's client got."""

    world: World
    http: httpx2.AsyncClient
    run_id: str | None = None

    @property
    def runs(self) -> str:
        return f"/v1/workspaces/{WORKSPACE}/sources/{self.world.case.source}/agent-runs"

    async def start(self, server: types.Implementation | None) -> None:
        assert server is not None
        answer = await self.http.post(
            self.runs,
            json={
                "stream": self.world.case.stream,
                "server": {"name": server.name, "version": server.version},
            },
        )
        assert answer.status_code == 201, answer.text
        self.run_id = answer.json()["id"]

    async def next(self) -> Step:
        answer = await self.http.get(f"{self.runs}/{self.run_id}/next")
        assert answer.status_code == 200, answer.text
        step = answer.json()
        if step["status"] == "run_done":
            return "done"
        if step["status"] == "window_done":
            return "window"
        call = step["call"]
        return HandedOut(call["tool"], call["arguments"], call["id"])

    async def post(self, call: HandedOut, result: types.CallToolResult) -> None:
        answer = await self.http.post(
            f"{self.runs}/{self.run_id}/responses",
            json={
                "call_id": call.id,
                "tool": call.tool,
                "arguments": call.arguments,
                "result": result.model_dump(mode="json", by_alias=True, exclude_none=True),
            },
        )
        assert answer.status_code == 200, answer.text

    async def finish(self) -> None:
        answer = await self.http.post(f"{self.runs}/{self.run_id}/finish")
        assert answer.status_code == 200, answer.text


@asynccontextmanager
async def api_capture(world: World) -> AsyncGenerator[ApiCapture]:
    settings = Settings(database_url=SecretStr(world.agent.url))
    app = create_app(settings, pool=world.agent.pool, embedder=FakeEmbedder())
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        base_url="http://memini.test",
        headers={"Authorization": f"Bearer {world.token}"},
    ) as http:
        yield ApiCapture(world, http)


@asynccontextmanager
async def mcp_client(app: ASGIApp, url: str) -> AsyncGenerator[Client]:
    """The agent's session with the server: the SDK's client over streamable HTTP."""
    async with (
        httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app)) as http,
        Client(streamable_http_client(url, http_client=http)) as client,
    ):
        yield client


@dataclass(frozen=True)
class Outcome:
    """What one executor did: its run report, and the calls the server answered for it."""

    run: Row
    calls: list[str]
    said: tuple[str, ...] = ()
    """What the skill's ``next`` said of each window done (the hook recipes)."""


async def run_agent(world: World, server: FakeServer, app: ASGIApp) -> tuple[Outcome, datetime]:
    """A scripted agent runs the stream through the agent executor against ``server``; its run
    report, its calls and the ``as_of`` its run pinned."""
    mark = len(server.received)
    capture_of = hook_capture if world.case.capture == "hook" else api_capture
    async with mcp_client(app, world.case.url) as client, capture_of(world) as capture:
        await capture.start(client.server_info)
        for _ in range(MAX_STEPS):
            step = await capture.next()
            if step == "done":
                break
            if step == "window":
                continue
            result = await client.call_tool(step.tool, dict(step.arguments))
            await capture.post(step, result)
        else:
            raise AssertionError("the agent run never ended")
        await capture.finish()
        said = tuple(capture.said) if isinstance(capture, HookCapture) else ()
    await wait_for_run_writes()
    as_of = await scalar(world.agent.db, "SELECT as_of FROM agent_run")
    (run,) = await rows(world.agent.db, "SELECT * FROM run WHERE kind = 'agent_sync'")
    return Outcome(run, server.calls_since(mark), said), as_of


# --- The service executor ------------------------------------------------------------------------


async def no_dns(host: str, port: int) -> list[IPAddress]:
    raise AssertionError(f"no name is resolved in this test: {host}:{port}")


@dataclass
class FakeClock:
    """The budget's clock: a wait passes at once and moves the clock on."""

    now: float = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 0.0)
        await anyio.lowlevel.checkpoint()


async def run_service(world: World, server: FakeServer, app: ASGIApp, as_of: datetime) -> Outcome:
    """One ``mcp_sync`` job of the stream with ``as_of`` pinned, run by a worker against
    ``server``; its run report and its calls."""
    mark = len(server.received)
    clock = FakeClock()
    handler = mcp_sync_handler(
        world.service.pool,
        keyring=Keyring(CredentialKey(KEY)),
        indexer=PayloadIndexer(FakeEmbedder()),
        options=ServiceOptions(
            policy=world.case.policy(),
            retry=RetryPolicy(attempts=MAX_ATTEMPTS, base=0.01, cap=0.05),
            http_transport=lambda: httpx2.ASGITransport(app=app),
            resolver=no_dns,
            clock=clock,
            sleep=clock.sleep,
        ),
    )
    db = world.service.db
    job = await enqueue(
        db,
        workspace_id=world.workspace,
        owner_id=world.owner,
        kind=MCP_SYNC,
        source_id=world.source,
        stream_id=world.stream,
        args={AS_OF_ARG: as_of.isoformat()},
        dedupe_key=stream_dedupe_key(world.stream),
        max_attempts=1,
    )
    worker = Worker(
        world.service.pool,
        {MCP_SYNC: handler},
        lease=timedelta(seconds=30),
        poll_interval=timedelta(milliseconds=20),
        retry=JobRetry(base=timedelta(seconds=30)),
    )
    with anyio.fail_after(30):
        async with anyio.create_task_group() as group:
            group.start_soon(worker.run)
            try:
                while await scalar(db, "SELECT status FROM job WHERE id = %s", job.id) in (
                    "queued",
                    "running",
                ):
                    await anyio.sleep(0.02)
            finally:
                worker.stop()
    await wait_for_run_writes()
    (run,) = await rows(db, "SELECT * FROM run WHERE kind = %s", MCP_SYNC)
    return Outcome(run, server.calls_since(mark))


# --- What is compared ----------------------------------------------------------------------------

# Every column but the times of the write; chunks without their vector (the FakeEmbedder's, made
# from the text the text hash covers) and their tsvector (made from the content). Persons by name
# and aliases: their ids are made in each database. Each stored response with its body, as JSON.
SNAPSHOT: Final[dict[str, LiteralString]] = {
    "records": "SELECT to_jsonb(r) - 'ingested_at' - 'updated_at' AS row FROM record r ORDER BY id",
    "chunks": "SELECT to_jsonb(c) - 'created_at' - 'updated_at' - 'embedding' - 'lexical' AS row"
    " FROM chunk c ORDER BY id",
    "persons": "SELECT p.display_name, array(SELECT a.kind || ' ' || a.value FROM alias a"
    " WHERE a.person_id = p.id ORDER BY 1) AS aliases, p.merged_into IS NULL AS canonical"
    " FROM person p ORDER BY 1, 2",
    "person_links": "SELECT rp.record_id, rp.record_kind, rp.role, rp.anchor, rp.occurred_at,"
    " p.display_name, array(SELECT a.kind || ' ' || a.value FROM alias a"
    " WHERE a.person_id = p.id ORDER BY 1) AS aliases"
    " FROM record_person rp JOIN person p ON p.id = rp.person_id ORDER BY 1, 3, 4, 6, 7",
    "checkpoint": "SELECT id, key, cursor, checked_until, checked_from FROM stream ORDER BY id",
    "windows": "SELECT stream_id, kind, window_from, window_to, recipe_digest, server_name,"
    " server_version, parsed_at IS NOT NULL AS parsed, complete, pruned_at FROM raw_window"
    " ORDER BY window_from",
    "responses": "SELECT w.window_from, r.call, r.tool, r.arguments, r.call_key, r.page,"
    " r.item_id, r.is_error, r.record_ids, r.body_hash, b.json AS body"
    " FROM raw_response r JOIN raw_window w ON w.id = r.window_id"
    " JOIN (SELECT source_id, hash, convert_from(body, 'UTF8')::jsonb AS json FROM raw_body) b"
    " ON b.source_id = r.source_id AND b.hash = r.body_hash"
    " ORDER BY w.window_from, r.call_key",
}

_RUN_OF_WINDOWS: Final = (
    "SELECT DISTINCT r.kind FROM raw_window w JOIN run r ON r.id = w.run_id ORDER BY 1"
)
"""The kinds of the runs that stored the raw windows of a database."""
_STORED: Final = (
    "SELECT concat_ws(' ', (SELECT string_agg(convert_from(body, 'UTF8'), ' ') FROM raw_body),"
    " (SELECT string_agg(convert_from(payload, 'UTF8') || normalized_text, ' ') FROM record),"
    " (SELECT string_agg(content, ' ') FROM chunk))"
)
"""Every stored body, payload, normalized text and chunk of a database, in one text."""


ENVELOPE: Final = frozenset({"resultType", "_meta"})
"""What the result object the server sent holds besides its content blocks and ``isError``: the
protocol's ``resultType``, and ``_meta`` naming the server. A hook gets the blocks only."""


async def snapshot(db: Conn) -> dict[str, list[Row]]:
    return {name: await rows(db, query) for name, query in SNAPSHOT.items()}


def assert_same(case: Case, found: dict[str, list[Row]], other: dict[str, list[Row]]) -> None:
    """``found`` (database A, the service executor) and ``other`` (database B, the agent executor)
    hold the same, and something of each kind.

    The run API stores the whole result object the agent's client got, as the service executor
    stores what the server sent: the bodies are the same bytes. A capture hook posts the content
    blocks Claude Code hands it, which the service puts in the envelope of a result; so for the
    hook recipes the bodies differ by :data:`ENVELOPE` exactly, and so do their hashes.
    """
    assert len(found["responses"]) == len(other["responses"])
    for ours, theirs in zip(found["responses"], other["responses"], strict=True):
        if case.capture == "hook":
            sent, hooked = ours.pop("body"), theirs.pop("body")
            assert set(sent) - set(hooked) == ENVELOPE
            assert {key: sent[key] for key in hooked} == hooked
            del ours["body_hash"], theirs["body_hash"]
    for name, values in found.items():
        assert values, f"database A holds no {name}"
        assert values == other[name], f"the {name} of the two executors differ"


async def assert_identical(world: World, service: Outcome, agent: Outcome) -> dict[str, list[Row]]:
    """The two runs made the same calls, planned the same windows, ran the same steps, left the
    same windows incomplete and counted the same where they count the same thing, and the two
    databases hold the same (:func:`assert_same`). Returns what database A holds.

    The executors' own counts have names of their own (``mcp_calls``, ``calls_handed_out``); the
    counts of ingest, persons, the index and the raw store are kept by both."""
    assert service.calls == agent.calls
    ours, theirs = service.run["report"], agent.run["report"]
    assert ours["details"]["plan"] == theirs["details"]["plan"]
    assert incomplete_windows(service.run) == incomplete_windows(agent.run)
    shared = sorted(ours["counts"].keys() & theirs["counts"].keys())
    assert {"records_inserted", "person_links_inserted", "chunks_inserted"} <= set(shared)
    assert [ours["counts"][name] for name in shared] == [theirs["counts"][name] for name in shared]
    # The service executor's first step opens the session; the agent's session is the agent's.
    assert [(s["name"], s["status"]) for s in ours["steps"]][1:] == [
        (s["name"], s["status"]) for s in theirs["steps"]
    ]
    assert ours["steps"][0]["name"] == "connect"
    found = await snapshot(world.service.db)
    assert_same(world.case, found, await snapshot(world.agent.db))
    return found


def incomplete_windows(run: Mapping[str, Any]) -> list[Json]:
    """The incomplete windows a run report lists, without the id of each raw window: made in each
    database."""
    return [
        {key: value for key, value in window.items() if key != "window_id"}
        for window in run["report"]["details"].get("incomplete_windows", [])
    ]


def missing_of(report: Mapping[str, Any]) -> list[Json]:
    """The missing calls the run report lists, all windows together."""
    return [
        call
        for window in report["details"].get("incomplete_windows", [])
        for call in window["missing_calls"]
    ]


# --- 1. One recipe, both executors, identical records --------------------------------------------


@pytest.mark.parametrize(
    "world",
    [
        pytest.param(HEARTH, id="hearth-json-hook"),
        pytest.param(QUILLPOST, id="quillpost-text-hook"),
        pytest.param(MILLNOTES, id="millnotes-structured-api"),
    ],
    indirect=True,
)
async def test_one_recipe_run_by_both_executors_yields_identical_records(world: World) -> None:
    server = world.case.server(world.now)
    async with server.serving() as app:
        agent, as_of = await run_agent(world, server, app)
        service = await run_service(world, server, app, as_of)

    assert agent.run["status"] == "succeeded", agent.run["error"]
    assert service.run["status"] == "succeeded", service.run["error"]
    found = await assert_identical(world, service, agent)

    assert len(found["records"]) == world.case.records
    assert {row["row"]["source_id"] for row in found["records"]} == {str(world.source)}
    assert all(window["complete"] for window in found["windows"])
    # The checkpoint moved to the time pinned for the agent run, in both.
    assert [row["checked_until"] for row in found["checkpoint"]] == [as_of]
    plan = agent.run["report"]["details"]["plan"]
    assert len(found["windows"]) == len(plan["windows"])
    # The server answered each executor's calls once each, nothing retried.
    assert len(agent.calls) == len(set(agent.calls)) == len(found["responses"])
    for deployment, kind in ((world.service, MCP_SYNC), (world.agent, "agent_sync")):
        # Each database holds the windows of its own executor only.
        assert await rows(deployment.db, _RUN_OF_WINDOWS) == [{"kind": kind}]
        # Redaction ran the same on both sides: the fake token is in no stored row.
        assert FAKE_TOKEN not in await scalar(deployment.db, _STORED)


# --- 2. An incomplete window ---------------------------------------------------------------------


@pytest.mark.parametrize("world", [pytest.param(HEARTH, id="hearth")], indirect=True)
@pytest.mark.parametrize(
    ("fault", "check"),
    [
        pytest.param(TruncatedHearth, "truncated", id="truncated-page"),
        pytest.param(LossyHearth, "reply_counts", id="lost-reply"),
    ],
)
async def test_an_incomplete_window_keeps_the_checkpoint_and_lists_the_missing_calls(
    world: World, fault: type[FakeHearth], check: str
) -> None:
    hearth = fault(threads=recent_threads(world.now))
    server = hearth_server(hearth)
    async with server.serving() as app:
        agent, as_of = await run_agent(world, server, app)
        service = await run_service(world, server, app, as_of)

    assert agent.run["status"] == service.run["status"] == "partial"
    found = await assert_identical(world, service, agent)

    # The older window is complete, the newer one is not: the checkpoint stays at the end of the
    # older one, not at as_of.
    plan = agent.run["report"]["details"]["plan"]["windows"]
    older, newer = ((when(w["from"]), when(w["to"])) for w in plan)
    assert [w["complete"] for w in found["windows"]] == [True, False]
    [checkpoint] = found["checkpoint"]
    assert (checkpoint["checked_until"], checkpoint["checked_from"]) == (older[1], older[0])
    assert checkpoint["checked_until"] < as_of

    # The exact call that would complete the newer window, in both run reports.
    if check == "truncated":
        expected = {
            "call": "list",
            "tool": "channel_history",
            "arguments": {
                "channel": CHANNEL,
                "oldest": math.floor(newer[0].timestamp()),
                "latest": math.ceil(newer[1].timestamp()),
                "limit": 100,
            },
            "page": 0,
            "item_id": None,
        }
    else:
        lost = hearth.threads[2].root.ts
        expected = {
            "call": "detail",
            "tool": "thread_replies",
            "arguments": {"channel": CHANNEL, "thread": lost},
            "page": 0,
            "item_id": lost,
        }
    for outcome in (service, agent):
        [listed] = missing_of(outcome.run["report"])
        assert {key: listed[key] for key in expected} == expected
        assert (listed["check"], listed["repeat"]) == (check, True)
        (window,) = outcome.run["report"]["details"]["incomplete_windows"]
        assert (when(window["from"]), when(window["to"])) == newer
        # Each executor made the failing call as often as the other, and gave up.
        made = canonical([expected["tool"], expected["arguments"]])
        assert outcome.calls.count(made) == MAX_ATTEMPTS

    # memini.recipes.missing_calls over the stored window names the same call in both databases.
    for deployment in (world.service, world.agent):
        window_id = await scalar(deployment.db, "SELECT id FROM raw_window WHERE complete IS FALSE")
        raw, responses = await load_window(deployment.db, window_id)
        stored = WindowInput(world.case.recipe, raw.stream, raw.start, raw.end, responses)
        assert [c.to_json() for c in missing_calls(stored)] == missing_of(service.run["report"])
    # The agent was told so when its window was done.
    assert any("incomplete, 1 calls left" in said for said in agent.said), agent.said


# --- 3. A parser fix replays from the stored raw responses ---------------------------------------


@pytest.mark.parametrize("world", [pytest.param(HEARTH, id="hearth")], indirect=True)
async def test_a_parser_fix_replays_from_the_stored_raw_responses(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    name = world.case.recipe.parser.name
    register_parser(name, hearth_parser(names=False), replace=True)  # authors as user ids: a bug
    server = hearth_of(world.now)
    async with server.serving() as app:
        agent, as_of = await run_agent(world, server, app)
        service = await run_service(world, server, app, as_of)
        before = await assert_identical(world, service, agent)
        assert authors(before) == {"U0001", "U0002", "U0003", "U0004"}
        calls = len(server.received)

        register_parser(name, hearth_parser(names=True), replace=True)  # the fix
        # Database A with the library.
        result = await replay(
            world.service.db,
            world.access,
            WORKSPACE,
            world.case.source,
            recipe=world.case.recipe,
            parser=get_parser(name),
            runs=world.service.pool,
            indexer=PayloadIndexer(FakeEmbedder()),
        )
        assert (result.updated, result.inserted) == (world.case.records, 0)
        # Database B with the operator command, in a thread of its own: it runs its own loop.
        for variable in (*environment_variables(), "MEMINI_PRINCIPAL"):
            monkeypatch.delenv(variable, raising=False)
        monkeypatch.setenv("MEMINI_DATABASE_URL", world.agent.url)

        def fake_embedder(settings: Settings) -> Embedder:
            return FakeEmbedder()

        monkeypatch.setattr(replay_cli, "create_embedder", fake_embedder)
        argv = [
            "replay",
            "--workspace",
            WORKSPACE,
            "--source",
            world.case.source,
            "--recipe",
            str(RECIPES / world.case.recipe_file),
        ]
        capsys.readouterr()
        code = await anyio.to_thread.run_sync(main, argv)
        printed = capsys.readouterr().out
        assert code == 0, printed
        assert f"{world.case.records} changed" in printed
        await wait_for_run_writes()

        assert len(server.received) == calls  # zero calls to the server

    after = await snapshot(world.service.db)
    assert_same(world.case, after, await snapshot(world.agent.db))
    assert authors(after) == {"Ann Example", "Bo Sample", "Cy Placeholder", "Dee Sample"}
    hashes = {row["row"]["id"]: row["row"]["content_hash"] for row in before["records"]}
    assert all(row["row"]["content_hash"] != hashes[row["row"]["id"]] for row in after["records"])
    assert {row["row"]["id"] for row in after["records"]} == set(hashes)
    assert {link["display_name"] for link in after["person_links"]} == authors(after)
    # The raw responses are as they were: nothing was fetched or stored again.
    assert after["responses"] == before["responses"]
    assert after["checkpoint"] == before["checkpoint"]
    for deployment in (world.service, world.agent):
        assert await scalar(deployment.db, "SELECT count(*) FROM run WHERE kind = 'replay'") == 1


def authors(found: Mapping[str, list[Row]]) -> set[str]:
    return {
        message["author"]
        for row in found["records"]
        for message in json.loads(bytes.fromhex(row["row"]["payload"][2:]))["items"]
    }
