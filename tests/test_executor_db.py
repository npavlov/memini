"""The service executor (memini-bz1) against a real Postgres and a fake MCP server.

Acceptance: a scheduled run against a fake MCP server ingests records; a timeout and a rate-limit
answer are retried within budget; a request to a non-allowlisted address is refused. The server is
the invented Hearth of docs/recipes.md (tests/fake_mcp.py), served by the official MCP SDK inside
the test process over streamable HTTP (httpx2's ASGI transport, no socket) or as a subprocess over
stdio; no test reaches a real server or the network. Every name, id, token and text is synthetic.
"""

import io
import json
import sys
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final, LiteralString

import anyio
import httpcore2
import httpx2
import pytest
from psycopg import AsyncConnection
from psycopg.rows import TupleRow, dict_row
from pydantic import SecretStr

from fake_mcp import FakeMcp, recent_threads
from fake_mcp_stdio import EXPECTED_TOKEN
from hearth_helpers import CHANNEL, HEARTH_RECIPE_PATH, FakeHearth, hearth_parser
from memini.auth import WriteAccess
from memini.credentials import (
    CredentialKey,
    Keyring,
    OAuthClient,
    OAuthTokens,
    put_api_key,
    put_oauth,
    read_oauth,
)
from memini.db import Pool, connect, open_pool
from memini.executors.agent import finish_run, next_call, start_run, submit_response
from memini.executors.budget import RetryPolicy
from memini.executors.egress import EgressPolicy, IPAddress
from memini.executors.service import MCP_SYNC, ServiceOptions, mcp_sync_handler
from memini.executors.source import HttpEndpoint, StdioEndpoint
from memini.jobs import Handler, Scheduler, Worker, enqueue, stream_dedupe_key
from memini.jobs import RetryPolicy as JobRetry
from memini.recipes import register_parser, unregister_parser
from memini.runs import wait_for_run_writes
from memini.sources import (
    InvalidSourceError,
    NewMcpSource,
    SourceUpdate,
    create_mcp_source,
    update_source,
)
from memini.workspaces import create_workspace, ensure_principal

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

type Conn = AsyncConnection[TupleRow]
type Row = dict[str, Any]

HOST: Final = "hearth.example"
URL: Final = f"https://{HOST}/mcp"
API_KEY: Final = "synthetic-api-key-7f3a9c41e2"
KEY: Final = b"k" * 32
STDIO_SCRIPT: Final = Path(__file__).parent / "fake_mcp_stdio.py"
FAST_RETRY: Final = RetryPolicy(attempts=3, base=0.01, cap=0.05)


def recipe_text(**rate: str) -> str:
    """The hearth recipe of docs/recipes.md, with ``[rate]`` values replaced."""
    text = HEARTH_RECIPE_PATH.read_text(encoding="utf-8")
    for key, value in rate.items():
        start = text.index(f"\n{key} = ") + 1
        end = text.index("\n", start)
        text = text[:start] + f"{key} = {value}" + text[end:]
    return text


async def no_dns(host: str, port: int) -> list[IPAddress]:
    raise AssertionError(f"no name is resolved in this test: {host}:{port}")


@dataclass
class Env:
    pool: Pool
    db: Conn
    owner: uuid.UUID
    workspace: uuid.UUID
    fake: FakeMcp
    app: Any
    now: datetime
    keyring: Keyring = field(default_factory=lambda: Keyring(CredentialKey(KEY)))

    @property
    def access(self) -> WriteAccess:
        return WriteAccess.for_owner(self.owner)

    def policy(self, **changes: Any) -> EgressPolicy:
        settings: dict[str, Any] = {"endpoints": [HOST], "commands": [sys.executable]}
        return EgressPolicy.build(**(settings | changes))

    def options(self, **changes: Any) -> ServiceOptions:
        app = self.app
        settings: dict[str, Any] = {
            "policy": self.policy(),
            "retry": FAST_RETRY,
            "http_transport": lambda: httpx2.ASGITransport(app=app),
            "resolver": no_dns,
        }
        return ServiceOptions(**(settings | changes))

    def handler(self, **changes: Any) -> Handler:
        return mcp_sync_handler(self.pool, keyring=self.keyring, options=self.options(**changes))


