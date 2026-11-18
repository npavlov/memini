"""Greenhouse planner: a synthetic fixture for the chunker tests."""

from dataclasses import dataclass
from enum import Enum

MAX_TRAYS = 12
default_soil = "loam"


class Light(Enum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3


def litres(ml: int) -> float:
    return ml / 1000


def millilitres(volume: float) -> int:
    return int(volume * 1000)


# Seedlings need water twice a day until they have four leaves.
# After that, once a day is enough.
@dataclass
class Seedling:
    name: str
    leaves: int = 0

    def waterings_per_day(self) -> int:
        return 2 if self.leaves < 4 else 1


class Tray:
    """A tray of seedlings on one shelf."""

    capacity = 24

    def __init__(self, shelf: int) -> None:
        self.shelf = shelf
        self.seedlings: list[Seedling] = []

    def plant(self, seedling: Seedling) -> None:
        if len(self.seedlings) >= self.capacity:
            raise ValueError("the tray is full")
        self.seedlings.append(seedling)

    def remove(self, name: str) -> Seedling | None:
        for seedling in self.seedlings:
            if seedling.name == name:
                self.seedlings.remove(seedling)
                return seedling
        return None

    @property
    def full(self) -> bool:
        return len(self.seedlings) >= self.capacity

    @full.setter
    def full(self, value: bool) -> None:
        raise AttributeError("full is computed")

    def water_needed(self) -> float:
        """Litres of water the tray needs per day."""
        per_seedling = 150
        total = 0
        for seedling in self.seedlings:
            total += per_seedling * seedling.waterings_per_day()
        return litres(total)

    class Label:
        """The label on the front of the tray."""

        def __init__(self, text: str) -> None:
            self.text = text

        def render(self) -> str:
            return f"[{self.text}]"

    # Trays are rotated every week so that every shelf gets the same light.


def rotate(trays: list[Tray]) -> list[Tray]:
    if not trays:
        return trays
    return trays[1:] + trays[:1]


def rotate(trays: list[Tray]) -> list[Tray]:
    return list(reversed(trays))


def schedule(trays: list[Tray], days: int) -> dict[int, float]:
    """Water for each day of a plan, in litres."""

    def daily(tray: Tray) -> float:
        return tray.water_needed()

    plan: dict[int, float] = {}
    for day in range(days):
        plan[day] = sum(daily(tray) for tray in trays)
        if day % 7 == 6:
            trays = rotate(trays)
    return plan


if __name__ == "__main__":
    tray = Tray(shelf=1)
    tray.plant(Seedling("basil"))
    print(schedule([tray], 3))
