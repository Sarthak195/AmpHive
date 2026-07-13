"""
AmpHive segmented-billing helpers (Pricing v2 — docs/PRICING_V2_SPEC.md)
========================================================================
Cost math for time-of-day tariffs, where a single session can accrue energy
under MORE THAN ONE coins-per-kWh rate as it crosses slot boundaries mid-charge
(services/pricing.py resolve_rate_window supplies each rate + its boundary).

The state lives on ``ChargingSession`` (three nullable columns added in
migration 0018):

  - ``settled_cost_coins``     — coins already accrued for CLOSED segments.
  - ``rate_segment_start_kwh`` — the ``energy_kwh`` reading at which the CURRENT
                                 (open) segment began.
  - ``rate_coins_per_kwh``     — the rate of the CURRENT open segment.

FORWARD-ONLY INVARIANT: ``energy_kwh`` only ever rises, so the open segment's
energy (``energy_kwh - rate_segment_start_kwh``) is never negative; every
helper still clamps at 0.0 to stay robust against an out-of-order reading, and
already-settled cost is frozen so a later rate change never re-prices past
energy (same immutability rationale as the ``rate_coins_per_kwh`` snapshot).

A session with ``rate_segment_start_kwh IS NULL`` is a LEGACY single-rate
session — the only kind Phase 1 produces — and bills flat, exactly as before.
Phase 1 does NOT call these from any live billing path; that wiring is a later
phase.
"""
from decimal import Decimal

from backend.services.money import ZERO_MONEY, energy_cost, to_money
from backend.services.telemetry import COINS_PER_KWH


def _current_rate(session):
    """The session's current open-segment rate, or the global env default for a
    pre-tariff row whose snapshot is NULL (mirrors services/pricing.py)."""
    rate = session.rate_coins_per_kwh
    return rate if rate is not None else COINS_PER_KWH


def session_cost(session, energy_kwh) -> Decimal:
    """
    Total coins owed for ``session`` at cumulative ``energy_kwh``, as 2dp
    Decimal money.

    Legacy single-rate session (``rate_segment_start_kwh`` is None): the whole
    energy at the session's snapshot rate (or the env default when NULL) —
    identical to the pre-Pricing-v2 calculation.

    Segmented session: the frozen ``settled_cost_coins`` of the closed
    segments PLUS the open segment — energy above ``rate_segment_start_kwh``
    (clamped at 0) at the current rate.
    """
    if session.rate_segment_start_kwh is None:
        return energy_cost(energy_kwh, _current_rate(session))

    settled = session.settled_cost_coins if session.settled_cost_coins is not None else ZERO_MONEY
    open_energy = max(0.0, float(energy_kwh) - float(session.rate_segment_start_kwh))
    return to_money(settled + energy_cost(open_energy, _current_rate(session)))


def close_out_segment(session, new_rate, at_energy, valid_until) -> None:
    """
    Close the current billing segment at cumulative energy ``at_energy`` and
    open a new one at ``new_rate`` (valid until wall-clock ``valid_until``).
    Mutates ``session`` in place; returns None.

    Call this the instant a session crosses a slot boundary: it freezes the
    just-consumed energy (from the open segment's start up to ``at_energy``) at
    the OLD rate into ``settled_cost_coins``, then repoints the open segment at
    ``at_energy`` / ``new_rate``. Because settled cost is accumulated and never
    revisited, past energy keeps its old price no matter how the rate changes
    afterwards (the forward-only invariant).

    Legacy/first-touch (``rate_segment_start_kwh`` is None): initialise the
    accrual state first — nothing settled yet (0), the open segment starts at
    energy 0 — so a session started before Pricing v2 upgrades cleanly on its
    first boundary crossing.
    """
    if session.rate_segment_start_kwh is None:
        session.settled_cost_coins = ZERO_MONEY
        session.rate_segment_start_kwh = 0.0

    segment_start = float(session.rate_segment_start_kwh)
    settled = session.settled_cost_coins if session.settled_cost_coins is not None else ZERO_MONEY
    # Accrue the closing segment's energy at the CURRENT (old) rate.
    accrued = energy_cost(max(0.0, float(at_energy) - segment_start), _current_rate(session))

    session.settled_cost_coins = to_money(settled + accrued)
    session.rate_segment_start_kwh = float(at_energy)
    session.rate_coins_per_kwh = new_rate
    session.rate_valid_until = valid_until
