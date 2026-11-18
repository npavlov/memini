"""The intake planner (memini.intake.plan) and the rule by which ingest moves a stream's checked
range (memini.ingest.checkpoint). No database. Acceptance of memini-69b: plans are deterministic for
a given state; a failed stream gets the same window again; history backfill stops at the retention
start. Every time here is synthetic."""

import itertools
import random
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from memini.ingest import Checkpoint, WindowClaim, WindowGapError
from memini.ingest.checkpoint import advance_checkpoint
from memini.intake import (
    DEFAULT_HISTORY,
    MAX_WINDOWS,
    Plan,
    PlannedWindow,
    StreamState,
    WindowSettings,
    plan_windows,
    resolve_settings,
)
from memini.recipes import load_recipe

RECIPES = Path(__file__).resolve().parent / "fixtures" / "recipes"

DAY = timedelta(days=1)
HOUR = timedelta(hours=1)
AS_OF = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)

# The settings of the Hearth example recipe: overlap 2d, initial 14d, backfill 30d, max_span 7d.
HEARTH = WindowSettings(overlap=2 * DAY, initial=14 * DAY, backfill=30 * DAY, max_span=7 * DAY)


def spans(plan: Plan) -> list[tuple[str, datetime, datetime]]:
    return [(window.kind, window.start, window.end) for window in plan.windows]


# --- Settings ------------------------------------------------------------------------------------


def test_settings_are_the_recipes_window_with_the_sources_overrides() -> None:
    window = load_recipe(RECIPES / "hearth.toml").window

    assert resolve_settings(window) == HEARTH
    assert WindowSettings.from_recipe(window) == HEARTH
    assert resolve_settings(
        window, {"overlap": "3d", "max_span": None, "history": "90d", "initial": 2 * DAY}
    ) == WindowSettings(
        overlap=3 * DAY, initial=2 * DAY, backfill=30 * DAY, max_span=None, history=90 * DAY
    )
    assert resolve_settings(window, {"backfill": None}).backfill is None


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"overlaps": "1d"}, r"window\.overlaps: unknown key"),
        ({"overlap": None}, r"window\.overlap: a duration, never none"),
        ({"initial": None}, r"window\.initial: a duration, never none"),
        ({"backfill": "a month"}, r"window\.backfill: a duration is numbers"),
        ({"history": "0d"}, r"window\.history: a duration must be longer than zero"),
        ({"max_span": 7}, r"window\.max_span: a duration is numbers"),
    ],
)
def test_bad_overrides_are_refused_naming_the_key(
    overrides: dict[str, object], message: str
) -> None:
    window = load_recipe(RECIPES / "hearth.toml").window
    with pytest.raises(ValueError, match=message):
        resolve_settings(window, overrides)


def test_settings_refuse_lengths_that_cannot_plan() -> None:
    with pytest.raises(ValueError, match="overlap"):
        WindowSettings(overlap=-HOUR, initial=DAY)
    for name in ("initial", "backfill", "max_span", "history"):
        values = {"overlap": HOUR, "initial": DAY, name: timedelta(0)}
        with pytest.raises(ValueError, match=name):
            WindowSettings(**values)
    assert WindowSettings(overlap=timedelta(0), initial=DAY).overlap == timedelta(0)


# --- A new stream --------------------------------------------------------------------------------


def test_a_new_stream_loads_a_short_recent_window_oldest_first() -> None:
    plan = plan_windows(StreamState(), HEARTH, AS_OF)

    assert spans(plan) == [
        ("initial", AS_OF - 14 * DAY, AS_OF - 7 * DAY),
        ("initial", AS_OF - 7 * DAY, AS_OF),
    ]
    assert [window.moves for window in plan.windows] == ["checkpoint", "checkpoint"]
    assert plan.forward == plan.windows
    assert plan.backfill == ()
    assert (plan.forward_skipped, plan.backfill_skipped, plan.truncated) == (
        None,
        "initial_load",
        False,
    )


