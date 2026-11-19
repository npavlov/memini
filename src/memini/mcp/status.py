"""The ``status`` tool: whether the base is current, per stream, per workspace index and as a
whole.

The structured result is :class:`memini.status.ServiceStatus`, the same as ``GET /v1/status``
returns; the definitions of lag, "behind" and "current", and what is shown of a run's error, are in
:mod:`memini.status`.
"""

from datetime import datetime

from memini import status
from memini.auth import authorization
from memini.mcp.text import inline
from memini.mcp.tools import NoArguments, Tool, ToolCall

__all__ = ["STATUS_TOOL", "summarize_status"]


async def _status(call: ToolCall, _arguments: NoArguments) -> status.ServiceStatus:
    # Called through its module, so a test can watch the tool go through the filter.
    access = authorization.authorize_read(call.principal)
    fingerprint = status.service_fingerprint(call.services.embedder)
    return await status.read_status(call.pool, access, fingerprint=fingerprint)


def _duration(seconds: float) -> str:
    whole = round(seconds)
    if abs(whole) < 120:
        return f"{whole}s"
    if abs(whole) < 2 * 3600:
        return f"{whole // 60}m"
    if abs(whole) < 2 * 86400:
        return f"{whole // 3600}h"
    return f"{whole // 86400}d"


def _time(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _stream_line(workspace: str, stream: status.StreamStatus) -> str:
    where = f"{workspace}/{stream.source} ({stream.source_kind}) {inline(stream.key)}"
    if stream.lag_seconds is None:
        state = "never checked"
    else:
        state = f"lag {_duration(stream.lag_seconds)}"
    if stream.missing:
        state = f"missing at the source, {state}"
    if stream.schedule_seconds is not None:
        state += f", schedule {_duration(stream.schedule_seconds)}"
    run = stream.last_run
    if run is None:
        state += ", no run yet"
    elif run.finished_at is None:
        state += f", run {run.status} since {_time(run.started_at)}"
    else:
        state += f", last run {run.status} at {_time(run.finished_at)}"
    failure = stream.last_failure
    if failure is not None:
        times = "once" if stream.failures == 1 else f"{stream.failures} times"
        state += f"; failed {times} in a row"
        if failure.error is not None:
            state += f": {inline(failure.error)}"
    if stream.records_not_indexed:
        state += f"; {stream.records_not_indexed} records not indexed"
    if "index_stale" in stream.behind:
        state += "; index stale"
    return f"- {where}: {state}"


def _index_line(workspace: status.WorkspaceStatus) -> str:
    index = workspace.index
    state = f"index {index.state}"
    if index.changed:
        state += f" (changed: {', '.join(index.changed)})"
    if index.records_not_indexed:
        state += f", {index.records_not_indexed} records not indexed"
    if index.reindex is not None:
        state += f"; run `{index.reindex}`"
    return f"- {workspace.name}: {state}"


def summarize_status(result: status.ServiceStatus) -> str:
    """The text content of a status result: the verdict, the database, streams that are behind,
    fail or whose ref is missing at the source, and workspaces whose index is behind."""
    database = result.database
    if database.reachable:
        schema = (
            f"schema version {database.schema_version}"
            f" (this version expects {database.expected_schema_version})"
        )
        database_line = f"Database: reachable, {schema}."
    else:
        database_line = "Database: not reachable."
    streams = [(w.name, s) for w in result.workspaces for s in w.streams]
    not_current = sum(1 for _, s in streams if s.current is False)
    missing = sum(1 for _, s in streams if s.missing)
    counts = f"behind or never checked: {not_current}"
    if missing:
        counts += f"; missing at the source: {missing}"
    lines = [
        f"Memini {result.version}, as of {_time(result.as_of)}.",
        f"Current: {'yes' if result.current else 'no'}.",
        database_line,
        f"Workspaces: {len(result.workspaces)}; streams: {len(streams)}; {counts}.",
        *(
            _stream_line(workspace, stream)
            for workspace, stream in streams
            if stream.current is False or stream.missing or stream.failures
        ),
        f"Index: {result.index.note}",
        *(_index_line(workspace) for workspace in result.workspaces if workspace.index.behind),
    ]
    return "\n".join(lines)


STATUS_TOOL = Tool(
    name="status",
    title="Status",
    description=(
        "Whether Memini's knowledge base is current. Per stream of every workspace the token may "
        "read: its source, schedule, lag (seconds since everything up to checked_until was "
        "stored), why it is behind if it is (lag, never checked, records not indexed, a stale "
        "index), its last run, failed runs in a row with the first line of the last error, how "
        "far back its history is loaded, its git pin, and whether what it follows is missing at "
        "the source (a deleted branch). Per workspace: its index (ok, stale, none or off), "
        "record and chunk counts, and the command that brings it up to date. Also the database "
        "and schema version. Read-only; takes no arguments."
    ),
    arguments=NoArguments,
    result=status.ServiceStatus,
    run=_status,
    summarize=summarize_status,
)
