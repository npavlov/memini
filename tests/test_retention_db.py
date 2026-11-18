"""Retention (memini.removal.expire_source) against a real Postgres: expired records go with their
chunks, in batches, counted in the run report; records inside the retention stay; evidence of an
expired record reads as expired; the scheduler enqueues a retention job per source; a git source
loses only old tombstones.

Acceptance of memini-hoq: with a clock moved past the retention period, records and chunks are gone
and the run report counts them. Every value is synthetic (tests/ingest_helpers.py,
tests/git_helpers.py).
"""

import shutil
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, LiteralString

import anyio
import pytest
from psycopg import AsyncConnection
from psycopg.rows import TupleRow, dict_row

from git_helpers import SourceRepo
from ingest_helpers import DAY, thread, tombstone
from memini.auth import Principal, WriteAccess, authorize_read
from memini.cli.serve import background_jobs
from memini.db import Pool, connect, open_pool
from memini.embeddings import FakeEmbedder
from memini.git import GitOptions, MirrorManager
from memini.gitsync import GIT_SYNC, git_sync_handler
from memini.ids import record_id
from memini.ingest import Batch, IngestResult, PayloadIndexer, RecordIn, ingest
from memini.jobs import Job, Scheduler, enqueue_due_retention, execute_job
from memini.knowledge import EvidenceRef, NewItem, get_item, record_item
from memini.removal import RETENTION, expire_source, retention_handler
from memini.runs import wait_for_run_writes
from memini.sources import (
    NewGitSource,
    NewPushSource,
    SourceUpdate,
    create_git_source,
    create_push_source,
    update_source,
)
from memini.workspaces import create_workspace, ensure_principal

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

type Conn = AsyncConnection[TupleRow]

RETENTION_DAYS = 30


@dataclass(frozen=True)
class Env:
    url: str
    db: Conn
    pool: Pool
    owner: uuid.UUID
    access: WriteAccess
    acme: uuid.UUID
    chat: uuid.UUID
    """A push source of acme that keeps records RETENTION_DAYS days."""
    notes: uuid.UUID
    """A push source of acme that keeps its records."""
    now: datetime
    embedder: FakeEmbedder


@pytest.fixture
async def env(migrated_database_url: str) -> AsyncIterator[Env]:
    async with (
        connect(migrated_database_url, autocommit=True) as db,
        open_pool(migrated_database_url, min_size=1, max_size=6, timeout=5) as pool,
    ):
        owner = await ensure_principal(db, "owner")
        acme = await create_workspace(db, owner_id=owner, name="acme")
        access = WriteAccess.for_owner(owner)
        chat = await create_push_source(
            db, access, "acme", NewPushSource(name="chat", retention_days=RETENTION_DAYS)
        )
        notes = await create_push_source(db, access, "acme", NewPushSource(name="notes"))
        now = (await (await db.execute("SELECT now()")).fetchone() or (None,))[0]
        assert isinstance(now, datetime)
        yield Env(
            migrated_database_url,
            db,
            pool,
            owner,
            access,
            acme.id,
            chat.id,
            notes.id,
            now.astimezone(UTC),
            FakeEmbedder(),
        )


async def send(env: Env, *records: RecordIn, source: str = "chat") -> IngestResult:
    result = await ingest(
        env.db,
        env.access,
        "acme",
        source,
        Batch(records=records),
        runs=env.pool,
        indexer=PayloadIndexer(env.embedder),
    )
    await wait_for_run_writes()
    return result


async def rows(db: Conn, query: LiteralString, *params: Any) -> list[tuple[Any, ...]]:
    cur = await db.execute(query, params)
    return [tuple(row) for row in await cur.fetchall()]


async def scalar(db: Conn, query: LiteralString, *params: Any) -> Any:
    found = await rows(db, query, *params)
    assert len(found) == 1
    return found[0][0]


def rid(source: uuid.UUID, external_id: str, kind: str = "thread") -> str:
    return record_id(source, kind, external_id)  # pyright: ignore[reportArgumentType]


async def records_of(env: Env, source: uuid.UUID) -> list[str]:
    found = await rows(
        env.db, "SELECT external_id FROM record WHERE source_id = %s ORDER BY 1", source
    )
    return [external_id for (external_id,) in found]


