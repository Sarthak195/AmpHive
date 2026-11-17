"""DB-free tests for services/pricing.py cheapest_window (F6, charge setup)."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from backend.services.pricing import cheapest_window

IST = timezone(timedelta(hours=5, minutes=30))  # fixed offset: no tzdata on CI Windows boxes
FLAT = Decimal("10.00")


def _slot(start, end, price, mask=127):
    return SimpleNamespace(
        start_min=start, end_min=end, price_per_kwh=Decimal(str(price)), days_mask=mask
    )


# Night rate wrapping midnight is two slots: [22:00,24:00) + [00:00,06:00).
NIGHT = [_slot(1320, 1440, 4), _slot(0, 360, 4)]


def _at(h, mi=0, day=17):  # 2026-11-17 is a Tuesday
    return datetime(2026, 11, day, h, mi, tzinfo=IST)


def test_flat_tariff_has_no_window():
    assert cheapest_window([], FLAT, _at(12)) is None


def test_picks_night_start_over_midnight_wrap():
    start, rate = cheapest_window(NIGHT, FLAT, _at(12))
    assert (start.hour, start.minute, start.day) == (22, 0, 17)
    assert rate == Decimal("4.00")


def test_inside_cheapest_window_returns_none():
    assert cheapest_window(NIGHT, FLAT, _at(23, 30)) is None
    assert cheapest_window(NIGHT, FLAT, _at(2)) is None


def test_flat_gap_after_a_pricey_peak_can_be_the_cheapest():
    peak = [_slot(1080, 1320, 15)]  # 18:00-22:00 at 15, flat 10 otherwise
    start, rate = cheapest_window(peak, FLAT, _at(19))
    assert (start.hour, rate) == (22, FLAT)


def test_days_mask_bit0_is_monday_and_next_day_is_considered():
    sat_only = [_slot(0, 360, 3, mask=1 << 5)]  # Saturday 00:00-06:00
    # Friday 20:00 -> Saturday 00:00 is 4 h away, inside 24 h.
    start, rate = cheapest_window(sat_only, FLAT, _at(20, day=20))
    assert (start.day, start.hour, rate) == (21, 0, Decimal("3.00"))
    # Tuesday: the Saturday slot is days away, so nothing qualifies.
    assert cheapest_window(sat_only, FLAT, _at(20)) is None