def test_the_initial_window_is_clipped_at_the_history_start() -> None:
    # A retention of 10 days: nothing older than as_of - 10d is loaded.
    plan = plan_windows(StreamState(retention=10 * DAY), HEARTH, AS_OF)
    assert plan.history_start == AS_OF - 10 * DAY
    assert spans(plan) == [
        ("initial", AS_OF - 10 * DAY, AS_OF - 3 * DAY),
        ("initial", AS_OF - 3 * DAY, AS_OF),
    ]

    # A source's history limit clips it the same way; the later of the two starts wins.
    limited = replace(HEARTH, history=5 * DAY)
    assert spans(plan_windows(StreamState(retention=10 * DAY), limited, AS_OF)) == [
        ("initial", AS_OF - 5 * DAY, AS_OF)
    ]

    # Without either, history reaches DEFAULT_HISTORY back, or the initial window if longer.
    assert plan_windows(StreamState(), HEARTH, AS_OF).history_start == AS_OF - DEFAULT_HISTORY
    long_initial = WindowSettings(overlap=DAY, initial=DEFAULT_HISTORY + 30 * DAY)
    plan = plan_windows(StreamState(), long_initial, AS_OF)
    assert spans(plan) == [("initial", AS_OF - DEFAULT_HISTORY - 30 * DAY, AS_OF)]


# --- Forward -------------------------------------------------------------------------------------


def test_the_forward_window_starts_an_overlap_behind_the_checkpoint() -> None:
    checkpoint = AS_OF - 6 * HOUR
    state = StreamState(checked_until=checkpoint, checked_from=checkpoint - 14 * DAY)
    settings = replace(HEARTH, backfill=None)

    plan = plan_windows(state, settings, AS_OF)

    assert spans(plan) == [("forward", checkpoint - 2 * DAY, AS_OF)]
    assert plan.windows[0].moves == "checkpoint"
    assert (plan.forward_skipped, plan.backfill_skipped) == (None, "not_configured")


def test_a_long_forward_window_is_split_into_a_chain_oldest_first() -> None:
    checkpoint = AS_OF - 20 * DAY
    state = StreamState(checked_until=checkpoint, checked_from=checkpoint - 14 * DAY)

    forward = plan_windows(state, replace(HEARTH, backfill=None), AS_OF).windows

    assert [(w.start, w.end) for w in forward] == [
        (AS_OF - 22 * DAY, AS_OF - 15 * DAY),
        (AS_OF - 15 * DAY, AS_OF - 8 * DAY),
        (AS_OF - 8 * DAY, AS_OF - DAY),
        (AS_OF - DAY, AS_OF),
    ]
    # Each window starts where the one before it ended: once that one is complete, at the
    # checkpoint, so ingest never sees a gap.
    assert all(a.end == b.start for a, b in itertools.pairwise(forward))


def test_the_overlap_does_not_reach_before_the_history_start_nor_past_the_checkpoint() -> None:
    # The retention start falls inside the overlap: the window starts there.
    checkpoint = AS_OF - 9 * DAY
    state = StreamState(checked_until=checkpoint, checked_from=checkpoint, retention=10 * DAY)
    plan = plan_windows(state, replace(HEARTH, max_span=None), AS_OF)
    assert spans(plan)[0] == ("forward", AS_OF - 10 * DAY, AS_OF)

    # A stream idle for longer than its retention starts at its checkpoint, never after it:
    # a complete window that starts after the checkpoint is refused as a gap.
    checkpoint = AS_OF - 30 * DAY
    state = StreamState(checked_until=checkpoint, checked_from=checkpoint, retention=10 * DAY)
    plan = plan_windows(state, replace(HEARTH, max_span=None), AS_OF)
    assert spans(plan)[0] == ("forward", checkpoint, AS_OF)


def test_no_forward_window_when_as_of_is_not_after_the_checkpoint() -> None:
    checkpoint = AS_OF
    state = StreamState(checked_until=checkpoint, checked_from=checkpoint - 20 * DAY)

    caught_up = plan_windows(state, HEARTH, AS_OF)
    assert caught_up.forward == ()
    assert caught_up.forward_skipped == "caught_up"
    # The history step does not depend on the forward window.
    assert [w.kind for w in caught_up.windows] == ["backfill"] * 5

    # The clock went back, or another writer moved the checkpoint past the pinned time.
    behind = plan_windows(state, HEARTH, AS_OF - HOUR)
    assert behind.forward == ()
    assert behind.forward_skipped == "checkpoint_ahead"
    assert all(window.end <= AS_OF - HOUR for window in behind.windows)

    # A frontier after as_of too: nothing at all, and the plan says why.
    far_behind = plan_windows(state, HEARTH, AS_OF - 30 * DAY)
    assert far_behind.windows == ()
    assert (far_behind.forward_skipped, far_behind.backfill_skipped) == (
        "checkpoint_ahead",
        "frontier_ahead",
    )


