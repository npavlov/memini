"""The ``git_sync`` job: fetch the mirror of a repository, then sync the streams the job names.

One job syncs one stream, the job's ``stream_id``, as the scheduler and a triggered sync enqueue
it; a job without a stream syncs every stream of its source, one after the other. The worker runs
one job per source at a time, so a source's mirror and records have one writer.

The steps, recorded in the job's run report:

1. ``fetch``: create the mirror, or fetch it (:meth:`~memini.git.MirrorManager.sync`). The
   database time just before it becomes the stream's ``checked_until`` when the sync completes.
2. Check the workspace's index fingerprint (:func:`~memini.index.check_index`). An index built with
   another embedding model or chunker refuses the sync with
   :class:`~memini.index.IndexMismatchError`, which names ``memini reindex``: no record changes and
   the pin stays, so a stale index never mixes with new rows (principle 9). The fetch has run by
   then, so the mirror a rebuild reads from exists and is current. Once the rebuild is done, the
   next sync catches up from the pin.
3. :func:`~memini.gitsync.sync.sync_stream` for each stream: ``diff`` or ``list``, then ``index``;
   or ``tombstone`` for a ref the remote no longer has, which is marked missing, not retried.
4. ``retention``, only for a source with ``retention_days``: its tombstones deleted that long ago
   are removed (:func:`~memini.removal.expire_source`); files of the tree never expire.
"""

import os
import shutil
from contextlib import nullcontext
from datetime import datetime
from typing import Final
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import TupleRow

from memini.db import Pool
from memini.embeddings import Embedder
from memini.git import GitOptions, MirrorManager
from memini.gitsync.sync import (
    DEFAULT_LIMITS,
    GitSource,
    GitStream,
    StreamSyncResult,
    SyncLimits,
    sync_stream,
)
from memini.index import check_index, current_fingerprint
from memini.jobs import SYNC_KINDS, Handler, Job
from memini.removal import DEFAULT_RETENTION_BATCH, expire_source
from memini.runs import current_run
from memini.sources import GitConfig

__all__ = ["GIT_SYNC", "git_sync_handler", "mirrors_unusable", "run_git_sync"]

GIT_SYNC: Final = SYNC_KINDS["repository"]
"""The job kind of a git sync: ``git_sync``."""


def mirrors_unusable(options: GitOptions) -> str | None:
    """Why mirrors cannot be kept with ``options``, or ``None`` if they can: the git executable is
    missing, or the mirror directory cannot be created or written. Creates the directory."""
    if shutil.which(options.git) is None:
        return f"the git executable ({options.git}) is not on PATH"
    directory = options.mirror_dir
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as error:
        return f"the mirror directory {directory} cannot be created: {error.strerror or error}"
    if not os.access(directory, os.W_OK | os.X_OK):
        return f"the mirror directory {directory} is not writable (MEMINI_GIT_MIRROR_DIR)"
    return None


def git_sync_handler(
    pool: Pool,
    mirrors: MirrorManager,
    embedder: Embedder,
    *,
    limits: SyncLimits = DEFAULT_LIMITS,
    retention_batch: int = DEFAULT_RETENTION_BATCH,
) -> Handler:
    """The handler of ``git_sync`` jobs: :func:`run_git_sync` with these mirrors and embedder."""

    async def handle(job: Job) -> None:
        await run_git_sync(
            pool, mirrors, embedder, job, limits=limits, retention_batch=retention_batch
        )

    return handle


async def run_git_sync(
    pool: Pool,
    mirrors: MirrorManager,
    embedder: Embedder,
    job: Job,
    *,
    limits: SyncLimits = DEFAULT_LIMITS,
    retention_batch: int = DEFAULT_RETENTION_BATCH,
) -> list[StreamSyncResult]:
    """Sync the stream of ``job`` (or every stream of its source), then pass retention over the
    source; see the module.

    Raises:
        LookupError: the job's source or stream does not exist.
        ValueError: the source is not a repository, or has no location.
        RemoteNotAllowed, GitError: the fetch failed; nothing else ran.
        IndexMismatchError: the workspace's index is out of date; no record changed.
        RevisionNotFound: a tracked ref names no commit (a tag of a tree or blob). A ref that
            does not exist at the remote is no error: its stream is marked missing
            (:mod:`memini.gitsync.sync`).
        EmbeddingError: the model failed; see :func:`~memini.gitsync.sync.sync_stream`.
    """
    if job.source_id is None:
        raise ValueError(f"{GIT_SYNC} job {job.id} names no source")
    async with pool.connection() as conn, conn.transaction():
        source, streams, checked_until, retention = await _load(conn, job.source_id, job.stream_id)
    if not source.config.location:
        raise ValueError(f"repository {source.id} has no location")

    run = current_run()
    if run is None:
        fetched = await mirrors.sync(source.id, source.config.location)
    else:
        async with run.step("fetch"):
            fetched = await mirrors.sync(source.id, source.config.location)
        run.count("refs_updated", len(fetched.updates))

    results: list[StreamSyncResult] = []
    async with pool.connection() as conn:
        async with conn.transaction():
            await check_index(conn, source.workspace_id, current_fingerprint(embedder.spec))
        for stream in streams:
            results.append(
                await sync_stream(
                    conn,
                    mirrors,
                    fetched.mirror,
                    embedder,
                    source=source,
                    stream=stream,
                    checked_until=checked_until,
                    limits=limits,
                )
            )
        if retention:
            async with run.step("retention") if run is not None else nullcontext():
                await expire_source(conn, source.id, batch_size=retention_batch)
    return results


async def _load(
    conn: AsyncConnection[TupleRow], source_id: UUID, stream_id: UUID | None
) -> tuple[GitSource, list[GitStream], datetime, bool]:
    """The source, the streams to sync, the database time, and whether the source has a
    retention."""
    cur = await conn.execute(
        "SELECT workspace_id, owner_id, kind, config, now(), retention_days IS NOT NULL"
        " FROM source WHERE id = %s",
        (source_id,),
    )
    row = await cur.fetchone()
    if row is None:
        raise LookupError(f"no source {source_id}")
    workspace_id, owner_id, kind, config, now, retention = row
    if kind != "repository":
        raise ValueError(f"source {source_id} is a {kind} source, not a repository")
    if stream_id is None:
        cur = await conn.execute(
            "SELECT id, key, cursor FROM stream WHERE source_id = %s ORDER BY key", (source_id,)
        )
    else:
        cur = await conn.execute(
            "SELECT id, key, cursor FROM stream WHERE source_id = %s AND id = %s",
            (source_id, stream_id),
        )
    streams = [GitStream(id=row[0], key=row[1], cursor=row[2]) for row in await cur.fetchall()]
    if stream_id is not None and not streams:
        raise LookupError(f"no stream {stream_id} in source {source_id}")
    source = GitSource(
        id=source_id,
        workspace_id=workspace_id,
        owner_id=owner_id,
        config=GitConfig.from_json(config),
    )
    return source, streams, now, bool(retention)
