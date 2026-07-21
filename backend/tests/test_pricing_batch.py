"""
Batched price display — services/pricing.py resolve_price_display_batch and
its pure pieces (audit: the /api/plugs/available + /api/plugs/public per-plug
tariff-resolution N+1).

All DB-free, in the test_pricing_v2.py style (SimpleNamespace stubs, FIXED
tz-aware datetimes, never datetime.now):

1. `_pick_tariff` — the pure chain: plug -> group -> tenant default, first id
   that resolves to a LIVE row wins, a dangling id falls through, an
   empty/fully-dangling chain yields None.
2. `_price_display_from_slots` — the pure display core extracted from
   resolve_price_display: flat tariffs, the same-local-day "next price"
   preview, the adjacent-equal-price suppression, and the cross-day
   suppression.
3. `resolve_price_display_batch` end-to-end over a stubbed AsyncSession:
   exactly TWO queries for any list size (tariffs IN + slots IN), zero
   queries for an all-default list, and per-plug triples matching what the
   single-plug path would produce.
"""
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.services.pricing import (
    _pick_tariff, _price_display_from_slots, default_rate,
    resolve_price_display_batch,
)


def _slot(tariff_id, start_min, end_min, price, days_mask=127):
    return SimpleNamespace(
        tariff_id=tariff_id, start_min=start_min, end_min=end_min,
        price_per_kwh=Decimal(str(price)), days_mask=days_mask,
    )


def _tariff(id, price):
    return SimpleNamespace(id=id, price_per_kwh=Decimal(str(price)))


def _plug(id, tariff_id=None):
    return SimpleNamespace(id=id, tariff_id=tariff_id)


def _tenant(default_tariff_id=None, timezone="UTC"):
    return SimpleNamespace(default_tariff_id=default_tariff_id, timezone=timezone)


def _at(y, mo, d, h, mi):
    """FIXED tz-aware (UTC) instant — deterministic; never datetime.now."""
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


# =============================================================================
# 1. _pick_tariff (pure chain fallthrough)
# =============================================================================

def test_pick_tariff_chain_order_and_dangling_fallthrough():
    t_plug, t_group, t_tenant = _tariff(1, "9"), _tariff(2, "7"), _tariff(3, "5")
    live = {1: t_plug, 2: t_group, 3: t_tenant}

    assert _pick_tariff((1, 2, 3), live) is t_plug        # plug link wins
    assert _pick_tariff((None, 2, 3), live) is t_group    # then group
    assert _pick_tariff((None, None, 3), live) is t_tenant
    # A dangling id (99 has no live row) falls through to the next link —
    # exactly like the per-plug scalar_one_or_none() chain.
    assert _pick_tariff((99, 2, 3), live) is t_group
    assert _pick_tariff((99, 98, 3), live) is t_tenant
    assert _pick_tariff((99, 98, 97), live) is None
    assert _pick_tariff((None, None, None), live) is None


# =============================================================================
# 2. _price_display_from_slots (pure display core)
# =============================================================================

def test_display_core_flat_tariff_no_slots():
    rate, boundary, nxt = _price_display_from_slots([], _at(2026, 7, 13, 12, 0), Decimal("6.00"))
    assert (rate, boundary, nxt) == (Decimal("6.00"), None, None)


def test_display_core_previews_real_same_day_change():
    # 09:00–17:00 @ 8; flat 6 after — at noon the ribbon shows 8 -> 6 @ 17:00.
    slots = [_slot(1, 540, 1020, "8.00")]
    rate, boundary, nxt = _price_display_from_slots(slots, _at(2026, 7, 13, 12, 0), Decimal("6.00"))
    assert rate == Decimal("8.00")
    assert (boundary.hour, boundary.minute) == (17, 0)
    assert nxt == Decimal("6.00")


