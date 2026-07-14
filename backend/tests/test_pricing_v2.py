"""
Pricing v2 — Phase 1 unit + resolution tests (docs/PRICING_V2_SPEC.md).

What's proven here:

1. [DB-free] services/pricing.py `_slot_rate_and_bound` — the pure,
   deterministic time-of-day resolution core: a covering slot yields its rate +
   its end boundary; minute == end_min falls through (half-open interval); a
   gap yields (None, next slot's start); days_mask excludes the wrong weekday;
   and a midnight-wrapping rate modelled as two slots ([1320,1440) + [0,360))
   resolves correctly on both sides of midnight. FIXED tz-aware datetimes only
   — never datetime.now.
2. [DB-free] services/billing.py `session_cost` / `close_out_segment` — the
   segment-accrual math: a legacy (unsegmented) session bills flat; a
   segmented session sums the frozen settled cost + the open segment; and
   `close_out_segment` freezes each closing segment at its rate and repoints
   the open one, including the legacy first-touch init path.
3. [DB-gated — needs TEST_DATABASE_URL, CI's postgres:15 service; skipped
   locally by policy, same as test_pricing.py] services/pricing.py
   `resolve_rate_window`: a plug whose tariff carries a slot resolves the SLOT
   rate + boundary inside the window and the tariff's flat price outside it;
   the fallback chain still resolves through the tenant default in the window
   API; and `resolve_rate_for_plug` still returns the window's scalar rate.

Phase 1 is schema + resolution + billing helpers only — nothing here is wired
into a live billing path, so current billing behaviour is unchanged.
"""
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio


# =============================================================================
# 1. DB-free: _slot_rate_and_bound (the pure time-of-day resolution core)
# =============================================================================

def _slot(start_min, end_min, price, days_mask=127):
    """A TariffSlot-like stub for the pure resolver — no DB, no ORM."""
    return SimpleNamespace(
        start_min=start_min, end_min=end_min,
        price_per_kwh=Decimal(str(price)), days_mask=days_mask,
    )


def _at(y, mo, d, h, mi):
    """A FIXED tz-aware (UTC) datetime — deterministic; never datetime.now."""
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


def test_slot_rate_and_bound_covering_slot_returns_rate_and_end():
    from backend.services.pricing import _slot_rate_and_bound

    slots = [_slot(540, 1020, "8.00")]        # 09:00–17:00 daily
    rate, boundary = _slot_rate_and_bound(slots, _at(2026, 7, 13, 12, 0))
    assert rate == Decimal("8.00")
    assert boundary == _at(2026, 7, 13, 17, 0)  # the covering slot's end today


def test_slot_rate_and_bound_end_min_is_exclusive_next_slot_covers():
    """minute == first slot's end_min is NOT in its half-open [start,end); the
    adjacent slot starting exactly there covers instead."""
    from backend.services.pricing import _slot_rate_and_bound

    slots = [_slot(360, 720, "5.00"), _slot(720, 1080, "9.00")]  # adjacent
    rate, boundary = _slot_rate_and_bound(slots, _at(2026, 7, 13, 12, 0))  # 720
    assert rate == Decimal("9.00")               # second slot covers
    assert boundary == _at(2026, 7, 13, 18, 0)   # its end (1080 min)


def test_slot_rate_and_bound_end_min_no_following_slot_is_none():
    """minute == end_min with nothing after -> uncovered, no later boundary."""
    from backend.services.pricing import _slot_rate_and_bound

    slots = [_slot(360, 720, "5.00")]
    rate, boundary = _slot_rate_and_bound(slots, _at(2026, 7, 13, 12, 0))  # 720
    assert rate is None
    assert boundary is None


def test_slot_rate_and_bound_gap_returns_none_and_next_start():
    from backend.services.pricing import _slot_rate_and_bound

    slots = [_slot(360, 480, "5.00"), _slot(600, 720, "9.00")]  # gap 08:00–10:00
    rate, boundary = _slot_rate_and_bound(slots, _at(2026, 7, 13, 9, 0))  # 540
    assert rate is None
    assert boundary == _at(2026, 7, 13, 10, 0)   # next slot's start (600 min)


