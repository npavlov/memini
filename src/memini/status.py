"""Freshness of the base: what one status call reports (requirements, principle 10).

:func:`read_status` answers, for the workspaces the caller may read, how far each stream is behind
its source, what its last runs did, how far back its history is loaded, whether its records are
indexed, the state of each workspace's index, and whether the base as a whole is current. The MCP
``status`` tool, ``GET /v1/status`` and ``memini status`` all show this one model.

Definitions:

- **Lag** of a stream: ``now() - checked_until``, in seconds, by the database clock. ``None`` until
  the stream has stored its first complete window.
- The **index state** of a workspace, against the fingerprint the service's embedder makes
  (:func:`memini.index.current_fingerprint`): ``ok``, built with it; ``stale``, built with another
  one, so search and indexing in the workspace are refused until ``memini reindex --workspace
  NAME`` rebuilds it (principle 9); ``none``, nothing indexed yet; ``off``, the service has no
  embedding provider, so nothing is indexed or searched and the stored fingerprint is not compared.
- A record is **not indexed** when it is live (not a tombstone), keeps a payload (a thread, a
  meeting or a document that was pushed or synced by a recipe) and has no chunk: every payload has
  at least one chunk once it is indexed. The files of a repository are not counted: they keep no
  payload, an empty file has no chunk, and a git sync writes a file's record and chunks together
  or not at all. Records stored while no embedding provider was set stay not indexed until
  ``memini reindex`` indexes them (requirements, section 4).
- A stream is **behind** for each of these reasons, listed in ``behind``: ``never_checked``, it
  has a schedule and never completed a window; ``lag``, its lag is over
  :data:`STALE_AFTER_SCHEDULES` times its schedule (the run that is due may be waiting or still
  running, but more than one was missed); ``not_indexed``, it holds records that are not indexed
  while an embedding provider is set; ``index_stale``, its workspace's index is stale.
- A stream is **current** when it is behind for no reason. Its ``current`` is ``False`` when it is
  behind, ``True`` when it has a schedule and is not, and ``None`` for a stream without a schedule
  that is not behind: it runs only when triggered, so its lag is not judged.
- A workspace's index is **behind** when it is stale, or holds records that are not indexed while
  an embedding provider is set; its ``reindex`` names the command that brings it up to date.
- The **base** is current when the database is reachable, no stream is behind and no workspace's
  index is behind. With no streams it is trivially current.
- A stream is **missing** when what it follows no longer exists at the source: a git ref deleted
  at the remote, or never there. Its cursor says ``"missing": true``
  (:func:`memini.gitsync.missing_cursor`) and its records are tombstones until it comes back. It
  is reported, not counted as behind: its lag is that of the sync that found it gone, and the base
  holds what the source holds.

Runs of a stream: ``last_run`` is the newest run, a running one included. ``failures`` counts the
failed runs since the newest that succeeded or was partial (every attempt of a job is a run, so a
job retried three times that failed each time counts 3), and ``last_failure`` is the newest of
them. A cancelled run (a shutdown) neither counts nor ends the count.

**Error text** (principle 11). A run's ``error`` is written by the service's own code: the type and
message of an exception from git, the index, an embedding provider or the database, or a reason a
job chose (:mod:`memini.runs`). It names ids, refs, paths, settings and fields, never the content
of a record (a refused batch names the field, never the value), and it is redacted when it is
written. Status shows less of it still: its first line only (a database error's ``DETAIL`` line,
which can quote a key value, and the rest of git's stderr stay out), redacted again with
:func:`memini.logs.redact` (secrets registered since), without control characters, and at most
:data:`MAX_ERROR_LINE` characters. The whole text stays on the run, behind ``GET /v1/runs/{id}``
for the same ``read`` scope.

**History.** ``checked_from`` is the stream's history frontier: everything from it to
``checked_until`` is stored. ``history_start`` is how far back the intake planner loads history
for the stream, the later of the retention start and the source's history limit, as the newest
run that planned windows recorded it (``details.plan`` of its report,
:func:`memini.intake.record_plan`); it is ``None`` for streams nobody plans (push sources, git
refs). ``history_loaded`` says whether the frontier has reached it. A history still loading is
reported, not counted as behind: the planner loads it a step per run on purpose.

Every read of a workspace, source, stream, run, record, chunk or index state goes through the
caller's :class:`~memini.auth.ReadFilter`. The schema version is service metadata, not data in a
workspace, so it needs no filter.
"""

