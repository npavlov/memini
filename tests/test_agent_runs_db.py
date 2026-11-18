"""The agent executor's API against a real Postgres (memini-2wl): a scripted fake agent completes a
run end to end; a response for a call not handed out, or for another call under its id, is refused
and stores nothing; failed calls are handed out again and then given up; scopes and the workspace
limit hold. The capture hook itself is tested in tests/test_agent_capture_db.py. Every value is
synthetic (tests/agent_helpers.py)."""

import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, LiteralString

import httpx2
import pytest
from psycopg import AsyncConnection
from psycopg.rows import TupleRow
from pydantic import SecretStr

from agent_helpers import (
    SOURCE,
    McpSource,
    add_mcp_source,
    call_hearth,
    every_text,
    fake_hearth,
    hearth_parser_registered,
    payloads_of,
    recipe_text,
    response_body,
)
from hearth_helpers import (
    CHANNEL,
    FAKE_TOKEN,
    HEARTH_RECIPE_PATH,
    FakeHearth,
    Target,
    hearth_parser,
    run_window,
)
from memini.api import create_app
from memini.auth import Scope, WriteAccess, create_token
from memini.config import Settings
from memini.db import connect, open_pool
from memini.executors.agent import MAX_ATTEMPTS, RECIPE_KEY
from memini.intake import PlannedWindow
from memini.recipes import load_recipe
from memini.workspaces import create_workspace, ensure_principal

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

type Conn = AsyncConnection[TupleRow]
type Json = dict[str, Any]

RUNS = f"/v1/workspaces/acme/sources/{SOURCE}/agent-runs"


@dataclass(frozen=True)
class Env:
    db: Conn
    client: httpx2.AsyncClient
    owner: uuid.UUID
    acme: uuid.UUID
    other: uuid.UUID
    hearth: McpSource
    agent: dict[str, str]
    """A token with the ingest scope."""


async def bearer(
    db: Conn, owner: uuid.UUID, *scopes: Scope, workspace_id: uuid.UUID | None = None
) -> dict[str, str]:
    new = await create_token(
        db, owner_id=owner, name="test agent", scopes=scopes, workspace_id=workspace_id
    )
    return {"Authorization": f"Bearer {new.secret.get_secret_value()}"}


@pytest.fixture
async def env(migrated_database_url: str) -> AsyncIterator[Env]:
    settings = Settings(database_url=SecretStr(migrated_database_url))
    async with (
        connect(migrated_database_url, autocommit=True) as db,
        open_pool(migrated_database_url, min_size=1, max_size=4, timeout=5.0) as pool,
    ):
        owner = await ensure_principal(db, "owner")
        acme = await create_workspace(db, owner_id=owner, name="acme")
        other = await create_workspace(db, owner_id=owner, name="other")
        hearth = await add_mcp_source(db, acme.id, owner)
        agent = await bearer(db, owner, "ingest")
        transport = httpx2.ASGITransport(app=create_app(settings, pool=pool))
        with hearth_parser_registered():
            async with httpx2.AsyncClient(
                transport=transport, base_url="http://memini.test"
            ) as client:
                yield Env(db, client, owner, acme.id, other.id, hearth, agent)


def ok(response: httpx2.Response, status_code: int = 200) -> Json:
    assert response.status_code == status_code, response.text
    body: Json = response.json()
    return body


def refused(response: httpx2.Response, status_code: int, code: str) -> Json:
    assert response.status_code == status_code, response.text
    error: Json = response.json()["error"]
    assert error["code"] == code, error
    return error


async def rows(db: Conn, query: LiteralString, *params: Any) -> list[tuple[Any, ...]]:
    cur = await db.execute(query, params)
    return [tuple(row) for row in await cur.fetchall()]


async def scalar(db: Conn, query: LiteralString, *params: Any) -> Any:
    found = await rows(db, query, *params)
    assert len(found) == 1
    return found[0][0]


async def responses_stored(env: Env) -> int:
    return int(await scalar(env.db, "SELECT count(*) FROM raw_response"))