@pytest.fixture
async def env(migrated_database_url: str) -> AsyncIterator[Env]:
    register_parser("hearth.thread", hearth_parser(names=True), replace=True)
    try:
        async with (
            open_pool(migrated_database_url, min_size=1, max_size=8, timeout=5) as pool,
            connect(migrated_database_url, autocommit=True) as db,
        ):
            owner = await ensure_principal(db, "owner")
            workspace = await create_workspace(db, owner_id=owner, name="acme")
            now = await scalar(db, "SELECT now()")
            fake = FakeMcp(FakeHearth(threads=recent_threads(now)))
            async with fake.asgi() as app:
                yield Env(pool, db, owner, workspace.id, fake, app, now)
    finally:
        unregister_parser("hearth.thread")


async def rows(db: Conn, query: LiteralString, *params: Any) -> list[Row]:
    async with db.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        return list(await cur.fetchall())


async def scalar(db: Conn, query: LiteralString, *params: Any) -> Any:
    cur = await db.execute(query, params)
    row = await cur.fetchone()
    assert row is not None
    return row[0]


async def add_source(
    env: Env,
    *,
    endpoint: HttpEndpoint | StdioEndpoint | None = None,
    recipe: str | None = None,
    schedule: timedelta | None = None,
    policy: EgressPolicy | None = None,
    name: str = "hearth",
) -> uuid.UUID:
    source = await create_mcp_source(
        env.db,
        env.access,
        "acme",
        NewMcpSource(
            name=name,
            endpoint=endpoint or HttpEndpoint(URL),
            streams=[CHANNEL],
            recipe_toml=recipe or recipe_text(),
            schedule=schedule,
        ),
        policy=policy or env.policy(),
    )
    return source.id


async def stream_of(env: Env, source: uuid.UUID) -> uuid.UUID:
    return await scalar(env.db, "SELECT id FROM stream WHERE source_id = %s", source)


@asynccontextmanager
async def running(*services: Worker | Scheduler, timeout: float = 30) -> AsyncGenerator[None]:
    with anyio.fail_after(timeout):
        async with anyio.create_task_group() as group:
            for service in services:
                group.start_soon(service.run)
            try:
                yield
            finally:
                for service in services:
                    service.stop()
    await wait_for_run_writes()


def worker(env: Env, handler: Handler) -> Worker:
    return Worker(
        env.pool,
        {MCP_SYNC: handler},
        lease=timedelta(seconds=30),
        poll_interval=timedelta(milliseconds=20),
        retry=JobRetry(base=timedelta(seconds=30)),
    )


async def sync(env: Env, source: uuid.UUID, handler: Handler | None = None) -> Row:
    """Enqueue one sync of the source's stream (one attempt), run it with a worker, and return
    its run row."""
    stream = await stream_of(env, source)
    job = await enqueue(
        env.db,
        workspace_id=env.workspace,
        owner_id=env.owner,
        kind=MCP_SYNC,
        source_id=source,
        stream_id=stream,
        dedupe_key=stream_dedupe_key(stream),
        max_attempts=1,
    )
    async with running(worker(env, handler or env.handler())):
        while await scalar(env.db, "SELECT status FROM job WHERE id = %s", job.id) in (
            "queued",
            "running",
        ):
            await anyio.sleep(0.02)
    found = await rows(env.db, "SELECT * FROM run WHERE stream_id = %s", stream)
    assert len(found) == 1
    return found[0]


async def threads(env: Env, source: uuid.UUID) -> dict[str, list[str]]:
    found = await rows(
        env.db,
        "SELECT external_id, convert_from(payload, 'UTF8') AS payload FROM record"
        " WHERE source_id = %s AND deleted_at IS NULL ORDER BY external_id",
        source,
    )
    return {
        row["external_id"]: [m["author"] for m in json.loads(row["payload"])["items"]]
        for row in found
    }


