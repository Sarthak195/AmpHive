"""
TelemetryStore behavior: relay/voltage passthrough and the `is_stale` flag the
live stream now emits so the frontend can warn a driver when the gateway link
drops instead of freezing on stale values.
"""
import asyncio
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from backend.services import telemetry as telemetry_mod
from backend.services.telemetry import TelemetryStore


@pytest.fixture(autouse=True)
def _fresh_store():
    # TelemetryStore is a singleton; reset it between tests so state doesn't leak.
    TelemetryStore._instance = None
    yield
    TelemetryStore._instance = None


def test_update_records_relay_and_voltage():
    store = TelemetryStore()
    store.update(plug_id=1, power_w=1200.0, current_a=5.2, energy_kwh=0.4,
                 status="charging", voltage_v=241.5, relay_on=True)
    snap = store.get_latest(1)
    assert snap.voltage_v == 241.5
    assert snap.relay_on is True
    assert snap.current_a == 5.2


@pytest.mark.asyncio
async def test_stream_flags_stale_snapshot():
    store = TelemetryStore()
    store.start_session(2)
    store.update(plug_id=2, power_w=1000.0, current_a=4.3, energy_kwh=0.1,
                 status="charging", voltage_v=230.0, relay_on=True)

    # Force the snapshot to look old so the stream marks it stale.
    store._data[2].updated_at = time.time() - (telemetry_mod.TELEMETRY_STALE_AFTER_SEC + 5)

    gen = store.stream(2)
    frame = await asyncio.wait_for(gen.__anext__(), timeout=2.0)
    await gen.aclose()

    assert frame["is_stale"] is True
    assert frame["age_sec"] >= telemetry_mod.TELEMETRY_STALE_AFTER_SEC


@pytest.mark.asyncio
async def test_stream_fresh_snapshot_not_stale():
    store = TelemetryStore()
    store.start_session(3)
    store.update(plug_id=3, power_w=500.0, current_a=2.1, energy_kwh=0.05,
                 status="charging", voltage_v=230.0, relay_on=True)

    gen = store.stream(3)
    frame = await asyncio.wait_for(gen.__anext__(), timeout=2.0)
    await gen.aclose()

    assert frame["is_stale"] is False
    assert frame["relay_on"] is True


# --- Per-session tariff rate snapshot (feat/tariff-pricing) -------------------

def test_update_uses_snapshotted_rate_not_global_default(monkeypatch):
    """start_session(plug_id, rate_coins_per_kwh=...) must make the live
    cost_coins calc bill at THAT rate, not the global COINS_PER_KWH default."""
    monkeypatch.setattr(telemetry_mod, "COINS_PER_KWH", 5.0)
    store = TelemetryStore()

    store.start_session(4, rate_coins_per_kwh=Decimal("8.00"))
    store.update(plug_id=4, power_w=1000.0, current_a=4.0, energy_kwh=2.0,
                 status="charging")

    snap = store.get_latest(4)
    assert snap.cost_coins == 16.0  # 2.0 kWh * 8.00, NOT 2.0 * 5.00 = 10.00


def test_update_falls_back_to_env_default_when_no_rate_snapshotted():
    """start_session with no rate (legacy caller / no tariff resolved) keeps
    the old global-default behavior."""
    store = TelemetryStore()

    store.start_session(5)  # no rate_coins_per_kwh
    store.update(plug_id=5, power_w=1000.0, current_a=4.0, energy_kwh=2.0,
                 status="charging")

    snap = store.get_latest(5)
    assert snap.cost_coins == 2.0 * telemetry_mod.COINS_PER_KWH


def test_end_session_clears_the_snapshotted_rate():
    """A plug reused for a new session with no explicit rate must not
    silently inherit the previous session's snapshotted rate."""
    store = TelemetryStore()

    store.start_session(6, rate_coins_per_kwh=Decimal("8.00"))
    store.end_session(6)
    assert 6 not in store._session_rates

    store.start_session(6)  # next session on the same plug, no rate resolved
    store.update(plug_id=6, power_w=500.0, current_a=2.0, energy_kwh=1.0,
                 status="charging")

    snap = store.get_latest(6)
    assert snap.cost_coins == 1.0 * telemetry_mod.COINS_PER_KWH


