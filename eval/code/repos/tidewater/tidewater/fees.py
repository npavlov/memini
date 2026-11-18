"""Harbour fees: berth hire by the started hour, pilotage, and the night surcharge."""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

NIGHT_START_HOUR = 22
NIGHT_END_HOUR = 6
NIGHT_SURCHARGE_RATE = 0.25
"""A quarter more for every hour that starts at night."""


@dataclass(frozen=True)
class FeeSchedule:
    rate_per_metre_hour: float
    pilotage_flat: float
    pilotage_per_metre: float

    def berth(self, length_m: float, hours: float) -> float:
        """Berth hire: every started hour is charged in full, by the vessel's length."""
        started = math.ceil(hours)
        return round(started * length_m * self.rate_per_metre_hour, 2)

    def pilotage(self, length_m: float) -> float:
        """One pilotage per entry: a flat part and a part by length."""
        return round(self.pilotage_flat + length_m * self.pilotage_per_metre, 2)


def is_night(moment: datetime) -> bool:
    return moment.hour >= NIGHT_START_HOUR or moment.hour < NIGHT_END_HOUR


def night_surcharge(base: float, hours_at_night: int, total_hours: int) -> float:
    """The extra charged for the hours that start at night, as a share of the base fee."""
    if total_hours <= 0:
        return 0.0
    share = hours_at_night / total_hours
    return round(base * share * NIGHT_SURCHARGE_RATE, 2)


def count_night_hours(arrival: datetime, departure: datetime) -> int:
    """How many of the started hours of a stay start at night."""
    count = 0
    moment = arrival
    while moment < departure:
        if is_night(moment):
            count += 1
        moment += timedelta(hours=1)
    return count


def invoice_total(
    schedule: FeeSchedule, length_m: float, arrival: datetime, departure: datetime
) -> float:
    """Everything a stay costs: berth hire, the night surcharge and one pilotage."""
    hours = (departure - arrival).total_seconds() / 3600
    base = schedule.berth(length_m, hours)
    surcharge = night_surcharge(base, count_night_hours(arrival, departure), math.ceil(hours))
    return round(base + surcharge + schedule.pilotage(length_m), 2)