import functools
import logging
from collections import defaultdict
from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final, Literal, cast
from uuid import UUID

import psycopg
from psycopg import AsyncConnection, sql
from psycopg.rows import TupleRow
from pydantic import BaseModel, Field

from memini import __version__
from memini.auth import ReadFilter
from memini.db import Pool, load_migrations
from memini.embeddings import Embedder
from memini.index import (
    IndexFingerprint,
    IndexState,
    current_fingerprint,
    read_index_states,
    reindex_command,
)
from memini.logs import redact
from memini.workspaces import list_workspaces

__all__ = [
    "MAX_ERROR_LINE",
    "STALE_AFTER_SCHEDULES",
    "BehindReason",
    "DatabaseStatus",
    "GitPin",
    "IndexStatus",
    "ServiceIndexState",
    "ServiceStatus",
    "StreamRun",
    "StreamStatus",
    "WorkspaceIndex",
    "WorkspaceIndexState",
    "WorkspaceStatus",
    "error_line",
    "expected_schema_version",
    "read_status",
    "service_fingerprint",
    "stream_is_current",
]

STALE_AFTER_SCHEDULES: Final = 2
"""A scheduled stream is current while its lag is at most this many times its schedule."""

MAX_ERROR_LINE: Final = 200
"""Characters of a run's error that status shows at most (see the module)."""

SourceKind = Literal["git", "push", "mcp"]
RunStatus = Literal["running", "succeeded", "partial", "failed", "cancelled"]
BehindReason = Literal["never_checked", "lag", "not_indexed", "index_stale"]
WorkspaceIndexState = Literal["ok", "stale", "none", "off"]
ServiceIndexState = Literal["ok", "behind", "stale", "none", "off", "unknown"]

_log = logging.getLogger(__name__)


class StreamRun(BaseModel):
    """A run of a stream. ``GET /v1/runs/{id}`` has its report and its whole error."""

    id: UUID
    kind: str = Field(description="What ran: git_sync, mcp_sync, ingest …")
    status: RunStatus = Field(description="running, succeeded, partial, failed or cancelled.")
    started_at: datetime
    finished_at: datetime | None = Field(description="null while the run is running.")
    error: str | None = Field(
        default=None,
        description=f"Why the run failed or is partial: the first line of its error, redacted, at"
        f" most {MAX_ERROR_LINE} characters; null for other runs.",
    )


class GitPin(BaseModel):
    """Where a git stream's records are complete (``stream.cursor``, :mod:`memini.gitsync`)."""

    commit: str | None = Field(
        description="The commit the stream's records are complete at; null before the first"
        " complete sync and while the ref is missing at the remote."
    )
    partial: bool = Field(
        description="A sync committed records past the pin, then stopped: the next sync lists the"
        " ref in full."
    )


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
        description="false when the stream is behind for any reason in behind; true when it has a"
        " schedule and is not; null for a stream without a schedule that is not behind (its lag"
        " is not judged)."
    )
    behind: list[BehindReason] = Field(
        default_factory=list[BehindReason],
        description="Why the stream is behind; empty when it is not. never_checked: scheduled,"
        " never completed a window. lag: the lag is over"
        f" {STALE_AFTER_SCHEDULES} times the schedule. not_indexed: records stored but not"
        " indexed while an embedding provider is set. index_stale: the workspace's index was"
        " built with another fingerprint; see the workspace's index.",
    )
    last_run: StreamRun | None = Field(description="The newest run; null if it never ran.")
    failures: int = Field(
        default=0,
        description="Failed runs since the newest run that succeeded or was partial; every attempt"
        " of a job is a run.",
    )
    last_failure: StreamRun | None = Field(
        default=None, description="The newest of those failed runs; null when failures is 0."
    )
    missing: bool = Field(
        default=False,
        description="What the stream follows no longer exists at the source (a git ref deleted at "
        "the remote): its records are tombstones until it comes back.",
    )
    git: GitPin | None = Field(default=None, description="The pin of a git stream; null otherwise.")
    checked_from: datetime | None = Field(
        default=None,
        description="Where the stream's checked history starts: everything from it to"
        " checked_until is stored. null before the first complete window, and where the start"
        " is not recorded (a git ref): the history then starts at checked_until.",
    )
    history_start: datetime | None = Field(
        default=None,
        description="How far back the intake planner loads history for the stream, as its newest"
        " planned run recorded it; null for a stream without one (push, git).",
    )
    history_loaded: bool | None = Field(
        default=None,
        description="Whether the history is loaded back to history_start; null without one.",
    )
    records: int = Field(default=0, description="Live records the stream stored last.")
    records_not_indexed: int = Field(
        default=0,
        description="Of those, records with a payload but no chunk: stored, not indexed.",
    )