async def chunks_of(env: Env, *record_ids: str) -> int:
    count = await scalar(
        env.db, "SELECT count(*) FROM chunk WHERE record_id = ANY(%s::text[])", list(record_ids)
    )
    return int(count)


async def versions(env: Env, record: str) -> dict[str, str]:
    """xmin of the record and its chunks: it changes exactly when a row is written."""
    found = await rows(
        env.db,
        "SELECT 'record:' || id, xmin::text FROM record WHERE id = %s"
        " UNION ALL SELECT 'chunk:' || id, xmin::text FROM chunk WHERE record_id = %s",
        record,
        record,
    )
    return {key: value for key, value in found}


async def cite(env: Env, record: str, at: datetime, anchor: str = "msg-1") -> uuid.UUID:
    item = await record_item(
        env.db,
        env.access,
        "acme",
        NewItem(
            kind="decision",
            statement="The greenhouse is watered at six.",
            decided_at=at,
            evidence=[EvidenceRef(record_id=record, anchor=anchor)],
        ),
    )
    return item.id


async def evidence_state(env: Env, item: uuid.UUID) -> str:
    principal = Principal(principal_id=env.owner, token_id=uuid.uuid4(), scopes=frozenset())
    chain = await get_item(env.db, authorize_read(principal), item)
    return chain.item.evidence[0].record_state


def job_of(env: Env, source: uuid.UUID, kind: str = RETENTION) -> Job:
    return Job(
        id=uuid.uuid4(),
        workspace_id=env.acme,
        owner_id=env.owner,
        source_id=source,
        stream_id=None,
        kind=kind,
        args={},
        attempt=1,
        max_attempts=5,
        worker="test",
    )


async def last_run(env: Env, source: uuid.UUID, kind: str) -> Mapping[str, Any]:
    async with env.db.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM run WHERE source_id = %s AND kind = %s ORDER BY started_at DESC LIMIT 1",
            (source, kind),
        )
        run = await cur.fetchone()
    assert run is not None
    return run


# --- The acceptance ------------------------------------------------------------------------------


async def test_records_past_retention_go_with_their_chunks_and_the_run_counts_them(
    env: Env,
) -> None:
    old = env.now - 40 * DAY
    recent = env.now - 10 * DAY
    await send(
        env,
        thread("garden/1", old),
        thread("garden/2", old + DAY),
        thread("garden/3", old + 2 * DAY),
        thread("garden/4", recent),
    )
    await send(env, thread("shed/1", old), source="notes")  # a source without retention
    expired = [rid(env.chat, f"garden/{n}") for n in (1, 2, 3)]
    item = await cite(env, expired[0], old)
    chunks = await chunks_of(env, *expired)
    assert chunks > 0
    kept = await versions(env, rid(env.chat, "garden/4"))
    assert len(kept) > 1

    await execute_job(
        job_of(env, env.chat), {RETENTION: retention_handler(env.pool, batch_size=2)}, env.pool
    )
    await wait_for_run_writes()

    assert await records_of(env, env.chat) == ["garden/4"]
    assert await chunks_of(env, *expired) == 0
    assert await records_of(env, env.notes) == ["shed/1"]
    # The record inside the retention, and its chunks, were not written at all.
    assert await versions(env, rid(env.chat, "garden/4")) == kept

    run = await last_run(env, env.chat, RETENTION)
    assert run["status"] == "succeeded"
    counts = run["report"]["counts"]
    assert counts["records_expired"] == 3
    assert counts["records_expired_cited"] == 1
    assert counts["chunks_deleted"] == chunks
    assert counts["symbols_deleted"] == 0
    assert run["report"]["details"]["retention"]["retention_days"] == RETENTION_DAYS
    assert run["report"]["details"]["retention"]["rule"] == "occurred_at"

    # The knowledge item stays; its evidence reads as expired, and only the cited record left a
    # trace.
    assert await evidence_state(env, item) == "expired"
    assert await rows(env.db, "SELECT record_id, source_id, record_kind FROM expired_record") == [
        (expired[0], env.chat, "thread")
    ]