def test_slot_rate_and_bound_days_mask_excludes_wrong_weekday():
    """2026-07-13 is a Monday (weekday 0). A weekends-only mask makes an
    otherwise-covering all-day slot inactive today — no rate, no boundary."""
    from backend.services.pricing import _slot_rate_and_bound

    weekend_only = (1 << 5) | (1 << 6)           # Sat=bit5, Sun=bit6 -> 96
    slots = [_slot(0, 1440, "9.00", days_mask=weekend_only)]
    rate, boundary = _slot_rate_and_bound(slots, _at(2026, 7, 13, 12, 0))  # Mon
    assert rate is None
    assert boundary is None


def test_slot_rate_and_bound_days_mask_includes_right_weekday():
    """Positive control for the mask: the same weekends-only slot DOES apply on
    a Saturday, and its end_min == 1440 boundary rolls to next-day midnight."""
    from backend.services.pricing import _slot_rate_and_bound

    weekend_only = (1 << 5) | (1 << 6)
    slots = [_slot(0, 1440, "9.00", days_mask=weekend_only)]
    rate, boundary = _slot_rate_and_bound(slots, _at(2026, 7, 18, 12, 0))  # Sat
    assert rate == Decimal("9.00")
    assert boundary == _at(2026, 7, 19, 0, 0)    # 1440 min -> next-day midnight


def test_slot_rate_and_bound_midnight_wraparound_two_slots():
    """A 22:00–06:00 night rate modelled as two slots resolves correctly on
    both sides of midnight, with a day slot filling the middle."""
    from backend.services.pricing import _slot_rate_and_bound

    slots = [
        _slot(1320, 1440, "3.00"),   # 22:00–24:00 night
        _slot(360, 1320, "7.00"),    # 06:00–22:00 day
        _slot(0, 360, "3.00"),       # 00:00–06:00 night
    ]

    # 23:30 -> late-night slot; boundary rolls to next-day midnight.
    rate, boundary = _slot_rate_and_bound(slots, _at(2026, 7, 13, 23, 30))
    assert rate == Decimal("3.00")
    assert boundary == _at(2026, 7, 14, 0, 0)

    # 00:30 (next day) -> early-morning slot; boundary at 06:00 same day.
    rate, boundary = _slot_rate_and_bound(slots, _at(2026, 7, 14, 0, 30))
    assert rate == Decimal("3.00")
    assert boundary == _at(2026, 7, 14, 6, 0)

    # Midday -> the day slot.
    rate, boundary = _slot_rate_and_bound(slots, _at(2026, 7, 14, 12, 0))
    assert rate == Decimal("7.00")
    assert boundary == _at(2026, 7, 14, 22, 0)


# =============================================================================
# 2. DB-free: session_cost / close_out_segment (segment-accrual math)
# =============================================================================

def _session(rate=None, settled=None, seg_start=None):
    """A ChargingSession-like stub for the billing helpers — no DB, no ORM."""
    return SimpleNamespace(
        rate_coins_per_kwh=(Decimal(str(rate)) if rate is not None else None),
        settled_cost_coins=(Decimal(str(settled)) if settled is not None else None),
        rate_segment_start_kwh=seg_start,
        rate_valid_until=None,
    )


def test_session_cost_legacy_single_rate():
    """No segment marker -> flat: 2.0 kWh * 5.00 = 10.00."""
    from backend.services.billing import session_cost

    assert session_cost(_session(rate="5.00"), 2.0) == Decimal("10.00")


def test_session_cost_legacy_null_rate_falls_back_to_env_default():
    """A NULL snapshot (pre-tariff row) bills at the global env default rate."""
    from backend.services.billing import session_cost
    from backend.services.money import energy_cost
    from backend.services.telemetry import COINS_PER_KWH

    assert session_cost(_session(rate=None), 2.0) == energy_cost(2.0, COINS_PER_KWH)


def test_session_cost_single_open_segment():
    """Segmented, nothing settled yet: 0 + (2.0-0.0)*5.00 = 10.00."""
    from backend.services.billing import session_cost

    s = _session(rate="5.00", settled="0.00", seg_start=0.0)
    assert session_cost(s, 2.0) == Decimal("10.00")


def test_session_cost_multi_segment_settled_plus_open():
    """18.00 settled + open (5.0-3.0)*6.00 = 12.00 -> 30.00."""
    from backend.services.billing import session_cost

    s = _session(rate="6.00", settled="18.00", seg_start=3.0)
    assert session_cost(s, 5.0) == Decimal("30.00")


