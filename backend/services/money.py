"""
AmpHive money helpers
======================
Coin/rupee wallet amounts are stored as ``NUMERIC(12,2)`` (see the four money
columns in ``database/models.py``: ``User.coin_balance``,
``ChargingSession.coins_spent``, ``LedgerTransaction.amount`` /
``.balance_after``). In Python those columns surface as :class:`decimal.Decimal`.

All wallet arithmetic must go through :class:`~decimal.Decimal` so repeated
credit/debit cycles don't accumulate binary-float rounding drift — that drift is
exactly why the columns moved off ``Float``. Mixing a ``Decimal`` balance with a
``float`` in the same expression raises ``TypeError``, so any value entering the
wallet math from a float source (energy × rate, Razorpay amounts, env rates)
must be normalised with :func:`to_money` first.

Physical quantities (``energy_kwh``, ``power_w``, voltage, current) intentionally
stay ``float`` — they are measurements, not currency, and don't need base-10
exactness.
"""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

# Two decimal places — coins mirror rupees (paise-level granularity).
MONEY_QUANT = Decimal("0.01")

ZERO_MONEY = Decimal("0.00")

# Defense-in-depth ceiling: every money column this passes through is
# NUMERIC(12,2) (see module docstring) — 12 total digits, 2 after the point —
# so 9,999,999,999.99 is the largest value the DB could ever store anyway.
# Upstream guards (e.g. the mqtt telemetry ingestion's MAX_PLAUSIBLE_KWH
# bound) are meant to keep anything absurd from reaching this module at all,
# but if one ever slips through — a corrupted/huge float from *any* future
# caller, not just telemetry — Decimal.quantize() below raises a raw
# decimal.InvalidOperation for a magnitude too large for the default 28-digit
# context to round to 2dp. That single exception, uncaught, has been shown to
# take down every finalize path that computes a cost from it (a session can
# never settle and its plug stays OCCUPIED forever). Clamping here instead
# means to_money() itself can never be the reason a caller crashes.
MAX_MONEY = Decimal("9999999999.99")


def to_money(value) -> Decimal:
    """Coerce an int/float/str/Decimal to a 2-dp :class:`~decimal.Decimal`.

    Rounds half-up to 2 places. Goes via ``Decimal(str(value))`` for float
    inputs so a value like ``0.1`` doesn't smuggle in its binary tail
    (``Decimal(0.1)`` is ``0.1000000000000000055…``, whereas
    ``Decimal("0.1")`` is exact).

    Out-of-range magnitudes (and non-finite NaN/Infinity, which real wallet
    math never produces but a poisoned input could) are clamped to
    :data:`MAX_MONEY` rather than left to raise/propagate — see MAX_MONEY.
    """
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    if not value.is_finite():
        # NaN has no sign to clamp toward; ±Infinity does.
        if value.is_nan():
            return ZERO_MONEY
        return MAX_MONEY if value > 0 else -MAX_MONEY
    try:
        result = value.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return MAX_MONEY if value > 0 else -MAX_MONEY
    if result > MAX_MONEY:
        return MAX_MONEY
    if result < -MAX_MONEY:
        return -MAX_MONEY
    return result


def energy_cost(energy_kwh, rate_coins_per_kwh) -> Decimal:
    """Coins owed for ``energy_kwh`` at ``rate_coins_per_kwh`` (coins/kWh), as
    money-safe ``Decimal``.

    ``rate_coins_per_kwh`` is typically a ``Decimal`` already (a
    ``Tariff.price_per_kwh`` / ``ChargingSession.rate_coins_per_kwh``
    snapshot) but may also be a bare float/str (the legacy ``COINS_PER_KWH``
    env fallback) — it is normalised via :func:`to_money` first so the
    multiplication never mixes a raw float with a ``Decimal`` (see module
    docstring: "energy × rate" is exactly the case that must not skip
    ``to_money``).

    ``energy_kwh`` is deliberately **not** rounded before the multiply — only
    the final product is quantized to 2dp. Rounding the energy operand first
    would throw away precision the final rounding is supposed to see (e.g.
    0.333 kWh × 5.00 = 1.665 → 1.67, not 0.33 × 5.00 = 1.65).
    """
    rate = to_money(rate_coins_per_kwh)
    energy = Decimal(str(energy_kwh))
    return to_money(energy * rate)