async def test_a_clock_moved_past_the_retention_expires_what_was_inside_it(env: Env) -> None:
    await send(env, thread("garden/1", env.now - 10 * DAY), thread("garden/2", env.now - DAY))
    chunks = await chunks_of(env, rid(env.chat, "garden/1"))

    today = await expire_source(env.db, env.chat)
    later = await expire_source(env.db, env.chat, as_of=env.now + 25 * DAY, batch_size=1)

    assert (today.removed.records, today.batches) == (0, 0)
    assert today.cutoff is not None
    assert abs(today.cutoff - (env.now - RETENTION_DAYS * DAY)) < timedelta(minutes=5)
    assert (later.removed.records, later.removed.chunks, later.batches) == (1, chunks, 1)
    assert later.cutoff == env.now + 25 * DAY - RETENTION_DAYS * DAY
    assert await records_of(env, env.chat) == ["garden/2"]


async def test_tombstones_expire_by_their_time_too(env: Env) -> None:
    await send(env, thread("garden/1", env.now - 40 * DAY), thread("garden/2", env.now - DAY))
    await send(env, tombstone("garden/1"), tombstone("garden/2"))

    result = await expire_source(env.db, env.chat)

    assert result.removed.records == 1
    assert await records_of(env, env.chat) == ["garden/2"]


async def test_a_source_without_retention_keeps_everything(env: Env) -> None:
    await send(env, thread("shed/1", env.now - 4000 * DAY), source="notes")

    result = await expire_source(env.db, env.notes, as_of=env.now + 10_000 * DAY)

    assert (result.retention_days, result.cutoff, result.removed.records) == (None, None, 0)
    assert await records_of(env, env.notes) == ["shed/1"]
    with pytest.raises(LookupError):
        await expire_source(env.db, uuid.uuid4())


async def test_a_record_an_ingest_batch_holds_waits_for_the_next_pass(env: Env) -> None:
    await send(env, thread("garden/1", env.now - 40 * DAY), thread("garden/2", env.now - 40 * DAY))

    async with connect(env.url) as holder, holder.transaction():
        await holder.execute(
            "SELECT id FROM record WHERE id = %s FOR NO KEY UPDATE", (rid(env.chat, "garden/1"),)
        )
        first = await expire_source(env.db, env.chat)
    second = await expire_source(env.db, env.chat)

    assert (first.removed.records, second.removed.records) == (1, 1)
    assert await records_of(env, env.chat) == []


async def test_expired_differs_from_forgotten_and_gone(env: Env) -> None:
    """A record that comes back reads as stored again."""
    old = env.now - 40 * DAY
    await send(env, thread("garden/1", old))
    item = await cite(env, rid(env.chat, "garden/1"), old)
    await expire_source(env.db, env.chat)
    assert await evidence_state(env, item) == "expired"

    await send(env, thread("garden/1", env.now - DAY))  # sent again, inside the retention
    assert await evidence_state(env, item) == "stored"


# --- Scheduling ----------------------------------------------------------------------------------


async def test_the_scheduler_enqueues_one_retention_job_per_source_with_a_retention(
    env: Env,
) -> None:
    every = timedelta(hours=2)

    first = await enqueue_due_retention(env.db, every=every)
    again = await enqueue_due_retention(env.db, every=every)
    jobs = await rows(
        env.db, "SELECT kind, source_id, stream_id, dedupe_key, status FROM job ORDER BY created_at"
    )
    next_at = await scalar(env.db, "SELECT retention_next_at FROM source WHERE id = %s", env.chat)

    assert (first.streams, first.jobs, again.streams, again.jobs) == (1, 1, 0, 0)
    assert jobs == [("retention", env.chat, None, str(env.chat), "queued")]
    assert abs(next_at - (env.now + every)) < timedelta(minutes=5)
    assert (
        await scalar(env.db, "SELECT retention_next_at FROM source WHERE id = %s", env.notes)
        is None
    )

    # Due again while its job still waits: the time moves on, no second job.
    await env.db.execute("UPDATE source SET retention_next_at = now() - interval '1 second'")
    merged = await enqueue_due_retention(env.db, every=every)
    assert (merged.streams, merged.jobs) == (1, 0)

    # A changed retention applies at the next look.
    await update_source(env.db, env.access, "acme", "chat", SourceUpdate(retention_days=7))
    assert (
        await scalar(env.db, "SELECT retention_next_at FROM source WHERE id = %s", env.chat) is None
    )
    await update_source(env.db, env.access, "acme", "notes", SourceUpdate(retention_days=90))
    await env.db.execute("UPDATE job SET status = 'succeeded', finished_at = now()")
    assert (await Scheduler(env.pool, sync_kinds={}, retention_every=every).tick()) == 2

    with pytest.raises(ValueError, match="positive"):
        await enqueue_due_retention(env.db, every=timedelta(0))


