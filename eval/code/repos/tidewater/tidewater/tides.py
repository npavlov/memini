"""Tide tables: the height of the water over a day, and the windows a deep vessel can enter in."""

import csv
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path


@dataclass(frozen=True)
class TidePoint:
    time: datetime
    height_m: float


class TideTable:
    """High and low waters of one harbour, read from a CSV file of time and height."""

    def __init__(self, points: list[TidePoint]) -> None:
        self.points = sorted(points, key=lambda p: p.time)

    @classmethod
    def load(cls, path: Path) -> "TideTable":
        with path.open(newline="") as handle:
            rows = csv.reader(handle)
            return cls([TidePoint(datetime.fromisoformat(t), float(h)) for t, h in rows])

    def height_at(self, moment: datetime) -> float:
        """The water height at a moment, interpolated linearly between two table points."""
        before = max((p for p in self.points if p.time <= moment), key=lambda p: p.time)
        after = min((p for p in self.points if p.time >= moment), key=lambda p: p.time)
        if before.time == after.time:
            return before.height_m
        span = (after.time - before.time).total_seconds()
        part = (moment - before.time).total_seconds() / span
        return before.height_m + (after.height_m - before.height_m) * part


def safe_windows(
    table: TideTable, needed_m: float, day: datetime, step_minutes: int = 10
) -> list[tuple[datetime, datetime]]:
    """The periods of a day when the water is at least ``needed_m`` deep: when a vessel of that
    draught may enter. Checked every ``step_minutes``."""
    windows: list[tuple[datetime, datetime]] = []
    start: datetime | None = None
    moment = day.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_day = moment + timedelta(days=1)
    while moment < end_of_day:
        deep = table.height_at(moment) >= needed_m
        if deep and start is None:
            start = moment
        elif not deep and start is not None:
            windows.append((start, moment))
            start = None
        moment += timedelta(minutes=step_minutes)
    if start is not None:
        windows.append((start, end_of_day))
    return windows
