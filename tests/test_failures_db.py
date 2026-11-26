"""Failure tests (memini-69f): a worker killed mid-sync, the database restarted mid-batch, sources
that hang.

Acceptance: in every case no checkpoint advances without its data, jobs resume, and the index stays
consistent. Each test breaks one thing for real (a worker process gets SIGKILL, the database server
is killed and started again, a server accepts a connection and never answers) and then checks the
base with :func:`consistency_helpers.assert_consistent`: every record agrees with its chunks, every
checkpoint with its records, nothing is left half written, and no advisory lock is left behind.

- The worker that is killed is a process of the test's own (tests/failure_worker.py), never the
  service.
- The database that is crashed is a server of the test's own, in a Docker container started for
  this module (tests/disposable_postgres.py): the shared development database and the service
  container of CI are never stopped. Without Docker those tests skip.
- The sources that hang listen on 127.0.0.1 and serve synthetic data: a fake MCP server
  (tests/fake_mcp.py) and a repository over git's dumb HTTP protocol. Nothing reaches the network.

Leases, timeouts and backoffs are seconds or less: the module takes about a minute, most of it
spent waiting for leases and timeouts to run out.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Iterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Final, LiteralString

import anyio
import httpx2
import psycopg
import pytest
import uvicorn
from mcp.client import session as mcp_client_session
from psycopg import AsyncConnection, sql
from psycopg.rows import TupleRow, dict_row
from pydantic import SecretStr
from starlette.types import ASGIApp, Receive, Scope, Send

from consistency_helpers import advisory_locks, assert_consistent, consistency_problems
from disposable_postgres import DisposablePostgres, free_port, start_postgres
from fake_mcp import FakeMcp, recent_threads
from git_helpers import SourceRepo
from hearth_helpers import CHANNEL, HEARTH_RECIPE_PATH, FakeHearth, hearth_parser
from memini.api import create_app
from memini.auth import WriteAccess, create_token
from memini.config import Settings
from memini.credentials import CredentialKey, Keyring
from memini.db import Pool, connect, database_check, migrate, open_pool
from memini.embeddings import FakeEmbedder
from memini.executors.budget import RetryPolicy
from memini.executors.egress import EgressPolicy
from memini.executors.service import MCP_SYNC, ServiceOptions, mcp_sync_handler
from memini.executors.source import HttpEndpoint, StdioEndpoint
from memini.git import GitOptions, MirrorManager
from memini.gitsync import GIT_SYNC, SyncLimits, git_sync_handler, read_pin
from memini.ingest import PayloadIndexer
from memini.jobs import Handler, Job, Scheduler, Worker, enqueue, execute_job, stream_dedupe_key
from memini.jobs import RetryPolicy as JobRetry
from memini.recipes import register_parser, unregister_parser
from memini.runs import wait_for_run_writes
from memini.sources import NewGitSource, NewMcpSource, create_git_source, create_mcp_source
from memini.workspaces import create_workspace, ensure_principal

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs the git executable")

type Conn = AsyncConnection[TupleRow]
type Row = dict[str, Any]

FAST_POLL: Final = timedelta(milliseconds=20)
FAILURE_WORKER: Final = Path(__file__).parent / "failure_worker.py"
MAIN: Final = "refs/heads/main"
MAX_BLOB: Final = 4096
KEY: Final = b"k" * 32


# --- Shared --------------------------------------------------------------------------------------


async def rows(db: Conn, query: LiteralString, *params: object) -> list[Row]:
    async with db.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        return list(await cur.fetchall())


async def scalar(db: Conn, query: LiteralString, *params: object) -> Any:
    cur = await db.execute(query, params)
    row = await cur.fetchone()
    assert row is not None
    return row[0]


async def eventually(
    check: Callable[[], Awaitable[bool]], *, timeout: float = 20.0, what: str = "a condition"
) -> None:
    """Wait until ``check`` passes; fail the test after ``timeout`` seconds."""
    try:
        with anyio.fail_after(timeout):
            while not await check():
                await anyio.sleep(0.05)
    except TimeoutError:
        pytest.fail(f"{what} did not happen within {timeout} seconds")


async def job_row(db: Conn, job_id: uuid.UUID) -> Row:
    (row,) = await rows(db, "SELECT * FROM job WHERE id = %s", job_id)
    return row


async def job_is(db: Conn, job_id: uuid.UUID, *statuses: str) -> bool:
    return (await job_row(db, job_id))["status"] in statuses


async def runs_of(db: Conn, stream: uuid.UUID) -> list[Row]:
    return await rows(db, "SELECT * FROM run WHERE stream_id = %s ORDER BY started_at", stream)


def steps(run: Row) -> list[tuple[str, str]]:
    return [(step["name"], step["status"]) for step in run["report"]["steps"]]


def file_counts(run: Row) -> dict[str, int]:
    counts: dict[str, int] = run["report"]["counts"]
    return {name: count for name, count in counts.items() if name.startswith("files_")}


async def no_locks_held(db: Conn) -> bool:
    return await advisory_locks(db) == []


@asynccontextmanager
async def running(*services: Worker | Scheduler, timeout: float = 60) -> AsyncGenerator[None]:
    """Run workers and schedulers for the block, as ``memini serve`` does; stop and await them at
    its end, and write the pending run reports."""
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


def worker(pool: Pool, handlers: dict[str, Handler], **changes: Any) -> Worker:
    settings: dict[str, Any] = {
        "lease": timedelta(seconds=5),
        "poll_interval": FAST_POLL,
        "grace": timedelta(seconds=2),
        "retry": JobRetry(base=timedelta(seconds=30)),
    }
    return Worker(pool, handlers, **(settings | changes))


async def enqueue_sync(
    db: Conn,
    *,
    workspace: uuid.UUID,
    owner: uuid.UUID,
    kind: str,
    source: uuid.UUID,
    stream: uuid.UUID,
    max_attempts: int = 5,
) -> uuid.UUID:
    enqueued = await enqueue(
        db,
        workspace_id=workspace,
        owner_id=owner,
        kind=kind,
        source_id=source,
        stream_id=stream,
        dedupe_key=stream_dedupe_key(stream),
        max_attempts=max_attempts,
    )
    assert enqueued.created
    return enqueued.id


# --- Git fixtures --------------------------------------------------------------------------------


def function(name: str, value: int) -> str:
    """A Python function long enough to be a chunk of its own."""
    return (
        f"def {name}(items):\n"
        f'    """Return the {name} of the items, plus {value}."""\n'
        "    total = 0\n"
        "    for item in items:\n"
        "        total += item\n"
        "    if total < 0:\n"
        "        total = 0\n"
        f"    return total + {value}\n"
    )


def blob(repo: SourceRepo, path: str) -> str:
    return repo.git("rev-parse", f"main:{path}")


@dataclass
class GitEnv:
    url: str
    pool: Pool
    db: Conn
    options: GitOptions
    mirrors: MirrorManager
    repo: SourceRepo
    owner: uuid.UUID

    @property
    def access(self) -> WriteAccess:
        return WriteAccess.for_owner(self.owner)


@dataclass(frozen=True)
class GitSrc:
    workspace: uuid.UUID
    id: uuid.UUID
    stream: uuid.UUID


@pytest.fixture
async def git_env(migrated_database_url: str, tmp_path: Path) -> AsyncIterator[GitEnv]:
    sources = tmp_path / "sources"
    options = GitOptions(
        mirror_dir=tmp_path / "mirrors",
        local_roots=(sources,),
        fetch_timeout=30,
        command_timeout=30,
        max_blob_size=MAX_BLOB,
    )
    async with (
        open_pool(migrated_database_url, min_size=1, max_size=8, timeout=5) as pool,
        connect(migrated_database_url, autocommit=True) as db,
    ):
        owner = await ensure_principal(db, "owner")
        yield GitEnv(
            migrated_database_url,
            pool,
            db,
            options,
            MirrorManager(options),
            SourceRepo(sources / "shop"),
            owner,
        )


async def add_git_source(
    env: GitEnv, workspace: str, *, location: str | None = None, options: GitOptions | None = None
) -> GitSrc:
    found = await create_workspace(env.db, owner_id=env.owner, name=workspace)
    source = await create_git_source(
        env.db,
        env.access,
        workspace,
        NewGitSource(name="shop", location=location or str(env.repo.path), refs=["main"]),
        options=options or env.options,
    )
    stream = await scalar(env.db, "SELECT id FROM stream WHERE source_id = %s", source.id)
    return GitSrc(found.id, source.id, stream)


async def sync_in_process(
    env: GitEnv, src: GitSrc, embedder: FakeEmbedder, *, limits: SyncLimits
) -> None:
    """One git_sync of the source's stream, run as the worker runs it, uninterrupted."""
    job = Job(
        id=uuid.uuid4(),
        workspace_id=src.workspace,
        owner_id=env.owner,
        source_id=src.id,
        stream_id=src.stream,
        kind=GIT_SYNC,
        args={},
        attempt=1,
        max_attempts=1,
        worker="test",
    )
    handler = git_sync_handler(env.pool, env.mirrors, embedder, limits=limits)
    await execute_job(job, {GIT_SYNC: handler}, env.pool)
    await wait_for_run_writes()


