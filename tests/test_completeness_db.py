"""Completeness checks through the raw response store against a real Postgres. Acceptance of
memini-n74: a truncated page, a missing reply and an unfollowed cursor each keep the stream's
checkpoint and name the missing call, in the result and in the run report; a complete window
moves the checkpoint. The executor is the loop both executors run: make the missing calls, store
each result, until none is missing or the budget is spent (tests/hearth_helpers.py). Every value
is synthetic."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Final, LiteralString

import pytest
from psycopg import AsyncConnection
from psycopg.rows import TupleRow

from hearth_helpers import (
    CHANNEL,
    WINDOW_1,
    FakeClient,
    FakeHearth,
    LossyHearth,
    Target,
    TruncatedHearth,
    hearth_parser,
    hearth_recipe,
    run_window,
)
from memini.auth import WriteAccess
from memini.db import connect
from memini.responses import INCOMPLETE_DETAIL, WindowIngest
from memini.runs import record_run
from memini.workspaces import create_workspace, ensure_principal

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

type Conn = AsyncConnection[TupleRow]

FIRST_CALL: Final = {
    "channel": CHANNEL,
    "oldest": int(WINDOW_1.start.timestamp()),
    "latest": int(WINDOW_1.end.timestamp()),
    "limit": 100,
}


@dataclass(frozen=True)
class Env:
    db: Conn
    runs: Conn
    access: WriteAccess
    target: Target


@pytest.fixture
async def env(migrated_database_url: str) -> AsyncIterator[Env]:
    async with (
        connect(migrated_database_url, autocommit=True) as db,
        connect(migrated_database_url, autocommit=True) as runs,
    ):
        owner = await ensure_principal(db, "owner")
        acme = await create_workspace(db, owner_id=owner, name="acme")
        cur = await db.execute(
            "INSERT INTO source (workspace_id, owner_id, name, kind)"
            " VALUES (%s, %s, 'hearth', 'mcp') RETURNING id",
            (acme.id, owner),
        )
        source = await cur.fetchone()
        assert source is not None
        cur = await db.execute(
            "INSERT INTO stream (workspace_id, owner_id, source_id, key) VALUES (%s, %s, %s, %s)"
            " RETURNING id",
            (acme.id, owner, source[0], CHANNEL),
        )
        stream = await cur.fetchone()
        assert stream is not None
        yield Env(
            db, runs, WriteAccess.for_owner(owner), Target(acme.id, owner, source[0], stream[0])
        )


async def rows(db: Conn, query: LiteralString, *params: Any) -> list[tuple[Any, ...]]:
    cur = await db.execute(query, params)
    return [tuple(row) for row in await cur.fetchall()]


async def checkpoint(env: Env) -> tuple[Any, Any]:
    [found] = await rows(env.db, "SELECT checked_until, checked_from FROM stream")
    return found[0], found[1]


async def sync(env: Env, server: FakeHearth, **kwargs: Any) -> tuple[WindowIngest, dict[str, Any]]:
    """Run WINDOW_1 against ``server`` inside a sync run; the result and the run's report."""
    target = env.target
    async with record_run(
        env.runs,
        workspace_id=target.workspace_id,
        owner_id=target.owner_id,
        kind="sync",
        source_id=target.source_id,
        stream_id=target.stream_id,
        progress_interval=None,
    ) as run:
        done = await run_window(
            env.db,
            FakeClient(server).call_tool,
            target,
            WINDOW_1,
            recipe=hearth_recipe(),
            parser=hearth_parser(),
            access=env.access,
            **kwargs,
        )
    [(report,)] = await rows(env.db, "SELECT report FROM run WHERE id = %s", run.id)
    return done, report


def listed(report: dict[str, Any]) -> list[dict[str, Any]]:
    """The missing calls the run report lists for its one incomplete window."""
    [entry] = report["details"][INCOMPLETE_DETAIL]
    assert entry["stream"] == CHANNEL
    assert (entry["from"], entry["to"]) == (WINDOW_1.start.isoformat(), WINDOW_1.end.isoformat())
    return entry["missing_calls"]


async def test_a_truncated_page_keeps_the_checkpoint_and_names_the_page(env: Env) -> None:
    done, report = await sync(env, TruncatedHearth(), max_repeats=1)

    assert (done.complete, done.checkpoint_moved) == (False, False)
    assert await checkpoint(env) == (None, None)
    [call] = done.missing
    assert (call.call, call.tool, call.arguments, call.check, call.repeat) == (
        "list",
        "channel_history",
        FIRST_CALL,
        "truncated",
        True,
    )
    assert listed(report) == [call.to_json()]
    assert report["counts"]["windows_incomplete"] == 1
    assert report["counts"]["missing_calls"] == 1
    # An incomplete window stores what it got: the threads of the page it read.
    assert done.inserted == 2


async def test_a_missing_reply_keeps_the_checkpoint_and_names_the_detail_call(env: Env) -> None:
    server = LossyHearth()
    keys = server.threads[2].root.ts

    done, report = await sync(env, server, max_repeats=1)

    assert (done.complete, done.checkpoint_moved) == (False, False)
    assert await checkpoint(env) == (None, None)
    [call] = done.missing
    assert (call.call, call.tool, call.arguments, call.item_id, call.check) == (
        "detail",
        "thread_replies",
        {"channel": CHANNEL, "thread": keys},
        keys,
        "reply_counts",
    )
    assert done.incomplete == (
        "item 2 of call 1: its detail call holds fewer replies than the listing counts",
    )
    assert listed(report) == [call.to_json()]
    assert done.inserted == 4


async def test_an_unfollowed_cursor_keeps_the_checkpoint_and_names_the_next_page(
    env: Env,
) -> None:
    server = FakeHearth()

    done, report = await sync(env, server, max_calls=1)  # the budget ends after the first page

    assert (done.complete, done.checkpoint_moved) == (False, False)
    assert await checkpoint(env) == (None, None)
    cursor = done.missing[0]
    assert (cursor.call, cursor.arguments, cursor.page, cursor.check) == (
        "list",
        {**FIRST_CALL, "cursor": "cur-2"},
        1,
        "page",
    )
    assert [c.check for c in done.missing] == ["page", "detail", "detail"]
    assert done.incomplete[0] == "the cursor of call 1 (the listing) was not followed"
    assert listed(report) == [c.to_json() for c in done.missing]

    # The next run of the window makes every call, and the checkpoint moves with its data.
    again, report = await sync(env, server)
    assert (again.complete, again.checkpoint_moved, again.missing) == (True, True, ())
    assert await checkpoint(env) == (WINDOW_1.end, WINDOW_1.start)
    assert INCOMPLETE_DETAIL not in report.get("details", {})


async def test_a_complete_window_moves_the_checkpoint(env: Env) -> None:
    client = FakeClient(FakeHearth())

    done = await run_window(
        env.db,
        client.call_tool,
        env.target,
        WINDOW_1,
        recipe=hearth_recipe(),
        parser=hearth_parser(),
        access=env.access,
    )

    assert (done.complete, done.checkpoint_moved, done.missing, done.incomplete) == (
        True,
        True,
        (),
        (),
    )
    assert client.calls == 5  # the calls sync_window makes, found from the missing calls alone
    assert await checkpoint(env) == (WINDOW_1.end, WINDOW_1.start)
    assert await rows(env.db, "SELECT complete FROM raw_window") == [(True,)]