class WorkspaceIndex(BaseModel):
    """The index of one workspace."""

    state: WorkspaceIndexState = Field(
        description="ok: built with the fingerprint the service is configured for. stale: built"
        " with another one; search and indexing in the workspace are refused until it is"
        " rebuilt. none: nothing indexed yet. off: no embedding provider is configured."
    )
    built_with: str | None = Field(
        description="The embedding model the index was built with; null when nothing is indexed."
    )
    changed: list[str] = Field(
        description="When stale: what differs from the service's configuration (model,"
        " dimension, chunker version …)."
    )
    records: int = Field(description="Live records in the workspace.")
    records_not_indexed: int = Field(
        description="Live records with a payload but no chunk: stored, not indexed."
    )
    chunks: int = Field(description="Chunks in the workspace's index.")
    behind: bool = Field(
        description="Stale, or records not indexed while an embedding provider is configured."
    )
    reindex: str | None = Field(
        description="The command that brings the index up to date when it is behind."
    )


class WorkspaceStatus(BaseModel):
    id: UUID
    name: str
    streams: list[StreamStatus] = Field(description="By source name, then key.")
    index: WorkspaceIndex


class DatabaseStatus(BaseModel):
    reachable: bool
    schema_version: int | None = Field(
        description="The newest migration applied; null when the database is not reachable."
    )
    expected_schema_version: int = Field(
        description="The newest migration this version of Memini ships."
    )


class IndexStatus(BaseModel):
    """The index across the workspaces the caller may read."""

    state: ServiceIndexState = Field(
        description="off: no embedding provider is configured. unknown: the database is not"
        " reachable. stale: some workspace's index was built with another fingerprint. behind:"
        " some workspace holds records that are not indexed. none: nothing is indexed yet. ok:"
        " otherwise. Each workspace has its own index state."
    )
    configured: str | None = Field(
        description="The embedding model the service indexes and searches with; null when off."
    )
    note: str


class ServiceStatus(BaseModel):
    """Whether the base is current, and the state of everything that decides it."""

    version: str = Field(description="The Memini version.")
    as_of: datetime = Field(description="When this was read, by the database clock if reachable.")
    current: bool = Field(
        description="True when the database is reachable, no stream is behind and no workspace's"
        " index is behind; streams without a schedule are judged by their index only."
    )
    database: DatabaseStatus
    workspaces: list[WorkspaceStatus] = Field(
        description="The workspaces the token may read, by name."
    )
    index: IndexStatus


def stream_is_current(schedule_seconds: float | None, lag_seconds: float | None) -> bool | None:
    """Whether a stream is current by its lag alone; ``None`` for a stream without a schedule.
    :func:`read_status` also counts the index (see the module)."""
    if schedule_seconds is None:
        return None
    if lag_seconds is None:
        return False
    return lag_seconds <= STALE_AFTER_SCHEDULES * schedule_seconds


def error_line(text: str | None) -> str | None:
    """What status shows of a run's error: the first line, redacted, printable, at most
    :data:`MAX_ERROR_LINE` characters (see the module); ``None`` for no text."""
    if text is None:
        return None
    lines = redact(text).strip().splitlines()
    line = "".join(c for c in lines[0] if c.isprintable()).strip() if lines else ""
    if len(line) > MAX_ERROR_LINE:
        line = line[: MAX_ERROR_LINE - 1] + "…"
    return line or None


def service_fingerprint(embedder: Embedder | None) -> IndexFingerprint | None:
    """The fingerprint the service indexes and searches with: that of its embedder, ``None``
    without one."""
    return None if embedder is None else current_fingerprint(embedder.spec)


@functools.cache
def expected_schema_version() -> int:
    """The newest migration this version of Memini ships."""
    return load_migrations()[-1].version


async def read_status(
    pool: Pool | None, access: ReadFilter, *, fingerprint: IndexFingerprint | None = None
) -> ServiceStatus:
    """The status of the base as ``access`` may see it.

    ``fingerprint`` is what the service indexes and searches with (:func:`service_fingerprint`);
    ``None`` when no embedding provider is configured: indexes are then reported ``off``.

    A database that cannot be reached is reported, not raised: the status is then not current and
    lists no workspaces. Without a pool the database counts as not reachable.
    """
    if pool is not None:
        try:
            async with pool.connection() as conn:
                return await _read(conn, access, fingerprint)
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
        index=_service_index(fingerprint, [], reachable=False),
    )


