"""Incremental code sync (memini.gitsync) against a real Postgres and real git mirrors.

Acceptance of memini-2ca: a new commit processes only changed files; the chunks of a deleted file
are gone; a failure mid-run leaves the old pin and a consistent index. Fixture repositories are
made here, with fixed dates; every file is synthetic. Vectors come from the FakeEmbedder.
"""

import os
import shutil
import subprocess
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
import pytest
from psycopg import AsyncConnection
from psycopg.rows import TupleRow, dict_row

from git_helpers import SourceRepo
from memini.auth import WriteAccess
from memini.chunking import chunk_file
from memini.cli.serve import background_jobs
from memini.db import Pool, connect, open_pool
from memini.embeddings import EmbeddingError, EmbeddingSpec, FakeEmbedder
from memini.git import GitCommandError, GitOptions, Mirror, MirrorManager, RevisionNotFound
from memini.gitsync import (
    DEFAULT_LIMITS,
    GIT_SYNC,
    FileRules,
    MirrorContent,
    SyncLimits,
    git_sync_handler,
    read_pin,
    run_git_sync,
    split_code_external_id,
    stream_pin,
)
from memini.index import (
    IndexMismatchError,
    RawContentUnavailableError,
    RecordRef,
    current_fingerprint,
    default_raw_content,
    read_index_state,
    reindex,
)
from memini.jobs import Job, execute_job
from memini.runs import wait_for_run_writes
from memini.sources import (
    GitConfig,
    NewGitSource,
    SourceUpdate,
    create_git_source,
    trigger_sync,
    update_source,
)
from memini.workspaces import create_workspace, ensure_principal

pytestmark = [
    pytest.mark.integration,
    pytest.mark.git,
    pytest.mark.anyio,
    pytest.mark.skipif(shutil.which("git") is None, reason="needs the git executable"),
]

type Conn = AsyncConnection[TupleRow]

MAIN = "refs/heads/main"
DEV = "refs/heads/dev"
MAX_BLOB = 2048


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


def module(*functions: str) -> str:
    return "\n\n".join(functions)


BINARY = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR synthetic image"


class CountingMirrors(MirrorManager):
    """Records the blobs read; can be made to fail the read of a later batch."""

    def __init__(self, options: GitOptions) -> None:
        super().__init__(options)
        self.reads: list[list[str]] = []
        self.fail_from_read: int | None = None

    @property
    def read_oids(self) -> set[str]:
        return {oid for batch in self.reads for oid in batch}

    async def read_blobs(self, mirror: Mirror, oids: Sequence[str]) -> dict[str, bytes]:
        self.reads.append(list(oids))
        if self.fail_from_read is not None and len(self.reads) >= self.fail_from_read:
            raise GitCommandError("cat-file", 128, "synthetic failure")
        return await super().read_blobs(mirror, oids)


class FlakyEmbedder(FakeEmbedder):
    """Fails model call number ``fail_at`` (1-based); the others answer."""

    def __init__(self, *, fail_at: int) -> None:
        super().__init__()
        self.fail_at = fail_at

    async def _embed_batch(self, texts: list[str]) -> Sequence[Sequence[float]]:
        if len(self.batches) + 1 == self.fail_at:
            self.batches.append(list(texts))
            raise EmbeddingError("synthetic failure of the embedding model")
        return await super()._embed_batch(texts)


@dataclass
class Env:
    url: str
    pool: Pool
    db: Conn
    options: GitOptions
    mirrors: CountingMirrors
    repo: SourceRepo
    owner: uuid.UUID
    workspace: uuid.UUID
    embedder: FakeEmbedder

    @property
    def access(self) -> WriteAccess:
        return WriteAccess.for_owner(self.owner)


@dataclass(frozen=True)
class Src:
    id: uuid.UUID
    name: str
    streams: Mapping[str, uuid.UUID]


@pytest.fixture
async def env(migrated_database_url: str, tmp_path: Path) -> AsyncIterator[Env]:
    sources = tmp_path / "sources"
    options = GitOptions(
        mirror_dir=tmp_path / "mirrors",
        local_roots=(sources,),
        fetch_timeout=30,
        command_timeout=30,
        max_blob_size=MAX_BLOB,
    )
    repo = SourceRepo(sources / "shop")
    async with (
        open_pool(migrated_database_url, min_size=1, max_size=8, timeout=5) as pool,
        connect(migrated_database_url, autocommit=True) as db,
    ):
        owner = await ensure_principal(db, "owner")
        workspace = await create_workspace(db, owner_id=owner, name="acme")
        yield Env(
            migrated_database_url,
            pool,
            db,
            options,
            CountingMirrors(options),
            repo,
            owner,
            workspace.id,
            FakeEmbedder(),
        )