# --- History -------------------------------------------------------------------------------------


def test_a_history_step_ends_at_the_frontier_newest_first() -> None:
    frontier = AS_OF - 14 * DAY
    state = StreamState(checked_until=AS_OF - HOUR, checked_from=frontier)

    plan = plan_windows(state, HEARTH, AS_OF)

    assert plan.backfill == tuple(
        PlannedWindow("backfill", start, end)
        for start, end in [
            (frontier - 7 * DAY, frontier),
            (frontier - 14 * DAY, frontier - 7 * DAY),
            (frontier - 21 * DAY, frontier - 14 * DAY),
            (frontier - 28 * DAY, frontier - 21 * DAY),
            (frontier - 30 * DAY, frontier - 28 * DAY),
        ]
    )
    assert {window.moves for window in plan.backfill} == {"frontier"}
    # Forward first, then history.
    assert plan.windows[0].kind == "forward"
    assert plan.backfill_skipped is None


def test_a_stream_whose_frontier_was_never_recorded_steps_back_from_its_checkpoint() -> None:
    checkpoint = AS_OF - HOUR
    plan = plan_windows(
        StreamState(checked_until=checkpoint), replace(HEARTH, max_span=None), AS_OF
    )
    assert plan.backfill == (PlannedWindow("backfill", checkpoint - 30 * DAY, checkpoint),)


def test_history_backfill_stops_exactly_at_the_retention_start() -> None:
    state = StreamState(
        checked_until=AS_OF - 14 * DAY, checked_from=AS_OF - 28 * DAY, retention=70 * DAY
    )
    settings = replace(HEARTH, max_span=None)
    as_of = AS_OF
    frontiers: list[datetime] = []
    for _ in range(10):
        plan = plan_windows(state, settings, as_of)
        if not plan.backfill:
            assert plan.backfill_skipped == "history_loaded"
            break
        (step,) = plan.backfill
        assert step.start >= plan.history_start
        # The step completes: ingest moves the frontier to its start.
        state = replace(state, checked_from=step.start)
        frontiers.append(step.start)
        as_of += HOUR
    else:
        pytest.fail("the backfill never stopped")

    # Two full steps of 30 days and a last one cut at the retention start of its run.
    assert frontiers == [
        AS_OF - 58 * DAY,
        AS_OF + HOUR - 70 * DAY,  # as_of of the second run, less the retention
    ]


def test_a_retention_change_midway_moves_where_history_stops() -> None:
    state = StreamState(
        checked_until=AS_OF - HOUR, checked_from=AS_OF - 40 * DAY, retention=60 * DAY
    )
    settings = replace(HEARTH, max_span=None)

    assert plan_windows(state, settings, AS_OF).backfill == (
        PlannedWindow("backfill", AS_OF - 60 * DAY, AS_OF - 40 * DAY),
    )
    # Shorter: the frontier is already past the new start; history is loaded.
    shorter = plan_windows(replace(state, retention=30 * DAY), settings, AS_OF)
    assert (shorter.backfill, shorter.backfill_skipped) == ((), "history_loaded")
    # Longer: history resumes from the frontier.
    assert plan_windows(replace(state, retention=90 * DAY), settings, AS_OF).backfill == (
        PlannedWindow("backfill", AS_OF - 70 * DAY, AS_OF - 40 * DAY),
    )
    # None: as far as the default history.
    assert plan_windows(replace(state, retention=None), settings, AS_OF).backfill == (
        PlannedWindow("backfill", AS_OF - 70 * DAY, AS_OF - 40 * DAY),
    )
    # A source's history limit stops it too, before the retention start.
    limited = replace(settings, history=50 * DAY)
    assert plan_windows(replace(state, retention=90 * DAY), limited, AS_OF).backfill == (
        PlannedWindow("backfill", AS_OF - 50 * DAY, AS_OF - 40 * DAY),
    )