async def _read(
    conn: AsyncConnection[TupleRow], access: ReadFilter, fingerprint: IndexFingerprint | None
) -> ServiceStatus:
    cur = await conn.execute("SELECT now(), (SELECT max(version) FROM schema_migration)")
    row = await cur.fetchone()
    if row is None:  # a SELECT without FROM always returns one row
        raise RuntimeError("SELECT now() returned no row")
    as_of: datetime = row[0]
    schema_version: int | None = row[1]

    workspaces = await list_workspaces(conn, access)
    indexes = await _indexes(conn, access, fingerprint)
    stale = {workspace_id for workspace_id, index in indexes.items() if index.state == "stale"}
    streams = await _streams(conn, access, fingerprint, stale)
    statuses = [
        WorkspaceStatus(
            id=workspace.id,
            name=workspace.name,
            streams=streams[workspace.id],
            # A workspace created after the counts were read has nothing in it yet.
            index=indexes.get(workspace.id) or _index(workspace.name, None, fingerprint),
        )
        for workspace in workspaces
    ]
    current = not any(
        status.index.behind or any(stream.current is False for stream in status.streams)
        for status in statuses
    )
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
        index=_service_index(fingerprint, statuses, reachable=True),
    )


def _readable(
    access: ReadFilter, alias: str, *, source: str | None = "source_id"
) -> sql.Composable:
    return access.where(
        workspace_id=sql.Identifier(alias, "workspace_id"),
        owner_id=sql.Identifier(alias, "owner_id"),
        source_id=None if source is None else sql.Identifier(alias, source),
    )


# --- Index ---------------------------------------------------------------------------------------

# A live record with a payload and no chunk is not indexed (see the module). The chunks of a
# record are found by the chunk table's (workspace_id, source_id, record_id, id) key.
_NOT_INDEXED: Final = sql.SQL("""
rec.payload IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM chunk c
    WHERE c.workspace_id = rec.workspace_id AND c.source_id = rec.source_id
      AND c.record_id = rec.id AND {chunk_readable}
)
""")

_WORKSPACE_COUNTS: Final = sql.SQL("""
SELECT w.id, w.name, n.records, n.not_indexed,
       (SELECT count(*) FROM chunk c WHERE c.workspace_id = w.id AND {chunk_readable})
FROM workspace w
CROSS JOIN LATERAL (
    SELECT count(*) AS records, count(*) FILTER (WHERE {not_indexed}) AS not_indexed
    FROM record rec
    WHERE rec.workspace_id = w.id AND rec.deleted_at IS NULL AND {record_readable}
) n
WHERE {workspace_readable}
""")


async def _indexes(
    conn: AsyncConnection[TupleRow], access: ReadFilter, fingerprint: IndexFingerprint | None
) -> dict[UUID, WorkspaceIndex]:
    states = await read_index_states(conn, access)
    query = _WORKSPACE_COUNTS.format(
        chunk_readable=_readable(access, "c"),
        not_indexed=_NOT_INDEXED.format(chunk_readable=_readable(access, "c")),
        record_readable=_readable(access, "rec"),
        workspace_readable=access.where(
            workspace_id=sql.Identifier("w", "id"), owner_id=sql.Identifier("w", "owner_id")
        ),
    )
    cur = await conn.execute(query)
    indexes: dict[UUID, WorkspaceIndex] = {}
    for workspace_id, name, records, not_indexed, chunks in await cur.fetchall():
        indexes[workspace_id] = _index(
            name,
            states.get(workspace_id),
            fingerprint,
            records=records,
            not_indexed=not_indexed,
            chunks=chunks,
        )
    return indexes


def _index(
    workspace: str,
    state: IndexState | None,
    fingerprint: IndexFingerprint | None,
    *,
    records: int = 0,
    not_indexed: int = 0,
    chunks: int = 0,
) -> WorkspaceIndex:
    changed: tuple[str, ...] = ()
    if fingerprint is None:
        condition: WorkspaceIndexState = "off"
    elif state is None:
        condition = "none"
    elif state.fingerprint != fingerprint.digest:
        condition = "stale"
        changed = state.built_with.changes(fingerprint) or ("fingerprint encoding",)
    else:
        condition = "ok"
    behind = condition == "stale" or (fingerprint is not None and not_indexed > 0)
    return WorkspaceIndex(
        state=condition,
        built_with=None if state is None else state.built_with.embedding.describe(),
        changed=list(changed),
        records=records,
        records_not_indexed=not_indexed,
        chunks=chunks,
        behind=behind,
        reindex=reindex_command(workspace) if behind else None,
    )