async def add_source(
    env: Env,
    *,
    name: str = "shop",
    refs: Sequence[str] = ("main",),
    include: Sequence[str] = (),
    exclude: Sequence[str] = (),
) -> Src:
    source = await create_git_source(
        env.db,
        env.access,
        "acme",
        NewGitSource(
            name=name, location=str(env.repo.path), refs=refs, include=include, exclude=exclude
        ),
        options=env.options,
    )
    cur = await env.db.execute("SELECT key, id FROM stream WHERE source_id = %s", (source.id,))
    return Src(source.id, name, {row[0]: row[1] for row in await cur.fetchall()})


async def sync(
    env: Env,
    src: Src,
    ref: str = MAIN,
    *,
    embedder: FakeEmbedder | None = None,
    limits: SyncLimits = DEFAULT_LIMITS,
) -> dict[str, Any]:
    """Run one git_sync job of the stream of ``ref`` as the worker runs it; return its run row."""
    job = Job(
        id=uuid.uuid4(),
        workspace_id=env.workspace,
        owner_id=env.owner,
        source_id=src.id,
        stream_id=src.streams[ref],
        kind=GIT_SYNC,
        args={},
        attempt=1,
        max_attempts=5,
        worker="test",
    )
    handler = git_sync_handler(env.pool, env.mirrors, embedder or env.embedder, limits=limits)
    try:
        await execute_job(job, {GIT_SYNC: handler}, env.pool)
    finally:
        await wait_for_run_writes()
    return await last_run(env, src, ref)


async def last_run(env: Env, src: Src, ref: str = MAIN) -> dict[str, Any]:
    async with env.db.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM run WHERE stream_id = %s ORDER BY started_at DESC LIMIT 1",
            (src.streams[ref],),
        )
        row = await cur.fetchone()
    assert row is not None
    return row


def steps(run: Mapping[str, Any]) -> list[tuple[str, str]]:
    return [(step["name"], step["status"]) for step in run["report"]["steps"]]