def test_a_chain_longer_than_max_windows_is_cut() -> None:
    state = StreamState(checked_until=AS_OF - 10 * DAY, checked_from=AS_OF - 20 * DAY)
    settings = WindowSettings(overlap=DAY, initial=DAY, backfill=5 * DAY, max_span=DAY)

    plan = plan_windows(state, settings, AS_OF, max_windows=3)

    assert spans(plan) == [
        ("forward", AS_OF - 11 * DAY, AS_OF - 10 * DAY),
        ("forward", AS_OF - 10 * DAY, AS_OF - 9 * DAY),
        ("forward", AS_OF - 9 * DAY, AS_OF - 8 * DAY),
        ("backfill", AS_OF - 21 * DAY, AS_OF - 20 * DAY),
        ("backfill", AS_OF - 22 * DAY, AS_OF - 21 * DAY),
        ("backfill", AS_OF - 23 * DAY, AS_OF - 22 * DAY),
    ]
    assert plan.truncated
    with pytest.raises(ValueError, match="max_windows"):
        plan_windows(state, settings, AS_OF, max_windows=0)

    # A checkpoint a year old and windows of a minute: the default cut, without the rest.
    old = StreamState(checked_until=AS_OF - 365 * DAY)
    minutes = WindowSettings(overlap=HOUR, initial=DAY, max_span=timedelta(minutes=1))
    plan = plan_windows(old, minutes, AS_OF)
    assert (len(plan.windows), plan.truncated) == (MAX_WINDOWS, True)
    # The overlap stops at the history start, which is the checkpoint here (365 days back).
    assert plan.windows[0].start == AS_OF - 365 * DAY
    assert plan.windows[-1].end == AS_OF - 365 * DAY + MAX_WINDOWS * timedelta(minutes=1)


def test_inputs_that_cannot_be_planned_are_refused() -> None:
    naive = datetime(2026, 11, 18, 12, 0)  # no time zone: the point of the test
    with pytest.raises(ValueError, match="as_of needs a time zone"):
        plan_windows(StreamState(), HEARTH, naive)
    with pytest.raises(ValueError, match="checked_until needs a time zone"):
        plan_windows(StreamState(checked_until=naive), HEARTH, AS_OF)
    with pytest.raises(ValueError, match="checked_from"):
        plan_windows(StreamState(checked_from=AS_OF - DAY), HEARTH, AS_OF)
    with pytest.raises(ValueError, match="checked_from"):
        plan_windows(StreamState(checked_until=AS_OF - DAY, checked_from=AS_OF), HEARTH, AS_OF)
    with pytest.raises(ValueError, match="retention"):
        plan_windows(StreamState(retention=timedelta(0)), HEARTH, AS_OF)


# --- What the executors get ----------------------------------------------------------------------


def test_a_plan_reads_as_json_for_the_run_report() -> None:
    plan = plan_windows(
        StreamState(checked_until=AS_OF - HOUR, checked_from=AS_OF - 14 * DAY, retention=20 * DAY),
        replace(HEARTH, max_span=None),
        AS_OF,
    )

    assert plan.to_json() == {
        "as_of": "2026-11-18T12:00:00Z",
        "history_start": "2026-10-29T12:00:00Z",
        "windows": [
            {
                "kind": "forward",
                "from": "2026-11-16T11:00:00Z",
                "to": "2026-11-18T12:00:00Z",
                "moves": "checkpoint",
            },
            {
                "kind": "backfill",
                "from": "2026-10-29T12:00:00Z",
                "to": "2026-11-04T12:00:00Z",
                "moves": "frontier",
            },
        ],
        "forward_skipped": None,
        "backfill_skipped": None,
        "truncated": False,
    }
    assert plan.windows[0].label == "forward 2026-11-16T11:00:00Z/2026-11-18T12:00:00Z"


def test_a_planned_window_makes_its_ingest_claim() -> None:
    window = PlannedWindow("forward", AS_OF - DAY, AS_OF)

    assert window.claim(complete=True, snapshot=True, cursor={"after": "c-1"}) == WindowClaim(
        start=AS_OF - DAY, end=AS_OF, complete=True, snapshot=True, cursor={"after": "c-1"}
    )
    # A snapshot is claimed with a complete window only.
    assert window.claim(complete=False, snapshot=True) == WindowClaim(
        start=AS_OF - DAY, end=AS_OF, complete=False
    )


# --- The ingest rule -----------------------------------------------------------------------------


def claim(start: datetime, end: datetime, *, complete: bool = True) -> WindowClaim:
    return WindowClaim(start=start, end=end, complete=complete)