async def start(env: Env, **body: Any) -> Json:
    return ok(await env.client.post(RUNS, json={"stream": CHANNEL, **body}, headers=env.agent), 201)


async def next_step(env: Env, run: Json) -> Json:
    return ok(await env.client.get(f"{RUNS}/{run['id']}/next", headers=env.agent))


async def post(env: Env, run: Json, body: Mapping[str, Any]) -> httpx2.Response:
    return await env.client.post(f"{RUNS}/{run['id']}/responses", json=body, headers=env.agent)


async def finish(env: Env, run: Json) -> Json:
    return ok(await env.client.post(f"{RUNS}/{run['id']}/finish", headers=env.agent))


async def scripted_agent(env: Env, server: FakeHearth) -> tuple[Json, list[str], Json]:
    """A fake agent: start, then make each call handed out and post its result as the hook
    does, until the run is done; then finish. Returns the run, the steps and the finish."""
    run = await start(env)
    steps: list[str] = []
    for _ in range(100):
        step = await next_step(env, run)
        steps.append(step["status"])
        if step["status"] == "run_done":
            break
        if step["status"] == "window_done":
            continue
        call = step["call"]
        result = call_hearth(server, call["tool"], call["arguments"])
        receipt = ok(await post(env, run, response_body(call, result)))
        assert receipt["stored"] is True
        assert set(receipt) == {
            "call_id",
            "window",
            "position",
            "stored",
            "is_error",
            "responses",
            "redacted",
        }
    else:
        raise AssertionError("the run never ended")
    return run, steps, await finish(env, run)


# --- End to end ----------------------------------------------------------------------------------


async def test_a_scripted_agent_completes_a_run_end_to_end(env: Env) -> None:
    server = fake_hearth()
    run, steps, done = await scripted_agent(env, server)

    # The plan: a new stream loads 14 days back from the pinned time, in two windows of 7 days.
    assert [w["kind"] for w in run["windows"]] == ["initial", "initial"]
    assert run["windows"][0]["status"] == "open"
    # The older window: a list page and the replies of one thread; the newer: two list pages
    # and the replies of two threads.
    assert steps == ["call"] * 2 + ["window_done"] + ["call"] * 4 + ["window_done", "run_done"]

    assert [(w["ingested"], w["window"]["complete"]) for w in done["windows"]] == [
        (True, True),
        (True, True),
    ]
    assert sum(w["records"] for w in done["windows"]) == 5
    as_of = datetime.fromisoformat(run["as_of"])
    assert datetime.fromisoformat(done["checked_until"]) == as_of  # the window ends at as_of
    assert done["agent_run"]["state"] == "finished"
    assert done["agent_run"]["run_id"] == done["run_id"]

    records = await rows(
        env.db,
        "SELECT external_id FROM record WHERE source_id = %s AND deleted_at IS NULL",
        env.hearth.source_id,
    )
    assert len(records) == 5
    checked = await scalar(
        env.db, "SELECT checked_until FROM stream WHERE id = %s", env.hearth.stream_id
    )
    assert checked == as_of
    assert await responses_stored(env) == 6
    # Redaction at the store: the fake token is nowhere, raw or parsed.
    stored = "\n".join(
        str(value)
        for (value,) in await rows(
            env.db,
            "SELECT convert_from(body, 'UTF8') FROM raw_body"
            " UNION ALL SELECT convert_from(payload, 'UTF8') FROM record",
        )
    )
    assert FAKE_TOKEN not in stored

    # The run report: kind agent_sync, the plan, the session's counts, a step per window.
    report = ok(await env.client.get(f"/v1/runs/{done['run_id']}", headers=await admin(env)))
    assert (report["kind"], report["status"]) == ("agent_sync", "succeeded")
    counts = report["report"]["counts"]
    assert counts["calls_handed_out"] == 6
    assert counts["responses_stored"] == 6
    assert counts["windows_ingested"] == counts["windows_complete"] == 2
    assert len(report["report"]["details"]["plan"]["windows"]) == 2
    assert [step["name"].split()[0] for step in report["report"]["steps"]] == ["initial"] * 2
    windows = await rows(env.db, "SELECT run_id FROM raw_window")
    assert {found for (found,) in windows} == {uuid.UUID(done["run_id"])}
    # The calls handed out are gone with the run; the raw responses keep them.
    assert await scalar(env.db, "SELECT count(*) FROM agent_call") == 0

    # A finished run takes no more calls.
    refused(
        await env.client.get(f"{RUNS}/{run['id']}/next", headers=env.agent),
        409,
        "agent_run_finished",
    )
    refused(
        await env.client.post(f"{RUNS}/{run['id']}/finish", headers=env.agent),
        409,
        "agent_run_finished",
    )

    # The next run goes on from the checkpoint, the overlap behind it, and loads older history.
    again = await start(env)
    assert again["windows"][0]["kind"] == "forward"
    assert datetime.fromisoformat(again["windows"][0]["from"]) < as_of
    assert {w["kind"] for w in again["windows"][1:]} == {"backfill"}


