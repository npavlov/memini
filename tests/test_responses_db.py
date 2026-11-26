"""The raw response store and replay against a real Postgres. Acceptance of memini-kdj: a parser
change followed by replay updates records with zero calls to the source. Every value is synthetic
(tests/hearth_helpers.py: the invented Hearth chat server of docs/recipes.md)."""

import hashlib
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, LiteralString

import pytest
from psycopg import AsyncConnection, errors
from psycopg.rows import TupleRow

from hearth_helpers import (
    CHANNEL,
    DAY,
    FAKE_TOKEN,
    SERVER,
    T0,
    WINDOW_1,
    WINDOW_2,
    FakeClient,
    FakeHearth,
    Target,
    hearth_parser,
    hearth_recipe,
    late_reply,
    sync_window,
)
from memini.auth import WriteAccess
from memini.db import connect
from memini.ids import record_id
from memini.ingest import Batch, RecordIn, ingest
from memini.jobs import EXTRACT_KIND
from memini.recipes import ParsedWindow, Recipe, WindowInput, recipe_digest
from memini.removal import ForgetTarget, forget_record
from memini.responses import (
    InvalidResponseError,
    NotAnMcpSourceError,
    ReplayError,
    ResponseTooLargeError,
    WindowClosedError,
    WindowNotFoundError,
    begin_window,
    delete_raw_responses_before,
    delete_raw_responses_for_records,
    ingest_window,
    replay,
    store_response,
)
from memini.sources import SourceBusyError
from memini.workspaces import NotFoundError, create_workspace, ensure_principal

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

type Conn = AsyncConnection[TupleRow]


@dataclass(frozen=True)
class Env:
    db: Conn
    other: Conn
    """A second session, for the source's lock."""
    runs: Conn
    access: WriteAccess
    target: Target
    recipe: Recipe


@pytest.fixture
async def env(migrated_database_url: str) -> AsyncIterator[Env]:
    async with (
        connect(migrated_database_url, autocommit=True) as db,
        connect(migrated_database_url, autocommit=True) as other,
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
            db,
            other,
            runs,
            WriteAccess.for_owner(owner),
            Target(acme.id, owner, source[0], stream[0]),
            hearth_recipe(),
        )


async def rows(db: Conn, query: LiteralString, *params: Any) -> list[tuple[Any, ...]]:
    cur = await db.execute(query, params)
    return [tuple(row) for row in await cur.fetchall()]


async def scalar(db: Conn, query: LiteralString, *params: Any) -> Any:
    found = await rows(db, query, *params)
    assert len(found) == 1
    return found[0][0]


async def versions(db: Conn) -> dict[str, str]:
    """xmin of every record, stream and raw row: it changes exactly when a row is written."""
    found = await rows(
        db,
        "SELECT 'record:' || id, xmin::text FROM record"
        " UNION ALL SELECT 'stream:' || id, xmin::text FROM stream"
        " UNION ALL SELECT 'window:' || id, xmin::text FROM raw_window"
        " UNION ALL SELECT 'response:' || id, xmin::text FROM raw_response"
        " UNION ALL SELECT 'body:' || hash, xmin::text FROM raw_body",
    )
    return {key: value for key, value in found}


async def authors(env: Env) -> dict[str, list[str]]:
    """The authors of each live thread, by external id."""
    found = await rows(
        env.db,
        "SELECT external_id, convert_from(payload, 'UTF8') FROM record"
        " WHERE source_id = %s AND deleted_at IS NULL",
        env.target.source_id,
    )
    return {
        external_id: [m["author"] for m in json.loads(payload)["messages"]]
        for external_id, payload in found
    }


def rid(env: Env, root_ts: str) -> str:
    return record_id(env.target.source_id, "thread", f"{CHANNEL}/{root_ts}")


async def sync(env: Env, client: FakeClient, window: Any = WINDOW_1, **kwargs: Any) -> Any:
    kwargs.setdefault("parser", hearth_parser())
    return await sync_window(
        env.db, client, env.target, window, recipe=env.recipe, access=env.access, **kwargs
    )


async def run_replay(env: Env, **kwargs: Any) -> Any:
    kwargs.setdefault("parser", hearth_parser())
    return await replay(
        env.db, env.access, "acme", "hearth", recipe=env.recipe, runs=env.runs, **kwargs
    )