def test_the_first_complete_window_sets_the_whole_range() -> None:
    new = Checkpoint(checked_until=None, cursor=None)

    assert advance_checkpoint(new, claim(AS_OF - DAY, AS_OF), {"page": "p2"}) == Checkpoint(
        checked_until=AS_OF, cursor={"page": "p2"}, checked_from=AS_OF - DAY
    )
    assert advance_checkpoint(new, claim(AS_OF - DAY, AS_OF, complete=False), None) is None


def test_a_complete_window_moves_the_checkpoint_forward_and_the_frontier_back() -> None:
    before = Checkpoint(
        checked_until=AS_OF - DAY, cursor={"page": "p1"}, checked_from=AS_OF - 3 * DAY
    )

    # Forward, with an overlap: the checkpoint and its cursor move, the frontier stays.
    assert advance_checkpoint(before, claim(AS_OF - 2 * DAY, AS_OF), {"page": "p2"}) == replace(
        before, checked_until=AS_OF, cursor={"page": "p2"}
    )
    # Back, reaching the frontier: the frontier moves, the checkpoint and its cursor stay.
    assert advance_checkpoint(before, claim(AS_OF - 5 * DAY, AS_OF - 3 * DAY), {"x": 1}) == replace(
        before, checked_from=AS_OF - 5 * DAY
    )
    # One window over the whole range and beyond: both move.
    assert advance_checkpoint(before, claim(AS_OF - 9 * DAY, AS_OF), None) == Checkpoint(
        checked_until=AS_OF, cursor=None, checked_from=AS_OF - 9 * DAY
    )
    # Inside the range, below the frontier with a gap, or incomplete: nothing moves.
    assert advance_checkpoint(before, claim(AS_OF - 2 * DAY, AS_OF - DAY), None) is None
    assert advance_checkpoint(before, claim(AS_OF - 9 * DAY, AS_OF - 4 * DAY), None) is None
    assert advance_checkpoint(before, claim(AS_OF - 9 * DAY, AS_OF, complete=False), None) is None
    # After the checkpoint: a gap, refused.
    with pytest.raises(WindowGapError):
        advance_checkpoint(before, claim(AS_OF - HOUR, AS_OF), None)
    # An incomplete window may start anywhere.
    assert advance_checkpoint(before, claim(AS_OF - HOUR, AS_OF, complete=False), None) is None


def test_a_range_whose_start_was_never_recorded_starts_at_its_checkpoint() -> None:
    legacy = Checkpoint(checked_until=AS_OF - DAY, cursor=None)

    assert advance_checkpoint(legacy, claim(AS_OF - 2 * DAY, AS_OF), None) == Checkpoint(
        checked_until=AS_OF, cursor=None, checked_from=AS_OF - 2 * DAY
    )
    assert advance_checkpoint(legacy, claim(AS_OF - 2 * DAY, AS_OF - DAY), None) == replace(
        legacy, checked_from=AS_OF - 2 * DAY
    )
    assert advance_checkpoint(legacy, claim(AS_OF - 3 * DAY, AS_OF - 2 * DAY), None) is None


# --- Properties over generated states ------------------------------------------------------------


@dataclass(frozen=True)
class Case:
    state: StreamState
    settings: WindowSettings
    as_of: datetime


def _duration(rng: random.Random, low_hours: int, high_hours: int) -> timedelta:
    return timedelta(hours=rng.randint(low_hours, high_hours), minutes=rng.choice([0, 0, 17, 45]))


def generate(rng: random.Random) -> Case:
    as_of = AS_OF + timedelta(
        minutes=rng.randint(-100_000, 100_000), microseconds=rng.randint(0, 999_999)
    )
    settings = WindowSettings(
        overlap=rng.choice([timedelta(0), _duration(rng, 1, 96)]),
        initial=_duration(rng, 1, 24 * 60),
        backfill=rng.choice([None, _duration(rng, 1, 24 * 90)]),
        max_span=rng.choice([None, _duration(rng, 1, 24 * 14)]),
        history=rng.choice([None, None, _duration(rng, 24, 24 * 400)]),
    )
    retention = rng.choice([None, timedelta(days=rng.randint(1, 400))])
    until: datetime | None = None
    frontier: datetime | None = None
    if rng.random() < 0.85:
        until = as_of + timedelta(hours=rng.randint(-24 * 60, 6))
        if rng.random() < 0.9:
            frontier = until - timedelta(hours=rng.randint(0, 24 * 500))
    return Case(
        StreamState(checked_until=until, checked_from=frontier, retention=retention),
        settings,
        as_of,
    )


