"""Berths of the harbour, and the allocator that assigns arriving vessels to them."""

from dataclasses import dataclass, field

SAFETY_MARGIN_METRES = 0.5
"""Water kept under the keel on top of the vessel's draught."""


@dataclass
class Vessel:
    name: str
    length_m: float
    draught_m: float
    hazardous: bool = False


@dataclass
class Berth:
    number: int
    length_m: float
    depth_at_low_water_m: float
    accepts_hazardous: bool = False
    occupant: Vessel | None = None

    def fits(self, vessel: Vessel) -> bool:
        """Whether the vessel is short and shallow enough for this berth, and allowed here."""
        if vessel.length_m > self.length_m:
            return False
        if vessel.draught_m + SAFETY_MARGIN_METRES > self.depth_at_low_water_m:
            return False
        return self.accepts_hazardous or not vessel.hazardous


class NoBerthAvailable(Exception):
    """No free berth fits the vessel."""


@dataclass
class BerthAllocator:
    berths: list[Berth] = field(default_factory=list)

    def allocate(self, vessel: Vessel) -> Berth:
        """Give the vessel the smallest free berth that fits it; ties go to the lower number."""
        candidates = [b for b in self.berths if b.occupant is None and b.fits(vessel)]
        if not candidates:
            raise NoBerthAvailable(f"no free berth fits {vessel.name}")
        best = min(candidates, key=lambda b: (b.length_m, b.number))
        best.occupant = vessel
        return best

    def release(self, number: int) -> Vessel | None:
        """Free the berth with this number and return the vessel that left it."""
        for berth in self.berths:
            if berth.number == number:
                vessel, berth.occupant = berth.occupant, None
                return vessel
        raise KeyError(number)

    def occupancy(self) -> float:
        """The share of berths that are taken, from 0 to 1."""
        if not self.berths:
            return 0.0
        taken = sum(1 for b in self.berths if b.occupant is not None)
        return taken / len(self.berths)
