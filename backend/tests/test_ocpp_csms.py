"""
[C2] OCPP 1.6J central system, DB-free tests (run everywhere).

- Flag off: importing backend.main imports neither routers/ocpp.py nor
  services/ocpp_adapter.py (checked in a fresh interpreter, since this file
  itself imports both). Flag on: the WebSocket route is mounted through the
  Socket.io wrapper.
- OCPP-J framing: CALLERROR codes for malformed frames, unknown actions and
  invalid payloads, and a validation error never echoes the idTag.
- The WebSocket layer, driven by a Starlette TestClient simulator against a
  small app that includes only the OCPP router: subprotocol required, HTTP
  Basic required (wrong password, wrong username, no header), handshake and
  connection caps, boot accepted, authorize accepted and blocked, oversized
  frame closed with 1009, message rate limit closed with 1008.
- begin_active_session and finalize_charging_session with actuate=False
  publish no MQTT command.

The full transaction that bills through the ledger, the admission gates and
the per-charge-point scoping are DB-gated (test_ocpp_csms_db.py).
"""
import base64
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.routers import ocpp as ocpp_router
from backend.services import ocpp_adapter as adapter
from backend.services.rate_limit import SlidingWindowRateLimiter

REPO_ROOT = Path(__file__).resolve().parents[2]
CP = adapter.ChargePointCtx(id=1, charge_point_id="CP1", tenant_id=1, gateway_id="ocpp-CP1")
PASSWORD = "correct-horse-battery"


def _basic(user: str, password: str) -> dict:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _run_import(flag: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "OCPP_CSMS_ENABLED"}
    if flag:
        env["OCPP_CSMS_ENABLED"] = flag
    code = (
        "import json, sys\n"
        "from backend.main import app\n"
        "out = {m: m in sys.modules for m in "
        "('backend.routers.ocpp', 'backend.services.ocpp_adapter')}\n"
        "from starlette.testclient import TestClient\n"
        "from starlette.testclient import WebSocketDenialResponse\n"
        "try:\n"
        "    with TestClient(app).websocket_connect('/ocpp/CP1'):\n"
        "        out['ws'] = 'accepted'\n"
        "except WebSocketDenialResponse as exc:\n"
        "    out['ws'] = exc.status_code\n"
        "except Exception as exc:\n"
        "    out['ws'] = type(exc).__name__\n"
        "print('RESULT' + json.dumps(out))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=str(REPO_ROOT), env=env,
        capture_output=True, text=True, timeout=120,
    )
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT")]
    assert line, proc.stderr[-2000:]
    return json.loads(line[0][len("RESULT"):])


def test_flag_off_imports_nothing_and_flag_on_mounts_the_route():
    off = _run_import("")
    assert off["backend.routers.ocpp"] is False
    assert off["backend.services.ocpp_adapter"] is False
    assert off["ws"] != 400  # no OCPP route answered

    on = _run_import("1")
    assert on["backend.routers.ocpp"] is True
    assert on["backend.services.ocpp_adapter"] is True
    assert on["ws"] == 400  # the OCPP route refused the missing subprotocol


# --- framing --------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("raw,code", [
    ("not json", "FormationViolation"),
    ('{"a": 1}', "FormationViolation"),
    ('[2, "m1", "Heartbeat"]', "FormationViolation"),
    ('[2, 5, "Heartbeat", {}]', "FormationViolation"),
    ('[2, "m1", "RemoteStartTransaction", {}]', "NotImplemented"),
    ('[2, "m1", "Authorize", {"idTag": "TOO-LONG-TAG-0123456789"}]', "FormationViolation"),
    ("[" * 8000, "FormationViolation"),  # RecursionError in json.loads, under the size cap
])
async def test_dispatch_errors(raw, code):
    reply = json.loads(await adapter.dispatch(CP, raw))
    assert reply[0] == adapter.CALLERROR
    assert reply[2] == code
    assert "TOO-LONG-TAG" not in json.dumps(reply)


@pytest.mark.asyncio
async def test_callresult_from_the_charge_point_gets_no_reply():
    assert await adapter.dispatch(CP, '[3, "x", {}]') is None