def test_display_core_suppresses_equal_price_across_boundary():
    # Adjacent slots repeating the SAME price — no "next price" hint.
    slots = [_slot(1, 540, 720, "8.00"), _slot(1, 720, 1020, "8.00")]
    rate, boundary, nxt = _price_display_from_slots(slots, _at(2026, 7, 13, 10, 0), Decimal("8.00"))
    assert rate == Decimal("8.00")
    assert boundary is None and nxt is None


def test_display_core_suppresses_cross_day_boundary():
    # Monday-only slot, resolved on a Sunday evening: the next boundary is
    # tomorrow — a bare "@ HH:MM" ribbon can't convey a date, so no preview.
    monday_only = [_slot(1, 540, 1020, "8.00", days_mask=1)]
    rate, boundary, nxt = _price_display_from_slots(
        monday_only, _at(2026, 7, 12, 20, 0), Decimal("6.00")  # 2026-07-12 = Sunday
    )
    assert rate == Decimal("6.00")
    assert boundary is None and nxt is None


# =============================================================================
# 3. resolve_price_display_batch (stubbed session)
# =============================================================================

def _db_returning(*results_in_order):
    """AsyncSession stub: each execute() pops the next canned result, whose
    .scalars() yields the given ORM-like stubs."""
    canned = [MagicMock(scalars=MagicMock(return_value=list(items))) for items in results_in_order]
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=canned)
    return db


@pytest.mark.asyncio
async def test_batch_two_queries_for_any_list_size_and_matching_triples():
    # Three plugs, three different chain outcomes, ONE batch:
    #   p1 -> own tariff 1 (slot 09:00–17:00 @ 8, flat 6) -> preview 8 -> 6 @ 17:00
    #   p2 -> dangling own id 99, group tariff 2 (flat 7, no slots)
    #   p3 -> tenant default 3 (flat 5, no slots)
    p1, p2, p3 = _plug(1, tariff_id=1), _plug(2, tariff_id=99), _plug(3)
    tenant = _tenant(default_tariff_id=3, timezone="UTC")
    tariffs = [_tariff(1, "6.00"), _tariff(2, "7.00"), _tariff(3, "5.00")]
    slots = [_slot(1, 540, 1020, "8.00")]

    db = _db_returning(tariffs, slots)
    out = await resolve_price_display_batch(
        db,
        [(p1, None, tenant), (p2, 2, tenant), (p3, None, tenant)],
        at=_at(2026, 7, 13, 12, 0),
    )

    assert db.execute.await_count == 2      # tariffs IN + slots IN — never per-plug
    rate1, boundary1, next1 = out[1]
    assert rate1 == Decimal("8.00") and next1 == Decimal("6.00")
    assert (boundary1.hour, boundary1.minute) == (17, 0)
    assert out[2] == (Decimal("7.00"), None, None)
    assert out[3] == (Decimal("5.00"), None, None)


@pytest.mark.asyncio
async def test_batch_empty_chains_cost_zero_queries_and_fall_back_to_env_default():
    # No tariff anywhere (and no tenant at all for p2) -> env default, and the
    # batch never touches the DB.
    db = _db_returning()
    out = await resolve_price_display_batch(
        db,
        [(_plug(1), None, _tenant()), (_plug(2), None, None)],
        at=_at(2026, 7, 13, 12, 0),
    )
    assert db.execute.await_count == 0
    assert out[1] == (default_rate(), None, None)
    assert out[2] == (default_rate(), None, None)


@pytest.mark.asyncio
async def test_batch_fully_dangling_chain_falls_back_to_env_default():
    # Ids exist in the chain but none resolve to a live row (deleted tariffs):
    # the tariff query runs, finds nothing, no slots query is worth making —
    # per-plug result is the env default, same as the single-plug path.
    db = _db_returning([])                 # tariffs IN -> empty
    out = await resolve_price_display_batch(
        db, [(_plug(1, tariff_id=99), 98, _tenant(default_tariff_id=97))],
        at=_at(2026, 7, 13, 12, 0),
    )
    assert db.execute.await_count == 1     # no winners -> slots query skipped
    assert out[1] == (default_rate(), None, None)