async def admin(env: Env) -> dict[str, str]:
    return await bearer(env.db, env.owner, "read", "admin")


async def test_the_records_are_those_the_same_calls_give_the_service_side_loop(env: Env) -> None:
    """One recipe, two executors: the records of an agent run equal those of the loop both
    executors run (hearth_helpers.run_window) over the same windows and the same server."""
    server = fake_hearth()
    run, _, _ = await scripted_agent(env, server)

    direct = await add_mcp_source(env.db, env.acme, env.owner, name="hearth-direct")
    recipe = load_recipe(HEARTH_RECIPE_PATH)

    async def call_tool(tool: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        return call_hearth(server, tool, arguments)

    for window in run["windows"]:
        planned = PlannedWindow(
            kind=window["kind"],
            start=datetime.fromisoformat(window["from"]),
            end=datetime.fromisoformat(window["to"]),
        )
        done = await run_window(
            env.db,
            call_tool,
            Target(env.acme, env.owner, direct.source_id, direct.stream_id),
            planned,
            recipe=recipe,
            parser=hearth_parser(names=True),
            access=WriteAccess.for_owner(env.owner),
        )
        assert done.complete

    query: LiteralString = (
        "SELECT external_id, payload, occurred_at, external_url FROM record WHERE source_id = %s"
    )
    agent_records = payloads_of(await rows(env.db, query, env.hearth.source_id))
    direct_records = payloads_of(await rows(env.db, query, direct.source_id))
    assert len(agent_records) == 5
    assert agent_records == direct_records


# --- Calls that were not handed out --------------------------------------------------------------


async def test_a_response_for_a_call_not_handed_out_is_refused_and_stores_nothing(
    env: Env,
) -> None:
    server = fake_hearth()
    run = await start(env)
    call = (await next_step(env, run))["call"]
    result = call_hearth(server, call["tool"], call["arguments"])
    good = response_body(call, result)

    refused(
        await post(env, run, {**good, "call_id": str(uuid.uuid4())}), 409, "call_not_handed_out"
    )
    refused(await post(env, run, {**good, "tool": "thread_replies"}), 409, "call_mismatch")
    changed = {**call["arguments"], "limit": 50}
    error = refused(await post(env, run, {**good, "arguments": changed}), 409, "call_mismatch")
    assert "arguments" in error["message"]
    as_text = {**call["arguments"], "oldest": str(call["arguments"]["oldest"])}
    refused(await post(env, run, {**good, "arguments": as_text}), 409, "call_mismatch")
    # A call of another run is not one this run handed out.
    assert await responses_stored(env) == 0

    receipt = ok(await post(env, run, good))
    assert (receipt["stored"], receipt["position"], receipt["responses"]) == (True, 1, 1)
    again = ok(await post(env, run, good))  # a hook that posts twice
    assert (again["stored"], again["position"]) == (False, 1)
    assert await responses_stored(env) == 1

    # The answer never repeats the response.
    assert all(text not in str(receipt) for text in every_text(server.threads))


async def test_what_is_posted_is_a_tool_result(env: Env) -> None:
    server = fake_hearth()
    run = await start(env)
    call = (await next_step(env, run))["call"]
    result = call_hearth(server, call["tool"], call["arguments"])
    base = response_body(call, result)

    for bad in ({"result": 12}, {"result": [{"text": "no type"}]}, {"result": {"isError": False}}):
        refused(await post(env, run, {**base, **bad}), 422, "invalid_result")
    refused(await post(env, run, {**base, "error": "boom"}), 422, "invalid_result")  # both
    assert await responses_stored(env) == 0

    # The whole MCP result object is taken as it is; a text becomes one text block.
    ok(await post(env, run, {**base, "result": result}))
    stored = await scalar(env.db, "SELECT convert_from(body, 'UTF8') FROM raw_body")
    assert '"isError":false' in stored


# --- Retries and giving up -----------------------------------------------------------------------


async def test_a_call_without_a_response_is_handed_out_again_then_given_up(env: Env) -> None:
    run = await start(env)
    first = await next_step(env, run)
    ids = [first["call"]["id"]]
    for attempt in range(2, MAX_ATTEMPTS + 1):
        again = await next_step(env, run)
        assert again["call"]["id"] == ids[0]  # the same call, under the same id
        assert again["call"]["attempt"] == attempt
    done = await next_step(env, run)
    assert (done["status"], done["complete"], done["left"]) == ("window_done", False, 1)

    # The window was not complete: the rest of its chain is skipped, and the run is done.
    end = await next_step(env, run)
    assert end["status"] == "run_done"
    finished = await finish(env, run)
    assert [w["window"]["status"] for w in finished["windows"]] == ["ingested", "skipped"]
    assert finished["checked_until"] is None
    report = ok(await env.client.get(f"/v1/runs/{finished['run_id']}", headers=await admin(env)))
    assert report["status"] == "partial"
    incomplete = report["report"]["details"]["incomplete_windows"]
    assert len(incomplete) == 1
    assert incomplete[0]["missing_calls"][0]["tool"] == "channel_history"
    assert report["report"]["counts"]["calls_abandoned"] == 1


async def test_a_failed_call_is_handed_out_again_until_its_attempts_are_spent(env: Env) -> None:
    run = await start(env)
    seen: list[str] = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        step = await next_step(env, run)
        call = step["call"]
        assert call["attempt"] == attempt
        assert call["check"] == ("page" if attempt == 1 else "failed")
        seen.append(call["id"])
        receipt = ok(
            await post(
                env,
                run,
                {
                    "call_id": call["id"],
                    "tool": call["tool"],
                    "arguments": call["arguments"],
                    "error": "MCP error -32603: the server is unavailable",
                },
            )
        )
        assert receipt["is_error"] is True
    assert len(set(seen)) == MAX_ATTEMPTS  # each attempt a call of its own
    done = await next_step(env, run)
    assert (done["status"], done["complete"]) == ("window_done", False)
    finished = await finish(env, run)
    assert finished["windows"][0]["incomplete"]
    assert finished["checked_until"] is None


async def test_a_run_finished_early_stores_what_it_got_and_keeps_the_checkpoint(env: Env) -> None:
    server = fake_hearth()
    run = await start(env)
    call = (await next_step(env, run))["call"]
    ok(
        await post(
            env, run, response_body(call, call_hearth(server, call["tool"], call["arguments"]))
        )
    )
    finished = await finish(env, run)

    assert [(w["ingested"], w["window"]["status"]) for w in finished["windows"]] == [
        (True, "ingested"),
        (False, "skipped"),
    ]
    assert finished["windows"][0]["window"]["complete"] is False
    assert finished["checked_until"] is None
    assert finished["agent_run"]["counts"]["calls_handed_out"] == 1


# --- Refusals ------------------------------------------------------------------------------------


async def test_a_stream_has_one_run_at_a_time(env: Env) -> None:
    run = await start(env)
    error = refused(
        await env.client.post(RUNS, json={"stream": CHANNEL}, headers=env.agent),
        409,
        "agent_run_open",
    )
    assert run["id"] in error["message"]
    await finish(env, run)
    await start(env)


async def test_what_cannot_run_is_refused(env: Env) -> None:
    client, agent = env.client, env.agent
    refused(await client.post(RUNS, json={"stream": "C9999"}, headers=agent), 404, "not_found")
    refused(
        await client.post(
            "/v1/workspaces/acme/sources/nothing/agent-runs",
            json={"stream": CHANNEL},
            headers=agent,
        ),
        404,
        "not_found",
    )
    await env.db.execute(
        "INSERT INTO source (workspace_id, owner_id, name, kind) VALUES (%s, %s, 'chat', 'push')",
        (env.acme, env.owner),
    )
    refused(
        await client.post(
            "/v1/workspaces/acme/sources/chat/agent-runs", json={"stream": CHANNEL}, headers=agent
        ),
        409,
        "not_mcp_source",
    )
    await add_mcp_source(env.db, env.acme, env.owner, name="bare", config={})
    error = refused(
        await client.post(
            "/v1/workspaces/acme/sources/bare/agent-runs", json={"stream": CHANNEL}, headers=agent
        ),
        409,
        "source_not_ready",
    )
    assert RECIPE_KEY in error["message"]
    unknown = recipe_text().replace('name = "hearth.thread"', 'name = "hearth.unknown"')
    await add_mcp_source(env.db, env.acme, env.owner, name="noparser", config={RECIPE_KEY: unknown})
    refused(
        await client.post(
            "/v1/workspaces/acme/sources/noparser/agent-runs",
            json={"stream": CHANNEL},
            headers=agent,
        ),
        409,
        "source_not_ready",
    )
    refused(
        await client.post(
            RUNS, json={"stream": CHANNEL, "server": {"name": "another-mcp"}}, headers=agent
        ),
        409,
        "server_mismatch",
    )
    run = await start(env, server={"name": "hearth-mcp", "version": "9.0.0"})
    assert run["server_version"] == "9.0.0"
    assert run["counts"] == {"server_version_unchecked": 1}


async def test_scopes_and_the_workspace_limit_hold(env: Env) -> None:
    run = await start(env)
    reader = await bearer(env.db, env.owner, "read")
    refused(
        await env.client.get(f"{RUNS}/{run['id']}/next", headers=reader), 403, "insufficient_scope"
    )
    refused(
        await env.client.post(RUNS, json={"stream": CHANNEL}, headers=reader),
        403,
        "insufficient_scope",
    )
    elsewhere = await bearer(env.db, env.owner, "ingest", workspace_id=env.other)
    refused(await env.client.get(f"{RUNS}/{run['id']}/next", headers=elsewhere), 404, "not_found")
    refused(await env.client.get(f"{RUNS}/{run['id']}", headers=elsewhere), 404, "not_found")
    inside = await bearer(env.db, env.owner, "ingest", workspace_id=env.acme)
    assert ok(await env.client.get(f"{RUNS}/{run['id']}", headers=inside))["id"] == run["id"]
    # A run of another source is not found under this one.
    other = await add_mcp_source(env.db, env.acme, env.owner, name="hearth-two")
    assert other.source_id != env.hearth.source_id
    refused(
        await env.client.get(
            f"/v1/workspaces/acme/sources/hearth-two/agent-runs/{run['id']}", headers=env.agent
        ),
        404,
        "not_found",
    )


async def test_a_changed_recipe_stops_the_calls(env: Env) -> None:
    run = await start(env)
    changed = recipe_text().replace("calls_per_minute = 50", "calls_per_minute = 40")
    await env.db.execute(
        "UPDATE source SET config = jsonb_set(config, '{recipe}', to_jsonb(%s::text))"
        " WHERE id = %s",
        (changed, env.hearth.source_id),
    )
    refused(
        await env.client.get(f"{RUNS}/{run['id']}/next", headers=env.agent), 409, "recipe_changed"
    )
    finished = await finish(env, run)  # finish parses with the recipe as it is now
    assert finished["agent_run"]["state"] == "finished"
