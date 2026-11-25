"""How far the rebuild of a workspace's index is: for status, and for the refusal of search.

A rebuild is a ``reindex`` job (:mod:`memini.index.auto`, which a service started with
``auto_reindex`` enqueues after an upgrade) or ``memini reindex`` by hand. Either records a
``reindex`` run, whose report carries the ``progress`` detail :func:`memini.index.reindex` keeps:
the records of the workspace and how many it has visited. This module reads the two tables and
says, for a workspace, whether a rebuild waits in the queue or runs, and how far it is.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal, cast
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import TupleRow

__all__ = ["REINDEX_KIND", "RebuildProgress", "read_rebuild", "read_rebuilds"]

REINDEX_KIND: Final = "reindex"
"""The kind of the job and of the run of a rebuild."""

type Conn = AsyncConnection[TupleRow]

RebuildState = Literal["queued", "running"]


@dataclass(frozen=True, slots=True)
class RebuildProgress:
    """A rebuild of a workspace's index that waits or runs."""

    state: RebuildState
    records_done: int | None
    """Records visited so far; ``None`` before the running rebuild has counted them."""
    records_total: int | None
    """Records of the workspace when the rebuild started; ``None`` likewise."""
    started_at: datetime | None
    """When the running rebuild started; ``None`` while it waits."""

    @property
    def percent(self) -> int | None:
        """Whole percent of the records visited; ``None`` while unknown."""
        if self.records_done is None or not self.records_total:
            return None
        return min(100, self.records_done * 100 // self.records_total)

    def describe(self) -> str:
        """For messages: ``queued``, or ``312 of 1,472 records (21%)``."""
        if self.state == "queued":
            return "queued to start"
        if self.records_done is None or self.records_total is None:
            return "started"
        return f"{self.records_done:,} of {self.records_total:,} records ({self.percent}%)"


_QUERY: Final = """
SELECT w.id,
       (SELECT j.status FROM job j
         WHERE j.workspace_id = w.id AND j.kind = %(kind)s AND j.status IN ('queued', 'running')
         ORDER BY j.created_at DESC LIMIT 1),
       r.started_at,
       r.report -> 'details' -> 'progress'
  FROM workspace w
  LEFT JOIN LATERAL (
       SELECT started_at, report FROM run
        WHERE workspace_id = w.id AND kind = %(kind)s AND status = 'running'
        ORDER BY started_at DESC LIMIT 1) r ON true
 WHERE w.id = ANY(%(ids)s)
"""


async def read_rebuilds(conn: Conn, workspace_ids: Iterable[UUID]) -> dict[UUID, RebuildProgress]:
    """The rebuild of each of the workspaces that has one waiting or running, by workspace id."""
    ids = list(workspace_ids)
    if not ids:
        return {}
    cur = await conn.execute(_QUERY, {"kind": REINDEX_KIND, "ids": ids})
    found: dict[UUID, RebuildProgress] = {}
    for row in await cur.fetchall():
        progress = _progress(row)
        if progress is not None:
            found[row[0]] = progress
    return found


async def read_rebuild(conn: Conn, workspace_id: UUID) -> RebuildProgress | None:
    """The rebuild of the workspace that waits or runs; ``None`` if there is none."""
    return (await read_rebuilds(conn, [workspace_id])).get(workspace_id)


def _progress(row: Sequence[Any]) -> RebuildProgress | None:
    job_status, started_at, detail = row[1], row[2], row[3]
    if started_at is None:  # no running run: a job that waits, or nothing
        if job_status in ("queued", "running"):
            return RebuildProgress("queued", None, None, None)
        return None
    values: Mapping[str, object] = {}
    if isinstance(detail, Mapping):
        values = cast(Mapping[str, object], detail)
    return RebuildProgress(
        "running",
        _count(values.get("records_done")),
        _count(values.get("records_total")),
        started_at,
    )


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