def test_close_out_segment_accrues_and_repoints():
    from backend.services.billing import close_out_segment, session_cost

    valid_until = datetime(2026, 7, 13, 17, 0, tzinfo=timezone.utc)
    s = _session(rate="5.00", settled="0.00", seg_start=0.0)

    # First boundary: settled = 0 + (2.0-0.0)*5.00 = 10.00; open -> 2.0 @ 8.00.
    close_out_segment(s, new_rate=Decimal("8.00"), at_energy=2.0, valid_until=valid_until)
    assert s.settled_cost_coins == Decimal("10.00")
    assert s.rate_segment_start_kwh == 2.0
    assert s.rate_coins_per_kwh == Decimal("8.00")
    assert s.rate_valid_until == valid_until

    # Second boundary: settled += (5.0-2.0)*8.00 = 24.00 -> 34.00; open -> 5.0 @ 6.00.
    close_out_segment(s, new_rate=Decimal("6.00"), at_energy=5.0, valid_until=None)
    assert s.settled_cost_coins == Decimal("34.00")
    assert s.rate_segment_start_kwh == 5.0
    assert s.rate_coins_per_kwh == Decimal("6.00")

    # End-to-end total at 7.0 kWh = 34.00 + (7.0-5.0)*6.00 = 46.00.
    assert session_cost(s, 7.0) == Decimal("46.00")


def test_close_out_segment_legacy_first_touch_init():
    """A legacy (unsegmented) session upgrades cleanly on its first boundary:
    init settled=0/seg_start=0, then accrue at the old snapshot rate."""
    from backend.services.billing import close_out_segment

    valid_until = datetime(2026, 7, 13, 17, 0, tzinfo=timezone.utc)
    s = _session(rate="5.00")
    assert s.rate_segment_start_kwh is None and s.settled_cost_coins is None

    close_out_segment(s, new_rate=Decimal("8.00"), at_energy=2.0, valid_until=valid_until)
    assert s.settled_cost_coins == Decimal("10.00")   # (2.0-0.0)*5.00
    assert s.rate_segment_start_kwh == 2.0
    assert s.rate_coins_per_kwh == Decimal("8.00")
    assert s.rate_valid_until == valid_until


# =============================================================================
# 2b. DB-free: reprice_session_if_due branching (resolve_rate_window patched)
# =============================================================================

def _live_session(rate, valid_until, energy_kwh, settled="0.00", seg_start=0.0):
    """An ACTIVE segmented session stub for reprice_session_if_due — the DB
    resolver is patched out, so db/plug are never touched."""
    return SimpleNamespace(
        rate_coins_per_kwh=Decimal(str(rate)),
        settled_cost_coins=Decimal(str(settled)),
        rate_segment_start_kwh=seg_start,
        rate_valid_until=valid_until,
        energy_kwh=energy_kwh,
    )


def _patch_resolve(monkeypatch, rate, boundary):
    """Force resolve_rate_window to a fixed (rate, boundary), and flag if it
    was called — so a test can assert the cheap no-op paths never hit the DB."""
    called = {"n": 0}

    async def _fake(db, plug, at=None):
        called["n"] += 1
        return Decimal(str(rate)), boundary

    monkeypatch.setattr("backend.services.pricing.resolve_rate_window", _fake)
    return called


@pytest.mark.asyncio
async def test_reprice_flat_session_is_noop_without_resolving(monkeypatch):
    """rate_valid_until is None (flat tariff) -> no reprice, no DB resolve."""
    from backend.services.pricing import reprice_session_if_due

    called = _patch_resolve(monkeypatch, "9.00", None)
    s = _live_session("5.00", valid_until=None, energy_kwh=3.0)
    assert await reprice_session_if_due(None, s, None) is None
    assert called["n"] == 0
    assert s.rate_coins_per_kwh == Decimal("5.00")


@pytest.mark.asyncio
async def test_reprice_before_boundary_is_noop_without_resolving(monkeypatch):
    """at < rate_valid_until -> segment still open, no resolve, no change."""
    from backend.services.pricing import reprice_session_if_due

    called = _patch_resolve(monkeypatch, "9.00", None)
    future = datetime(2026, 7, 14, 18, 0, tzinfo=timezone.utc)
    s = _live_session("5.00", valid_until=future, energy_kwh=3.0)
    at = datetime(2026, 7, 14, 17, 0, tzinfo=timezone.utc)
    assert await reprice_session_if_due(None, s, None, at=at) is None
    assert called["n"] == 0
    assert s.rate_coins_per_kwh == Decimal("5.00")


