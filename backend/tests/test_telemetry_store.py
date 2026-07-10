"""
TelemetryStore behavior: relay/voltage passthrough and the `is_stale` flag the
live stream now emits so the frontend can warn a driver when the gateway link
drops instead of freezing on stale values.
"""
import asyncio
import time

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