def shift_zone(moment: datetime | None, zone: timezone) -> datetime | None:
    return None if moment is None else moment.astimezone(zone)


def test_the_same_state_gives_the_same_plan() -> None:
    rng = random.Random(69)  # noqa: S311 - generated test cases, not secrets
    zone = timezone(timedelta(hours=5, minutes=30))
    for _ in range(2000):
        case = generate(rng)
        plan = plan_windows(case.state, case.settings, case.as_of)
        assert plan_windows(case.state, case.settings, case.as_of) == plan
        # The same instants in another time zone are the same state.
        shifted = StreamState(
            checked_until=shift_zone(case.state.checked_until, zone),
            checked_from=shift_zone(case.state.checked_from, zone),
            retention=case.state.retention,
        )
        again = plan_windows(shifted, case.settings, case.as_of.astimezone(zone))
        assert again == plan
        assert again.to_json() == plan.to_json()


def test_every_plan_keeps_the_rules_of_its_windows() -> None:
    rng = random.Random(1105)  # noqa: S311 - generated test cases, not secrets
    for _ in range(2000):
        case = generate(rng)
        plan = plan_windows(case.state, case.settings, case.as_of)
        until = case.state.checked_until
        frontier = case.state.checked_from or until
        span = case.settings.max_span

        for window in plan.windows:
            assert window.start < window.end <= plan.as_of
            # Only a stream idle for longer than its history loads older time: from its
            # checkpoint, as a complete window may not start after it.
            assert window.start >= plan.history_start or (
                window.kind == "forward"
                and until is not None
                and until < plan.history_start
                and plan.forward[0].start == until
            )
            assert span is None or window.end - window.start <= span
            assert window.start.tzinfo is UTC
            assert window.end.tzinfo is UTC

        forward, backfill = plan.forward, plan.backfill
        assert plan.windows == forward + backfill  # forward first
        assert all(a.end == b.start for a, b in itertools.pairwise(forward))
        assert all(a.start == b.end for a, b in itertools.pairwise(backfill))
        if until is None:
            assert {w.kind for w in forward} == {"initial"}
            assert backfill == ()
            assert forward[0].start == max(plan.as_of - case.settings.initial, plan.history_start)
            assert plan.truncated or forward[-1].end == plan.as_of
        else:
            assert {w.kind for w in forward} <= {"forward"}
            if forward:
                assert forward[0].start <= until  # never a gap after the checkpoint
                assert forward[0].start >= until - case.settings.overlap or (
                    forward[0].start == plan.history_start
                )
                assert plan.truncated or forward[-1].end == plan.as_of
            else:
                assert plan.forward_skipped == (
                    "caught_up" if until == plan.as_of else "checkpoint_ahead"
                )
            if backfill:
                assert frontier is not None
                assert case.settings.backfill is not None
                assert backfill[0].end == frontier
                assert frontier - backfill[-1].start <= case.settings.backfill
                assert plan.truncated or backfill[-1].start == max(
                    frontier - case.settings.backfill, plan.history_start
                )
            else:
                assert plan.backfill_skipped is not None


def run_once(
    checkpoint: Checkpoint, plan: Plan, fails: set[int]
) -> tuple[Checkpoint, list[PlannedWindow]]:
    """Run the plan as an executor does, each window through the ingest rule; the windows at the
    positions in ``fails`` come back incomplete. Returns the checkpoint after the run and the
    windows that were run."""
    ran: list[PlannedWindow] = []
    for chain in (plan.forward, plan.backfill):
        for window in chain:
            complete = len(ran) not in fails
            ran.append(window)
            moved = advance_checkpoint(checkpoint, window.claim(complete=complete), None)
            if moved is not None:
                checkpoint = moved
            if not complete:
                break
    return checkpoint, ran


def state_of(checkpoint: Checkpoint, retention: timedelta | None) -> StreamState:
    return StreamState(
        checked_until=checkpoint.checked_until,
        checked_from=checkpoint.checked_from,
        retention=retention,
    )


def covers(chain: tuple[PlannedWindow, ...], start: datetime, end: datetime) -> bool:
    """Whether the windows of a chain, contiguous, cover ``[start, end)``."""
    if not chain:
        return False
    return min(w.start for w in chain) <= start and end <= max(w.end for w in chain)


