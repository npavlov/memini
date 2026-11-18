"""What the planner needs from the database: a stream's state, and the ``as_of`` pinned for a run;
and where its plan goes, the run report."""

from datetime import datetime, timedelta
from typing import Any, Final
from uuid import UUID

from psycopg import AsyncConnection

from memini.intake.plan import Plan, StreamState, WindowSettings, plan_windows
from memini.runs import Run

__all__ = [
    "AS_OF_ARG",
    "PLAN_DETAIL",
    "load_stream_state",
    "pin_as_of",
    "plan_stream",
    "record_plan",
]

AS_OF_ARG: Final = "as_of"
"""The key of a sync job's ``args`` that holds the time pinned for its run."""

PLAN_DETAIL: Final = "plan"
"""The name of the plan in a run report's ``details``."""

_STATE: Final = """
SELECT st.checked_until, st.checked_from, s.retention_days
FROM stream st JOIN source s ON s.id = st.source_id
WHERE st.id = %s
"""

# Sets as_of on the first call for the job and returns it on every call. The outer SELECT sees the
# row as it was before the UPDATE: pinned has a row when this call set the value, and otherwise
# the row holds the value an earlier call set. No row: no such job.
_PIN: Final = """
WITH pinned AS (
    UPDATE job SET args = args || jsonb_build_object('as_of', now()), updated_at = now()
    WHERE id = %(job)s AND NOT args ? 'as_of'
    RETURNING args ->> 'as_of' AS as_of
)
SELECT coalesce(p.as_of, j.args ->> 'as_of') FROM job j LEFT JOIN pinned p ON true
WHERE j.id = %(job)s
"""


async def load_stream_state(conn: AsyncConnection[Any], stream_id: UUID) -> StreamState:
    """The checked range of the stream ``stream_id`` and its source's retention.

    Reads in a transaction of its own (a savepoint inside the caller's), so an idle connection
    stays idle for the ingest batches that follow.

    Raises:
        LookupError: no such stream.
    """
    async with conn.transaction():
        cur = await conn.execute(_STATE, (stream_id,))
        row = await cur.fetchone()
    if row is None:
        raise LookupError(f"stream {stream_id} does not exist")
    checked_until: datetime | None = row[0]
    checked_from: datetime | None = row[1]
    retention_days: int | None = row[2]
    return StreamState(
        checked_until=checked_until,
        checked_from=checked_from,
        retention=None if retention_days is None else timedelta(days=retention_days),
    )


async def pin_as_of(conn: AsyncConnection[Any], job_id: UUID) -> datetime:
    """The ``as_of`` of the job ``job_id``: the database time of its first call, kept in the job's
    ``args`` (:data:`AS_OF_ARG`), so that every attempt of the job plans from the same time and a
    retry of a failed attempt gets the same windows.

    Runs in a transaction of its own (a savepoint inside the caller's): give it an idle connection
    so that the pin commits before the attempt goes on. A job enqueued with ``as_of`` in its args
    keeps that time.

    Raises:
        LookupError: no such job.
        ValueError: the job's ``as_of`` is not a time with a zone.
    """
    async with conn.transaction():
        cur = await conn.execute(_PIN, {"job": job_id})
        row = await cur.fetchone()
    if row is None:
        raise LookupError(f"job {job_id} does not exist")
    value: object = row[0]
    if not isinstance(value, str):  # ->> returns text, or NULL for a JSON null
        raise ValueError(f"job {job_id}: {AS_OF_ARG} is not a time")
    try:
        as_of = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"job {job_id}: {AS_OF_ARG} is not a time") from None
    if as_of.tzinfo is None:
        raise ValueError(f"job {job_id}: {AS_OF_ARG} has no time zone")
    return as_of


async def plan_stream(
    conn: AsyncConnection[Any], stream_id: UUID, settings: WindowSettings, as_of: datetime
) -> Plan:
    """The plan of the stream ``stream_id`` at ``as_of``: :func:`load_stream_state`, then
    :func:`~memini.intake.plan_windows`."""
    return plan_windows(await load_stream_state(conn, stream_id), settings, as_of)


def record_plan(run: Run, plan: Plan) -> None:
    """Put ``plan`` in the run's report, as ``details.plan`` (principle 10): which windows the run
    attempts, and why any are left out."""
    run.detail(PLAN_DETAIL, plan.to_json())
