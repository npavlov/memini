import pytest

from tidewater.berths import Berth, BerthAllocator, NoBerthAvailable, Vessel


def harbour() -> BerthAllocator:
    return BerthAllocator(
        [
            Berth(1, length_m=120, depth_at_low_water_m=9.0),
            Berth(2, length_m=60, depth_at_low_water_m=5.0),
            Berth(3, length_m=60, depth_at_low_water_m=5.0, accepts_hazardous=True),
        ]
    )


def test_the_smallest_berth_that_fits_is_chosen():
    berth = harbour().allocate(Vessel("small", length_m=40, draught_m=3.0))
    assert berth.number == 2


def test_a_deep_vessel_needs_a_deep_berth():
    berth = harbour().allocate(Vessel("deep", length_m=40, draught_m=6.0))
    assert berth.number == 1


def test_hazardous_cargo_goes_only_where_it_is_accepted():
    berth = harbour().allocate(Vessel("tanker", length_m=50, draught_m=4.0, hazardous=True))
    assert berth.number == 3


def test_nothing_fits_a_long_vessel():
    with pytest.raises(NoBerthAvailable):
        harbour().allocate(Vessel("long", length_m=200, draught_m=4.0))