def test_runs_with_failures_never_leave_a_gap_and_load_history_to_its_start() -> None:
    rng = random.Random(2026)  # noqa: S311 - generated test cases, not secrets
    for _ in range(300):
        settings = WindowSettings(
            overlap=_duration(rng, 0, 72),
            initial=_duration(rng, 1, 24 * 30),
            backfill=_duration(rng, 1, 24 * 45),
            max_span=rng.choice([None, _duration(rng, 1, 24 * 10)]),
        )
        retention = timedelta(days=rng.randint(1, 200))
        as_of = AS_OF
        checkpoint = Checkpoint(checked_until=None, cursor=None)

        # Runs where windows fail at random. advance_checkpoint raises WindowGapError for a
        # complete window that starts after the checkpoint: no plan ever leads to one.
        for _ in range(40):
            plan = plan_windows(state_of(checkpoint, retention), settings, as_of)
            fails = {i for i in range(len(plan.windows)) if rng.random() < 0.25}
            after, ran = run_once(checkpoint, plan, fails)

            # The retry of the run, with its pinned as_of, plans from where the run left the
            # range: the same plan when nothing moved, and every failed window planned again, as
            # far as other windows of the run did not check it.
            retry = plan_windows(state_of(after, retention), settings, as_of)
            if after == checkpoint:
                assert retry == plan
            for failed in (ran[i] for i in sorted(fails) if i < len(ran)):
                if after.checked_until is None:  # still new: the same initial load again
                    assert retry.forward == plan.forward
                elif failed.moves == "checkpoint":
                    start = max(failed.start, after.checked_until)
                    if start < failed.end:
                        assert covers(retry.forward, start, failed.end), (failed, retry)
                else:
                    assert after.checked_from is not None
                    end = min(failed.end, after.checked_from)
                    if failed.start < end:
                        assert covers(retry.backfill, failed.start, end), (failed, retry)

            # The checkpoint moves forward only, the frontier back only, as one range.
            if checkpoint.checked_until is not None:
                assert after.checked_until is not None
                assert after.checked_from is not None
                assert after.checked_until >= checkpoint.checked_until
                assert after.checked_from <= (checkpoint.checked_from or checkpoint.checked_until)
            checkpoint = after
            as_of += timedelta(hours=rng.randint(1, 30))

        # Runs that do not fail load history until the retention start, and stop there.
        last: Plan | None = None
        for _ in range(1000):
            plan = plan_windows(state_of(checkpoint, retention), settings, as_of)
            if plan.backfill_skipped == "history_loaded":
                break
            checkpoint, _ = run_once(checkpoint, plan, set())
            last = plan if plan.backfill else last
            as_of += HOUR
        else:
            pytest.fail("the history never ended")
        assert checkpoint.checked_from is not None
        assert checkpoint.checked_from <= plan.history_start
        if last is not None:
            assert checkpoint.checked_from >= last.history_start


def test_a_failed_stream_gets_the_same_window_again() -> None:
    state = StreamState(
        checked_until=AS_OF - 3 * DAY, checked_from=AS_OF - 17 * DAY, retention=90 * DAY
    )
    before = Checkpoint(
        checked_until=state.checked_until, cursor=None, checked_from=state.checked_from
    )
    plan = plan_windows(state, HEARTH, AS_OF)
    first_forward, first_backfill = plan.forward[0], plan.backfill[0]

    # The first window of each chain comes back incomplete: ingest moves nothing, so the state
    # is the same, and the retry, with the run's pinned as_of, gets the very same plan.
    after, ran = run_once(before, plan, {0, 1})
    assert after == before
    assert ran == [first_forward, first_backfill]
    assert plan_windows(state, HEARTH, AS_OF) == plan

    # A later run, with a later as_of, starts at the same places.
    later = plan_windows(state, HEARTH, AS_OF + 6 * HOUR)
    assert later.forward[0].start == first_forward.start
    assert later.backfill == plan.backfill

    # Only the forward window fails: the history step goes on, and the retry plans the same
    # forward windows with the next history step.
    after, ran = run_once(before, plan, {0})
    assert ran == [first_forward, *plan.backfill]
    assert after == replace(before, checked_from=plan.backfill[-1].start)
    retry = plan_windows(replace(state, checked_from=after.checked_from), HEARTH, AS_OF)
    assert retry.forward == plan.forward
    assert retry.backfill[0].end == plan.backfill[-1].start