async def stream_state(db: Conn, stream: uuid.UUID) -> tuple[Any, datetime | None]:
    (row,) = await rows(db, "SELECT cursor, checked_until FROM stream WHERE id = %s", stream)
    return row["cursor"], row["checked_until"]


async def file_records(db: Conn, src: GitSrc) -> dict[str, tuple[str, bool]]:
    """path -> (blob id, deleted)."""
    found = await rows(
        db,
        "SELECT external_id, content_hash, deleted_at IS NOT NULL AS deleted FROM record"
        " WHERE source_id = %s",
        src.id,
    )
    return {
        row["external_id"].removeprefix(f"{MAIN}:"): (row["content_hash"], row["deleted"])
        for row in found
    }


async def source_state(db: Conn, src: GitSrc) -> dict[str, Any]:
    """What a sync leaves of a source, without ids or times of its own: its pin, its records and
    their chunks, symbols and vectors, by external id."""
    records = await rows(
        db,
        "SELECT external_id, kind, content_hash, deleted_at IS NOT NULL AS deleted, occurred_at"
        " FROM record WHERE source_id = %s ORDER BY external_id",
        src.id,
    )
    chunks = await rows(
        db,
        "SELECT r.external_id, c.key, c.kind, c.text_hash, c.start_line, c.end_line, c.symbol,"
        " c.embedding::text AS embedding FROM chunk c JOIN record r ON r.id = c.record_id"
        " WHERE c.source_id = %s ORDER BY r.external_id, c.key",
        src.id,
    )
    symbols = await rows(
        db,
        "SELECT r.external_id, s.qualified_name, s.ordinal, s.kind FROM symbol s"
        " JOIN record r ON r.id = s.record_id WHERE s.source_id = %s"
        " ORDER BY r.external_id, s.qualified_name, s.ordinal",
        src.id,
    )
    cursor, _ = await stream_state(db, src.stream)
    return {"pin": cursor, "records": records, "chunks": chunks, "symbols": symbols}


# --- A worker killed mid-sync --------------------------------------------------------------------


@dataclass
class ChildWorker:
    """A worker process of the test's own (tests/failure_worker.py)."""

    process: subprocess.Popen[bytes]
    log: Path
    marker: Path

    def output(self) -> str:
        return self.log.read_text(encoding="utf-8", errors="replace")[-4000:]