def file_counts(run: Mapping[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = run["report"]["counts"]
    return {name: count for name, count in counts.items() if name.startswith("files_")}


async def records(env: Env, src: Src) -> dict[str, tuple[str, bool]]:
    """external id -> (content hash, deleted)."""
    cur = await env.db.execute(
        "SELECT external_id, content_hash, deleted_at IS NOT NULL FROM record WHERE source_id = %s",
        (src.id,),
    )
    return {row[0]: (row[1], row[2]) for row in await cur.fetchall()}


async def live(env: Env, src: Src) -> dict[str, str]:
    """external id -> content hash, of the live records."""
    return {key: oid for key, (oid, deleted) in (await records(env, src)).items() if not deleted}


async def stream_row(env: Env, src: Src, ref: str = MAIN) -> tuple[Any, Any]:
    cur = await env.db.execute(
        "SELECT cursor, checked_until FROM stream WHERE id = %s", (src.streams[ref],)
    )
    row = await cur.fetchone()
    assert row is not None
    return row[0], row[1]


async def chunk_versions(env: Env, src: Src) -> dict[str, str]:
    """chunk id -> xmin: a row's xmin changes exactly when the row is written."""
    cur = await env.db.execute("SELECT id, xmin::text FROM chunk WHERE source_id = %s", (src.id,))
    return {row[0]: row[1] for row in await cur.fetchall()}


async def chunk_ids(env: Env, query: Any, params: Sequence[object]) -> set[str]:
    cur = await env.db.execute(query, params)
    return {row[0] for row in await cur.fetchall()}


async def chunks_of(env: Env, src: Src, external_id: str) -> int:
    cur = await env.db.execute(
        "SELECT (SELECT count(*) FROM chunk c WHERE c.record_id = r.id),"
        " (SELECT count(*) FROM symbol s WHERE s.record_id = r.id)"
        " FROM record r WHERE r.source_id = %s AND r.external_id = %s",
        (src.id, external_id),
    )
    row = await cur.fetchone()
    assert row is not None
    return int(row[0]) + int(row[1])


async def assert_consistent(env: Env, src: Src) -> None:
    """Every live record has exactly the chunks its blob gives; no tombstone has any."""
    mirror = env.mirrors.open(src.id)
    cur = await env.db.execute(
        "SELECT id, external_id, content_hash, deleted_at IS NOT NULL FROM record"
        " WHERE source_id = %s",
        (src.id,),
    )
    for rid, external_id, content_hash, deleted in await cur.fetchall():
        found = await env.db.execute("SELECT id FROM chunk WHERE record_id = %s", (rid,))
        stored = {row[0] for row in await found.fetchall()}
        if deleted:
            assert stored == set(), external_id
            assert await chunks_of(env, src, external_id) == 0, external_id
            continue
        content = (await MirrorManager.read_blobs(env.mirrors, mirror, [content_hash]))[
            content_hash
        ]
        _, path = split_code_external_id(external_id)
        assert stored == {chunk.id for chunk in chunk_file(rid, path, content).chunks}, external_id


def blob(repo: SourceRepo, path: str, ref: str = "main") -> str:
    return repo.git("rev-parse", f"{ref}:{path}")


def embedded_paths(fake: FakeEmbedder) -> set[str]:
    """The files whose chunk texts the model got: the path in each header line."""
    return {text.split("\n", 1)[0].split(": ", 1)[0] for text in fake.texts}


# --- First sync ----------------------------------------------------------------------------------


async def test_a_first_sync_indexes_every_selected_file_and_pins_the_commit(env: Env) -> None:
    repo = env.repo
    (repo.path / "link.py").parent.mkdir(parents=True, exist_ok=True)
    (repo.path / "link.py").symlink_to("src/shop.py")
    repo.git("add", "link.py")
    head = repo.commit(
        {
            "src/shop.py": module(function("total", 1), function("average", 2)),
            "src/util.py": module(function("clamp", 3)),
            "README.md": "# Shop\n\nA synthetic fixture.\n",
            "docs/guide.md": "# Guide\n",  # the source's exclude
            "web/package-lock.json": '{"lockfileVersion": 3}\n',  # built-in exclude
            "assets/logo.png": BINARY,
            "data/huge.txt": "x" * (MAX_BLOB + 1),
        }
    )
    src = await add_source(env, exclude=["docs/**"])

    run = await sync(env, src)

    assert run["status"] == "succeeded", run["error"]
    assert steps(run) == [("fetch", "succeeded"), ("list", "succeeded"), ("index", "succeeded")]
    assert file_counts(run) == {
        "files_added": 3,
        "files_changed": 0,
        "files_deleted": 0,
        "files_unchanged": 0,
        "files_skipped": 5,
        "files_skipped_excluded": 2,
        "files_skipped_not_a_file": 1,
        "files_skipped_too_large": 1,
        "files_skipped_binary": 1,
    }
    counts = run["report"]["counts"]
    assert counts["chunks_inserted"] == 4  # total, average, clamp, the README window
    assert counts["texts_embedded"] == 4
    assert await live(env, src) == {
        f"{MAIN}:README.md": blob(repo, "README.md"),
        f"{MAIN}:src/shop.py": blob(repo, "src/shop.py"),
        f"{MAIN}:src/util.py": blob(repo, "src/util.py"),
    }
    assert embedded_paths(env.embedder) == {"README.md", "src/shop.py", "src/util.py"}

    cursor, checked_until = await stream_row(env, src)
    rules = FileRules(GitConfig(location="", exclude=("docs/**",)), MAX_BLOB)
    assert cursor == {"commit": head, "rules": rules.digest}
    assert run["started_at"] < checked_until < run["finished_at"]
    pin = await stream_pin(env.db, src.id, MAIN)
    assert pin is not None
    assert pin.commit == head
    cur = await env.db.execute(
        "SELECT DISTINCT occurred_at, stream_id FROM record WHERE source_id = %s", (src.id,)
    )
    assert [(row[0].isoformat(), row[1]) for row in await cur.fetchall()] == [
        ("2026-01-02T12:00:00+00:00", src.streams[MAIN])
    ]
    await assert_consistent(env, src)


async def test_a_sync_without_a_new_commit_changes_nothing_but_checked_until(env: Env) -> None:
    env.repo.commit({"src/shop.py": module(function("total", 1))})
    src = await add_source(env)
    await sync(env, src)
    before_records, before_chunks = await records(env, src), await chunk_versions(env, src)
    cursor, checked_until = await stream_row(env, src)
    env.embedder.reset()
    env.mirrors.reads.clear()

    run = await sync(env, src)

    assert steps(run) == [("fetch", "succeeded"), ("diff", "succeeded"), ("index", "succeeded")]
    assert set(file_counts(run).values()) == {0}
    assert env.embedder.batches == []
    assert env.mirrors.reads == []
    assert await records(env, src) == before_records
    assert await chunk_versions(env, src) == before_chunks
    new_cursor, new_checked_until = await stream_row(env, src)
    assert new_cursor == cursor
    assert new_checked_until > checked_until


# --- A new commit --------------------------------------------------------------------------------


async def test_a_new_commit_processes_only_the_files_it_changed(env: Env) -> None:
    repo = env.repo
    repo.commit(
        {
            "src/shop.py": module(function("total", 1), function("average", 2)),
            "src/util.py": module(function("clamp", 3)),
            "README.md": "# Shop\n",
        }
    )
    src = await add_source(env)
    await sync(env, src)
    before = await chunk_versions(env, src)
    untouched = await chunk_ids(
        env,
        "SELECT c.id FROM chunk c JOIN record r ON r.id = c.record_id"
        " WHERE (r.external_id, c.key) IN ((%s, 'total#0'), (%s, 'clamp#0'))",
        (f"{MAIN}:src/shop.py", f"{MAIN}:src/util.py"),
    )
    assert len(untouched) == 2
    env.embedder.reset()
    env.mirrors.reads.clear()

    head = repo.commit(
        {
            "src/shop.py": module(function("total", 1), function("average", 20)),
            "src/new.py": module(function("median", 4)),
            "README.md": None,
        }
    )
    run = await sync(env, src)

    assert run["status"] == "succeeded", run["error"]
    assert steps(run) == [("fetch", "succeeded"), ("diff", "succeeded"), ("index", "succeeded")]
    assert file_counts(run) == {
        "files_added": 1,
        "files_changed": 1,
        "files_deleted": 1,
        "files_unchanged": 0,
        "files_skipped": 0,
    }
    # Only the changed files were read, and only their new texts embedded.
    assert env.mirrors.read_oids == {blob(repo, "src/shop.py"), blob(repo, "src/new.py")}
    assert embedded_paths(env.embedder) == {"src/shop.py", "src/new.py"}
    assert not any("def total" in text for text in env.embedder.texts)
    assert len(env.embedder.texts) == 2  # average, median

    after = await chunk_versions(env, src)
    assert {chunk: after[chunk] for chunk in untouched} == {
        chunk: before[chunk] for chunk in untouched
    }

    # The deleted file: a tombstone without chunks or symbols.
    assert (await records(env, src))[f"{MAIN}:README.md"][1] is True
    assert await chunks_of(env, src, f"{MAIN}:README.md") == 0
    assert set(await live(env, src)) == {f"{MAIN}:src/shop.py", f"{MAIN}:src/util.py",
                                         f"{MAIN}:src/new.py"}  # fmt: skip
    pin = read_pin((await stream_row(env, src))[0])
    assert pin is not None
    assert (pin.commit, pin.partial) == (head, False)
    await assert_consistent(env, src)


async def test_a_file_deleted_and_added_again_is_one_record_brought_back(env: Env) -> None:
    repo = env.repo
    repo.commit({"src/util.py": module(function("clamp", 3)), "src/shop.py": function("a", 1)})
    src = await add_source(env)
    await sync(env, src)
    repo.commit({"src/util.py": None})
    await sync(env, src)
    assert (await records(env, src))[f"{MAIN}:src/util.py"][1] is True
    env.embedder.reset()

    repo.commit({"src/util.py": module(function("clamp", 3))})
    run = await sync(env, src)

    assert file_counts(run)["files_added"] == 1
    assert (await records(env, src))[f"{MAIN}:src/util.py"] == (blob(repo, "src/util.py"), False)
    assert len(env.embedder.texts) == 1  # its vector went with its chunks
    await assert_consistent(env, src)


# --- Rules ---------------------------------------------------------------------------------------


async def test_globs_select_files_and_a_change_of_them_lists_the_ref_again(env: Env) -> None:
    repo = env.repo
    repo.commit(
        {
            "src/app.py": module(function("run", 1)),
            "src/test_app.py": module(function("check", 2)),
            "scripts/deploy.py": module(function("deploy", 3)),
            "README.md": "# App\n",
        }
    )
    src = await add_source(env, include=["src/**"], exclude=["**/test_*.py"])

    run = await sync(env, src)

    assert set(await live(env, src)) == {f"{MAIN}:src/app.py"}
    assert file_counts(run)["files_skipped_excluded"] == 3
    env.embedder.reset()

    # No new commit: the changed globs alone make the next sync list the ref in full.
    await update_source(env.db, env.access, "acme", src.name, SourceUpdate(exclude=[]))
    run = await sync(env, src)

    assert steps(run) == [("fetch", "succeeded"), ("list", "succeeded"), ("index", "succeeded")]
    assert set(await live(env, src)) == {f"{MAIN}:src/app.py", f"{MAIN}:src/test_app.py"}
    assert file_counts(run)["files_unchanged"] == 1
    assert embedded_paths(env.embedder) == {"src/test_app.py"}

    await update_source(env.db, env.access, "acme", src.name, SourceUpdate(include=["scripts/**"]))
    run = await sync(env, src)

    assert set(await live(env, src)) == {f"{MAIN}:scripts/deploy.py"}
    assert file_counts(run)["files_deleted"] == 2
    await assert_consistent(env, src)


async def test_a_file_that_turns_binary_or_too_large_becomes_a_tombstone(env: Env) -> None:
    repo = env.repo
    repo.commit(
        {
            "src/shop.py": module(function("total", 1)),
            "src/util.py": module(function("clamp", 3)),
            "notes.txt": "plain text\n",
        }
    )
    src = await add_source(env)
    await sync(env, src)

    repo.commit({"notes.txt": BINARY, "src/util.py": "# padding\n" * 300})
    run = await sync(env, src)

    assert file_counts(run) == {
        "files_added": 0,
        "files_changed": 0,
        "files_deleted": 2,
        "files_unchanged": 0,
        "files_skipped": 2,
        "files_skipped_too_large": 1,
        "files_skipped_binary": 1,
    }
    assert set(await live(env, src)) == {f"{MAIN}:src/shop.py"}
    assert await chunks_of(env, src, f"{MAIN}:notes.txt") == 0
    assert await chunks_of(env, src, f"{MAIN}:src/util.py") == 0
    await assert_consistent(env, src)


# --- Failures ------------------------------------------------------------------------------------


async def start_with_four_files(env: Env) -> tuple[Src, str]:
    env.repo.commit(
        {
            "src/a.py": module(function("alpha", 1)),
            "src/b.py": module(function("beta", 2)),
            "src/c.py": module(function("gamma", 3)),
            "src/d.py": module(function("delta", 4)),
        }
    )
    src = await add_source(env)
    await sync(env, src)
    return src, env.repo.head()


def change_three_delete_one(repo: SourceRepo) -> str:
    return repo.commit(
        {
            "src/a.py": module(function("alpha", 10)),
            "src/b.py": module(function("beta", 20)),
            "src/c.py": module(function("gamma", 30)),
            "src/d.py": None,
        }
    )


async def test_a_failure_mid_run_keeps_the_old_pin_and_a_consistent_index(env: Env) -> None:
    src, old = await start_with_four_files(env)
    cursor, checked_until = await stream_row(env, src)
    before = await records(env, src)
    head = change_three_delete_one(env.repo)
    one_file_per_transaction = SyncLimits(batch_files=1)
    flaky = FlakyEmbedder(fail_at=2)  # a.py commits, b.py fails

    with pytest.raises(EmbeddingError):
        await sync(env, src, embedder=flaky, limits=one_file_per_transaction)

    run = await last_run(env, src)
    assert run["status"] == "failed"
    assert "synthetic failure" in run["error"]
    new_cursor, new_checked_until = await stream_row(env, src)
    assert new_cursor == {**cursor, "partial": True}  # the pin stays; the next sync lists in full
    assert new_checked_until == checked_until
    after = await records(env, src)
    assert after[f"{MAIN}:src/a.py"] == (blob(env.repo, "src/a.py"), False)  # committed
    for name in ("b", "c", "d"):  # not reached: as they were at the old pin
        assert after[f"{MAIN}:src/{name}.py"] == before[f"{MAIN}:src/{name}.py"]
    assert after[f"{MAIN}:src/d.py"][1] is False
    await assert_consistent(env, src)
    alpha = next(text for text in flaky.texts if "alpha" in text)

    rerun = FakeEmbedder()
    run = await sync(env, src, embedder=rerun, limits=one_file_per_transaction)

    assert run["status"] == "succeeded", run["error"]
    assert steps(run) == [("fetch", "succeeded"), ("list", "succeeded"), ("index", "succeeded")]
    assert file_counts(run) == {
        "files_added": 0,
        "files_changed": 2,
        "files_deleted": 1,
        "files_unchanged": 1,
        "files_skipped": 0,
    }
    assert alpha not in rerun.texts  # embedded once, by the failed run
    assert embedded_paths(rerun) == {"src/b.py", "src/c.py"}
    assert (await stream_row(env, src))[0] == {"commit": head, "rules": cursor["rules"]}
    assert await live(env, src) == {
        f"{MAIN}:src/{name}.py": blob(env.repo, f"src/{name}.py") for name in ("a", "b", "c")
    }
    assert await chunks_of(env, src, f"{MAIN}:src/d.py") == 0
    assert old != head
    await assert_consistent(env, src)


async def test_a_failure_in_a_one_batch_sync_changes_nothing(env: Env) -> None:
    src, _ = await start_with_four_files(env)
    cursor, checked_until = await stream_row(env, src)
    before_records, before_chunks = await records(env, src), await chunk_versions(env, src)
    change_three_delete_one(env.repo)

    with pytest.raises(EmbeddingError):
        await sync(env, src, embedder=FlakyEmbedder(fail_at=3))

    assert await stream_row(env, src) == (cursor, checked_until)  # no partial mark either
    assert await records(env, src) == before_records
    assert await chunk_versions(env, src) == before_chunks

    run = await sync(env, src)

    assert steps(run) == [("fetch", "succeeded"), ("diff", "succeeded"), ("index", "succeeded")]
    assert file_counts(run)["files_changed"] == 3
    await assert_consistent(env, src)


async def test_a_failed_read_of_a_later_batch_keeps_what_was_committed(env: Env) -> None:
    src, _ = await start_with_four_files(env)
    cursor, _ = await stream_row(env, src)
    change_three_delete_one(env.repo)
    env.mirrors.reads.clear()
    env.mirrors.fail_from_read = 2

    with pytest.raises(GitCommandError):
        await sync(env, src, limits=SyncLimits(batch_files=2))

    assert (await stream_row(env, src))[0] == {**cursor, "partial": True}
    assert (await records(env, src))[f"{MAIN}:src/b.py"][0] == blob(env.repo, "src/b.py")
    await assert_consistent(env, src)
    env.mirrors.fail_from_read = None

    run = await sync(env, src)

    assert run["status"] == "succeeded", run["error"]
    assert file_counts(run)["files_changed"] == 1  # c.py; a.py and b.py were committed before
    await assert_consistent(env, src)


async def test_a_lost_pin_falls_back_to_a_full_listing(env: Env) -> None:
    repo = env.repo
    old = repo.commit(
        {
            "src/a.py": module(function("alpha", 1)),
            "src/b.py": module(function("beta", 2)),
            "src/gone.py": module(function("gone", 3)),
        }
    )
    src = await add_source(env)
    await sync(env, src)
    # Rewrite the history (a force push), fetch it, and prune the old commits from the mirror.
    (repo.path / "src/b.py").write_text(module(function("beta", 20)))
    repo.git("add", "src/b.py")
    repo.git("rm", "--quiet", "src/gone.py")
    repo.git("commit", "--quiet", "--amend", "-m", "rewritten")
    head = repo.head()
    mirror = (await env.mirrors.sync(src.id, str(repo.path))).mirror
    prune(mirror.path)
    with pytest.raises(RevisionNotFound):
        await env.mirrors.resolve(mirror, old)
    env.embedder.reset()

    run = await sync(env, src)

    assert run["status"] == "succeeded", run["error"]
    assert steps(run) == [
        ("fetch", "succeeded"),
        ("diff", "failed"),
        ("list", "succeeded"),
        ("index", "succeeded"),
    ]
    assert file_counts(run) == {
        "files_added": 0,
        "files_changed": 1,
        "files_deleted": 1,
        "files_unchanged": 1,
        "files_skipped": 0,
    }
    assert embedded_paths(env.embedder) == {"src/b.py"}
    assert set(await live(env, src)) == {f"{MAIN}:src/a.py", f"{MAIN}:src/b.py"}
    pin = read_pin((await stream_row(env, src))[0])
    assert pin is not None
    assert pin.commit == head
    await assert_consistent(env, src)


def prune(git_dir: Path) -> None:
    """Remove every unreachable object from a bare repository now."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": "/nonexistent",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    for args in (["reflog", "expire", "--expire=now", "--all"], ["gc", "--quiet", "--prune=now"]):
        subprocess.run(  # noqa: S603 - fixed git arguments in a test
            ["git", f"--git-dir={git_dir}", *args],  # noqa: S607
            env=env,
            check=True,
            capture_output=True,
        )


async def test_a_ref_missing_from_the_mirror_fails_and_pins_nothing(env: Env) -> None:
    env.repo.commit({"src/a.py": module(function("alpha", 1))})
    src = await add_source(env, refs=["main", "missing"])

    with pytest.raises(RevisionNotFound):
        await sync(env, src, "refs/heads/missing")

    assert await stream_row(env, src, "refs/heads/missing") == (None, None)


# --- Refs ----------------------------------------------------------------------------------------


async def test_two_refs_of_one_source_sync_independently(env: Env) -> None:
    repo = env.repo
    repo.commit({"src/app.py": module(function("run", 1))})
    repo.git("checkout", "--quiet", "-b", "dev")
    repo.commit({"src/app.py": module(function("run", 2)), "src/extra.py": function("x", 3)})
    repo.git("checkout", "--quiet", "main")
    src = await add_source(env, refs=["main", "dev"])

    await sync(env, src, MAIN)
    await sync(env, src, DEV)

    assert await live(env, src) == {
        f"{MAIN}:src/app.py": blob(repo, "src/app.py", "main"),
        f"{DEV}:src/app.py": blob(repo, "src/app.py", "dev"),
        f"{DEV}:src/extra.py": blob(repo, "src/extra.py", "dev"),
    }
    main_cursor, main_checked = await stream_row(env, src, MAIN)
    before = await chunk_versions(env, src)
    main_chunks = await chunk_ids(
        env,
        "SELECT c.id FROM chunk c JOIN record r ON r.id = c.record_id"
        " WHERE starts_with(r.external_id, %s)",
        (f"{MAIN}:",),
    )

    repo.git("checkout", "--quiet", "dev")
    dev_head = repo.commit({"src/extra.py": None})
    repo.git("checkout", "--quiet", "main")
    run = await sync(env, src, DEV)

    assert file_counts(run)["files_deleted"] == 1
    assert await stream_row(env, src, MAIN) == (main_cursor, main_checked)
    after = await chunk_versions(env, src)
    assert {chunk_id: after[chunk_id] for chunk_id in main_chunks} == {
        chunk_id: before[chunk_id] for chunk_id in main_chunks
    }
    dev_pin = read_pin((await stream_row(env, src, DEV))[0])
    assert dev_pin is not None
    assert dev_pin.commit == dev_head

    # A ref no longer tracked: its stream goes, and the next sync of the source drops its files.
    await update_source(env.db, env.access, "acme", src.name, SourceUpdate(refs=["main"]))
    run = await sync(env, src, MAIN)

    assert file_counts(run)["files_deleted"] == 1
    assert set(await live(env, src)) == {f"{MAIN}:src/app.py"}
    assert await chunks_of(env, src, f"{DEV}:src/app.py") == 0
    await assert_consistent(env, src)


async def test_a_job_without_a_stream_syncs_every_ref_of_its_source(env: Env) -> None:
    env.repo.commit({"src/app.py": module(function("run", 1))})
    env.repo.git("branch", "dev")
    src = await add_source(env, refs=["main", "dev"])
    job = Job(
        id=uuid.uuid4(),
        workspace_id=env.workspace,
        owner_id=env.owner,
        source_id=src.id,
        stream_id=None,
        kind=GIT_SYNC,
        args={},
        attempt=1,
        max_attempts=5,
        worker="test",
    )

    results = await run_git_sync(env.pool, env.mirrors, env.embedder, job)

    assert [(result.ref, result.added, result.listed) for result in results] == [
        (DEV, 1, True),
        (MAIN, 1, True),
    ]
    assert set(await live(env, src)) == {f"{DEV}:src/app.py", f"{MAIN}:src/app.py"}
    assert len(env.embedder.texts) == 1  # the same text on both refs is embedded once


# --- The index fingerprint and the rebuild -------------------------------------------------------

OTHER_MODEL = EmbeddingSpec(provider="openai_compatible", model="fake-embedder-2", dimension=12)


async def test_a_stale_index_blocks_the_sync_until_reindex_reads_code_from_the_mirror(
    env: Env,
) -> None:
    repo = env.repo
    repo.commit({"src/a.py": module(function("alpha", 1)), "src/b.py": function("beta", 2)})
    src = await add_source(env)
    await sync(env, src)
    chunk_ids = set(await chunk_versions(env, src))
    before = await records(env, src)
    cursor = await stream_row(env, src)
    head = repo.commit({"src/b.py": function("beta", 20)})
    other = FakeEmbedder(OTHER_MODEL)

    with pytest.raises(IndexMismatchError, match="memini reindex --workspace acme"):
        await sync(env, src, embedder=other)

    assert other.batches == []
    assert await records(env, src) == before
    assert await stream_row(env, src) == cursor
    mirror = env.mirrors.open(src.id)
    assert (
        await env.mirrors.resolve(mirror, MAIN) == head
    )  # the fetch ran: the raw store is current

    async with connect(env.url, autocommit=True) as conn:
        result = await reindex(
            conn,
            other,
            workspace_id=env.workspace,
            raw=default_raw_content(conn, code=MirrorContent(env.mirrors)),
        )
        state = await read_index_state(conn, env.workspace)

    assert result.records_indexed == 2
    assert set(await chunk_versions(env, src)) == chunk_ids  # the same chunks, at the old pin
    assert state is not None
    assert state.built_with == current_fingerprint(OTHER_MODEL)

    run = await sync(env, src, embedder=other)

    assert run["status"] == "succeeded", run["error"]
    assert file_counts(run)["files_changed"] == 1
    await assert_consistent(env, src)


async def test_mirror_content_reads_blobs_and_refuses_without_a_mirror(env: Env) -> None:
    env.repo.commit({"src/a.py": module(function("alpha", 1))})
    src = await add_source(env)
    content = MirrorContent(env.mirrors)

    def ref(content_hash: str, source_id: uuid.UUID = src.id) -> RecordRef:
        return RecordRef(
            id="0" * 64,
            workspace_id=env.workspace,
            owner_id=env.owner,
            source_id=source_id,
            kind="code_file",
            external_id=f"{MAIN}:src/a.py",
            content_hash=content_hash,
            deleted=False,
        )

    with pytest.raises(RawContentUnavailableError, match="has no repository mirror yet"):
        await content.read(ref("0" * 40))
    await sync(env, src)

    document = await content.read(ref(blob(env.repo, "src/a.py")))

    assert document is not None
    assert (document.path, document.content) == ("src/a.py", module(function("alpha", 1)).encode())
    assert await content.read(ref("0" * 40)) is None  # no longer in the mirror


# --- Through the worker --------------------------------------------------------------------------


async def test_a_triggered_sync_runs_through_the_worker(env: Env) -> None:
    env.repo.commit({"src/a.py": module(function("alpha", 1))})
    src = await add_source(env)
    handler = git_sync_handler(env.pool, env.mirrors, env.embedder)

    async with background_jobs(env.pool, {GIT_SYNC: handler}):
        (job,) = await trigger_sync(env.db, env.access, "acme", src.name)
        with anyio.fail_after(20):
            while True:
                cur = await env.db.execute("SELECT status FROM job WHERE id = %s", (job.job_id,))
                row = await cur.fetchone()
                if row is not None and row[0] in ("succeeded", "quarantined"):
                    break
                await anyio.sleep(0.05)

    assert row[0] == "succeeded"
    run = await last_run(env, src)
    assert (run["kind"], run["status"]) == (GIT_SYNC, "succeeded")
    assert set(await live(env, src)) == {f"{MAIN}:src/a.py"}