def _service_index(
    fingerprint: IndexFingerprint | None,
    workspaces: Sequence[WorkspaceStatus],
    *,
    reachable: bool,
) -> IndexStatus:
    configured = None if fingerprint is None else fingerprint.embedding.describe()
    if fingerprint is None:
        return IndexStatus(
            state="off",
            configured=None,
            note="No embedding provider is configured: nothing is indexed or searched. Records"
            " stored meanwhile are indexed by `memini reindex` once one is set.",
        )
    if not reachable:
        return IndexStatus(
            state="unknown", configured=configured, note="The database is not reachable."
        )
    stale = [w.name for w in workspaces if w.index.state == "stale"]
    behind = [w.name for w in workspaces if w.index.behind and w.index.state != "stale"]
    if stale:
        return IndexStatus(
            state="stale",
            configured=configured,
            note=f"Built with another model or chunker in {_names(stale)}: search and indexing"
            f" there are refused until {_rebuild(stale)}.",
        )
    if behind:
        return IndexStatus(
            state="behind",
            configured=configured,
            note=f"Records stored but not indexed in {_names(behind)}; {_rebuild(behind)}"
            " indexes them.",
        )
    if not any(w.index.state == "ok" for w in workspaces):
        return IndexStatus(state="none", configured=configured, note="Nothing is indexed yet.")
    return IndexStatus(state="ok", configured=configured, note=f"Built with {configured}.")


def _rebuild(workspaces: Sequence[str]) -> str:
    if len(workspaces) == 1:
        return f"`{reindex_command(workspaces[0])}`"
    return f"`{reindex_command('NAME')}` for each"


def _names(workspaces: Collection[str]) -> str:
    noun = "workspace" if len(workspaces) == 1 else "workspaces"
    return f"{noun} {', '.join(workspaces)}"


# --- Streams -------------------------------------------------------------------------------------

# The newest run of each stream comes from run_stream_started_idx, and so do the newest run that
# succeeded or was partial (ok), the failed runs since (f, lf) and the newest planned run (p, mcp
# sources only). Lag and schedule are seconds as float8: extract() returns numeric.
_STREAMS: Final = sql.SQL("""
SELECT st.workspace_id, s.name, s.kind, st.key,
       extract(epoch FROM st.schedule)::float8,
       st.checked_until, st.checked_from,
       extract(epoch FROM now() - st.checked_until)::float8,
       st.cursor,
       r.id, r.kind, r.status, r.started_at, r.finished_at, r.error,
       f.failures,
       lf.id, lf.kind, lf.status, lf.started_at, lf.finished_at, lf.error,
       p.history_start,
       n.records, n.not_indexed
FROM stream st
JOIN source s ON s.id = st.source_id AND s.workspace_id = st.workspace_id
LEFT JOIN LATERAL (
    SELECT r.id, r.kind, r.status, r.started_at, r.finished_at, r.error
    FROM run r
    WHERE r.stream_id = st.id AND {r_readable}
    ORDER BY r.started_at DESC, r.id DESC
    LIMIT 1
) r ON TRUE
LEFT JOIN LATERAL (
    SELECT ok.id, ok.started_at
    FROM run ok
    WHERE ok.stream_id = st.id AND ok.status IN ('succeeded', 'partial') AND {ok_readable}
    ORDER BY ok.started_at DESC, ok.id DESC
    LIMIT 1
) ok ON TRUE
CROSS JOIN LATERAL (
    SELECT count(*) AS failures
    FROM run x
    WHERE x.stream_id = st.id AND x.status = 'failed' AND {x_readable}
      AND (ok.id IS NULL OR (x.started_at, x.id) > (ok.started_at, ok.id))
) f
LEFT JOIN LATERAL (
    SELECT lf.id, lf.kind, lf.status, lf.started_at, lf.finished_at, lf.error
    FROM run lf
    WHERE lf.stream_id = st.id AND lf.status = 'failed' AND {lf_readable}
      AND (ok.id IS NULL OR (lf.started_at, lf.id) > (ok.started_at, ok.id))
    ORDER BY lf.started_at DESC, lf.id DESC
    LIMIT 1
) lf ON TRUE
LEFT JOIN LATERAL (
    SELECT p.report #>> '{{details,plan,history_start}}' AS history_start
    FROM run p
    WHERE s.kind = 'mcp' AND p.stream_id = st.id
      AND p.report #> '{{details,plan,history_start}}' IS NOT NULL AND {p_readable}
    ORDER BY p.started_at DESC, p.id DESC
    LIMIT 1
) p ON TRUE
CROSS JOIN LATERAL (
    SELECT count(*) AS records, count(*) FILTER (WHERE {not_indexed}) AS not_indexed
    FROM record rec
    WHERE rec.stream_id = st.id AND rec.deleted_at IS NULL AND {record_readable}
) n
WHERE {stream_readable} AND {source_readable}
ORDER BY s.name, st.key
""")