@asynccontextmanager
async def child_worker(
    env: GitEnv, tmp_path: Path, *, name: str, hang_at: int, lease: float, batch_files: int
) -> AsyncGenerator[ChildWorker]:
    config = tmp_path / "worker.json"
    marker = tmp_path / "hanging"
    log = tmp_path / "worker.log"
    config.write_text(
        json.dumps(
            {
                "database_url": env.url,
                "mirror_dir": str(env.options.mirror_dir),
                "local_roots": [str(root) for root in env.options.local_roots],
                "max_blob_size": MAX_BLOB,
                "hang_at": hang_at,
                "marker": str(marker),
                "lease": lease,
                "batch_files": batch_files,
                "name": name,
            }
        ),
        encoding="utf-8",
    )
    with log.open("wb") as output:
        process = subprocess.Popen(  # noqa: S603 - this interpreter, a script of the tests
            [sys.executable, str(FAILURE_WORKER), str(config)],
            stdout=output,
            stderr=subprocess.STDOUT,
        )
    child = ChildWorker(process, log, marker)
    try:
        yield child
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)


@needs_git
@pytest.mark.git
async def test_a_worker_killed_mid_sync_keeps_its_pin_and_the_retry_ends_as_an_unbroken_sync(
    git_env: GitEnv, tmp_path: Path
) -> None:
    env = git_env
    repo = env.repo
    one_file_a_batch = SyncLimits(batch_files=1)
    names = [f"f{number}" for number in range(1, 9)]
    old = repo.commit(
        {
            "README.md": "# Shop\n\nA synthetic fixture.\n",
            **{f"src/{name}.py": function(name, 1) for name in names},
        }
    )
    # Two sources of the same repository, in workspaces of their own (vectors are reused within a
    # workspace only): one is interrupted, the other syncs the same history uninterrupted.
    src = await add_git_source(env, "acme")
    reference = await add_git_source(env, "reference")
    for each in (src, reference):
        await sync_in_process(env, each, FakeEmbedder(), limits=one_file_a_batch)
    pinned, checked_until = await stream_state(env.db, src.stream)
    pin = read_pin(pinned)
    assert pin is not None
    assert (pin.commit, pin.partial) == (old, False)

    # Six files change, one is deleted, one is added: seven files, a batch each.
    head = repo.commit(
        {
            **{f"src/{name}.py": function(name, 2) for name in names[:6]},
            "src/f7.py": None,
            "src/f9.py": function("f9", 2),
        }
    )
    job = await enqueue_sync(
        env.db,
        workspace=src.workspace,
        owner=env.owner,
        kind=GIT_SYNC,
        source=src.id,
        stream=src.stream,
    )

    # The worker process commits three batches and hangs in the fourth, inside its transaction.
    async with child_worker(
        env, tmp_path, name="doomed", hang_at=4, lease=3.0, batch_files=1
    ) as child:

        async def hanging() -> bool:
            if child.process.poll() is not None:
                pytest.fail(f"the worker process exited:\n{child.output()}")
            return child.marker.exists()

        await eventually(hanging, timeout=60, what="the worker reaching its fourth batch")
        assert (
            await scalar(
                env.db,
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
                " AND state = 'idle in transaction'",
            )
            >= 1
        )
        assert await stream_state(env.db, src.stream) == (
            {**pinned, "partial": True},
            checked_until,
        )
        child.process.send_signal(signal.SIGKILL)
        assert child.process.wait(timeout=10) == -signal.SIGKILL

    # Its sessions end with it: the open transaction rolls back and the source's lock is free.
    async def sessions_gone() -> bool:
        idle: int = await scalar(
            env.db,
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
            " AND state = 'idle in transaction'",
        )
        return idle == 0 and await no_locks_held(env.db)

    await eventually(sessions_gone, what="the killed worker's sessions ending")
    row = await job_row(env.db, job)
    assert (row["status"], row["attempts"], row["worker"]) == ("running", 1, "doomed/0")

    # The pin did not move and says partial; what committed is whole, the rest as it was.
    assert await stream_state(env.db, src.stream) == ({**pinned, "partial": True}, checked_until)
    files = await file_records(env.db, src)
    for name in names[:3]:
        assert files[f"src/{name}.py"] == (blob(repo, f"src/{name}.py"), False), name
    for name in names[3:7]:  # not reached: as they were at the old pin, f7 still live
        assert files[f"src/{name}.py"] == (repo.git("rev-parse", f"{old}:src/{name}.py"), False)
    assert "src/f9.py" not in files
    await assert_consistent(env.db, workspace_id=src.workspace, mirrors=env.mirrors)

    # Another worker puts the job back once the dead worker's lease has run out, and runs it: it
    # lists the ref in full and writes only what the dead one did not commit.
    rerun = FakeEmbedder()
    handler = git_sync_handler(env.pool, env.mirrors, rerun, limits=one_file_a_batch)
    async with running(worker(env.pool, {GIT_SYNC: handler}, name="survivor")):
        await eventually(lambda: job_is(env.db, job, "succeeded"), what="the job's second run")

    row = await job_row(env.db, job)
    assert row["attempts"] == 2
    assert row["last_error"] == "lease of doomed/0 expired; it died or hung"
    attempts = await runs_of(env.db, src.stream)
    last = attempts[-1]
    assert last["status"] == "succeeded", last["error"]
    assert steps(last) == [("fetch", "succeeded"), ("list", "succeeded"), ("index", "succeeded")]
    assert file_counts(last) == {
        "files_added": 1,
        "files_changed": 3,
        "files_deleted": 1,
        "files_unchanged": 5,
        "files_skipped": 0,
    }
    embedded = {text.split("\n", 1)[0].split(": ", 1)[0] for text in rerun.texts}
    assert embedded == {"src/f4.py", "src/f5.py", "src/f6.py", "src/f9.py"}
    cursor, _ = await stream_state(env.db, src.stream)
    assert cursor == {"commit": head, "rules": pinned["rules"]}
    await assert_consistent(env.db, mirrors=env.mirrors)
    assert await advisory_locks(env.db) == []

    # The same history synced without a crash ends in the same records, chunks and vectors.
    await sync_in_process(env, reference, FakeEmbedder(), limits=one_file_a_batch)
    assert await source_state(env.db, src) == await source_state(env.db, reference)
    await assert_consistent(env.db, mirrors=env.mirrors)