# --- Pricing v2: segment-aware live cost after a TOD reprice ------------------

def test_update_live_cost_reflects_segment_state():
    """After set_segment_state (a TOD boundary closed a segment), the live
    running cost = settled coins + open-segment energy at the new rate — the
    same figure services/billing.py session_cost bills."""
    store = TelemetryStore()

    store.start_session(7, rate_coins_per_kwh=Decimal("5.00"))
    # Boundary crossed at 2.0 kWh: 10.00 settled, now charging at 8.00/kWh.
    store.set_segment_state(7, settled_coins=Decimal("10.00"),
                            segment_start_kwh=2.0, rate_coins_per_kwh=Decimal("8.00"))
    store.update(plug_id=7, power_w=1000.0, current_a=4.0, energy_kwh=5.0,
                 status="charging")

    snap = store.get_latest(7)
    assert snap.cost_coins == 10.0 + (5.0 - 2.0) * 8.0  # 34.0


def test_start_session_resets_segment_state():
    """A plug reused for a new flat session must not inherit the previous
    session's settled/segment mirror (else its live cost would be inflated)."""
    store = TelemetryStore()

    store.start_session(8, rate_coins_per_kwh=Decimal("5.00"))
    store.set_segment_state(8, settled_coins=Decimal("99.00"), segment_start_kwh=4.0)
    store.end_session(8)

    store.start_session(8, rate_coins_per_kwh=Decimal("5.00"))
    store.update(plug_id=8, power_w=1000.0, current_a=4.0, energy_kwh=2.0,
                 status="charging")

    snap = store.get_latest(8)
    assert snap.cost_coins == 2.0 * 5.0  # 10.0 — clean single segment, no leak


# --- REC-11: rehydrate the mirror after a restart -----------------------------

def test_hydrate_session_rebuilds_rate_and_start_from_db(monkeypatch):
    """After a restart the maps are empty, so the first frame bills at the env
    default and the elapsed timer restarts. hydrate_session (fed the ACTIVE
    ChargingSession row by the caller) must restore the session's real rate and
    started_at so the NEXT frame streams DB-accurate cost/duration."""
    monkeypatch.setattr(telemetry_mod, "COINS_PER_KWH", 5.0)
    store = TelemetryStore()

    # First frame after restart: no start_session ran this process, so cost is
    # computed at the env default and the timer starts "now".
    store.update(plug_id=9, power_w=1000.0, current_a=4.0, energy_kwh=2.0,
                 status="charging")
    assert store.get_latest(9).cost_coins == 2.0 * 5.0  # env default, wrong rate

    # Caller hydrates from the row it just loaded: real rate 8.00/kWh, session
    # began 100s ago.
    started_at = datetime.now(timezone.utc) - timedelta(seconds=100)
    store.hydrate_session(9, rate_coins_per_kwh=Decimal("8.00"),
                          settled_cost_coins=Decimal("0"), rate_segment_start_kwh=0.0,
                          started_at=started_at)

    # Next frame now bills at the DB rate and the elapsed timer reflects
    # started_at, not the post-restart frame.
    store.update(plug_id=9, power_w=1000.0, current_a=4.0, energy_kwh=2.0,
                 status="charging")
    snap = store.get_latest(9)
    assert snap.cost_coins == 2.0 * 8.0  # DB rate, not the env default
    assert snap.duration_sec >= 100


def test_hydrate_session_no_op_when_already_tracked():
    """A session started in-process is already in the maps; hydrate_session must
    not clobber its live segment/rate state with the row snapshot."""
    store = TelemetryStore()

    store.start_session(10, rate_coins_per_kwh=Decimal("5.00"))
    store.set_segment_state(10, settled_coins=Decimal("10.00"),
                            segment_start_kwh=2.0, rate_coins_per_kwh=Decimal("8.00"))
    # A late hydrate with stale row values must be ignored.
    store.hydrate_session(10, rate_coins_per_kwh=Decimal("5.00"),
                          settled_cost_coins=Decimal("0"), rate_segment_start_kwh=0.0)

    store.update(plug_id=10, power_w=1000.0, current_a=4.0, energy_kwh=5.0,
                 status="charging")
    snap = store.get_latest(10)
    assert snap.cost_coins == 10.0 + (5.0 - 2.0) * 8.0  # live segment state preserved