def test_latest_reading_units_and_phases():
    mv = adapter.MeterValuesReq.model_validate({
        "connectorId": 1, "transactionId": 5,
        "meterValue": [
            {"timestamp": "t1", "sampledValue": [{"value": "1000"}]},
            {"timestamp": "t2", "sampledValue": [
                {"value": "2.5", "unit": "kWh"},
                {"value": "7.2", "measurand": "Power.Active.Import", "unit": "kW"},
                {"value": "99", "measurand": "Current.Import", "phase": "L1"},
                {"value": "nan", "measurand": "Voltage"},
            ]},
        ],
    }).meterValue
    reading = adapter.latest_reading(mv)
    assert reading == {"energy_wh": 2500.0, "power_w": 7200.0, "current_a": None, "voltage_v": None}


def test_id_tag_hash_is_case_insensitive_and_not_the_tag():
    assert adapter.hash_id_tag("abc123") == adapter.hash_id_tag("ABC123")
    assert "ABC123" not in adapter.hash_id_tag("ABC123")


def test_plausible_session_kwh_bounds_energy_by_elapsed_time():
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
    hour_ago = now - timedelta(hours=1)
    # An honest reading well under the bound passes through unchanged.
    assert adapter.plausible_session_kwh(11000, 1000, None, hour_ago, now) == (10.0, False)
    # 999 kWh one minute in is billed at what the connector could deliver.
    kwh, clamped = adapter.plausible_session_kwh(999_000, 0, None, now - timedelta(minutes=1), now)
    assert clamped and kwh == pytest.approx(adapter.MAX_CONNECTOR_KW / 60 + 0.1)
    # A register that went backwards keeps the last value fed (no reset banking).
    assert adapter.plausible_session_kwh(2000, 1000, 5.0, hour_ago, now) == (5.0, False)
    # Below meterStart is zero, and a naive started_at is read as UTC.
    assert adapter.plausible_session_kwh(500, 1000, None, hour_ago.replace(tzinfo=None), now) == (0.0, False)


# --- WebSocket layer --------------------------------------------------------

class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDb:
    """Answers db.execute() with the queued values, in order."""

    def __init__(self, values):
        self._values = list(values)
        self.commit = AsyncMock()

    async def execute(self, *_a, **_k):
        return _FakeResult(self._values.pop(0) if self._values else None)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_keep_alive_runs_one_bounded_update_per_interval(monkeypatch):
    dbs = []

    def factory():
        dbs.append(_FakeDb([]))
        return dbs[-1]

    monkeypatch.setattr(adapter, "_session_factory", factory)
    monkeypatch.setattr(adapter, "_last_keepalive", {})
    other = adapter.ChargePointCtx(id=2, charge_point_id="CP2", tenant_id=2, gateway_id="ocpp-CP2")
    await adapter.keep_sessions_alive(CP)
    await adapter.keep_sessions_alive(CP)        # throttled
    await adapter.keep_sessions_alive(other)     # its own throttle
    assert len(dbs) == 2
    assert all(db.commit.await_count == 1 for db in dbs)
    # Any handled CALL keeps the sessions alive; a refused frame does not.
    spy = AsyncMock()
    monkeypatch.setattr(adapter, "keep_sessions_alive", spy)
    await adapter.dispatch(CP, '[2, "m1", "Heartbeat", {}]')
    await adapter.dispatch(CP, '[2, "m2", "Nope", {}]')
    spy.assert_awaited_once_with(CP)


@pytest.fixture
def client(monkeypatch):
    async def fake_auth(cp_id, password):
        return CP if (cp_id, password) == ("CP1", PASSWORD) else None

    monkeypatch.setattr(adapter, "authenticate", fake_auth)
    # The keep-alive UPDATE has its own test; keep it off the fake DB queue.
    monkeypatch.setattr(adapter, "keep_sessions_alive", AsyncMock())
    monkeypatch.setattr(ocpp_router, "handshake_limiter", SlidingWindowRateLimiter(100, 60))
    monkeypatch.setattr(ocpp_router, "connections", {})
    app = FastAPI()
    app.include_router(ocpp_router.router)
    return TestClient(app)


def _connect(client, headers=None, subprotocols=("ocpp1.6",)):
    return client.websocket_connect(
        "/ocpp/CP1", subprotocols=list(subprotocols),
        headers=headers if headers is not None else _basic("CP1", PASSWORD),
    )