@needs_git
@pytest.mark.git
async def test_the_consistency_check_finds_what_a_failure_could_leave_behind(
    git_env: GitEnv,
) -> None:
    """The check the tests above rely on is not vacuous: a chunk lost, a tombstone that kept its
    chunks, and a pin whose commit the records do not match are each named."""
    env = git_env
    env.repo.commit({f"src/{name}.py": function(name, 1) for name in ("a", "b", "c")})
    src = await add_git_source(env, "acme")
    await sync_in_process(env, src, FakeEmbedder(), limits=SyncLimits())
    await assert_consistent(env.db, mirrors=env.mirrors)

    await env.db.execute(
        "DELETE FROM chunk WHERE id = (SELECT c.id FROM chunk c JOIN record r ON r.id = c.record_id"
        " WHERE r.external_id = %s LIMIT 1)",
        (f"{MAIN}:src/a.py",),
    )
    await env.db.execute(
        "UPDATE record SET deleted_at = now() WHERE external_id = %s", (f"{MAIN}:src/b.py",)
    )
    problems = await consistency_problems(env.db, mirrors=env.mirrors)

    assert len(problems) == 4, problems
    assert any(
        "src/a.py" in line and "its chunks do not match its content (1 missing, 0 extra" in line
        for line in problems
    )
    assert any("src/a.py" in line and "its symbols do not match" in line for line in problems)
    assert any("src/b.py" in line and "a tombstone with chunks" in line for line in problems)
    assert any(
        line.startswith(f"git stream {MAIN}") and "1 missing, 0 extra" in line for line in problems
    )


# --- The database restarted mid-batch ------------------------------------------------------------


@pytest.fixture(scope="module")
def own_postgres() -> Iterator[DisposablePostgres]:
    """A database server of this module's own, which its tests may crash; see the module."""
    with start_postgres() as server:
        yield server


@pytest.fixture
async def own_database(own_postgres: DisposablePostgres) -> AsyncIterator[str]:
    """A new, migrated database on the module's own server; the server runs again afterwards,
    whatever the test did to it."""
    name = f"memini_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(own_postgres.url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
    url = own_postgres.url.rsplit("/", 1)[0] + f"/{name}"
    await migrate(url)
    try:
        yield url
    finally:
        try:
            own_postgres.wait_ready(timeout=1)
        except psycopg.OperationalError:
            own_postgres.start()


class GatedEmbedder(FakeEmbedder):
    """The FakeEmbedder, whose first model call waits for :meth:`release`: a batch that embeds is
    held there, inside its transaction, for as long as the test needs."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = anyio.Event()
        self.released = anyio.Event()

    def release(self) -> None:
        self.released.set()

    async def _embed_batch(self, texts: list[str]) -> Sequence[Sequence[float]]:
        if not self.entered.is_set():
            self.entered.set()
            await self.released.wait()
        return await super()._embed_batch(texts)


async def crash_and_restart(
    server: DisposablePostgres, while_down: Callable[[], Awaitable[None]]
) -> None:
    """Kill the database server, run ``while_down``, start the server again. Docker runs in a
    thread: waiting for it must not hold up the service's tasks."""
    await anyio.to_thread.run_sync(server.crash)
    await while_down()
    await anyio.to_thread.run_sync(server.start)


async def until_accepted(
    send: Callable[[], Awaitable[httpx2.Response]], *, timeout: float = 30.0
) -> httpx2.Response:
    """Send a request until the service answers 200, as a client of the API does: 503
    ``unavailable`` means "try again". Right after a restart of the database the pool has fewer
    connections than it will have, and adds them one at a time (psycopg_pool backs off between
    attempts that failed while the database was down), so a request that needs two at once may
    wait past the pool's timeout for a few seconds."""
    with anyio.fail_after(timeout):
        while True:
            answer = await send()
            if answer.status_code == 200:
                return answer
            assert answer.status_code == 503, answer.text
            assert answer.json()["error"]["code"] == "unavailable", answer.text
            await anyio.sleep(0.2)


async def readiness(client: httpx2.AsyncClient) -> int:
    return (await client.get("/readyz")).status_code


async def ready(client: httpx2.AsyncClient) -> bool:
    return await readiness(client) == 200


def pushed_thread(number: int, at: datetime) -> dict[str, Any]:
    return {
        "kind": "thread",
        "external_id": f"general/{number}",
        "occurred_at": at.isoformat(),
        "payload": {
            "channel": "general",
            "messages": [
                {
                    "author": "Ann Example",
                    "sent_at": at.isoformat(),
                    "text": f"Kiln slot {number}?",
                },
                {"author": "Bo Sample", "sent_at": at.isoformat(), "text": "Friday works."},
            ],
        },
    }


