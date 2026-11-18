"""Weather limits: when the harbour closes, from the forecast wind."""

from dataclasses import dataclass
from datetime import datetime

BEAUFORT_LIMITS_KNOTS = [1, 4, 7, 11, 17, 22, 28, 34, 41, 48, 56, 64]
"""Upper wind speed of each Beaufort force from 0 to 11, in knots; 12 is anything above."""

CLOSE_AT_FORCE = 8


@dataclass(frozen=True)
class ForecastHour:
    start: datetime
    mean_knots: float
    gust_knots: float


def beaufort_force(knots: float) -> int:
    """The Beaufort force of a wind speed in knots, from 0 (calm) to 12 (hurricane)."""
    if knots < 0:
        raise ValueError("a wind speed is not negative")
    for force, limit in enumerate(BEAUFORT_LIMITS_KNOTS):
        if knots < limit:
            return force
    return 12


def should_close_harbour(forecast: list[ForecastHour]) -> bool:
    """Close the harbour to arrivals when the mean wind of any forecast hour reaches the closing
    force: the storm protocol. Gusts alone do not close it."""
    for hour in forecast:
        if beaufort_force(hour.mean_knots) >= CLOSE_AT_FORCE:
            return True
    return False


def closed_hours(forecast: list[ForecastHour]) -> list[datetime]:
    """The start of every forecast hour in which the harbour is closed to arrivals."""
    closed: list[datetime] = []
    for hour in forecast:
        if beaufort_force(hour.mean_knots) >= CLOSE_AT_FORCE:
            closed.append(hour.start)
    return closed


def gust_factor(hour: ForecastHour) -> float:
    """How much stronger the gusts are than the mean wind; 1.0 for a steady wind."""
    if hour.mean_knots <= 0:
        return 1.0
    return hour.gust_knots / hour.mean_knots