def _call(ws, action, payload, uid="m1"):
    ws.send_text(json.dumps([2, uid, action, payload]))
    return json.loads(ws.receive_text())


@pytest.mark.parametrize("subprotocols", [(), ("ocpp2.0.1",)])
def test_missing_or_wrong_subprotocol_is_refused(client, subprotocols):
    from starlette.testclient import WebSocketDenialResponse

    with pytest.raises(WebSocketDenialResponse) as exc:
        with _connect(client, subprotocols=subprotocols):
            pass
    assert exc.value.status_code == 400


def test_connection_cap_503(client, monkeypatch):
    from starlette.testclient import WebSocketDenialResponse

    monkeypatch.setattr(ocpp_router, "MAX_CONNECTIONS", 0)
    with pytest.raises(WebSocketDenialResponse) as exc:
        with _connect(client):
            pass
    assert exc.value.status_code == 503


@pytest.mark.parametrize("headers", [
    _basic("CP1", "wrong-password"),
    _basic("CP2", PASSWORD),          # username must equal the path id
    {},                               # no credentials at all
    {"Authorization": "Basic !!!"},   # not base64
])
def test_bad_credentials_get_401(client, headers):
    from starlette.testclient import WebSocketDenialResponse

    with pytest.raises(WebSocketDenialResponse) as exc:
        with _connect(client, headers=headers):
            pass
    assert exc.value.status_code == 401


def test_boot_heartbeat_and_authorize(client, monkeypatch):
    import backend.services.wallet as wallet

    dbs = iter([
        _FakeDb([]),                                                  # boot update
        _FakeDb([7, SimpleNamespace(is_disabled=False, deleted_at=None)]),  # accepted
        _FakeDb([8, SimpleNamespace(is_disabled=False, deleted_at=None)]),  # low balance
        _FakeDb([None]),                                              # unknown tag
    ])
    monkeypatch.setattr(adapter, "_session_factory", lambda: next(dbs))
    balances = {7: 500, 8: 1}
    monkeypatch.setattr(wallet, "available_balance", AsyncMock(side_effect=lambda _db, uid: balances[uid]))

    with _connect(client) as ws:
        assert ws.accepted_subprotocol == "ocpp1.6"
        boot = _call(ws, "BootNotification", {"chargePointVendor": "Sim", "chargePointModel": "S1"})
        assert boot[0] == adapter.CALLRESULT and boot[1] == "m1"
        assert boot[2]["status"] == "Accepted"
        assert boot[2]["interval"] == adapter.HEARTBEAT_INTERVAL_SEC
        assert "currentTime" in _call(ws, "Heartbeat", {}, uid="m2")[2]
        assert _call(ws, "Authorize", {"idTag": "TAG-OK"})[2] == {"idTagInfo": {"status": "Accepted"}}
        assert _call(ws, "Authorize", {"idTag": "TAG-POOR"})[2] == {"idTagInfo": {"status": "Blocked"}}
        assert _call(ws, "Authorize", {"idTag": "TAG-NONE"})[2] == {"idTagInfo": {"status": "Invalid"}}


def test_oversized_message_closes_1009(client, monkeypatch):
    monkeypatch.setattr(ocpp_router, "MAX_MESSAGE_BYTES", 200)
    with _connect(client) as ws:
        ws.send_text(json.dumps([2, "m1", "Heartbeat", {"pad": "x" * 500}]))
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_text()
    assert exc.value.code == 1009


def test_message_rate_limit_closes_1008(client, monkeypatch):
    monkeypatch.setattr(ocpp_router, "_MESSAGE_RULE", (2, 60.0))
    with _connect(client) as ws:
        _call(ws, "Heartbeat", {}, uid="a")
        _call(ws, "Heartbeat", {}, uid="b")
        ws.send_text(json.dumps([2, "c", "Heartbeat", {}]))
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_text()
    assert exc.value.code == 1008


def test_handshake_limit_429(client, monkeypatch):
    from starlette.testclient import WebSocketDenialResponse

    monkeypatch.setattr(ocpp_router, "handshake_limiter", SlidingWindowRateLimiter(1, 60))
    with _connect(client):
        pass
    with pytest.raises(WebSocketDenialResponse) as exc:
        with _connect(client):
            pass
    assert exc.value.status_code == 429


