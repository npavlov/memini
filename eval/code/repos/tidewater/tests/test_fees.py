from datetime import datetime

from tidewater.fees import FeeSchedule, invoice_total, night_surcharge

SCHEDULE = FeeSchedule(rate_per_metre_hour=0.8, pilotage_flat=120.0, pilotage_per_metre=1.5)


def test_a_started_hour_is_charged_in_full():
    assert SCHEDULE.berth(50, 1.2) == SCHEDULE.berth(50, 2)


def test_no_surcharge_by_day():
    assert night_surcharge(100.0, 0, 5) == 0.0


def test_invoice_includes_one_pilotage():
    total = invoice_total(SCHEDULE, 40, datetime(2026, 5, 1, 10), datetime(2026, 5, 1, 12))
    assert total == SCHEDULE.berth(40, 2) + SCHEDULE.pilotage(40)
