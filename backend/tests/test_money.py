"""
Tests for backend.services.money — the shared Decimal money coercion used by
every wallet/billing path (services/billing.py session_cost, wallet debits,
Razorpay top-ups, etc).

DB-free: pure Decimal/float logic, no DB or event loop needed.

Covers the defense-in-depth fix for the telemetry DoS: an out-of-range
magnitude (or non-finite NaN/Infinity) fed into to_money() must never raise a
raw decimal.InvalidOperation — every caller that computes a cost from
untrusted upstream data (mqtt telemetry chief among them) would otherwise
crash on it and, on the finalize path, leave the session ACTIVE and the plug
OCCUPIED forever. The primary defense is the MAX_PLAUSIBLE_KWH ceiling in
services/mqtt/telemetry.py (see test_mqtt_manager.py); this module is the
last line of defense for any value that might reach it another way.
"""
from decimal import Decimal

import pytest

from backend.services.billing import session_cost
from backend.services.money import MAX_MONEY, ZERO_MONEY, energy_cost, to_money

# =============================================================================
# 1. Normal-range behavior (unchanged by the fix)
# =============================================================================

def test_to_money_rounds_half_up_to_two_places():
    assert to_money(5.005) == Decimal("5.01")
    assert to_money("1.004") == Decimal("1.00")


def test_to_money_avoids_binary_float_tail():
    # Decimal(0.1) would be 0.1000000000000000055... — going via str(value)
    # sidesteps that, matching the module's documented contract.
    assert to_money(0.1) == Decimal("0.10")


def test_to_money_accepts_int_str_decimal():
    assert to_money(5) == Decimal("5.00")
    assert to_money("12.3") == Decimal("12.30")
    assert to_money(Decimal("3.14159")) == Decimal("3.14")


# =============================================================================
# 2. Defense-in-depth: extreme/non-finite values never raise
# =============================================================================

@pytest.mark.parametrize("huge", [1e30, 1e100, "1e30"])
def test_to_money_clamps_extreme_positive_magnitude(huge):
    """A magnitude too large for Decimal.quantize() to round to 2dp under the
    default 28-digit context (previously a raw decimal.InvalidOperation) is
    clamped to MAX_MONEY instead of raising."""
    assert to_money(huge) == MAX_MONEY


def test_to_money_clamps_extreme_negative_magnitude():
    assert to_money(-1e30) == -MAX_MONEY


def test_to_money_clamps_a_value_too_big_for_the_numeric_column():
    """Even a magnitude the Decimal context CAN quantize cleanly (few enough
    significant digits) must still be clamped if it exceeds what the
    underlying NUMERIC(12,2) column could ever store."""
    assert to_money("99999999999.99") == MAX_MONEY
    assert to_money("-99999999999.99") == -MAX_MONEY


def test_to_money_handles_infinity():
    assert to_money(float("inf")) == MAX_MONEY
    assert to_money(float("-inf")) == -MAX_MONEY


def test_to_money_handles_nan():
    """NaN has no sign to clamp toward; collapse to zero rather than raise or
    silently propagate a NaN Decimal into billing math."""
    assert to_money(float("nan")) == ZERO_MONEY


def test_to_money_normal_values_unaffected_by_the_clamp():
    """The clamp must not perturb any value inside the plausible/column
    range — only genuinely out-of-range input is touched."""
    assert to_money(Decimal("1234.565")) == Decimal("1234.57")
    assert to_money(0) == ZERO_MONEY


# =============================================================================
# 3. The actual billing call path survives an extreme energy reading
# =============================================================================

def test_energy_cost_survives_extreme_energy_without_raising():
    """energy_cost() (session_cost()'s legacy/flat-rate branch) must not
    raise for an absurd energy_kwh — this is what a 1e30 kwh telemetry frame
    would have driven finalize_charging_session's session_cost() call into
    before the MAX_PLAUSIBLE_KWH ingestion guard existed."""
    assert energy_cost(1e30, Decimal("5.00")) == MAX_MONEY
    assert energy_cost(-1e30, Decimal("5.00")) == -MAX_MONEY


def test_session_cost_survives_extreme_energy_for_a_legacy_session():
    from types import SimpleNamespace

    legacy_session = SimpleNamespace(
        rate_coins_per_kwh=Decimal("5.00"),
        settled_cost_coins=None,
        rate_segment_start_kwh=None,
    )
    assert session_cost(legacy_session, 1e30) == MAX_MONEY


def test_session_cost_survives_extreme_energy_for_a_segmented_session():
    from types import SimpleNamespace

    segmented_session = SimpleNamespace(
        rate_coins_per_kwh=Decimal("5.00"),
        settled_cost_coins=Decimal("10.00"),
        rate_segment_start_kwh=1.0,
    )
    assert session_cost(segmented_session, 1e30) == MAX_MONEY