async def test_a_database_crash_mid_batch_rolls_the_batch_back_and_the_api_recovers(
    own_postgres: DisposablePostgres, own_database: str
) -> None:
    gate = GatedEmbedder()
    async with connect(own_database, autocommit=True) as setup:
        owner = await ensure_principal(setup, "owner")
        token = await create_token(
            setup, owner_id=owner, name="pusher", scopes=("read", "admin", "ingest")
        )
        now: datetime = await scalar(setup, "SELECT now()")
    headers = {"Authorization": f"Bearer {token.secret.get_secret_value()}"}
    settings = Settings(database_url=SecretStr(own_database))
    route = "/v1/workspaces/acme/sources/chat/records"
    start, end = now - timedelta(days=2), now - timedelta(days=1)
    batch = {
        "stream": "general",
        "window": {"from": start.isoformat(), "to": end.isoformat(), "complete": True},
        "records": [pushed_thread(number, start + timedelta(hours=number)) for number in (1, 2, 3)],
    }

    async with open_pool(own_database, min_size=1, max_size=4, timeout=2) as pool:
        app = create_app(
            settings,
            pool=pool,
            readiness_checks={"database": database_check(pool)},
            readiness_timeout=1.0,
            embedder=gate,
        )
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://memini.test") as client:
            for path, body in (
                ("/v1/workspaces", {"name": "acme"}),
                ("/v1/workspaces/acme/sources", {"kind": "push", "name": "chat"}),
            ):
                created = await client.post(path, json=body, headers=headers)
                assert created.status_code == 201, created.text
            answers: list[httpx2.Response] = []
            during: dict[str, int] = {}

            async def push() -> None:
                answers.append(await client.post(route, json=batch, headers=headers))

            async def while_down() -> None:
                during["readyz"] = await readiness(client)
                during["api"] = (await client.get("/v1/workspaces", headers=headers)).status_code

            async with anyio.create_task_group() as group:
                group.start_soon(push)
                # The batch's records are written, its transaction open, its embedding held.
                with anyio.fail_after(20):
                    await gate.entered.wait()
                await crash_and_restart(own_postgres, while_down)
                gate.release()

            # While the database was down the service said so; the batch in flight failed.
            assert during == {"readyz": 503, "api": 503}
            (answer,) = answers
            assert answer.status_code == 503, answer.text
            assert answer.json()["error"]["code"] == "unavailable"

            # The pool recovers by itself: no restart of the service.
            await eventually(lambda: ready(client), what="readiness after the restart")
            async with connect(own_database, autocommit=True) as db:
                # Rolled back whole: no record, no chunk, no checkpoint.
                assert await scalar(db, "SELECT count(*) FROM record") == 0
                assert await scalar(db, "SELECT count(*) FROM chunk") == 0
                assert await scalar(db, "SELECT checked_until FROM stream") is None
                await assert_consistent(db)

                # The same batch again: stored with its checkpoint, in one transaction.
                again = await until_accepted(
                    lambda: client.post(route, json=batch, headers=headers)
                )
                body = again.json()
                assert body["counts"]["inserted"] == 3, body
                assert body["checkpoint"]["moved"] is True, body
                assert await scalar(db, "SELECT count(*) FROM record WHERE deleted_at IS NULL") == 3
                assert await scalar(db, "SELECT checked_until FROM stream") == end
                await assert_consistent(db)
                assert await advisory_locks(db) == []


# --- MCP sources ---------------------------------------------------------------------------------

HEARTH_HOST: Final = "hearth.example"
FAST_RETRY: Final = RetryPolicy(attempts=3, base=0.01, cap=0.05)


async def no_dns(host: str, port: int) -> list[Any]:
    raise AssertionError(f"no name is resolved in this test: {host}:{port}")


@pytest.fixture
def hearth_parser_registered() -> Iterator[None]:
    register_parser("hearth.thread", hearth_parser(names=True), replace=True)
    try:
        yield
    finally:
        unregister_parser("hearth.thread")


@dataclass(frozen=True)
class McpSrc:
    workspace: uuid.UUID
    owner: uuid.UUID
    id: uuid.UUID
    stream: uuid.UUID
    now: datetime


async def add_mcp_source(
    db: Conn,
    endpoint: HttpEndpoint | StdioEndpoint,
    policy: EgressPolicy,
    *,
    recipe: str | None = None,
) -> McpSrc:
    """A principal, the workspace acme and the hearth source in it, with one stream."""
    owner = await ensure_principal(db, "owner")
    workspace = await create_workspace(db, owner_id=owner, name="acme")
    source = await create_mcp_source(
        db,
        WriteAccess.for_owner(owner),
        "acme",
        NewMcpSource(
            name="hearth",
            endpoint=endpoint,
            streams=[CHANNEL],
            recipe_toml=recipe or HEARTH_RECIPE_PATH.read_text(encoding="utf-8"),
        ),
        policy=policy,
    )
    stream = await scalar(db, "SELECT id FROM stream WHERE source_id = %s", source.id)
    now = await scalar(db, "SELECT now()")
    return McpSrc(workspace.id, owner, source.id, stream, now)


def recipe_with(**rate: str) -> str:
    """The hearth recipe, with ``[rate]`` values replaced."""
    text = HEARTH_RECIPE_PATH.read_text(encoding="utf-8")
    for key, value in rate.items():
        start = text.index(f"\n{key} = ") + 1
        end = text.index("\n", start)
        text = text[:start] + f"{key} = {value}" + text[end:]
    return text


async def threads(db: Conn, source: uuid.UUID) -> dict[str, list[str]]:
    found = await rows(
        db,
        "SELECT external_id, convert_from(payload, 'UTF8') AS payload FROM record"
        " WHERE source_id = %s AND deleted_at IS NULL ORDER BY external_id",
        source,
    )
    return {
        row["external_id"]: [m["author"] for m in json.loads(row["payload"])["messages"]]
        for row in found
    }


def expected_threads(now: datetime) -> dict[str, list[str]]:
    return {
        f"{CHANNEL}/{t.root.ts}": [t.root.user_name, *(r.user_name for r in t.replies)]
        for t in recent_threads(now)
    }


async def checked_until(db: Conn, stream: uuid.UUID) -> datetime | None:
    return await scalar(db, "SELECT checked_until FROM stream WHERE id = %s", stream)


async def as_of(db: Conn, job: uuid.UUID) -> datetime:
    return datetime.fromisoformat((await job_row(db, job))["args"]["as_of"])