@pytest.mark.asyncio
async def test_reprice_at_boundary_rate_changed_closes_segment(monkeypatch):
    """Boundary passed and the rate actually changed -> close out the segment
    forward-only, repoint at the current energy, return (new_rate, boundary)."""
    from backend.services.pricing import reprice_session_if_due

    next_boundary = datetime(2026, 7, 14, 22, 0, tzinfo=timezone.utc)
    _patch_resolve(monkeypatch, "8.00", next_boundary)
    boundary = datetime(2026, 7, 14, 18, 0, tzinfo=timezone.utc)
    s = _live_session("5.00", valid_until=boundary, energy_kwh=2.0)

    out = await reprice_session_if_due(None, s, None, at=boundary)
    assert out == (Decimal("8.00"), next_boundary)
    assert s.settled_cost_coins == Decimal("10.00")   # (2.0-0.0)*5.00 frozen
    assert s.rate_segment_start_kwh == 2.0
    assert s.rate_coins_per_kwh == Decimal("8.00")
    assert s.rate_valid_until == next_boundary


@pytest.mark.asyncio
async def test_reprice_at_boundary_same_rate_just_advances(monkeypatch):
    """Adjacent slots repeating a price: no segment churn, no notification —
    only the next check point moves forward."""
    from backend.services.pricing import reprice_session_if_due

    next_boundary = datetime(2026, 7, 14, 22, 0, tzinfo=timezone.utc)
    _patch_resolve(monkeypatch, "5.00", next_boundary)   # same as current rate
    boundary = datetime(2026, 7, 14, 18, 0, tzinfo=timezone.utc)
    s = _live_session("5.00", valid_until=boundary, energy_kwh=2.0)

    assert await reprice_session_if_due(None, s, None, at=boundary) is None
    assert s.settled_cost_coins == Decimal("0.00")     # unchanged
    assert s.rate_segment_start_kwh == 0.0             # unchanged
    assert s.rate_valid_until == next_boundary         # advanced


@pytest.mark.asyncio
async def test_mark_tenant_sessions_for_reprice_gated_off_is_noop(monkeypatch):
    """[Phase 3] With AUTO_REPRICE_ACTIVE_SESSIONS off, an operator edit issues
    NO reprice UPDATE (edits then only affect sessions that start afterwards)."""
    import backend.services.pricing as pricing_mod
    from unittest.mock import AsyncMock

    monkeypatch.setattr(pricing_mod, "AUTO_REPRICE_ACTIVE_SESSIONS", False)
    db = AsyncMock()
    await pricing_mod.mark_tenant_sessions_for_reprice(db, tenant_id=1)
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_mark_tenant_sessions_for_reprice_issues_update_when_on(monkeypatch):
    """When on (default), it issues exactly one UPDATE (the frame hook/reaper do
    the actual per-session reprice + notify — this only expires the segment)."""
    import backend.services.pricing as pricing_mod
    from unittest.mock import AsyncMock

    monkeypatch.setattr(pricing_mod, "AUTO_REPRICE_ACTIVE_SESSIONS", True)
    db = AsyncMock()
    await pricing_mod.mark_tenant_sessions_for_reprice(db, tenant_id=1)
    db.execute.assert_awaited_once()


# =============================================================================
# 2c. DB-free: slot_overlaps (operator slot-CRUD validation core, Phase 4)
# =============================================================================

def test_slot_overlaps_same_days_intersecting_windows():
    from backend.services.pricing import slot_overlaps
    # 09:00–17:00 vs 10:00–11:40, all days -> overlap.
    assert slot_overlaps(540, 1020, 127, 600, 700, 127) is True


def test_slot_overlaps_touching_boundary_is_not_overlap():
    from backend.services.pricing import slot_overlaps
    # Half-open: 09:00–17:00 and 17:00–20:00 touch but don't overlap.
    assert slot_overlaps(540, 1020, 127, 1020, 1200, 127) is False


def test_slot_overlaps_disjoint_windows():
    from backend.services.pricing import slot_overlaps
    assert slot_overlaps(540, 1020, 127, 1100, 1200, 127) is False


def test_slot_overlaps_intersecting_windows_but_no_shared_day():
    from backend.services.pricing import slot_overlaps
    # Same clock window, but one is Mon-only (bit0) and the other Tue-only (bit1).
    assert slot_overlaps(540, 1020, 0b0000001, 600, 700, 0b0000010) is False