def steps(run: Mapping[str, Any]) -> list[tuple[str, str]]:
    return [(step["name"], step["status"]) for step in run["report"]["steps"]]


def counts(run: Mapping[str, Any]) -> dict[str, int]:
    return run["report"]["counts"]


def expected(env: Env) -> dict[str, list[str]]:
    return {
        f"{CHANNEL}/{t.root.ts}": [t.root.user_name, *(r.user_name for r in t.replies)]
        for t in recent_threads(env.now)
    }


# --- A scheduled run ------------------------------------------------------------------------------


async def test_a_scheduled_run_against_a_fake_server_ingests_records(env: Env) -> None:
    source = await add_source(env, schedule=timedelta(minutes=1))
    stream = await stream_of(env, source)

    scheduler = Scheduler(env.pool, sync_kinds={"mcp": MCP_SYNC}, interval=timedelta(seconds=0.05))
    async with running(scheduler, worker(env, env.handler())):
        while not await rows(
            env.db, "SELECT 1 FROM run WHERE stream_id = %s AND status <> 'running'", stream
        ):
            await anyio.sleep(0.02)

    (run,) = await rows(env.db, "SELECT * FROM run WHERE stream_id = %s", stream)
    assert run["kind"] == MCP_SYNC
    assert run["status"] == "succeeded", run["error"]
    assert await threads(env, source) == expected(env)

    # Two initial windows of seven days (max_span): an empty one, then the one with the threads:
    # two list pages and a detail call for each thread with replies.
    plan = run["report"]["details"]["plan"]
    assert [w["kind"] for w in plan["windows"]] == ["initial", "initial"]
    assert steps(run) == [("connect", "succeeded")] + [
        (f"initial {w['from']}/{w['to']}", "succeeded") for w in plan["windows"]
    ]
    assert [call.tool for call in env.fake.calls()] == [
        "channel_history",
        "channel_history",
        "channel_history",
        "thread_replies",
        "thread_replies",
    ]
    assert counts(run)["mcp_calls"] == 5
    assert counts(run)["windows_complete"] == 2
    assert counts(run)["records_inserted"] == 3
    assert run["report"]["details"]["drift"] == {
        "server_name": "hearth-mcp",
        "server_version": "2.3.1",
        "errors": [],
        "warnings": [],
    }
    assert run["report"]["details"]["budget"] == {"calls": 5, "max_calls": 3000, "spent": False}
    # The checkpoint moved to the time pinned for the run, in the ingest that stored the window.
    (job,) = await rows(env.db, "SELECT args FROM job WHERE stream_id = %s", stream)
    checked_until = await scalar(env.db, "SELECT checked_until FROM stream WHERE id = %s", stream)
    assert checked_until == datetime.fromisoformat(job["args"]["as_of"])
    # The raw responses are stored, every window parsed and complete.
    windows = await rows(
        env.db, "SELECT complete, server_name FROM raw_window WHERE stream_id = %s", stream
    )
    assert windows == [{"complete": True, "server_name": "hearth-mcp"}] * 2
    assert await scalar(env.db, "SELECT count(*) FROM raw_response") == 5
    assert env.fake.authorizations
    assert set(env.fake.authorizations) == {None}


async def test_a_second_run_moves_on_from_the_checkpoint_and_changes_nothing(env: Env) -> None:
    source = await add_source(env)
    await sync(env, source)
    before = await scalar(env.db, "SELECT array_agg(xmin::text ORDER BY id) FROM record")

    stream = await stream_of(env, source)
    await env.db.execute("DELETE FROM run WHERE stream_id = %s", (stream,))
    run = await sync(env, source)

    assert run["status"] == "succeeded", run["error"]
    plan = run["report"]["details"]["plan"]
    # Forward from the checkpoint less the overlap; then a history step of 30 days (backfill),
    # seven days a window (max_span), newest first: before the threads, so empty.
    assert [w["kind"] for w in plan["windows"]] == ["forward"] + ["backfill"] * 5
    assert counts(run)["records_unchanged"] == 3
    assert await scalar(env.db, "SELECT array_agg(xmin::text ORDER BY id) FROM record") == before


