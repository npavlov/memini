"""The intake planner (requirements, section 4, Scheduling and hygiene): which windows of a stream
one sync runs, from the stream's checked range, the recipe's window settings with the source's
overrides, the source's retention, and a time pinned for the run.

- :mod:`memini.intake.plan`: the planner itself, pure (:func:`plan_windows`), and the settings
  (:func:`resolve_settings`). Its module documents the windows.
- :mod:`memini.intake.state`: the stream's state from the database (:func:`load_stream_state`),
  the pinned time (:func:`pin_as_of`), the plan in the run report (:func:`record_plan`).

**State.** A stream's checked range is ``[checked_from, checked_until)``: the history frontier and
the checkpoint. Both move only in the ingest transaction that stores a complete window of their
data (principle 7; :mod:`memini.ingest.checkpoint`): a complete ``initial`` or ``forward`` window
moves the checkpoint forward to its end, a complete ``backfill`` window the frontier back to its
start. A backfill window ends at or before the checkpoint, so it never moves the checkpoint, and
its cursor is not stored. The executor's opaque ``stream.cursor`` is not the planner's: windows
are times.

**The pinned time.** ``as_of`` is the database clock (never this process's), pinned once per run:
for a sync job, by :func:`pin_as_of` into the job's ``args``, so every attempt of the job, retries
included, plans from the same time; for an agent executor's session, when the session starts, kept
with the session. No window ends after it, so a window never claims time that has not passed.

**What an executor does** (the service executor runs it in its ``mcp_sync`` job handler; the agent
executor in its start call and its next-call loop)::

    as_of = await pin_as_of(conn, job.id)                  # or the session's pinned time
    settings = resolve_settings(recipe.window, source_config.get("window"))
    plan = await plan_stream(conn, job.stream_id, settings, as_of)
    record_plan(run, plan)                                 # details.plan of the run report
    for chain in (plan.forward, plan.backfill):
        for window in chain:
            async with run.step(window.label):
                complete = ...                             # calls, raw responses, checks, parser
                await ingest(..., Batch(records=..., stream=key,
                                        window=window.claim(complete=complete, snapshot=...)))
            if not complete:
                break                                      # the rest of this chain waits

Each window is one ingest batch that claims it (:meth:`PlannedWindow.claim`), complete only when
every completeness check passed. A chain stops at its first incomplete window: the windows after it
would leave a gap (a later forward window is refused by ingest as ``window_gap``; a later history
window would store records and leave the frontier). The two chains are independent: an incomplete
forward window does not stop the history step. The next run plans from where the windows that
completed left the checkpoint and the frontier, so a failed or incomplete window is planned again.
"""

from memini.intake.plan import (
    DEFAULT_HISTORY,
    MAX_WINDOWS,
    OVERRIDABLE,
    BackfillSkip,
    ForwardSkip,
    Moves,
    Plan,
    PlannedWindow,
    StreamState,
    WindowKind,
    WindowSettings,
    plan_windows,
    resolve_settings,
)
from memini.intake.state import (
    AS_OF_ARG,
    PLAN_DETAIL,
    load_stream_state,
    pin_as_of,
    plan_stream,
    record_plan,
)

__all__ = [
    "AS_OF_ARG",
    "DEFAULT_HISTORY",
    "MAX_WINDOWS",
    "OVERRIDABLE",
    "PLAN_DETAIL",
    "BackfillSkip",
    "ForwardSkip",
    "Moves",
    "Plan",
    "PlannedWindow",
    "StreamState",
    "WindowKind",
    "WindowSettings",
    "load_stream_state",
    "pin_as_of",
    "plan_stream",
    "plan_windows",
    "record_plan",
    "resolve_settings",
]