def test_slot_overlaps_shared_day_subset_conflicts():
    from backend.services.pricing import slot_overlaps
    # Mon+Tue vs Tue-only, overlapping windows -> conflict on Tue.
    assert slot_overlaps(540, 1020, 0b0000011, 600, 700, 0b0000010) is True


# =============================================================================
# 3. DB-gated: resolve_rate_window against a real schema (needs TEST_DATABASE_URL)
# =============================================================================

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")

db_gated = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="needs TEST_DATABASE_URL pointing at a throwaway Postgres (CI service)",
)

ENUM_TYPES = ["gateway_status", "plug_status", "session_status", "tx_type", "user_role"]


@pytest_asyncio.fixture()
async def factory():
    """Engine + fresh schema per test; yields a session factory (same shape as
    test_session_limits.py / test_pricing.py)."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from backend.database.models import Base

    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        for enum_name in ENUM_TYPES:
            await conn.execute(text(f"DROP TYPE IF EXISTS {enum_name} CASCADE"))
        await conn.run_sync(Base.metadata.create_all)

    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


# --- Seed helpers (mirror test_pricing.py) -------------------------------------

async def _seed_tenant(factory, name: str = "Tenant", tz=None) -> int:
    from backend.database.models import Tenant

    async with factory() as db:
        tenant = Tenant(name=f"{name}-{uuid.uuid4().hex[:8]}")
        if tz is not None:
            tenant.timezone = tz
        db.add(tenant)
        await db.commit()
        return tenant.id


async def _seed_gateway(factory, tenant_id: int, gateway_id: str) -> str:
    from backend.database.models import Gateway

    gateway_id = f"{gateway_id}-{uuid.uuid4().hex[:8]}"
    async with factory() as db:
        gw = Gateway(id=gateway_id, tenant_id=tenant_id, name=gateway_id, vpn_ip=gateway_id)
        db.add(gw)
        await db.commit()
        return gw.id


async def _seed_tariff(factory, tenant_id: int, price: str, name: str = "Tariff") -> int:
    from backend.database.models import Tariff

    async with factory() as db:
        tariff = Tariff(tenant_id=tenant_id, name=name, price_per_kwh=Decimal(price))
        db.add(tariff)
        await db.commit()
        return tariff.id


async def _seed_slot(factory, tariff_id: int, start_min: int, end_min: int,
                     price: str, days_mask: int = 127) -> int:
    from backend.database.models import TariffSlot

    async with factory() as db:
        slot = TariffSlot(
            tariff_id=tariff_id, start_min=start_min, end_min=end_min,
            price_per_kwh=Decimal(price), days_mask=days_mask,
        )
        db.add(slot)
        await db.commit()
        return slot.id


async def _seed_plug(factory, gateway_id: str, group_id=None, tariff_id=None,
                     name: str = "Plug") -> int:
    from backend.database.models import Plug

    async with factory() as db:
        plug = Plug(
            gateway_id=gateway_id, name=name, local_ip="10.0.0.5",
            group_id=group_id, tariff_id=tariff_id,
        )
        db.add(plug)
        await db.commit()
        return plug.id


async def _load_plug(factory, plug_id: int):
    from sqlalchemy import select

    from backend.database.models import Plug

    async with factory() as db:
        return (await db.execute(select(Plug).where(Plug.id == plug_id))).scalar_one()


@db_gated
@pytest.mark.asyncio
async def test_resolve_rate_window_slot_rate_inside_flat_outside(factory):
    """A plug's tariff carries a 09:00–17:00 slot. Inside the window the slot
    rate + its end boundary win; outside, the tariff's flat price with no
    later boundary. Tenant zone = UTC so minute-of-day == the UTC wall-clock."""
    from backend.services.pricing import resolve_rate_window

    tenant_id = await _seed_tenant(factory, tz="UTC")
    gw = await _seed_gateway(factory, tenant_id, "gw-v2-slot")
    tariff_id = await _seed_tariff(factory, tenant_id, "5.00", "Base")
    await _seed_slot(factory, tariff_id, 540, 1020, "8.00")  # 09:00–17:00 @ 8.00
    plug_id = await _seed_plug(factory, gw, tariff_id=tariff_id)
    plug = await _load_plug(factory, plug_id)

    inside = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    outside = datetime(2026, 7, 13, 20, 0, tzinfo=timezone.utc)

    async with factory() as db:
        rate_in, bound_in = await resolve_rate_window(db, plug, at=inside)
    assert rate_in == Decimal("8.00")
    assert bound_in == datetime(2026, 7, 13, 17, 0, tzinfo=timezone.utc)

    async with factory() as db:
        rate_out, bound_out = await resolve_rate_window(db, plug, at=outside)
    assert rate_out == Decimal("5.00")   # flat tariff price outside the slot
    assert bound_out is None             # no later slot starts today


@db_gated
@pytest.mark.asyncio
async def test_resolve_rate_window_chain_fallback_tenant_default_no_slots(factory):
    """The fallback chain still applies in the window API: an unassigned,
    ungrouped plug resolves the tenant default tariff — flat, no boundary
    (that tariff has no slots)."""
    from sqlalchemy import select

    from backend.database.models import Tenant
    from backend.services.pricing import resolve_rate_window

    tenant_id = await _seed_tenant(factory, tz="UTC")
    gw = await _seed_gateway(factory, tenant_id, "gw-v2-chain")
    tariff_id = await _seed_tariff(factory, tenant_id, "7.50", "Tenant Default")
    plug_id = await _seed_plug(factory, gw)  # no plug/group tariff

    async with factory() as db:
        tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one()
        tenant.default_tariff_id = tariff_id
        await db.commit()

    plug = await _load_plug(factory, plug_id)
    at = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    async with factory() as db:
        rate, boundary = await resolve_rate_window(db, plug, at=at)
    assert rate == Decimal("7.50")
    assert boundary is None


@db_gated
@pytest.mark.asyncio
async def test_resolve_rate_for_plug_still_returns_window_scalar(factory):
    """The refactored resolve_rate_for_plug delegates to resolve_rate_window
    and returns its scalar rate. With no slots the flat price applies at every
    instant, so the (now-resolved) scalar equals the window rate and the
    tariff price deterministically."""
    from backend.services.pricing import resolve_rate_for_plug, resolve_rate_window

    tenant_id = await _seed_tenant(factory, tz="UTC")
    gw = await _seed_gateway(factory, tenant_id, "gw-v2-deleg")
    tariff_id = await _seed_tariff(factory, tenant_id, "4.25", "Flat")
    plug_id = await _seed_plug(factory, gw, tariff_id=tariff_id)
    plug = await _load_plug(factory, plug_id)

    async with factory() as db:
        scalar = await resolve_rate_for_plug(db, plug)
        window_rate, _ = await resolve_rate_window(db, plug)
    assert scalar == Decimal("4.25")
    assert scalar == window_rate


@db_gated
@pytest.mark.asyncio
async def test_resolve_price_display_current_next_and_boundary(factory):
    """[Phase 4] A plug's tariff is flat 5.00 with a 09:00–17:00 @ 8.00 slot.
    Before the slot: current=flat, next=slot @ its start. Inside: current=slot,
    next=flat @ its end. After (no more slots today): no next price at all."""
    from backend.services.pricing import resolve_price_display

    tenant_id = await _seed_tenant(factory, tz="UTC")
    gw = await _seed_gateway(factory, tenant_id, "gw-v2-disp")
    tariff_id = await _seed_tariff(factory, tenant_id, "5.00", "Base")
    await _seed_slot(factory, tariff_id, 540, 1020, "8.00")  # 09:00–17:00 @ 8.00
    plug_id = await _seed_plug(factory, gw, tariff_id=tariff_id)
    plug = await _load_plug(factory, plug_id)

    before = datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc)
    inside = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    after = datetime(2026, 7, 13, 20, 0, tzinfo=timezone.utc)

    async with factory() as db:
        r0, at0, nx0 = await resolve_price_display(db, plug, at=before)
        r1, at1, nx1 = await resolve_price_display(db, plug, at=inside)
        r2, at2, nx2 = await resolve_price_display(db, plug, at=after)

    assert (r0, nx0) == (Decimal("5.00"), Decimal("8.00"))
    assert at0 == datetime(2026, 7, 13, 9, 0, tzinfo=timezone.utc)
    assert (r1, nx1) == (Decimal("8.00"), Decimal("5.00"))
    assert at1 == datetime(2026, 7, 13, 17, 0, tzinfo=timezone.utc)
    assert (r2, at2, nx2) == (Decimal("5.00"), None, None)