async def test_a_database_crash_mid_sync_is_retried_by_the_running_service(
    own_postgres: DisposablePostgres, own_database: str, hearth_parser_registered: None
) -> None:
    gate = GatedEmbedder()
    policy = EgressPolicy.build(endpoints=[HEARTH_HOST])
    async with connect(own_database, autocommit=True) as setup:
        src = await add_mcp_source(setup, HttpEndpoint(f"https://{HEARTH_HOST}/mcp"), policy)
        token = await create_token(setup, owner_id=src.owner, name="reader", scopes=("read",))
        job = await enqueue_sync(
            setup,
            workspace=src.workspace,
            owner=src.owner,
            kind=MCP_SYNC,
            source=src.id,
            stream=src.stream,
        )
    headers = {"Authorization": f"Bearer {token.secret.get_secret_value()}"}
    fake = FakeMcp(FakeHearth(threads=recent_threads(src.now)))

    async with (
        fake.asgi() as hearth,
        open_pool(own_database, min_size=1, max_size=8, timeout=2) as pool,
    ):
        options = ServiceOptions(
            policy=policy,
            retry=FAST_RETRY,
            http_transport=lambda: httpx2.ASGITransport(app=hearth),
            resolver=no_dns,
        )
        handler = mcp_sync_handler(
            pool, keyring=Keyring(CredentialKey(KEY)), indexer=PayloadIndexer(gate), options=options
        )
        # The service: the worker and the scheduler on one pool, and the HTTP API on it too.
        service = worker(
            pool, {MCP_SYNC: handler}, lease=timedelta(seconds=4), heartbeat=timedelta(seconds=1)
        )
        scheduler = Scheduler(pool, sync_kinds={"mcp": MCP_SYNC}, interval=timedelta(seconds=0.2))
        app = create_app(
            Settings(database_url=SecretStr(own_database)),
            pool=pool,
            readiness_checks={"database": database_check(pool)},
            readiness_timeout=1.0,
            embedder=gate,
        )
        transport = httpx2.ASGITransport(app=app)
        async with (
            httpx2.AsyncClient(transport=transport, base_url="http://memini.test") as client,
            running(service, scheduler),
        ):
            # The first window (empty) is stored and moved the checkpoint; the second is held in
            # its ingest transaction, its records written and not committed.
            with anyio.fail_after(30):
                await gate.entered.wait()
            async with connect(own_database, autocommit=True) as db:
                moved = await checked_until(db, src.stream)
                assert moved is not None
                assert moved < await as_of(db, job)

            down: list[int] = []

            async def while_down() -> None:
                down.append(await readiness(client))

            await crash_and_restart(own_postgres, while_down)
            gate.release()
            assert down == [503]

            # At once, before the lease runs out: the batch in flight rolled back whole, the
            # checkpoint stayed with the first window, the job still names the lost attempt.
            async with connect(own_database, autocommit=True) as db:
                assert await scalar(db, "SELECT count(*) FROM record") == 0
                assert await checked_until(db, src.stream) == moved
                row = await job_row(db, job)
                assert (row["status"], row["attempts"]) == ("running", 1)
                await assert_consistent(db)

                # The pool recovers by itself. The worker lost its claim with the connection; once
                # the lease runs out it takes the job again, in the same process, and completes it.
                await eventually(lambda: ready(client), what="readiness after the restart")
                await eventually(
                    lambda: job_is(db, job, "succeeded", "quarantined"),
                    timeout=30,
                    what="the job's second attempt",
                )
            await until_accepted(lambda: client.get("/v1/workspaces", headers=headers))

    async with connect(own_database, autocommit=True) as db:
        row = await job_row(db, job)
        assert row["status"] == "succeeded", row["last_error"]
        assert row["attempts"] == 2
        assert row["last_error"].startswith("lease of ")
        assert await threads(db, src.id) == expected_threads(src.now)
        assert await checked_until(db, src.stream) == await as_of(db, job)
        last = (await runs_of(db, src.stream))[-1]
        assert last["status"] == "succeeded", last["error"]
        await assert_consistent(db)
        assert await advisory_locks(db) == []


# --- Sources that hang ---------------------------------------------------------------------------


@dataclass
class Stall:
    """Makes a server stall: while :attr:`stalling` is set it accepts each request and answers
    none. The stalled requests end when :attr:`released` is set, as the test ends."""

    stalling: bool = False
    stalled: int = 0
    released: anyio.Event = field(default_factory=anyio.Event)


def stalling(app: ASGIApp, stall: Stall) -> ASGIApp:
    async def wrapped(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and stall.stalling:
            stall.stalled += 1
            await stall.released.wait()
            return
        await app(scope, receive, send)

    return wrapped


@asynccontextmanager
async def serving(app: ASGIApp, stall: Stall) -> AsyncGenerator[int]:
    """Serve ``app`` over HTTP on a free port of 127.0.0.1, in this event loop, for the block;
    yield the port."""
    port = free_port()
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        lifespan="off",
        log_config=None,
        access_log=False,
        timeout_graceful_shutdown=1,
    )
    server = uvicorn.Server(config)
    async with anyio.create_task_group() as group:
        group.start_soon(server.serve)
        with anyio.fail_after(10):
            while not server.started:
                await anyio.sleep(0.01)
        try:
            yield port
        finally:
            stall.released.set()
            server.should_exit = True


def loopback_policy(port: int) -> EgressPolicy:
    """The egress policy that lets the service executor reach a server on 127.0.0.1 over plain
    http: both are refused unless the settings allow them."""
    return EgressPolicy.build(
        endpoints=[f"127.0.0.1:{port}"],
        networks=["127.0.0.0/8"],
        allow_http=True,
        commands=[sys.executable],
    )


@dataclass
class McpEnv:
    pool: Pool
    db: Conn
    fake: FakeMcp
    stall: Stall
    port: int
    base: datetime
    keyring: Keyring = field(default_factory=lambda: Keyring(CredentialKey(KEY)))

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    @property
    def policy(self) -> EgressPolicy:
        return loopback_policy(self.port)

    def handler(self) -> Handler:
        options = ServiceOptions(policy=self.policy, retry=FAST_RETRY)
        return mcp_sync_handler(
            self.pool,
            keyring=self.keyring,
            indexer=PayloadIndexer(FakeEmbedder()),
            options=options,
        )


@pytest.fixture
async def mcp_env(
    migrated_database_url: str, hearth_parser_registered: None
) -> AsyncIterator[McpEnv]:
    """The fake Hearth server over a real socket on 127.0.0.1, which can be made to stall."""
    async with (
        open_pool(migrated_database_url, min_size=1, max_size=8, timeout=5) as pool,
        connect(migrated_database_url, autocommit=True) as db,
    ):
        base: datetime = await scalar(db, "SELECT now()")
        fake = FakeMcp(FakeHearth(threads=recent_threads(base)))
        stall = Stall()
        async with fake.asgi() as hearth, serving(stalling(hearth, stall), stall) as port:
            yield McpEnv(pool, db, fake, stall, port, base)


