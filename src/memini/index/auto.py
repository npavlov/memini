"""The rebuild after an upgrade: a ``reindex`` job per out-of-date workspace.

An upgrade can change the index fingerprint (a new chunker, a grammar, the redaction); search in
every workspace built with the old one is then refused until its index is rebuilt (principle 9).
A service started with ``auto_reindex`` (the managed server's setting) does that by itself:

1. When it starts, :func:`enqueue_stale_reindexes` enqueues one ``reindex`` job for each workspace
   whose stored fingerprint differs from the one the service is configured for. A job of the same
   workspace that waits or runs already is kept (its dedupe key is ``workspace``).
2. The job worker runs :func:`reindex_handler`: :func:`memini.index.reindex` on a connection of its
   own, with the files of repositories read from their mirrors. Vectors are reused by their text
   hash, so only texts that changed are embedded again.
3. While it waits or runs, search refuses the workspace with
   :class:`~memini.index.IndexRebuildingError`, which says how far the rebuild is
   (:mod:`memini.index.progress`); status shows the same.

A failed attempt goes back to the queue with the worker's backoff, and a rebuild that stopped
resumes without embedding anything twice. ``memini reindex`` stays for installs without the
setting, and refuses while a job of the workspace runs (the rebuild's advisory lock).
"""

import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Final
from uuid import UUID

from psycopg import AsyncConnection

from memini.db import Pool
from memini.embeddings import Embedder
from memini.index.fingerprint import IndexFingerprint, current_fingerprint
from memini.index.progress import REINDEX_KIND
from memini.index.raw import RawContent, default_raw_content
from memini.index.rebuild import ReindexInProgressError, reindex
from memini.jobs.queue import Enqueued, Job, enqueue

__all__ = [
    "REINDEX_DEDUPE_KEY",
    "enqueue_reindex",
    "enqueue_stale_reindexes",
    "reindex_handler",
    "stale_workspaces",
]

log = logging.getLogger(__name__)

REINDEX_DEDUPE_KEY: Final = "workspace"
"""One rebuild of a workspace waits or runs at a time."""

type ReindexHandler = Callable[[Job], Awaitable[None]]


async def enqueue_reindex(
    conn: AsyncConnection[Any], *, workspace_id: UUID, owner_id: UUID
) -> Enqueued:
    """Enqueue the rebuild of the workspace's index; a queued or running one is returned instead."""
    return await enqueue(
        conn,
        workspace_id=workspace_id,
        owner_id=owner_id,
        kind=REINDEX_KIND,
        dedupe_key=REINDEX_DEDUPE_KEY,
    )


async def stale_workspaces(
    conn: AsyncConnection[Any], expected: IndexFingerprint
) -> list[tuple[UUID, UUID, str]]:
    """The workspaces whose index was built with another fingerprint than ``expected``: id, owner
    and name, by name. A workspace with nothing indexed has nothing to rebuild."""
    cur = await conn.execute(
        "SELECT s.workspace_id, w.owner_id, w.name FROM index_state s"
        " JOIN workspace w ON w.id = s.workspace_id"
        " WHERE s.fingerprint <> %s ORDER BY w.name",
        (expected.digest,),
    )
    return [(row[0], row[1], row[2]) for row in await cur.fetchall()]


async def enqueue_stale_reindexes(pool: Pool, embedder: Embedder) -> Sequence[str]:
    """Enqueue the rebuild of every out-of-date workspace; the names of the workspaces."""
    expected = current_fingerprint(embedder.spec)
    async with pool.connection() as conn:
        stale = await stale_workspaces(conn, expected)
        for workspace_id, owner_id, _name in stale:
            await enqueue_reindex(conn, workspace_id=workspace_id, owner_id=owner_id)
    names = [name for _id, _owner, name in stale]
    if names:
        log.info(
            "the index of %d workspaces is out of date; rebuilding it for %s",
            len(names),
            expected.describe(),
            extra={"workspaces": names},
        )
    return names


def reindex_handler(
    pool: Pool, embedder: Embedder, *, code: RawContent | None = None
) -> ReindexHandler:
    """The handler of ``reindex`` jobs: rebuild the job's workspace with ``embedder``, reading the
    files of repositories through ``code`` (:class:`memini.gitsync.MirrorContent` in the service).

    The rebuild commits record by record, so it runs on a connection of the pool switched to
    autocommit for the job, and given back as it was.
    """

    async def handle(job: Job) -> None:
        async with pool.connection() as conn:
            autocommit = conn.autocommit
            await conn.set_autocommit(True)
            try:
                result = await reindex(
                    conn,
                    embedder,
                    workspace_id=job.workspace_id,
                    raw=default_raw_content(conn, code=code),
                )
            except ReindexInProgressError:
                # `memini reindex` runs it by hand: that rebuild brings the index up to date.
                log.info("the workspace's index is rebuilt by hand already; the job ends")
                return
            finally:
                if not conn.closed:
                    await conn.set_autocommit(autocommit)
        log.info(
            "rebuilt the index of workspace %s after an upgrade: %d records, %d texts embedded,"
            " %d vectors reused",
            result.workspace,
            result.records,
            result.counts.embedded,
            result.counts.reused,
        )

    return handle
