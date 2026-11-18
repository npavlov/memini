"""Freshness of the base: what one status call reports (requirements, principle 10).

:func:`read_status` answers, for the workspaces the caller may read, how far each stream is behind
its source and what its last run did, and whether the base as a whole is current. The MCP ``status``
tool returns it as is; an HTTP route can return the same model.

Definitions:

- **Lag** of a stream: ``now() - checked_until``, in seconds, by the database clock. ``None`` until
  the stream has stored its first complete window.
- A stream is **current** when it has a schedule and its lag is at most
  :data:`STALE_AFTER_SCHEDULES` times that schedule: the run that is due may be waiting or still
  running, but none has been missed. A scheduled stream that never completed a window is not
  current. A stream without a schedule runs only when triggered, so it is not judged: its
  ``current`` is ``None``.
- The **base** is current when the database is reachable and no stream is judged not current. With
  no streams it is trivially current.

Every read of a workspace, source, stream or run goes through the caller's
:class:`~memini.auth.ReadFilter`. The schema version is service metadata, not data in a workspace,
so it needs no filter.

There is no search index yet; :class:`IndexStatus` says so instead of reporting made-up fields.
"""

import functools
import logging
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Final, Literal
from uuid import UUID

import psycopg
from psycopg import AsyncConnection, sql
from psycopg.rows import TupleRow
from pydantic import BaseModel, Field

from memini import __version__
from memini.auth import ReadFilter
from memini.db import Pool, load_migrations
from memini.workspaces import list_workspaces

__all__ = [
    "STALE_AFTER_SCHEDULES",
    "DatabaseStatus",
    "IndexStatus",
    "RunSummary",
    "ServiceStatus",
    "StreamStatus",
    "WorkspaceStatus",
    "expected_schema_version",
    "read_status",
    "stream_is_current",
]

STALE_AFTER_SCHEDULES: Final = 2
"""A scheduled stream is current while its lag is at most this many times its schedule."""

SourceKind = Literal["git", "push", "mcp"]
RunStatus = Literal["running", "succeeded", "partial", "failed", "cancelled"]

_log = logging.getLogger(__name__)


class RunSummary(BaseModel):
    """The newest run of a stream."""

    status: RunStatus = Field(description="running, succeeded, partial, failed or cancelled.")
    started_at: datetime
    finished_at: datetime | None = Field(description="null while the run is running.")


class StreamStatus(BaseModel):
    """One stream: the unit of incremental sync inside a source."""

    source: str = Field(description="The name of the source the stream belongs to.")
    source_kind: SourceKind = Field(description="git, push or mcp.")
    key: str = Field(description="What the stream follows: a ref, a channel, a query.")
    schedule_seconds: float | None = Field(
        description="Runs every this many seconds; null when it runs only when triggered."
    )
    checked_until: datetime | None = Field(
        description="Everything in the stream up to this time is stored; null before the first "
        "complete window."
    )
    lag_seconds: float | None = Field(
        description="Seconds from checked_until to now; null before the first complete window."
    )
    current: bool | None = Field(
        description=f"Whether the lag is at most {STALE_AFTER_SCHEDULES} times the schedule; "
        "false if it never completed a window; null for a stream without a schedule."
    )
    last_run: RunSummary | None = Field(description="The newest run; null if it never ran.")


class WorkspaceStatus(BaseModel):
    id: UUID
    name: str
    streams: list[StreamStatus] = Field(description="By source name, then key.")


class DatabaseStatus(BaseModel):
    reachable: bool
    schema_version: int | None = Field(
        description="The newest migration applied; null when the database is not reachable."
    )
    expected_schema_version: int = Field(
        description="The newest migration this version of Memini ships."
    )


class IndexStatus(BaseModel):
    state: Literal["none"] = Field(description="none: this version builds no search index yet.")
    note: str


class ServiceStatus(BaseModel):
    """Whether the base is current, and the state of everything that decides it."""

    version: str = Field(description="The Memini version.")
    as_of: datetime = Field(description="When this was read, by the database clock if reachable.")
    current: bool = Field(
        description="True when the database is reachable and no stream is behind or never "
        "checked; streams without a schedule do not count."
    )
    database: DatabaseStatus
    workspaces: list[WorkspaceStatus] = Field(
        description="The workspaces the token may read, by name."
    )
    index: IndexStatus


_NO_INDEX: Final = IndexStatus(
    state="none",
    note="There is no search index yet; index state is reported once Memini builds one.",
)