async def no_trace_of_a_run(db: Conn, src: McpSrc) -> None:
    """Nothing of the stream was written: no window, no response, no record, no checkpoint."""
    assert await scalar(db, "SELECT count(*) FROM raw_window") == 0
    assert await scalar(db, "SELECT count(*) FROM raw_response") == 0
    assert await scalar(db, "SELECT count(*) FROM record") == 0
    assert await checked_until(db, src.stream) is None


async def test_an_mcp_server_that_never_answers_fails_the_attempt_and_the_retry_completes(
    mcp_env: McpEnv,
) -> None:
    env = mcp_env
    src = await add_mcp_source(
        env.db, HttpEndpoint(env.url), env.policy, recipe=recipe_with(timeout='"1s"')
    )
    job = await enqueue_sync(
        env.db,
        workspace=src.workspace,
        owner=src.owner,
        kind=MCP_SYNC,
        source=src.id,
        stream=src.stream,
        max_attempts=3,
    )
    env.stall.stalling = True  # every request is accepted and never answered

    async def failed_once() -> bool:
        row = await job_row(env.db, job)
        return row["status"] == "queued" and row["attempts"] == 1

    async with running(worker(env.pool, {MCP_SYNC: env.handler()})):
        with anyio.fail_after(30):
            await eventually(failed_once, what="the attempt timing out")
            # The attempt failed on the session's timeout (rate.timeout), before any call: the
            # job waits for its retry, nothing of the stream was written, no lock is held.
            row = await job_row(env.db, job)
            assert row["last_error"].startswith("ConnectError: "), row["last_error"]
            (run,) = await runs_of(env.db, src.stream)
            assert run["status"] == "failed"
            assert steps(run) == [("connect", "failed")]
            await no_trace_of_a_run(env.db, src)
            assert env.stall.stalled >= 1
            assert await advisory_locks(env.db) == []

            # The server answers again; the retry, due now, completes the sync.
            env.stall.stalling = False
            await env.db.execute("UPDATE job SET run_after = now() WHERE id = %s", (job,))
            await eventually(lambda: job_is(env.db, job, "succeeded"), what="the retry")

    row = await job_row(env.db, job)
    assert row["attempts"] == 2
    assert await threads(env.db, src.id) == expected_threads(env.base)
    assert await checked_until(env.db, src.stream) == await as_of(env.db, job)
    await assert_consistent(env.db)
    assert await advisory_locks(env.db) == []


async def test_a_call_that_never_returns_leaves_its_window_incomplete_and_the_next_run_ends_it(
    mcp_env: McpEnv,
) -> None:
    env = mcp_env
    # One call at a time, so the three tries of one detail call meet the three hangs.
    recipe = recipe_with(timeout='"1s"', concurrency="1")
    src = await add_mcp_source(env.db, HttpEndpoint(env.url), env.policy, recipe=recipe)
    env.fake.fail("thread_replies", "hang", "hang", "hang")
    first = await enqueue_sync(
        env.db,
        workspace=src.workspace,
        owner=src.owner,
        kind=MCP_SYNC,
        source=src.id,
        stream=src.stream,
    )
    async with running(worker(env.pool, {MCP_SYNC: env.handler()})):
        await eventually(lambda: job_is(env.db, first, "succeeded"), what="the first run")

    # Each try timed out; out of tries, the call stays missing: the window is stored as far as it
    # goes, incomplete, and the checkpoint stays with the window before it.
    assert len(env.fake.hung) == 3
    (run,) = await runs_of(env.db, src.stream)
    assert run["status"] == "partial", run["error"]
    assert run["report"]["counts"]["mcp_failures_timeout"] == 3
    (incomplete,) = run["report"]["details"]["incomplete_windows"]
    failed = [call for call in incomplete["missing_calls"] if call["tool"] == "thread_replies"]
    assert len(failed) == 1
    windows = run["report"]["details"]["plan"]["windows"]
    first_window_end = datetime.fromisoformat(windows[0]["to"].replace("Z", "+00:00"))
    assert await checked_until(env.db, src.stream) == first_window_end
    assert first_window_end < await as_of(env.db, first)
    await assert_consistent(env.db)
    assert await advisory_locks(env.db) == []

    # The next run makes the missing call and completes the window.
    second = await enqueue_sync(
        env.db,
        workspace=src.workspace,
        owner=src.owner,
        kind=MCP_SYNC,
        source=src.id,
        stream=src.stream,
    )
    async with running(worker(env.pool, {MCP_SYNC: env.handler()})):
        await eventually(lambda: job_is(env.db, second, "succeeded"), what="the second run")
    last = (await runs_of(env.db, src.stream))[-1]
    assert last["status"] == "succeeded", last["error"]
    assert await threads(env.db, src.id) == expected_threads(env.base)
    assert await checked_until(env.db, src.stream) == await as_of(env.db, second)
    await assert_consistent(env.db)
    assert await advisory_locks(env.db) == []


