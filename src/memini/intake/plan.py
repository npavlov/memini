"""The planner: which windows of a stream one sync runs. Pure: no database, no clock.

Input: the stream's checked range (:class:`StreamState`: the checkpoint ``checked_until``, the
history frontier ``checked_from``, the source's retention), the window settings
(:class:`WindowSettings`: the recipe's ``[window]`` with the source's overrides) and ``as_of``, the
database time pinned for the run. Output: a :class:`Plan`, the windows ``[from, to)`` to run, in
the order to run them, each tagged with its kind and with what a complete result of it moves.

**History start.** Nothing older than the history start is planned: the later of the retention
start (``as_of`` less the source's retention) and ``as_of`` less ``history`` (the source's limit).
A source with neither keeps :data:`DEFAULT_HISTORY` back, or ``initial`` back if that is longer.

**A new stream** (no checkpoint) gets the initial load, ``[as_of - initial, as_of)``, clipped at
the history start: kind ``initial``. Its first complete window sets the checkpoint to its end and
the frontier to its start (:mod:`memini.ingest.checkpoint`). No history step yet.

**A stream with a checkpoint** gets the forward window ``[checkpoint - overlap, as_of)``: the
overlap margin catches late replies and edits. Its start is not before the history start, nor
after the checkpoint (a complete window must start at or before it, or ingest refuses the gap).
Kind ``forward``. When ``as_of`` is not after the checkpoint there is no forward window, and the
plan says why: ``caught_up`` (equal: a retry whose forward windows all completed), or
``checkpoint_ahead`` (the database clock went back, or another writer moved the checkpoint past
the pinned time). The planner never plans a window that ends after ``as_of``.

**History.** With ``backfill`` set, a stream with a checkpoint also gets at most one history step,
``[frontier - backfill, frontier)``, clipped at the history start: kind ``backfill``. The frontier
is ``checked_from`` (the checkpoint where it was never recorded). Once the frontier is at or before
the history start the history is loaded, and the plan has no backfill step: it stops exactly at
the retention start. A longer retention later resumes it from the frontier; a shorter one ends it.

**Splitting.** A window longer than ``max_span`` is split into windows of ``max_span`` (the last
one shorter). The initial and forward windows run oldest first: each complete one moves the
checkpoint to its end, where the next one starts. The history windows run newest first: each
complete one moves the frontier to its start, where the next one ends. Each chain holds at most
``max_windows`` windows; a longer one is cut (``truncated``), and the next run goes on from where
this one left the checkpoint or the frontier.

**Determinism.** The plan is a function of its input: the same state, settings and ``as_of`` give
the same plan, whatever the time zone of the datetimes (they are normalized to UTC). A run that
fails, or ends with a window incomplete, leaves the checkpoint and the frontier where they were
(principle 7), so its retry, with the same pinned ``as_of``, gets the same windows again. The last
run's result is therefore not an input: what it stored is already in the state.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal

from pydantic import JsonValue

from memini.ingest.model import WindowClaim
from memini.recipes.model import Window
from memini.recipes.times import parse_duration

__all__ = [
    "DEFAULT_HISTORY",
    "MAX_WINDOWS",
    "OVERRIDABLE",
    "BackfillSkip",
    "ForwardSkip",
    "Moves",
    "Plan",
    "PlannedWindow",
    "StreamState",
    "WindowKind",
    "WindowSettings",
    "plan_windows",
    "resolve_settings",
]

DEFAULT_HISTORY: Final = timedelta(days=365)
"""How far back history loads for a source with neither a retention nor a ``history`` limit (or
``initial`` back, if that is longer)."""

MAX_WINDOWS: Final = 100
"""Windows one chain (initial or forward; history) of a plan holds at most."""

OVERRIDABLE: Final = ("overlap", "initial", "backfill", "max_span", "history")
"""The keys of a source's ``window`` overrides. ``history`` is the source's only: no recipe sets
it."""

_REQUIRED: Final = frozenset({"overlap", "initial"})

WindowKind = Literal["initial", "forward", "backfill"]
"""``initial``: the first load of a new stream. ``forward``: from the checkpoint less the overlap
to ``as_of``. ``backfill``: a step of older history, ending at the frontier."""

Moves = Literal["checkpoint", "frontier"]
"""What a complete result of a window moves: the checkpoint forward to the window's end, or the
frontier back to its start."""

ForwardSkip = Literal["caught_up", "checkpoint_ahead"]
"""Why a stream with a checkpoint has no forward window: ``as_of`` equals the checkpoint, or is
before it."""

BackfillSkip = Literal["not_configured", "initial_load", "history_loaded", "frontier_ahead"]
"""Why a plan has no history step: no ``backfill`` setting; a new stream, which loads its initial
window first; the frontier is at or before the history start; the frontier is after ``as_of``."""


# --- Input ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class WindowSettings:
    """How the planner cuts a stream's history: the recipe's ``[window]`` with the source's
    overrides (:func:`resolve_settings`)."""

    overlap: timedelta
    """How far behind the checkpoint the forward window starts."""
    initial: timedelta
    """How much recent history the first sync of a stream loads."""
    backfill: timedelta | None = None
    """How much older history each later run loads; ``None``: none beyond the initial window."""
    max_span: timedelta | None = None
    """The longest window; a longer one is split. ``None``: no limit."""
    history: timedelta | None = None
    """How far back from ``as_of`` history loads at most, besides the retention; ``None``: as far
    as the retention, or :data:`DEFAULT_HISTORY` (at least ``initial``) without one."""

    def __post_init__(self) -> None:
        if self.overlap < timedelta(0):
            raise ValueError("overlap is not negative")
        for name in ("initial", "backfill", "max_span", "history"):
            value: timedelta | None = getattr(self, name)
            if value is not None and value <= timedelta(0):
                raise ValueError(f"{name} is longer than zero")

    @classmethod
    def from_recipe(cls, window: Window) -> "WindowSettings":
        """The settings of a recipe's ``[window]``, without overrides."""
        return cls(
            overlap=window.overlap,
            initial=window.initial,
            backfill=window.backfill,
            max_span=window.max_span,
        )