async def test_serve_runs_retention_on_its_own(env: Env) -> None:
    await send(env, thread("garden/1", env.now - 40 * DAY), thread("garden/2", env.now - DAY))
    async with background_jobs(env.pool, {RETENTION: retention_handler(env.pool)}):
        with anyio.fail_after(15):
            while await records_of(env, env.chat) != ["garden/2"]:
                await anyio.sleep(0.1)
    await wait_for_run_writes()

    run = await last_run(env, env.chat, RETENTION)
    assert (run["status"], run["report"]["counts"]["records_expired"]) == ("succeeded", 1)


# --- A git source --------------------------------------------------------------------------------

MAIN = "refs/heads/main"
KEPT = '''def water(plants):
    """Water the plants."""
    return [plant for plant in plants]
'''
REMOVED = '''def prune(plants):
    """Prune the plants."""
    return plants[:1]
'''


@pytest.mark.git
@pytest.mark.skipif(shutil.which("git") is None, reason="needs the git executable")
async def test_a_git_source_loses_old_tombstones_and_keeps_its_tree(
    env: Env, tmp_path: Path
) -> None:
    repos = tmp_path / "sources"
    repo = SourceRepo(repos / "shop")
    # The commits are dated January 2026: older than the retention, and the files stay.
    repo.commit({"src/water.py": KEPT, "src/prune.py": REMOVED, "src/old.py": REMOVED})
    options = GitOptions(
        mirror_dir=tmp_path / "mirrors", local_roots=(repos,), fetch_timeout=30, command_timeout=30
    )
    source = await create_git_source(
        env.db,
        env.access,
        "acme",
        NewGitSource(
            name="shop", location=str(repo.path), refs=["main"], retention_days=RETENTION_DAYS
        ),
        options=options,
    )
    stream = await scalar(env.db, "SELECT id FROM stream WHERE source_id = %s", source.id)
    handler = git_sync_handler(env.pool, MirrorManager(options), env.embedder, retention_batch=1)

    async def sync() -> Mapping[str, Any]:
        job = Job(
            id=uuid.uuid4(),
            workspace_id=env.acme,
            owner_id=env.owner,
            source_id=source.id,
            stream_id=stream,
            kind=GIT_SYNC,
            args={},
            attempt=1,
            max_attempts=5,
            worker="test",
        )
        await execute_job(job, {GIT_SYNC: handler}, env.pool)
        await wait_for_run_writes()
        run = await last_run(env, source.id, GIT_SYNC)
        assert run["status"] == "succeeded", run
        return run

    first = await sync()
    repo.commit({"src/prune.py": None, "src/old.py": None})
    deleted = await sync()
    # One tombstone was deleted long ago, the other just now.
    await env.db.execute(
        "UPDATE record SET deleted_at = now() - interval '40 days'"
        " WHERE source_id = %s AND external_id = %s",
        (source.id, f"{MAIN}:src/old.py"),
    )
    cited = await cite(
        env,
        rid(source.id, f"{MAIN}:src/water.py", "code_file"),
        env.now,
        anchor=repo.git("rev-parse", "HEAD"),
    )
    last = await sync()

    steps = [step["name"] for step in last["report"]["steps"]]
    assert [step["name"] for step in first["report"]["steps"]][-1] == "retention"
    assert deleted["report"]["counts"]["records_expired"] == 0
    assert steps == ["fetch", "diff", "index", "retention"]
    assert last["report"]["counts"]["records_expired"] == 1
    assert last["report"]["details"]["retention"]["rule"] == "deleted_at"
    assert await records_of(env, source.id) == [f"{MAIN}:src/prune.py", f"{MAIN}:src/water.py"]
    assert await evidence_state(env, cited) == "stored"

    # Directly, with the clock moved on: the other tombstone goes, the tree stays.
    result = await expire_source(env.db, source.id, as_of=env.now + 31 * DAY)
    assert result.removed.records == 1
    assert await records_of(env, source.id) == [f"{MAIN}:src/water.py"]