async def test_a_stdio_server_that_never_answers_is_stopped_with_the_attempt(
    mcp_env: McpEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = mcp_env
    # The SDK's server/discover probe waits a fixed 10 seconds, not the recipe's rate.timeout; over
    # HTTP httpx's timeout cuts it short, over stdio nothing does. Shortened to keep the test fast.
    monkeypatch.setattr(mcp_client_session, "DISCOVER_TIMEOUT_SECONDS", 1.0)
    pid_file = tmp_path / "server.pid"
    silent = "import os, sys, time; open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(600)"
    endpoint = StdioEndpoint(command=sys.executable, args=("-c", silent, str(pid_file)))
    src = await add_mcp_source(env.db, endpoint, env.policy, recipe=recipe_with(timeout='"1s"'))
    job = await enqueue_sync(
        env.db,
        workspace=src.workspace,
        owner=src.owner,
        kind=MCP_SYNC,
        source=src.id,
        stream=src.stream,
        max_attempts=1,
    )
    async with running(worker(env.pool, {MCP_SYNC: env.handler()})):
        await eventually(lambda: job_is(env.db, job, "quarantined"), what="the attempt failing")

    row = await job_row(env.db, job)
    assert row["last_error"].startswith("ConnectError: "), row["last_error"]
    await no_trace_of_a_run(env.db, src)
    assert await advisory_locks(env.db) == []
    # The server process went with the session: none is left running.
    pid = int(pid_file.read_text(encoding="utf-8"))

    async def gone() -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        return False

    await eventually(gone, timeout=10, what="the stdio server process ending")


class _GitRequest(SimpleHTTPRequestHandler):
    """Serves the files of a bare repository (git's dumb HTTP protocol), or stalls."""

    def do_GET(self) -> None:
        server = self.server
        assert isinstance(server, DumbGitServer)
        if server.stalling.is_set():
            server.stalled += 1
            server.released.wait(timeout=60)
            return
        super().do_GET()

    def log_message(self, format: str, *args: Any) -> None:
        del format, args


class DumbGitServer(ThreadingHTTPServer):
    """Repositories under ``root`` over git's dumb HTTP protocol on 127.0.0.1: static files, as
    any web server serves them. While :attr:`stalling` is set it accepts each request and
    answers none, as a remote that hangs."""

    daemon_threads = True

    def __init__(self, root: Path) -> None:
        super().__init__(("127.0.0.1", 0), partial(_GitRequest, directory=str(root)))
        self.stalling = threading.Event()
        self.released = threading.Event()
        self.stalled = 0

    @property
    def port(self) -> int:
        return int(self.server_address[1])


@pytest.fixture
def git_server(tmp_path: Path) -> Iterator[DumbGitServer]:
    root = tmp_path / "served"
    root.mkdir()
    server = DumbGitServer(root)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.released.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def publish(repo: SourceRepo, bare: Path) -> str:
    """Push ``main`` of ``repo`` to the served bare repository; return the commit."""
    if not bare.exists():
        repo.git("clone", "--bare", "--quiet", str(repo.path), str(bare))
    else:
        repo.git("push", "--quiet", str(bare), "main")
    repo.git("--git-dir", str(bare), "update-server-info")
    return repo.head()


@needs_git
@pytest.mark.git
async def test_a_git_remote_that_hangs_times_out_changes_nothing_and_the_next_sync_catches_up(
    git_env: GitEnv, git_server: DumbGitServer, tmp_path: Path
) -> None:
    env = git_env
    served = tmp_path / "served" / "shop.git"
    options = GitOptions(
        mirror_dir=env.options.mirror_dir,
        allow_http=True,
        fetch_timeout=2,
        command_timeout=30,
        max_blob_size=MAX_BLOB,
    )
    mirrors = MirrorManager(options)
    old = env.repo.commit({f"src/{name}.py": function(name, 1) for name in ("a", "b", "c")})
    publish(env.repo, served)
    src = await add_git_source(
        env, "acme", location=f"http://127.0.0.1:{git_server.port}/shop.git", options=options
    )
    handler = git_sync_handler(env.pool, mirrors, FakeEmbedder())

    def service() -> Worker:
        return worker(env.pool, {GIT_SYNC: handler}, retry=JobRetry(base=timedelta(seconds=0.1)))

    async def sync_job(max_attempts: int) -> uuid.UUID:
        return await enqueue_sync(
            env.db,
            workspace=src.workspace,
            owner=env.owner,
            kind=GIT_SYNC,
            source=src.id,
            stream=src.stream,
            max_attempts=max_attempts,
        )

    async with running(service()):
        first = await sync_job(1)
        await eventually(lambda: job_is(env.db, first, "succeeded"), what="the first sync")
    pinned, checked = await stream_state(env.db, src.stream)
    assert pinned is not None
    assert pinned["commit"] == old
    before = await file_records(env.db, src)

    # A new commit, and the remote stops answering: each fetch is killed at the fetch timeout,
    # the job is tried again after its backoff, then quarantined with its attempts spent.
    env.repo.commit({"src/a.py": function("a", 2)})
    head = publish(env.repo, served)
    git_server.stalling.set()
    stuck = await sync_job(2)
    async with running(service()):
        await eventually(
            lambda: job_is(env.db, stuck, "quarantined"), timeout=30, what="the fetches timing out"
        )
    row = await job_row(env.db, stuck)
    assert row["attempts"] == 2
    assert row["last_error"].startswith("GitTimeout: "), row["last_error"]
    attempts = [run for run in await runs_of(env.db, src.stream) if run["status"] == "failed"]
    assert [steps(run) for run in attempts] == [[("fetch", "failed")]] * 2
    assert git_server.stalled >= 2

    # Nothing moved: not the pin, not the records, not the mirror's refs; no lock is held.
    assert await stream_state(env.db, src.stream) == (pinned, checked)
    assert await file_records(env.db, src) == before
    mirror = mirrors.open(src.id)
    assert await mirrors.resolve(mirror, MAIN) == old
    await assert_consistent(env.db, mirrors=mirrors)
    assert await advisory_locks(env.db) == []

    # The remote answers again: the next sync fetches and catches up from the pin.
    git_server.stalling.clear()
    async with running(service()):
        last = await sync_job(1)
        await eventually(lambda: job_is(env.db, last, "succeeded"), what="the next sync")
    cursor, _ = await stream_state(env.db, src.stream)
    assert cursor == {"commit": head, "rules": pinned["rules"]}
    assert (await file_records(env.db, src))["src/a.py"] == (blob(env.repo, "src/a.py"), False)
    await assert_consistent(env.db, mirrors=mirrors)
    assert await advisory_locks(env.db) == []