def resolve_settings(
    window: Window, overrides: Mapping[str, object] | None = None
) -> WindowSettings:
    """The recipe's ``[window]`` with a source's overrides.

    ``overrides`` is the source's ``window`` table: any of :data:`OVERRIDABLE`, each a duration
    (``"3d"``, as in a recipe) or a :class:`~datetime.timedelta`; ``None`` turns ``backfill``,
    ``max_span`` or ``history`` off. A key left out keeps the recipe's value.

    Raises:
        ValueError: an unknown key, a value that is not a duration, or ``None`` for ``overlap`` or
            ``initial``; the message names the key.
    """
    values: dict[str, timedelta | None] = {
        "overlap": window.overlap,
        "initial": window.initial,
        "backfill": window.backfill,
        "max_span": window.max_span,
        "history": None,
    }
    for key, value in (overrides or {}).items():
        if key not in OVERRIDABLE:
            raise ValueError(f"window.{key}: unknown key; one of {', '.join(OVERRIDABLE)}")
        if value is None:
            if key in _REQUIRED:
                raise ValueError(f"window.{key}: a duration, never none")
            values[key] = None
            continue
        try:
            values[key] = parse_duration(value)
        except ValueError as error:
            raise ValueError(f"window.{key}: {error}") from None
    overlap, initial = values["overlap"], values["initial"]
    if overlap is None or initial is None:  # refused above
        raise ValueError("window: overlap and initial are durations")
    return WindowSettings(
        overlap=overlap,
        initial=initial,
        backfill=values["backfill"],
        max_span=values["max_span"],
        history=values["history"],
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class StreamState:
    """What the planner reads of a stream and its source
    (:func:`memini.intake.load_stream_state`)."""

    checked_until: datetime | None = None
    """The checkpoint; ``None`` for a new stream."""
    checked_from: datetime | None = None
    """The history frontier; ``None`` for a new stream, or where it was never recorded."""
    retention: timedelta | None = None
    """The source's retention (``retention_days``); ``None``: kept until deleted at the source."""


# --- Output --------------------------------------------------------------------------------------


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class PlannedWindow:
    """One window of a plan: ``[start, end)``, in UTC."""

    kind: WindowKind
    start: datetime
    end: datetime

    @property
    def moves(self) -> Moves:
        """What a complete result moves: the checkpoint (to ``end``), or the frontier (to
        ``start``). The first complete ``initial`` window sets both."""
        return "frontier" if self.kind == "backfill" else "checkpoint"

    @property
    def label(self) -> str:
        """A name for the run step that runs the window: ``forward 2026-11-04T00:00:00Z/…``."""
        return f"{self.kind} {_iso(self.start)}/{_iso(self.end)}"

    def claim(
        self,
        *,
        complete: bool,
        snapshot: bool = False,
        cursor: Mapping[str, Any] | None = None,
    ) -> WindowClaim:
        """The ingest claim of the window, for the batch that stores its records.

        ``complete`` only when every completeness check of the window passed. ``snapshot``: the
        recipe's ``stream.snapshot``; it is claimed with a complete window only. ``cursor`` is
        stored when the checkpoint moves, so it belongs to ``initial`` and ``forward`` windows.
        """
        return WindowClaim(
            start=self.start,
            end=self.end,
            complete=complete,
            snapshot=snapshot and complete,
            cursor=cursor,
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "kind": self.kind,
            "from": _iso(self.start),
            "to": _iso(self.end),
            "moves": self.moves,
        }


@dataclass(frozen=True, slots=True)
class Plan:
    """The windows one sync of a stream runs, in order, and why any are left out."""

    as_of: datetime
    """The database time pinned for the run, in UTC. No window ends after it."""
    history_start: datetime
    """The earliest time a window may start (see the module)."""
    windows: tuple[PlannedWindow, ...]
    """The ``initial`` or ``forward`` windows oldest first, then the ``backfill`` windows newest
    first."""
    forward_skipped: ForwardSkip | None
    """Why there is no forward window; ``None`` when there is one, and for a new stream."""
    backfill_skipped: BackfillSkip | None
    """Why there is no history step; ``None`` when there is one."""
    truncated: bool
    """A chain was cut at ``max_windows``."""

    @property
    def forward(self) -> tuple[PlannedWindow, ...]:
        """The ``initial`` or ``forward`` windows: a chain that moves the checkpoint."""
        return tuple(window for window in self.windows if window.moves == "checkpoint")

    @property
    def backfill(self) -> tuple[PlannedWindow, ...]:
        """The ``backfill`` windows: a chain that moves the frontier."""
        return tuple(window for window in self.windows if window.moves == "frontier")

    def to_json(self) -> dict[str, JsonValue]:
        """The plan for a run report (``details.plan``, :func:`memini.intake.record_plan`)."""
        return {
            "as_of": _iso(self.as_of),
            "history_start": _iso(self.history_start),
            "windows": [window.to_json() for window in self.windows],
            "forward_skipped": self.forward_skipped,
            "backfill_skipped": self.backfill_skipped,
            "truncated": self.truncated,
        }


# --- The planner ---------------------------------------------------------------------------------


def _utc(moment: datetime, name: str) -> datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"{name} needs a time zone")
    return moment.astimezone(UTC)