async def test_the_stdio_transport_runs_the_server_as_a_subprocess(env: Env) -> None:
    endpoint = StdioEndpoint(
        command=sys.executable,
        args=(str(STDIO_SCRIPT), str(int(env.now.timestamp()))),
        credential_env="HEARTH_TOKEN",
    )
    source = await add_source(env, endpoint=endpoint)
    await put_api_key(env.db, env.keyring, source_id=source, api_key=SecretStr(EXPECTED_TOKEN))

    run = await sync(env, source)

    assert run["status"] == "succeeded", run["error"]
    assert await threads(env, source) == expected(env)
    assert counts(run)["mcp_calls"] == 5
    assert "mcp_tool_errors" not in counts(run)  # the server got the token in HEARTH_TOKEN


# --- Credentials ----------------------------------------------------------------------------------


async def test_the_credential_goes_as_a_bearer_header_and_never_into_logs_or_rows(
    env: Env, log_output: io.StringIO
) -> None:
    source = await add_source(env)
    await put_api_key(env.db, env.keyring, source_id=source, api_key=SecretStr(API_KEY))

    run = await sync(env, source)

    assert run["status"] == "succeeded", run["error"]
    assert env.fake.authorizations
    assert set(env.fake.authorizations) == {f"Bearer {API_KEY}"}
    assert API_KEY not in log_output.getvalue()
    dumped = await scalar(
        env.db,
        "SELECT concat_ws(' ', (SELECT string_agg(report::text || coalesce(error, ''), ' ')"
        " FROM run), (SELECT string_agg(args::text || coalesce(last_error, ''), ' ') FROM job),"
        " (SELECT string_agg(convert_from(body, 'UTF8'), ' ') FROM raw_body),"
        " (SELECT string_agg(arguments::text, ' ') FROM raw_response),"
        " (SELECT string_agg(config::text, ' ') FROM source))",
    )
    assert API_KEY not in dumped