def stream_is_current(schedule_seconds: float | None, lag_seconds: float | None) -> bool | None:
    """Whether a stream is current; ``None`` for a stream without a schedule (see the module)."""
    if schedule_seconds is None:
        return None
    if lag_seconds is None:
        return False
    return lag_seconds <= STALE_AFTER_SCHEDULES * schedule_seconds


@functools.cache
def expected_schema_version() -> int:
    """The newest migration this version of Memini ships."""
    return load_migrations()[-1].version


async def read_status(pool: Pool | None, access: ReadFilter) -> ServiceStatus:
    """The status of the base as ``access`` may see it.

    A database that cannot be reached is reported, not raised: the status is then not current and
    lists no workspaces. Without a pool the database counts as not reachable.
    """
    if pool is not None:
        try:
            async with pool.connection() as conn:
                return await _read(conn, access)
        except psycopg.OperationalError as exc:
            # The type only: the text of a connection error can hold the URL with its password.
            _log.warning("status: the database is not reachable (%s)", type(exc).__name__)
    return ServiceStatus(
        version=__version__,
        as_of=datetime.now(UTC),
        current=False,
        database=DatabaseStatus(
            reachable=False,
            schema_version=None,
            expected_schema_version=expected_schema_version(),
        ),
        workspaces=[],
        index=_NO_INDEX,
    )


async def _read(conn: AsyncConnection[TupleRow], access: ReadFilter) -> ServiceStatus:
    cur = await conn.execute("SELECT now(), (SELECT max(version) FROM schema_migration)")
    row = await cur.fetchone()
    if row is None:  # a SELECT without FROM always returns one row
        raise RuntimeError("SELECT now() returned no row")
    as_of: datetime = row[0]
    schema_version: int | None = row[1]

    workspaces = await list_workspaces(conn, access)
    streams = await _streams(conn, access)
    statuses = [
        WorkspaceStatus(id=workspace.id, name=workspace.name, streams=streams.get(workspace.id, []))
        for workspace in workspaces
    ]
    current = all(stream.current is not False for status in statuses for stream in status.streams)
    return ServiceStatus(
        version=__version__,
        as_of=as_of,
        current=current,
        database=DatabaseStatus(
            reachable=True,
            schema_version=schema_version,
            expected_schema_version=expected_schema_version(),
        ),
        workspaces=statuses,
        index=_NO_INDEX,
    )


def _readable(access: ReadFilter, alias: str, *, source: str | None = None) -> sql.Composable:
    return access.where(
        workspace_id=sql.Identifier(alias, "workspace_id"),
        owner_id=sql.Identifier(alias, "owner_id"),
        source_id=sql.Identifier(alias, source or "source_id"),
    )


# The newest run of each stream comes from run_stream_started_idx. Lag and schedule are seconds as
# float8: extract() returns numeric.
_STREAMS: Final = sql.SQL("""
SELECT st.workspace_id, s.name, s.kind, st.key,
       extract(epoch FROM st.schedule)::float8,
       st.checked_until,
       extract(epoch FROM now() - st.checked_until)::float8,
       r.status, r.started_at, r.finished_at
FROM stream st
JOIN source s ON s.id = st.source_id AND s.workspace_id = st.workspace_id
LEFT JOIN LATERAL (
    SELECT r.status, r.started_at, r.finished_at
    FROM run r
    WHERE r.stream_id = st.id AND {run_readable}
    ORDER BY r.started_at DESC, r.id DESC
    LIMIT 1
) r ON TRUE
WHERE {stream_readable} AND {source_readable}
ORDER BY s.name, st.key
""")


async def _streams(
    conn: AsyncConnection[TupleRow], access: ReadFilter
) -> dict[UUID, list[StreamStatus]]:
    query = _STREAMS.format(
        run_readable=_readable(access, "r"),
        stream_readable=_readable(access, "st"),
        source_readable=_readable(access, "s", source="id"),
    )
    cur = await conn.execute(query)
    found: defaultdict[UUID, list[StreamStatus]] = defaultdict(list)
    for row in await cur.fetchall():
        found[row[0]].append(_stream(row))
    return found


def _stream(row: Sequence[Any]) -> StreamStatus:
    (_, source, kind, key, schedule, checked_until, lag, run_status, started_at, finished_at) = row
    last_run = (
        None
        if run_status is None
        else RunSummary(status=run_status, started_at=started_at, finished_at=finished_at)
    )
    return StreamStatus(
        source=source,
        source_kind=kind,
        key=key,
        schedule_seconds=schedule,
        checked_until=checked_until,
        lag_seconds=lag,
        current=stream_is_current(schedule, lag),
        last_run=last_run,
    )
