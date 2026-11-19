"""Pins the F7 meter contract to what the OCPP bridge (tools/ocpp_bridge)
consumes: entries built by the REAL meter_plug, JSON round-tripped, then fed to
the bridge. It pins the contract; it does not re-test the bridge."""
import asyncio
import importlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from backend.database.models import GatewayStatus, PlugStatus
from backend.routers.tokens import meter_plug
from backend.services.telemetry import TelemetrySnapshot

sys.path.append(str(Path(__file__).resolve().parents[2] / "tools"))  # last, so tools/ never shadows a real module
bridge = importlib.import_module("ocpp_bridge.bridge")

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)


def _plug(plug_id, status):
    return SimpleNamespace(id=plug_id, name=f"Bay {plug_id}", status=status)


def _gateway(seen_ago=5):
    return SimpleNamespace(status=GatewayStatus.ONLINE, last_seen_at=NOW - timedelta(seconds=seen_ago))


def _snap(plug_id, age):
    return TelemetrySnapshot(plug_id=plug_id, power_w=7100.5, current_a=31.2, voltage_v=229.8,
                             status="charging", updated_at=NOW.timestamp() - age)


def _body():
    session = SimpleNamespace(id=80, started_at=NOW - timedelta(hours=1), energy_kwh=1.838)
    entries = [
        meter_plug(_plug(1, PlugStatus.AVAILABLE), _gateway(), None, None, NOW),
        meter_plug(_plug(2, PlugStatus.OCCUPIED), _gateway(), session, _snap(2, 3), NOW),
        meter_plug(_plug(3, PlugStatus.OCCUPIED), _gateway(seen_ago=9999), None, _snap(3, 3600), NOW),
    ]
    body = {"generated_at": NOW.isoformat(), "plugs": entries}
    return json.loads(json.dumps(body))  # the wire form: no default= allowed


class _Cfg:
    id_tag = "AMPHIVE"
    poll_seconds = 10.0


def test_meter_entries_feed_the_bridge():
    idle, charging, stale = _body()["plugs"]
    assert [bridge.ocpp_status(p) for p in (idle, charging, stale)] == [
        "Available", "Charging", "Unavailable"]
    assert bridge.utc_iso(charging["reading_at"]) == "2026-11-18T11:59:57.000Z"
    assert bridge.utc_iso(charging["session"]["started_at"]) == "2026-11-18T11:00:00.000Z"
    assert bridge.utc_iso(stale["reading_at"]) == "2026-11-18T11:00:00.000Z"  # kept though stale
    assert stale["power_w"] is None and idle["reading_at"] is None


def test_charge_point_sync_sends_the_contract_values(tmp_path):
    sent: list[tuple[str, dict]] = []

    async def scenario(entry):
        cp = bridge.ChargePoint("AMPHIVE-2", entry["plug_id"], _Cfg(),
                                bridge.StateStore(tmp_path / f"s{entry['plug_id']}.json"))

        async def fake_call(action, payload):
            sent.append((action, payload))
            return {"transactionId": 9, "idTagInfo": {"status": "Accepted"}}

        cp.call = fake_call
        cp.snapshot = entry
        await cp._sync()

    idle, charging, stale = _body()["plugs"]
    asyncio.run(scenario(charging))
    start = next(p for a, p in sent if a == "StartTransaction")
    assert start["meterStart"] == 0 and isinstance(start["meterStart"], int)
    assert start["timestamp"] == "2026-11-18T11:00:00.000Z"
    mv = next(p for a, p in sent if a == "MeterValues")["meterValue"][0]
    assert mv["timestamp"] == "2026-11-18T11:59:57.000Z"
    values = {s["measurand"]: s["value"] for s in mv["sampledValue"]}
    assert values == {"Energy.Active.Import.Register": str(round(1.838 * 1000)),
                      "Power.Active.Import": "7100.50", "Current.Import": "31.20",
                      "Voltage": "229.80"}

    sent.clear()  # a session whose reading is null: the register stays, power/current/voltage go
    asyncio.run(scenario({**charging, "power_w": None, "current_a": None, "voltage_v": None,
                          "reading_at": None, "plug_id": 4}))
    mv = next(p for a, p in sent if a == "MeterValues")["meterValue"][0]
    assert [s["measurand"] for s in mv["sampledValue"]] == ["Energy.Active.Import.Register"]
    assert mv["timestamp"].endswith("Z")