async def new_window(env: Env, window: Any = WINDOW_1) -> uuid.UUID:
    raw = await begin_window(
        env.db,
        workspace_id=env.target.workspace_id,
        owner_id=env.target.owner_id,
        source_id=env.target.source_id,
        stream_id=env.target.stream_id,
        window=window,
        recipe_digest=recipe_digest(env.recipe),
        server=SERVER,
    )
    return raw.id


def page(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": False}


# --- Storing -------------------------------------------------------------------------------------


async def test_responses_are_stored_redacted_and_hashed(env: Env) -> None:
    client = FakeClient(FakeHearth())
    await sync(env, client)

    assert client.calls == 5  # two list pages, a detail call for each of the three threads
    stored = await rows(env.db, "SELECT hash, body FROM raw_body")
    assert stored
    for content_hash, body in stored:
        assert content_hash == hashlib.sha256(bytes(body)).hexdigest()
    every = "\n".join(
        str(value)
        for (value,) in await rows(
            env.db,
            "SELECT convert_from(body, 'UTF8') FROM raw_body"
            " UNION ALL SELECT arguments::text FROM raw_response"
            " UNION ALL SELECT coalesce(item_id, '') FROM raw_response"
            " UNION ALL SELECT convert_from(payload, 'UTF8') FROM record",
        )
    )
    assert FAKE_TOKEN not in every
    assert "[REDACTED:github_token]" in every
    # Nothing else is rewritten: the stored result is the server's, as canonical JSON.
    detail = await scalar(
        env.db,
        "SELECT convert_from(b.body, 'UTF8') FROM raw_response r"
        " JOIN raw_body b ON b.source_id = r.source_id AND b.hash = r.body_hash"
        " WHERE r.call = 'detail' AND r.item_id = %s",
        FakeHearth().threads[0].root.ts,
    )
    result = json.loads(detail)
    assert result["isError"] is False
    assert json.loads(result["content"][0]["text"])["messages"][1]["text"] == "Friday works for me."
    assert (
        await scalar(
            env.db, "SELECT count(*) FROM raw_window WHERE parsed_at IS NOT NULL AND complete"
        )
        == 1
    )


async def test_the_same_call_and_the_same_body_are_stored_once(env: Env) -> None:
    window = await new_window(env)
    arguments = {"channel": CHANNEL, "oldest": 1, "latest": 2, "limit": 100}

    first = await store_response(
        env.db,
        window,
        call="list",
        tool="channel_history",
        arguments=arguments,
        result=page('{"messages": []}'),
    )
    before = await versions(env.db)
    again = await store_response(
        env.db,
        window,
        call="list",
        tool="channel_history",
        arguments=dict(reversed(arguments.items())),  # another key order is the same call
        result={"isError": False, "content": [{"text": '{"messages": []}', "type": "text"}]},
    )
    assert (first.stored, first.body_stored) == (True, True)
    assert (again.stored, again.body_stored, again.position) == (False, False, first.position)
    assert await versions(env.db) == before

    # The same page in a later window (a later run): a new call row, the body stored once.
    later = await new_window(env, WINDOW_2)
    elsewhere = await store_response(
        env.db,
        later,
        call="list",
        tool="channel_history",
        arguments=arguments,
        result=page('{"messages": []}'),
    )
    assert (elsewhere.stored, elsewhere.body_stored) == (True, False)
    assert await scalar(env.db, "SELECT count(*) FROM raw_body") == 1
    assert await scalar(env.db, "SELECT count(*) FROM raw_response") == 2

    # A retry of the call with another result replaces it; the body only it held is deleted.
    retried = await store_response(
        env.db,
        later,
        call="list",
        tool="channel_history",
        arguments=arguments,
        result=page('{"messages": [], "has_more": false}'),
    )
    assert (retried.stored, retried.position) == (True, elsewhere.position)
    assert await scalar(env.db, "SELECT count(*) FROM raw_body") == 2  # the first window keeps its
    assert await scalar(env.db, "SELECT count(*) FROM raw_response") == 2


async def test_a_call_that_is_not_valid_is_refused(env: Env) -> None:
    window = await new_window(env)

    with pytest.raises(InvalidResponseError, match="item_id"):
        await store_response(
            env.db, window, call="detail", tool="thread_replies", arguments={}, result=page("")
        )
    with pytest.raises(InvalidResponseError, match="result: a JSON object"):
        await store_response(
            env.db,
            window,
            call="list",
            tool="t",
            arguments={},
            result=[],  # pyright: ignore[reportArgumentType]
        )
    with pytest.raises(ResponseTooLargeError):
        await store_response(
            env.db,
            window,
            call="list",
            tool="t",
            arguments={},
            result=page("x" * 100),
            max_size=64,
        )
    with pytest.raises(WindowNotFoundError):
        await store_response(env.db, uuid.uuid4(), call="list", tool="t", arguments={}, result={})

    cur = await env.db.execute(
        "INSERT INTO source (workspace_id, owner_id, name, kind) VALUES (%s, %s, 'notes', 'push')"
        " RETURNING id",
        (env.target.workspace_id, env.target.owner_id),
    )
    push = await cur.fetchone()
    assert push is not None
    cur = await env.db.execute(
        "INSERT INTO stream (workspace_id, owner_id, source_id, key) VALUES (%s, %s, %s, 'x')"
        " RETURNING id",
        (env.target.workspace_id, env.target.owner_id, push[0]),
    )
    stream = await cur.fetchone()
    assert stream is not None
    with pytest.raises(NotAnMcpSourceError):
        await begin_window(
            env.db,
            workspace_id=env.target.workspace_id,
            owner_id=env.target.owner_id,
            source_id=push[0],
            stream_id=stream[0],
            window=WINDOW_1,
            recipe_digest=recipe_digest(env.recipe),
            server=SERVER,
        )
    with pytest.raises(NotFoundError):
        await begin_window(
            env.db,
            workspace_id=env.target.workspace_id,
            owner_id=env.target.owner_id,
            source_id=env.target.source_id,
            stream_id=stream[0],
            window=WINDOW_1,
            recipe_digest=recipe_digest(env.recipe),
            server=SERVER,
        )


async def test_the_database_checks_bodies_and_calls(env: Env) -> None:
    window = await new_window(env)
    stored = await store_response(
        env.db, window, call="list", tool="t", arguments={}, result=page("a")
    )
    ids = (env.target.workspace_id, env.target.owner_id, env.target.source_id)

    with pytest.raises(errors.CheckViolation, match="raw_body_hash_check"):
        await env.db.execute(
            "INSERT INTO raw_body (workspace_id, owner_id, source_id, hash, body)"
            " VALUES (%s, %s, %s, %s, 'other bytes')",
            (*ids, "0" * 64),
        )
    with pytest.raises(errors.CheckViolation, match="raw_response_item_check"):
        await env.db.execute("UPDATE raw_response SET item_id = 'x' WHERE id = %s", (stored.id,))
    with pytest.raises(errors.ForeignKeyViolation):
        await env.db.execute(
            "UPDATE raw_response SET body_hash = %s WHERE id = %s", ("f" * 64, stored.id)
        )
    with pytest.raises(errors.CheckViolation, match="raw_window_bounds_check"):
        await env.db.execute(
            "UPDATE raw_window SET window_to = window_from WHERE id = %s", (window,)
        )


async def test_a_parsed_window_takes_no_more_calls(env: Env) -> None:
    await sync(env, FakeClient(FakeHearth()))
    window = await scalar(env.db, "SELECT id FROM raw_window")

    with pytest.raises(WindowClosedError):
        await store_response(env.db, window, call="list", tool="t", arguments={}, result=page(""))


async def test_a_later_attempt_at_a_window_replaces_the_earlier_ones(env: Env) -> None:
    abandoned = await new_window(env, WINDOW_2)  # a run that died before it parsed
    await store_response(env.db, abandoned, call="list", tool="t", arguments={}, result=page("1"))
    server = FakeHearth()
    server.threads[0].replies.clear()  # the first attempt sees a thread without its reply
    await sync(env, FakeClient(server))
    first = await scalar(env.db, "SELECT id FROM raw_window WHERE parsed_at IS NOT NULL")

    await sync(env, FakeClient(FakeHearth()))  # the job's retry: the same window again

    remaining = await rows(env.db, "SELECT id FROM raw_window")
    assert len(remaining) == 1
    assert remaining[0][0] not in (first, abandoned)
    assert (
        await scalar(
            env.db,
            "SELECT count(*) FROM raw_body b WHERE NOT EXISTS (SELECT 1 FROM raw_response r"
            " WHERE r.source_id = b.source_id AND r.body_hash = b.hash)",
        )
        == 0
    )


# --- Replay --------------------------------------------------------------------------------------


async def test_replay_after_a_parser_change_updates_records_without_calling_the_source(
    env: Env,
) -> None:
    client = FakeClient(FakeHearth())
    await sync(env, client)
    calls = client.calls
    assert all(a.startswith("U") for names in (await authors(env)).values() for a in names)
    stream_before = await rows(env.db, "SELECT checked_until, checked_from FROM stream")

    result = await run_replay(env, parser=hearth_parser(names=True))

    assert client.calls == calls  # zero calls to the source
    found = await authors(env)
    assert found[f"{CHANNEL}/{FakeHearth().threads[0].root.ts}"] == ["Ann Example", "Bo Sample"]
    assert all(not a.startswith("U") for names in found.values() for a in names)
    assert (result.windows, result.records, result.updated, result.unchanged) == (1, 4, 4, 0)
    assert await rows(env.db, "SELECT checked_until, checked_from FROM stream") == stream_before
    report = await scalar(env.db, "SELECT report FROM run WHERE kind = 'replay'")
    assert report["counts"]["records_updated"] == 4
    assert report["counts"]["replay_windows"] == 1


async def test_replay_with_the_same_parser_writes_nothing(env: Env) -> None:
    server = FakeHearth()
    client = FakeClient(server)
    await sync(env, client)
    server.threads[3].replies.append(late_reply())
    await sync(env, client, WINDOW_2)  # the overlap: the last thread again, with a new reply
    # A thread deleted at the source, seen as a tombstone.
    deleted = FakeHearth().threads[1].root.ts
    await ingest(
        env.db,
        env.access,
        "acme",
        "hearth",
        Batch(records=[RecordIn(kind="thread", external_id=f"{CHANNEL}/{deleted}", deleted=True)]),
    )
    before = await versions(env.db)

    result = await run_replay(env)

    assert await versions(env.db) == before
    assert result.windows == 2
    # The last thread is in both windows: only the later one's version is sent.
    assert (result.superseded, result.kept_deleted, result.unchanged) == (1, 1, 3)
    assert client.calls == 5 + 2


async def test_replay_of_one_run_leaves_records_a_later_window_holds(env: Env) -> None:
    server = FakeHearth()
    client = FakeClient(server)
    run_1, run_2 = uuid.uuid4(), uuid.uuid4()
    for run in (run_1, run_2):
        await env.db.execute(
            "INSERT INTO run (id, workspace_id, owner_id, source_id, kind)"
            " VALUES (%s, %s, %s, %s, 'sync')",
            (run, env.target.workspace_id, env.target.owner_id, env.target.source_id),
        )
    await sync(env, client, run_id=run_1)
    server.threads[3].replies.append(late_reply())
    await sync(env, client, WINDOW_2, run_id=run_2)
    last = f"{CHANNEL}/{server.threads[3].root.ts}"

    result = await run_replay(env, parser=hearth_parser(names=True), run_id=run_1)

    found = await authors(env)
    assert found[last] == ["U0004", "U0001", "U0002"]  # still the later window's, not run 1's
    assert found[f"{CHANNEL}/{server.threads[0].root.ts}"] == ["Ann Example", "Bo Sample"]
    assert (result.windows, result.superseded, result.updated) == (1, 1, 3)


async def test_replay_completes_a_window_a_buggy_parser_left_incomplete(env: Env) -> None:
    client = FakeClient(FakeHearth())
    done = await sync(env, client, parser=hearth_parser(count_root=True))
    assert not done.complete
    assert await rows(env.db, "SELECT checked_until FROM stream") == [(None,)]

    result = await run_replay(env)

    assert result.windows_complete == 1
    assert await rows(env.db, "SELECT checked_until, checked_from FROM stream") == [
        (WINDOW_1.end, WINDOW_1.start)
    ]
    assert await scalar(env.db, "SELECT complete FROM raw_window") is True
    assert client.calls == 5


async def test_a_failing_parser_stops_replay_before_it_writes(env: Env) -> None:
    await sync(env, FakeClient(FakeHearth()))
    before = await versions(env.db)

    def broken(window: WindowInput) -> ParsedWindow:
        raise KeyError("messages")

    with pytest.raises(ReplayError, match="KeyError"):
        await run_replay(env, parser=broken)
    assert await versions(env.db) == before

    other = env.recipe.model_copy(
        update={"server": env.recipe.server.model_copy(update={"name": "another-mcp"})}
    )
    with pytest.raises(ReplayError, match="another-mcp"):
        await replay(env.db, env.access, "acme", "hearth", recipe=other, parser=hearth_parser())
    assert await versions(env.db) == before


async def test_replay_waits_for_a_running_job_of_the_source(env: Env) -> None:
    from memini.jobs import source_lock_key

    await env.other.execute(
        "SELECT pg_advisory_lock(%s, %s)", source_lock_key(env.target.source_id)
    )
    with pytest.raises(SourceBusyError):
        await run_replay(env)
    await env.other.execute(
        "SELECT pg_advisory_unlock(%s, %s)", source_lock_key(env.target.source_id)
    )
    with pytest.raises(NotFoundError):
        await run_replay(env, stream="C9999")
    assert (await run_replay(env)).windows == 0  # nothing stored yet: nothing to do


# --- Retention and forget ------------------------------------------------------------------------


async def test_retention_deletes_the_windows_that_ended_before_the_cutoff(env: Env) -> None:
    server = FakeHearth()
    client = FakeClient(server)
    await sync(env, client)
    server.threads[3].replies.append(late_reply())
    await sync(env, client, WINDOW_2)
    responses_2 = await scalar(
        env.db,
        "SELECT count(*) FROM raw_response r JOIN raw_window w ON w.id = r.window_id"
        " WHERE w.window_from = %s",
        WINDOW_2.start,
    )

    deleted = await delete_raw_responses_before(
        env.db, source_id=env.target.source_id, before=WINDOW_1.end
    )

    assert (deleted.windows, deleted.responses) == (1, 5)
    assert await rows(env.db, "SELECT window_from FROM raw_window") == [(WINDOW_2.start,)]
    assert await scalar(env.db, "SELECT count(*) FROM raw_response") == responses_2
    assert (
        await scalar(
            env.db,
            "SELECT count(*) FROM raw_body b WHERE NOT EXISTS (SELECT 1 FROM raw_response r"
            " WHERE r.source_id = b.source_id AND r.body_hash = b.hash)",
        )
        == 0
    )
    nothing = await delete_raw_responses_before(
        env.db, source_id=env.target.source_id, before=T0 - DAY
    )
    assert nothing.windows == 0


async def test_forget_removes_the_responses_of_a_record(env: Env) -> None:
    server = FakeHearth()
    await sync(env, FakeClient(server))
    keys = server.threads[2].root.ts  # Cy's thread, with the token in a reply
    forgotten = rid(env, keys)
    held = await rows(
        env.db,
        "SELECT seq, call FROM raw_response WHERE %s = ANY(record_ids) ORDER BY seq",
        forgotten,
    )
    assert [call for _, call in held] == ["list", "detail"]
    unparsed = await new_window(env, WINDOW_2)  # a run in flight: no attribution yet
    await store_response(env.db, unparsed, call="list", tool="t", arguments={}, result=page("x"))

    async with env.db.transaction():  # as forget does: the record and its responses together
        await env.db.execute("DELETE FROM record WHERE id = %s", (forgotten,))
        deleted = await delete_raw_responses_for_records(
            env.db, source_id=env.target.source_id, record_ids=[forgotten]
        )

    assert (deleted.pruned, deleted.windows) == (2, 1)
    assert await rows(
        env.db, "SELECT seq FROM raw_response WHERE body_hash IS NULL ORDER BY seq"
    ) == [(seq,) for seq, _ in held]
    assert await scalar(env.db, "SELECT pruned_at IS NOT NULL FROM raw_window") is True
    bodies = "".join(
        value for (value,) in await rows(env.db, "SELECT convert_from(body, 'UTF8') FROM raw_body")
    )
    assert "Who has the shelf keys?" not in bodies
    assert "They are in the drawer." not in bodies

    # Replay does not bring it back, and claims the pruned window incomplete.
    result = await run_replay(env, parser=hearth_parser(names=True))
    assert await scalar(env.db, "SELECT count(*) FROM record WHERE id = %s", forgotten) == 0
    assert result.windows_complete == 0
    assert await scalar(env.db, "SELECT complete FROM raw_window") is False
    found = await authors(env)
    # The thread whose list page went with it is still built from its detail call.
    assert found[f"{CHANNEL}/{server.threads[0].root.ts}"] == ["Ann Example", "Bo Sample"]


async def test_ingest_window_claims_a_snapshot_when_the_recipe_says_so(env: Env) -> None:
    server = FakeHearth()
    await sync(env, FakeClient(server))
    gone = server.threads.pop(1)  # deleted at the source
    snapshot = env.recipe.model_copy(
        update={"stream": env.recipe.stream.model_copy(update={"snapshot": True})}
    )
    abandoned = await new_window(env, WINDOW_1)  # a run that died before it parsed

    done = await sync_window(
        env.db,
        FakeClient(server),
        env.target,
        WINDOW_1,
        recipe=snapshot,
        parser=hearth_parser(),
        access=env.access,
    )

    assert (done.complete, done.reconciled) == (True, 1)
    assert (
        await scalar(
            env.db,
            "SELECT deleted_at IS NOT NULL FROM record WHERE id = %s",
            rid(env, gone.root.ts),
        )
        is True
    )
    # The first parse of a window deletes the stream's earlier attempts at it and the windows
    # never parsed.
    assert await scalar(env.db, "SELECT count(*) FROM raw_window") == 1
    assert await scalar(env.db, "SELECT count(*) FROM raw_window WHERE id = %s", abandoned) == 0
    with pytest.raises(WindowNotFoundError):
        await ingest_window(
            env.db, env.access, uuid.uuid4(), recipe=env.recipe, parser=hearth_parser()
        )


async def test_forget_through_the_removal_core_removes_the_record_s_responses(env: Env) -> None:
    """memini forget (the removal core) reaches the raw response store: the bodies that held the
    record's text are gone, as tests/test_forget_db.py expects of every table."""
    server = FakeHearth()
    await sync(env, FakeClient(server))
    forgotten = rid(env, server.threads[2].root.ts)  # Cy's thread

    done = await forget_record(env.db, env.access, "acme", ForgetTarget(record_id=forgotten))

    assert done.record_id == forgotten
    assert (
        await rows(
            env.db,
            "SELECT seq FROM raw_response WHERE %s = ANY(record_ids) AND body_hash IS NOT NULL",
            forgotten,
        )
        == []
    )
    bodies = "".join(
        value for (value,) in await rows(env.db, "SELECT convert_from(body, 'UTF8') FROM raw_body")
    )
    assert "Who has the shelf keys?" not in bodies
    assert "They are in the drawer." not in bodies


async def test_a_window_ingested_with_extraction_enqueues_extract_jobs(env: Env) -> None:
    """An executor sets ``extract`` when an LLM provider is configured: the threads a window
    stores get an ``extract`` job each, as pushed ones do; without it, none."""
    server = FakeHearth()
    await sync(env, FakeClient(server), extract=True)

    records = await rows(env.db, "SELECT id FROM record WHERE kind = 'thread' ORDER BY id")
    jobs = await rows(
        env.db,
        "SELECT record_id FROM (SELECT args->>'record_id' AS record_id FROM job"
        " WHERE kind = %s) AS j ORDER BY record_id",
        EXTRACT_KIND,
    )
    assert records
    assert jobs == records


async def test_a_window_ingested_without_extraction_enqueues_none(env: Env) -> None:
    server = FakeHearth()
    await sync(env, FakeClient(server))

    assert await scalar(env.db, "SELECT count(*) FROM job WHERE kind = %s", EXTRACT_KIND) == 0