async def test_a_refused_oauth_token_is_refreshed_once_and_the_call_goes_on(env: Env) -> None:
    issued: list[dict[str, str]] = []

    async def token_endpoint(scope: Any, receive: Any, send: Any) -> None:
        body = b""
        more = True
        while more:
            message = await receive()
            body += message.get("body", b"")
            more = message.get("more_body", False)
        issued.append(dict(httpx2.QueryParams(body.decode())))
        answer = json.dumps(
            {"access_token": "synthetic-new-access", "token_type": "Bearer", "expires_in": 3600}
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": answer})

    mcp_app = env.app

    async def app(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http" and scope["path"] == "/oauth/token":
            await token_endpoint(scope, receive, send)
        else:
            await mcp_app(scope, receive, send)

    env.app = app
    source = await add_source(env)
    await put_oauth(
        env.db,
        env.keyring,
        source_id=source,
        tokens=OAuthTokens(
            access_token=SecretStr("synthetic-old-access"),
            refresh_token=SecretStr("synthetic-refresh"),
        ),
        client=OAuthClient(token_endpoint=f"https://{HOST}/oauth/token", client_id="memini-test"),
    )
    env.fake.answer_http(401)

    run = await sync(env, source)

    assert run["status"] == "succeeded", run["error"]
    assert issued == [
        {
            "grant_type": "refresh_token",
            "refresh_token": "synthetic-refresh",
            "client_id": "memini-test",
        }
    ]
    assert counts(run)["oauth_refreshed"] == 1
    assert env.fake.authorizations[-1] == "Bearer synthetic-new-access"
    stored = await read_oauth(env.db, env.keyring, source_id=source)
    assert stored.tokens.access_token.get_secret_value() == "synthetic-new-access"
    assert stored.tokens.refresh_token is not None  # the provider issued none: the old one stays
    assert stored.info.generation == 2
    assert await threads(env, source) == expected(env)


# --- Retries and the budget -----------------------------------------------------------------------


async def test_a_timeout_and_a_rate_limit_answer_are_retried_within_budget(env: Env) -> None:
    source = await add_source(env, recipe=recipe_text(timeout='"1s"'))
    env.fake.fail("channel_history", "pass", "slow")  # the second list call times out once
    env.fake.answer_http(429, "0")  # the first call is told to slow down
    env.fake.fail("thread_replies", "error")  # one detail call answers isError

    run = await sync(env, source)

    assert run["status"] == "succeeded", run["error"]
    assert await threads(env, source) == expected(env)
    assert env.fake.http_statuses == [429]
    found = counts(run)
    assert found["mcp_failures_rate_limited"] == 1
    assert found["mcp_failures_timeout"] == 1
    assert found["mcp_tool_errors"] == 1
    assert found["mcp_retries"] == 3
    assert found["mcp_calls"] == 5 + 3  # every try is a call of the budget
    assert run["report"]["details"]["budget"]["calls"] == 8
    assert found["windows_complete"] == 2


async def test_a_call_out_of_tries_leaves_its_window_incomplete_with_the_call_named(
    env: Env,
) -> None:
    source = await add_source(env)
    stream = await stream_of(env, source)
    env.fake.fail("thread_replies", "error", "error", "error")

    run = await sync(env, source)

    assert run["status"] == "partial"
    (incomplete,) = run["report"]["details"]["incomplete_windows"]
    assert incomplete["stream"] == CHANNEL
    failed = [c for c in incomplete["missing_calls"] if c["check"] == "failed"]
    assert len(failed) == 1
    assert failed[0]["tool"] == "thread_replies"
    assert failed[0]["repeat"] is True
    assert counts(run)["mcp_tool_errors"] == 3
    # The first window completed and moved the checkpoint; the second did not move it.
    plan = run["report"]["details"]["plan"]
    checked_until = await scalar(env.db, "SELECT checked_until FROM stream WHERE id = %s", stream)
    assert checked_until == datetime.fromisoformat(plan["windows"][0]["to"].replace("Z", "+00:00"))


async def test_a_spent_budget_ends_the_run_partial_with_the_missing_calls_named(env: Env) -> None:
    source = await add_source(env, recipe=recipe_text(max_calls="3"))
    stream = await stream_of(env, source)

    run = await sync(env, source)

    assert run["status"] == "partial"
    assert "rate.max_calls" in run["error"]
    assert run["report"]["details"]["budget"] == {"calls": 3, "max_calls": 3, "spent": True}
    assert len(env.fake.calls()) == 3
    (incomplete,) = run["report"]["details"]["incomplete_windows"]
    missing = incomplete["missing_calls"]
    assert {(c["tool"], c["check"]) for c in missing} == {("thread_replies", "detail")}
    assert len(missing) == 2
    windows = await rows(
        env.db, "SELECT complete FROM raw_window WHERE stream_id = %s ORDER BY window_from", stream
    )
    assert windows == [{"complete": True}, {"complete": False}]


# --- Drift ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"name": "another-mcp"}, "another name than hearth-mcp"),
        ({"tools": ("channel_history",)}, "does not list the tool thread_replies"),
        (
            {"history_schema": {"type": "object", "properties": {"channel": {}, "cursor": {}}}},
            "does not declare the arguments the recipe sends: latest, limit, oldest",
        ),
    ],
    ids=["server-name", "missing-tool", "undeclared-argument"],
)
async def test_drift_stops_the_run_before_any_call(
    env: Env, change: dict[str, Any], error: str
) -> None:
    for key, value in change.items():
        setattr(env.fake, key, value)
    source = await add_source(env)

    async with env.fake.asgi() as env.app:  # the server, made again with its new name
        run = await sync(env, source)

    assert run["status"] == "failed"
    assert run["error"].startswith("DriftError: the server drifted from the recipe")
    assert error in run["error"]
    assert error in " ".join(run["report"]["details"]["drift"]["errors"])
    assert steps(run) == [("connect", "failed")]
    assert env.fake.calls() == []
    assert await scalar(env.db, "SELECT count(*) FROM raw_window") == 0