def test_start_refused_for_unknown_connector_publishes_nothing(client, monkeypatch):
    """An unknown connector is Invalid before any admission runs; no MQTT."""
    from backend import state

    mgr = MagicMock()
    monkeypatch.setattr(state, "mqtt_manager", mgr)
    monkeypatch.setattr(adapter, "_session_factory", lambda: _FakeDb([None]))
    with _connect(client) as ws:
        reply = _call(ws, "StartTransaction", {
            "connectorId": 1, "idTag": "TAG-OK", "meterStart": 0, "timestamp": "t",
        })
    assert reply[2] == {"transactionId": 0, "idTagInfo": {"status": "Invalid"}}
    mgr.send_plug_command.assert_not_called()


# --- the shared start and finalize helpers publish nothing with actuate=False

@pytest.mark.asyncio
async def test_begin_active_session_without_actuation_publishes_nothing():
    from decimal import Decimal
    from unittest.mock import patch

    from backend.services import session_start

    db = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    user = MagicMock(id=3, coin_balance=Decimal("500"))
    plug = MagicMock(id=7, gateway_id="ocpp-CP1", local_ip="ocpp",
                     opening_hours=None, max_session_minutes=None)
    plug.name = "CP1 #1"
    mqtt = MagicMock()
    interval = AsyncMock()
    store = MagicMock()
    with patch.object(session_start, "check_circuit_admission", AsyncMock()), \
         patch.object(session_start, "resolve_rate_window",
                      AsyncMock(return_value=(Decimal("5.00"), None))), \
         patch.object(session_start, "available_balance", AsyncMock(return_value=Decimal("500"))), \
         patch.object(session_start, "max_rate_over_window",
                      AsyncMock(return_value=Decimal("5.00"))), \
         patch.object(session_start, "set_plug_telemetry_interval", interval), \
         patch.object(session_start.state, "mqtt_manager", mqtt), \
         patch.object(session_start.state, "telemetry_store", store), \
         patch("backend.services.socketio_manager.emit_plug_status", AsyncMock()), \
         patch("backend.services.notifications.notify", AsyncMock()):
        await session_start.begin_active_session(
            db, user, plug, MagicMock(tenant_id=1), max_kwh=None,
            max_duration_seconds=None, actuate=False,
        )
    mqtt.send_plug_command.assert_not_called()
    interval.assert_not_awaited()
    store.start_session.assert_called_once()
    db.add.assert_called_once()


@pytest.mark.asyncio
async def test_finalize_without_actuation_bills_but_publishes_nothing(monkeypatch):
    import contextlib
    from decimal import Decimal
    from unittest.mock import patch

    import backend.services.session_lifecycle as sl_mod
    from backend import state as state_module
    from backend.tests.test_session_lifecycle import (
        _active_session,
        _plug,
        _s_one,
        _s_one_or_none,
        _seq_db,
    )

    db = _seq_db(_s_one_or_none(MagicMock()), _s_one_or_none(_active_session()), _s_one(_plug()))
    mqtt = MagicMock()
    interval = AsyncMock()
    monkeypatch.setattr(state_module, "mqtt_manager", mqtt)
    monkeypatch.setattr(state_module, "telemetry_store", MagicMock())
    monkeypatch.setattr(sl_mod, "set_plug_telemetry_interval", interval)
    monkeypatch.setattr(
        sl_mod, "debit_wallet_clamped",
        AsyncMock(return_value=(Decimal("10.00"), Decimal("90.00"))),
    )
    with contextlib.ExitStack() as stack:
        for target, mock in [
            ("backend.services.socketio_manager.emit_plug_status", AsyncMock()),
            ("backend.services.notifications.notify", AsyncMock()),
            ("backend.services.plug_watch.notify_watchers_plug_available", AsyncMock(return_value=0)),
            ("backend.services.capacity.notify_capacity_available", AsyncMock()),
            ("backend.services.billing_emails.send_session_bill", MagicMock(return_value=None)),
            ("backend.services.billing_emails.schedule", MagicMock()),
        ]:
            stack.enter_context(patch(target, mock))
        receipt = await sl_mod.finalize_charging_session(db, 99, reason="ocpp: Local", actuate=False)

    assert receipt["coins_spent"] == 10.0
    mqtt.send_plug_command.assert_not_called()
    interval.assert_not_awaited()