async def _streams(
    conn: AsyncConnection[TupleRow],
    access: ReadFilter,
    fingerprint: IndexFingerprint | None,
    stale: Collection[UUID],
) -> defaultdict[UUID, list[StreamStatus]]:
    """The streams ``access`` may read, by workspace; ``stale``: the workspaces whose index is."""
    query = _STREAMS.format(
        r_readable=_readable(access, "r"),
        ok_readable=_readable(access, "ok"),
        x_readable=_readable(access, "x"),
        lf_readable=_readable(access, "lf"),
        p_readable=_readable(access, "p"),
        not_indexed=_NOT_INDEXED.format(chunk_readable=_readable(access, "c")),
        record_readable=_readable(access, "rec"),
        stream_readable=_readable(access, "st"),
        source_readable=_readable(access, "s", source="id"),
    )
    cur = await conn.execute(query)
    found: defaultdict[UUID, list[StreamStatus]] = defaultdict(list)
    for row in await cur.fetchall():
        workspace_id: UUID = row[0]
        found[workspace_id].append(
            _stream(row, indexing=fingerprint is not None, stale=workspace_id in stale)
        )
    return found


def _run(row: Sequence[Any]) -> StreamRun | None:
    run_id, kind, status, started_at, finished_at, error = row
    if run_id is None:
        return None
    return StreamRun(
        id=run_id,
        kind=kind,
        status=status,
        started_at=started_at,
        finished_at=finished_at,
        error=error_line(error) if status in ("failed", "partial") else None,
    )


def _stream(row: Sequence[Any], *, indexing: bool, stale: bool) -> StreamStatus:
    """A stream from a row of :data:`_STREAMS`. ``indexing``: an embedding provider is set;
    ``stale``: the index of the stream's workspace is."""
    (_, source, kind, key, schedule, checked_until, checked_from, lag, cursor) = row[:9]
    failures: int = row[15]
    history_start = _time(row[22])
    records: int = row[23]
    not_indexed: int = row[24]

    by_lag = stream_is_current(schedule, lag)
    behind: list[BehindReason] = []
    if by_lag is False:
        behind.append("never_checked" if lag is None else "lag")
    if indexing and not_indexed > 0:
        behind.append("not_indexed")
    if stale:
        behind.append("index_stale")

    pin = _mapping(cursor)
    commit = pin.get("commit")
    frontier: datetime | None = checked_from if checked_from is not None else checked_until
    return StreamStatus(
        source=source,
        source_kind=kind,
        key=key,
        schedule_seconds=schedule,
        checked_until=checked_until,
        lag_seconds=lag,
        current=False if behind else by_lag,
        behind=behind,
        last_run=_run(row[9:15]),
        failures=failures,
        last_failure=_run(row[16:22]),
        missing=pin.get("missing") is True,
        git=None
        if kind != "git"
        else GitPin(
            commit=commit if isinstance(commit, str) else None,
            partial=pin.get("partial") is True,
        ),
        checked_from=checked_from,
        history_start=history_start,
        history_loaded=None
        if history_start is None
        else (frontier is not None and frontier <= history_start),
        records=records,
        records_not_indexed=not_indexed,
    )


def _mapping(value: object) -> Mapping[str, object]:
    """A JSON object from the database; empty for anything else (a NULL cursor)."""
    return cast(Mapping[str, object], value) if isinstance(value, Mapping) else {}


def _time(value: object) -> datetime | None:
    """A time a run report holds as ISO text; ``None`` for anything else."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None