def _history_start(
    as_of: datetime, retention: timedelta | None, settings: WindowSettings
) -> datetime:
    limits = [as_of - limit for limit in (retention, settings.history) if limit is not None]
    return max(limits) if limits else as_of - max(DEFAULT_HISTORY, settings.initial)


type _Spans = list[tuple[datetime, datetime]]


def _oldest_first(start: datetime, end: datetime, span: timedelta | None, limit: int) -> _Spans:
    """``[start, end)`` in pieces of ``span`` from ``start``; at most ``limit`` of them."""
    if span is None:
        return [(start, end)]
    spans: _Spans = []
    low = start
    while low < end and len(spans) < limit:
        high = min(low + span, end)
        spans.append((low, high))
        low = high
    return spans


def _newest_first(start: datetime, end: datetime, span: timedelta | None, limit: int) -> _Spans:
    """``[start, end)`` in pieces of ``span`` from ``end``; at most ``limit`` of them."""
    if span is None:
        return [(start, end)]
    spans: _Spans = []
    high = end
    while high > start and len(spans) < limit:
        low = max(high - span, start)
        spans.append((low, high))
        high = low
    return spans


def plan_windows(
    state: StreamState,
    settings: WindowSettings,
    as_of: datetime,
    *,
    max_windows: int = MAX_WINDOWS,
) -> Plan:
    """The windows to run for a stream in ``state`` at ``as_of``; see the module.

    Raises:
        ValueError: a datetime without a time zone; a frontier without a checkpoint, or after it;
            a retention that is not longer than zero; ``max_windows`` below 1.
    """
    if max_windows < 1:
        raise ValueError("max_windows is at least 1")
    if state.retention is not None and state.retention <= timedelta(0):
        raise ValueError("a retention is longer than zero")
    as_of = _utc(as_of, "as_of")
    until = None if state.checked_until is None else _utc(state.checked_until, "checked_until")
    frontier = None if state.checked_from is None else _utc(state.checked_from, "checked_from")
    if frontier is not None and (until is None or frontier > until):
        raise ValueError("checked_from is set only with checked_until, and not after it")
    history_start = _history_start(as_of, state.retention, settings)

    # One piece more than max_windows tells that a chain was cut.
    chain: _Spans = []
    kind: WindowKind = "forward"
    forward_skipped: ForwardSkip | None = None
    if until is None:
        kind = "initial"
        start = max(as_of - settings.initial, history_start)
        chain = _oldest_first(start, as_of, settings.max_span, max_windows + 1)
    elif as_of <= until:
        forward_skipped = "caught_up" if as_of == until else "checkpoint_ahead"
    else:
        start = min(max(until - settings.overlap, history_start), until)
        chain = _oldest_first(start, as_of, settings.max_span, max_windows + 1)
    truncated = len(chain) > max_windows
    windows = [PlannedWindow(kind, low, high) for low, high in chain[:max_windows]]

    backfill_skipped: BackfillSkip | None = None
    if until is None:
        backfill_skipped = "initial_load"
    elif settings.backfill is None:
        backfill_skipped = "not_configured"
    else:
        edge = until if frontier is None else frontier
        if edge <= history_start:
            backfill_skipped = "history_loaded"
        elif edge > as_of:
            backfill_skipped = "frontier_ahead"
        else:
            start = max(edge - settings.backfill, history_start)
            step = _newest_first(start, edge, settings.max_span, max_windows + 1)
            truncated = truncated or len(step) > max_windows
            windows.extend(PlannedWindow("backfill", low, high) for low, high in step[:max_windows])

    return Plan(
        as_of=as_of,
        history_start=history_start,
        windows=tuple(windows),
        forward_skipped=forward_skipped,
        backfill_skipped=backfill_skipped,
        truncated=truncated,
    )