async def test_another_version_is_a_warning_in_the_report(env: Env) -> None:
    env.fake.version = "3.0.0"
    source = await add_source(env)

    async with env.fake.asgi() as env.app:
        run = await sync(env, source)

    assert run["status"] == "succeeded", run["error"]
    assert run["report"]["details"]["drift"]["warnings"] == [
        "the server's version 3.0.0 matches none of the recipe's server.versions (2.*)"
    ]


# --- Egress ---------------------------------------------------------------------------------------


@pytest.fixture
def no_sockets(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Fail on any socket connect, and record the attempts."""
    import socket

    attempts: list[str] = []

    def refuse(*args: Any, **kwargs: Any) -> Any:
        attempts.append("connect")
        raise AssertionError("a socket was opened")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(httpcore2.AnyIOBackend, "connect_tcp", refuse)
    return attempts


def resolver(addresses: Mapping[str, list[str]]) -> Callable[[str, int], Any]:
    import ipaddress

    async def resolve(host: str, port: int) -> list[IPAddress]:
        return [ipaddress.ip_address(a) for a in addresses.get(host, [])]

    return resolve


async def test_an_endpoint_outside_the_allowlist_is_refused_when_added(env: Env) -> None:
    with pytest.raises(InvalidSourceError) as refused:
        await add_source(env, endpoint=HttpEndpoint("https://elsewhere.example/mcp"))
    assert refused.value.setting == "endpoint.url"
    assert "MEMINI_MCP_ALLOWED_ENDPOINTS" in refused.value.reason
    assert "elsewhere" not in str(refused.value)


async def test_a_command_outside_the_allowlist_is_refused_when_added(env: Env) -> None:
    with pytest.raises(InvalidSourceError) as refused:
        await add_source(env, endpoint=StdioEndpoint(command="/bin/sh", args=("-c", "true")))
    assert refused.value.setting == "endpoint.command"


async def test_a_run_whose_endpoint_left_the_allowlist_is_refused_before_any_request(
    env: Env, no_sockets: list[str]
) -> None:
    source = await add_source(env)
    handler = env.handler(policy=env.policy(endpoints=["other.example"]))

    run = await sync(env, source, handler)

    assert run["status"] == "failed"
    assert run["error"].startswith("McpConfigError: endpoint.url: the endpoint is not in")
    assert env.fake.authorizations == []
    assert no_sockets == []


async def test_an_internal_address_is_refused_before_any_connection(
    env: Env, no_sockets: list[str]
) -> None:
    source = await add_source(env)
    # One address is public (here: a documentation network the test allows), one is not.
    handler = env.handler(
        policy=env.policy(networks=["203.0.113.0/24"]),
        http_transport=None,
        resolver=resolver({HOST: ["203.0.113.9", "10.1.2.3"]}),
    )

    run = await sync(env, source, handler)

    assert run["status"] == "failed"
    assert run["error"].startswith("EgressRefused: the endpoint's host is an internal address")
    assert "10.1.2.3" not in run["error"]
    assert no_sockets == []
    assert env.fake.authorizations == []


# --- Administration -------------------------------------------------------------------------------


async def test_an_mcp_source_keeps_its_recipe_endpoint_and_streams(env: Env) -> None:
    source_id = await add_source(env, schedule=timedelta(hours=1))
    (row,) = await rows(env.db, "SELECT kind, config FROM source WHERE id = %s", source_id)
    assert row["kind"] == "mcp"
    config = row["config"]
    assert config["endpoint"] == {"url": URL}
    assert config["recipe"]["name"] == "hearth"
    assert config["recipe"]["toml"] == recipe_text()
    assert len(config["recipe"]["digest"]) == 64
    assert config["window"] == {}

    changed = await update_source(
        env.db,
        env.access,
        "acme",
        "hearth",
        SourceUpdate(streams=["C0042", "C0043"], window={"initial": "3d", "backfill": None}),
        policy=env.policy(),
    )
    assert changed.refs == ("C0042", "C0043")
    assert changed.mcp is not None
    assert changed.mcp.window == {"initial": "3d", "backfill": None}
    assert changed.schedule == timedelta(hours=1)

    for update, setting in (
        (SourceUpdate(streams=["not a channel"]), "streams"),
        (SourceUpdate(window={"initial": "soon"}), "window.initial"),
        (SourceUpdate(window={"overlap": None}), "window.overlap"),
        (SourceUpdate(endpoint=HttpEndpoint("http://hearth.example/mcp")), "endpoint.url"),
        (SourceUpdate(recipe_toml="format = 1\n"), "recipe"),
        (SourceUpdate(refs=["main"]), "refs"),
    ):
        with pytest.raises(InvalidSourceError) as refused:
            await update_source(env.db, env.access, "acme", "hearth", update, policy=env.policy())
        assert refused.value.setting == setting


async def test_a_recipe_whose_parser_this_build_lacks_is_refused(env: Env) -> None:
    unregister_parser("hearth.thread")
    with pytest.raises(InvalidSourceError) as refused:
        await add_source(env)
    assert refused.value.setting == "recipe"
    assert "no parser named hearth.thread" in refused.value.reason


# --- Both executors -------------------------------------------------------------------------------


async def test_both_executors_run_a_source_of_the_admin_library_to_identical_records(
    env: Env,
) -> None:
    """A source made by memini.sources is what the agent executor reads too: the same recipe and
    windows give the same calls, and the same records (milestone M3)."""
    service_source = await add_source(env, name="by-service")
    agent_source = await add_source(env, name="by-agent")
    await sync(env, service_source)

    run = await start_run(env.db, env.access, "acme", "by-agent", CHANNEL)
    agent_calls: list[tuple[str, dict[str, Any]]] = []
    while True:
        step = await next_call(env.db, env.access, "acme", "by-agent", run.id)
        if step.status == "run_done":
            break
        if step.call is None:
            continue  # a window is done: the next call opens the following one
        call = step.call
        arguments = dict(call.arguments)
        agent_calls.append((call.tool, arguments))
        if call.tool == "channel_history":
            result = env.fake.hearth.channel_history(arguments)
        else:
            result = env.fake.hearth.thread_replies(arguments)
        await submit_response(
            env.db,
            env.access,
            "acme",
            "by-agent",
            run.id,
            call.id,
            tool=call.tool,
            arguments=arguments,
            result=result,
        )
    await finish_run(env.db, env.access, "acme", "by-agent", run.id, runs=env.pool)
    await wait_for_run_writes()

    # Each run pins its own as_of, a moment apart: the window bounds may differ by a second.
    def untimed(calls: list[tuple[str, dict[str, Any]]]) -> list[tuple[str, dict[str, Any]]]:
        return [
            (tool, {k: v for k, v in arguments.items() if k not in ("oldest", "latest")})
            for tool, arguments in calls
        ]

    service_calls = [(c.tool, dict(c.arguments)) for c in env.fake.calls()]
    assert untimed(agent_calls) == untimed(service_calls)
    for (_, agent), (_, service) in zip(agent_calls, service_calls, strict=True):
        for bound in ("oldest", "latest"):
            assert abs(agent.get(bound, 0) - service.get(bound, 0)) <= 5
    found = await rows(
        env.db,
        "SELECT source_id, external_id, convert_from(payload, 'UTF8') AS payload, occurred_at,"
        " external_url FROM record ORDER BY external_id",
    )
    by_source = {
        source: [
            (row["external_id"], row["payload"], row["occurred_at"], row["external_url"])
            for row in found
            if row["source_id"] == source
        ]
        for source in (service_source, agent_source)
    }
    assert len(by_source[service_source]) == 3
    assert by_source[agent_source] == by_source[service_source]
