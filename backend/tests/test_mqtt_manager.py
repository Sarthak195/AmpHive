"""
Tests for MQTTManager inbound handling.

Run:
    pip install -r backend/requirements.txt -r backend/requirements-dev.txt
    pytest backend/tests/test_mqtt_manager.py

DB-free: exercises the paho-thread -> event-loop marshaling of TelemetryStore
updates. asyncio.Event.set() (which TelemetryStore.update() calls to wake
stream() consumers) is not thread-safe, so the update must be scheduled onto
the loop via call_soon_threadsafe rather than run inline on the paho network
thread.
"""

import asyncio
import json
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services.mqtt_manager import MQTTManager


class _FakeResult:
    def __init__(self, val):
        self._val = val

    def scalar_one_or_none(self):
        return self._val


class _FakeDB:
    """Minimal async-context DB whose execute() always yields the same row."""
    def __init__(self, row):
        self._row = row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, *_a, **_k):
        return _FakeResult(self._row)


@pytest.mark.asyncio
async def test_telemetry_update_marshaled_onto_loop_not_paho_thread():
    """
    When an event loop is available, _handle_gateway_telemetry (called on the
    paho network thread) must NOT invoke telemetry_store.update() inline — it
    must schedule it on the loop. We prove this by calling the handler from a
    worker thread and asserting update() has not run when the handler returns,
    then that it runs (on the loop thread) once the loop gets a turn.
    """
    calls = []  # thread name for each update() invocation

    store = MagicMock()
    store.update.side_effect = lambda *a, **k: calls.append(threading.current_thread().name)

    MQTTManager._instance = None
    loop = asyncio.get_running_loop()
    mgr = MQTTManager(telemetry_store=store, event_loop=loop)

    worker_name = "paho-sim"
    handler_returned = threading.Event()

    def paho_thread():
        mgr._handle_gateway_telemetry("gw-1", {
            "plug_id": 1, "watts": 10.0, "kwh": 0.1,
            "voltage": 230.0, "current": 0.04, "status": "occupied",
        })
        handler_returned.set()

    t = threading.Thread(target=paho_thread, name=worker_name)
    t.start()
    handler_returned.wait(2.0)
    t.join(2.0)

    # The loop hasn't had a turn yet (this coroutine has been running), so the
    # scheduled callback can't have fired. update() must not have run inline.
    assert calls == [], "update() ran on the paho thread instead of being marshaled onto the loop"

    # Give the loop a turn so it processes the scheduled callback.
    await asyncio.sleep(0.05)

    assert len(calls) == 1, "scheduled telemetry update did not run on the loop"
    assert worker_name not in calls, "update() must run on the loop thread, not the paho thread"
    # Positional args: (plug_id, power_w, current_a, energy_kwh, status); voltage
    # and relay state (firmware ≥ 1.5.0) ride along as keywords. status
    # "occupied" → relay defaults on, voltage echoes the payload.
    store.update.assert_called_once_with(
        1, 10.0, 0.04, 0.1, "charging", voltage_v=230.0, relay_on=True
    )

    MQTTManager._instance = None


@pytest.mark.asyncio
@pytest.mark.parametrize("reported_sid,expected", [
    ("77", 77),         # normal: firmware echoes the backend session id
    (None, None),       # key absent (pre-session_id firmware)
    ("", None),         # idle firmware reports an empty string
    ("not-a-number", None),
    ("0", None),        # 0 is not a valid session id
])
async def test_session_id_parsed_and_forwarded_to_persist(reported_sid, expected):
    """
    _handle_gateway_telemetry must parse the firmware-echoed session_id (a JSON
    string) into an int and forward it to _persist_telemetry, so the reading is
    attributed to the exact session. Bad/empty/absent values forward None (the
    handler then falls back to the plug's active session).
    """
    MQTTManager._instance = None
    loop = asyncio.get_running_loop()
    # db_session_factory just needs to be truthy — _persist_telemetry is stubbed.
    mgr = MQTTManager(db_session_factory=lambda: None, event_loop=loop)

    captured = {}

    async def fake_persist(gateway_id, plug_id, watts, kwh, session_id=None,
                           sample=None, relay_on=False, is_offline=False,
                           today_kwh=None, month_kwh=None):
        captured.update(gateway_id=gateway_id, plug_id=plug_id, watts=watts,
                        kwh=kwh, session_id=session_id, is_offline=is_offline)

    mgr._persist_telemetry = fake_persist

    payload = {"plug_id": 3, "watts": 100.0, "kwh": 0.5, "status": "occupied"}
    if reported_sid is not None:
        payload["session_id"] = reported_sid

    mgr._handle_gateway_telemetry("gw-1", payload)
    await asyncio.sleep(0.05)  # let the scheduled coroutine run

    assert captured.get("gateway_id") == "gw-1"
    assert captured.get("plug_id") == 3
    assert captured.get("kwh") == 0.5
    assert captured.get("session_id") == expected
    assert captured.get("is_offline") is False  # live frame (no offline flag)

    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_handler_forwards_is_offline_for_resync_frame():
    """[TD#24] A resync frame carries offline:true; the handler must forward
    is_offline so _persist_telemetry can treat it as historical."""
    MQTTManager._instance = None
    loop = asyncio.get_running_loop()
    mgr = MQTTManager(db_session_factory=lambda: None, event_loop=loop)
    captured = {}

    async def fake_persist(gateway_id, plug_id, watts, kwh, session_id=None,
                           sample=None, relay_on=False, is_offline=False,
                           today_kwh=None, month_kwh=None):
        captured.update(session_id=session_id, is_offline=is_offline)

    mgr._persist_telemetry = fake_persist
    mgr._handle_gateway_telemetry("gw-1", {
        "plug_id": 3, "watts": 50.0, "kwh": 0.2, "status": "occupied",
        "session_id": "42", "relay": True, "offline": True,
    })
    await asyncio.sleep(0.05)

    assert captured.get("session_id") == 42
    assert captured.get("is_offline") is True
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_telemetry_update_direct_when_no_loop():
    """
    Without an event_loop reference (e.g. unit-test construction), the handler
    falls back to a direct update() call so behavior is still exercised.
    """
    store = MagicMock()

    MQTTManager._instance = None
    mgr = MQTTManager(telemetry_store=store)  # no event_loop

    mgr._handle_gateway_telemetry("gw-1", {
        "plug_id": 2, "watts": 20.0, "kwh": 0.2,
        "voltage": 230.0, "current": 0.087, "status": "available",
    })

    store.update.assert_called_once_with(
        plug_id=2, power_w=20.0, current_a=0.087, energy_kwh=0.2, status="idle",
        voltage_v=230.0, relay_on=False,
    )

    MQTTManager._instance = None


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,exp_type,exp_sev,exp_plug", [
    ({"error": "UNAUTHORIZED_ON", "plug_id": 5}, "UNAUTHORIZED_ON", "critical", 5),
    ({"error": "THERMAL_CUTOFF", "plug_id": 1}, "THERMAL_CUTOFF", "critical", 1),
    ({"error": "OVERCURRENT_CUTOFF"}, "OVERCURRENT_CUTOFF", "critical", None),
    ({"error": "OVERCURRENT_CAP", "plug_id": 5}, "OVERCURRENT_CAP", "warning", 5),  # soft cap trip
    # Software-agent local watchdog trip — expected end-of-session, severity info.
    ({"event": "LOCAL_LIMIT_CUTOFF", "reason": "ENERGY_LIMIT", "plug_id": 7},
     "LOCAL_LIMIT_CUTOFF", "info", 7),
    ({"event": "OTA_STARTED"}, "OTA_STARTED", "info", None),
    ({"event": "OTA_FAILED"}, "OTA_FAILED", "warning", None),
    ({"error": "SOMETHING_NEW"}, "SOMETHING_NEW", "warning", None),  # unknown → warning
])
async def test_alarm_parsed_and_persisted(payload, exp_type, exp_sev, exp_plug):
    """
    _handle_gateway_alarm must parse both the {"error":..} and {"event":..}
    shapes, map the type to a severity, extract plug_id when present, and forward
    to _persist_gateway_event. Unknown types default to "warning" (never dropped).
    """
    MQTTManager._instance = None
    loop = asyncio.get_running_loop()
    mgr = MQTTManager(db_session_factory=lambda: None, event_loop=loop)

    captured = {}

    async def fake_persist(gateway_id, plug_id, event_type, severity, detail):
        captured.update(gateway_id=gateway_id, plug_id=plug_id,
                        event_type=event_type, severity=severity, detail=detail)

    mgr._persist_gateway_event = fake_persist

    mgr._handle_gateway_alarm("gw-9", payload)
    await asyncio.sleep(0.05)

    assert captured.get("gateway_id") == "gw-9"
    assert captured.get("event_type") == exp_type
    assert captured.get("severity") == exp_sev
    assert captured.get("plug_id") == exp_plug

    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_alarm_missing_type_ignored():
    """A malformed alarm (no error/event key) is logged and dropped, not crashed."""
    MQTTManager._instance = None
    loop = asyncio.get_running_loop()
    mgr = MQTTManager(db_session_factory=lambda: None, event_loop=loop)

    called = {"n": 0}

    async def fake_persist(*a, **k):
        called["n"] += 1

    mgr._persist_gateway_event = fake_persist
    mgr._handle_gateway_alarm("gw-9", {"plug_id": 1})  # no error/event
    await asyncio.sleep(0.05)

    assert called["n"] == 0
    MQTTManager._instance = None


# ---------------------------------------------------------------------------
# Auto-maintenance on a critical SAFETY alarm (fault console)
# ---------------------------------------------------------------------------


class _AlarmDB:
    """
    Minimal fake session for driving _persist_gateway_event directly (the
    home of the safety-cutoff decisions since the driver-notifications merge
    — see test_notifications.py's _SeqDB for the sibling pattern that tests
    _finalize_session_after_cutoff itself). The gateway lookup always
    succeeds; add()/commit() are no-ops (add() also records the row so a test
    can inspect the persisted GatewayEvent) so the function runs past the
    persist step to reach the finalize/auto-maintenance sequencing below it.

    The SAME fake row answers both queries: the gateway lookup reads
    `.tenant_id` and the plug-ownership lookup reads `.gateway_id`. `.gateway_id`
    is preset to `owner_gateway_id` (default "gw-9", the id the alarm tests
    publish under) so a legit alarm's own plug passes the ownership check; point
    it at a DIFFERENT gateway to simulate a spoofed/foreign plug_id.
    """
    def __init__(self, owner_gateway_id="gw-9", tenant_id=1):
        self.committed = False
        self.added = []
        self._owner_gateway_id = owner_gateway_id
        self._tenant_id = tenant_id

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, *_a, **_k):
        r = MagicMock()
        row = MagicMock()
        row.tenant_id = self._tenant_id
        row.gateway_id = self._owner_gateway_id
        r.scalar_one_or_none.return_value = row
        return r

    def add(self, row):
        row.id = 1
        row.created_at = None
        self.added.append(row)

    async def commit(self):
        self.committed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type,plug_id,should_trigger", [
    ("THERMAL_CUTOFF", 5, True),
    ("OVERCURRENT_CUTOFF", 5, True),
    ("OVERCURRENT_CAP", 5, False),   # soft cap trip on a healthy plug — finalize but NOT maintenance
    ("LOCAL_LIMIT_CUTOFF", 5, False),  # agent limit trip on a healthy plug — finalize but NOT maintenance
    ("UNAUTHORIZED_ON", 5, False),   # accountability signal, not a hardware fault
    ("OTA_FAILED", 5, False),        # OTA lifecycle notices never trigger it
    ("OTA_STARTED", 5, False),
    ("THERMAL_CUTOFF", None, False),  # no plug_id resolved — nothing to act on
])
async def test_auto_maintenance_trigger_by_event_type(event_type, plug_id, should_trigger):
    """
    Only THERMAL_CUTOFF/OVERCURRENT_CUTOFF with a resolved plug_id call
    _auto_enter_maintenance — from _persist_gateway_event (sequenced after
    the safety-cutoff finalize; see _handle_gateway_alarm/_persist_gateway_event
    for why this can't be an independently-scheduled task). UNAUTHORIZED_ON,
    OTA_* events, and a missing plug_id must never trigger it.
    """
    MQTTManager._instance = None
    db = _AlarmDB()
    mgr = MQTTManager(db_session_factory=lambda: db)
    mgr._finalize_session_after_cutoff = AsyncMock()
    auto_maint_mock = AsyncMock()
    mgr._auto_enter_maintenance = auto_maint_mock

    with patch("backend.services.socketio_manager.emit_gateway_alarm", AsyncMock()):
        await mgr._persist_gateway_event("gw-9", plug_id, event_type, "critical", None)

    assert auto_maint_mock.called is should_trigger
    if should_trigger:
        auto_maint_mock.assert_awaited_once_with(plug_id, event_type)

    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_auto_maintenance_disabled_by_flag(monkeypatch):
    """With AUTO_MAINTENANCE_ON_CRITICAL_ALARM off, a THERMAL_CUTOFF with a
    valid plug_id must NOT call _auto_enter_maintenance."""
    import backend.services.mqtt_manager as mm
    monkeypatch.setattr(mm, "AUTO_MAINTENANCE_ON_CRITICAL_ALARM", False)

    MQTTManager._instance = None
    db = _AlarmDB()
    mgr = MQTTManager(db_session_factory=lambda: db)
    mgr._finalize_session_after_cutoff = AsyncMock()
    auto_maint_mock = AsyncMock()
    mgr._auto_enter_maintenance = auto_maint_mock

    with patch("backend.services.socketio_manager.emit_gateway_alarm", AsyncMock()):
        await mgr._persist_gateway_event("gw-9", 5, "THERMAL_CUTOFF", "critical", None)

    auto_maint_mock.assert_not_called()
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_finalize_runs_before_auto_maintenance():
    """
    Regression test for the ordering fix: auto-maintenance must be sequenced
    strictly AFTER the safety-cutoff finalize, in the same coroutine — NOT
    raced as an independently-scheduled task. finalize_charging_session sets
    the plug back to PlugStatus.AVAILABLE; if auto-maintenance ran first (or
    concurrently), that AVAILABLE write could land after the MAINTENANCE
    flip and silently undo it.
    """
    MQTTManager._instance = None
    db = _AlarmDB()
    mgr = MQTTManager(db_session_factory=lambda: db)

    order = []

    async def fake_finalize(plug_id, event_type):
        order.append("finalize")

    async def fake_auto_maint(plug_id, event_type):
        order.append("auto_maintenance")

    mgr._finalize_session_after_cutoff = fake_finalize
    mgr._auto_enter_maintenance = fake_auto_maint

    with patch("backend.services.socketio_manager.emit_gateway_alarm", AsyncMock()):
        await mgr._persist_gateway_event("gw-9", 5, "THERMAL_CUTOFF", "critical", None)

    assert order == ["finalize", "auto_maintenance"]
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_overcurrent_cap_finalizes_but_does_not_enter_maintenance():
    """OVERCURRENT_CAP is a soft/policy cap trip: the firmware already stopped
    charging, so the backend must FINALIZE the session (free the plug, bill,
    notify) — but the plug is healthy, so it must NOT be forced into MAINTENANCE.
    Locks the finalize/maintenance gate decoupling."""
    MQTTManager._instance = None
    db = _AlarmDB()
    mgr = MQTTManager(db_session_factory=lambda: db)
    finalize_mock = AsyncMock()
    auto_maint_mock = AsyncMock()
    mgr._finalize_session_after_cutoff = finalize_mock
    mgr._auto_enter_maintenance = auto_maint_mock

    with patch("backend.services.socketio_manager.emit_gateway_alarm", AsyncMock()):
        await mgr._persist_gateway_event("gw-9", 5, "OVERCURRENT_CAP", "warning", None)

    finalize_mock.assert_awaited_once_with(5, "OVERCURRENT_CAP")
    auto_maint_mock.assert_not_called()
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_overcurrent_cap_finalize_reason_string():
    """The OVERCURRENT_CAP finalize reason routes the driver notification via
    session_lifecycle — assert the exact reason string is passed to finalize."""
    MQTTManager._instance = None
    from unittest.mock import patch as _patch

    captured = {}

    async def _fake_finalize(db, session_id, reason=None):
        captured["reason"] = reason
        return {"ok": True}

    class _OneActive:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def execute(self, *_a, **_k):
            r = MagicMock()
            r.scalar_one_or_none.return_value = 42  # an ACTIVE session id
            return r

    mgr = MQTTManager(db_session_factory=lambda: _OneActive())
    with _patch("backend.services.session_lifecycle.finalize_charging_session", _fake_finalize):
        await mgr._finalize_session_after_cutoff(5, "OVERCURRENT_CAP")

    assert captured.get("reason") == "current cap exceeded: plug drew over its configured limit"
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_local_limit_cutoff_finalizes_but_does_not_enter_maintenance():
    """LOCAL_LIMIT_CUTOFF is the software agent's local kWh/duration watchdog
    trip: the agent already cut the plug OFF and cleared its local session, so
    the backend must FINALIZE the session (bill, free the plug, notify the
    driver) instead of orphaning it ACTIVE until the reaper — but the plug is
    healthy, so it must NOT be forced into MAINTENANCE. Mirrors the
    OVERCURRENT_CAP contract."""
    MQTTManager._instance = None
    db = _AlarmDB()
    mgr = MQTTManager(db_session_factory=lambda: db)
    finalize_mock = AsyncMock()
    auto_maint_mock = AsyncMock()
    mgr._finalize_session_after_cutoff = finalize_mock
    mgr._auto_enter_maintenance = auto_maint_mock

    with patch("backend.services.socketio_manager.emit_gateway_alarm", AsyncMock()):
        await mgr._persist_gateway_event("gw-9", 7, "LOCAL_LIMIT_CUTOFF", "info", None)

    finalize_mock.assert_awaited_once_with(7, "LOCAL_LIMIT_CUTOFF")
    auto_maint_mock.assert_not_called()
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_local_limit_cutoff_finalize_reason_string():
    """The LOCAL_LIMIT_CUTOFF finalize reason routes the driver notification via
    session_lifecycle — assert the exact reason string is passed to finalize."""
    MQTTManager._instance = None
    from unittest.mock import patch as _patch

    captured = {}

    async def _fake_finalize(db, session_id, reason=None):
        captured["reason"] = reason
        return {"ok": True}

    class _OneActive:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def execute(self, *_a, **_k):
            r = MagicMock()
            r.scalar_one_or_none.return_value = 42  # an ACTIVE session id
            return r

    mgr = MQTTManager(db_session_factory=lambda: _OneActive())
    with _patch("backend.services.session_lifecycle.finalize_charging_session", _fake_finalize):
        await mgr._finalize_session_after_cutoff(7, "LOCAL_LIMIT_CUTOFF")

    assert captured.get("reason") == "limit reached: session hit its energy/duration limit"
    MQTTManager._instance = None


# ---------------------------------------------------------------------------
# Cross-tenant alarm plug-ownership guard (payload plug_id spoofing)
# ---------------------------------------------------------------------------
#
# Broker ACLs scope *topics*, not payload claims: a gateway with valid creds
# for its OWN alarms topic could publish {"error":"THERMAL_CUTOFF","plug_id":N}
# naming another tenant's plug (plug ids are small sequential ints) and, without
# a check, force-finalize/maintenance/notify that victim plug. _persist_gateway_
# event must require the payload plug_id to belong to the publishing gateway
# (mirrors the telemetry ownership check above), nulling out a foreign id so the
# alarm degrades to a harmless gateway-level (plug_id=None) event.


@pytest.mark.asyncio
async def test_alarm_foreign_plug_id_dropped_no_finalize_maintenance_or_persist():
    """An alarm whose payload plug_id belongs to a DIFFERENT gateway must not
    finalize or force-maintenance the victim plug, and must not be persisted
    carrying the foreign plug_id (the spoofed id is nulled to None)."""
    MQTTManager._instance = None
    # plug 5 is owned by "gw-victim"; the attacker publishes under "gw-attacker".
    db = _AlarmDB(owner_gateway_id="gw-victim")
    mgr = MQTTManager(db_session_factory=lambda: db)
    finalize_mock = AsyncMock()
    auto_maint_mock = AsyncMock()
    mgr._finalize_session_after_cutoff = finalize_mock
    mgr._auto_enter_maintenance = auto_maint_mock

    with patch("backend.services.socketio_manager.emit_gateway_alarm", AsyncMock()):
        await mgr._persist_gateway_event("gw-attacker", 5, "THERMAL_CUTOFF", "critical", None)

    finalize_mock.assert_not_called()
    auto_maint_mock.assert_not_called()
    # An event is still recorded (audit of the attacker's own gateway), but the
    # foreign plug_id was nulled out — never recorded against it as if real.
    assert len(db.added) == 1
    assert db.added[0].plug_id is None
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_alarm_foreign_plug_unmetered_consumption_does_not_notify_cpos():
    """A spoofed UNMETERED_CONSUMPTION naming a victim plug must not bell-notify
    the attacker's CPOs about that plug — the plug-gated notify never fires once
    the foreign plug_id is nulled."""
    MQTTManager._instance = None
    db = _AlarmDB(owner_gateway_id="gw-victim")
    mgr = MQTTManager(db_session_factory=lambda: db)
    notify_mock = AsyncMock()
    mgr._notify_cpos_unmetered_consumption = notify_mock

    with patch("backend.services.socketio_manager.emit_gateway_alarm", AsyncMock()):
        await mgr._persist_gateway_event(
            "gw-attacker", 5, "UNMETERED_CONSUMPTION", "warning", "spoofed detail"
        )

    notify_mock.assert_not_called()
    assert db.added[0].plug_id is None
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_alarm_own_plug_finalize_and_maintenance_still_fire():
    """Regression guard for the ownership fix: a finalize-worthy alarm for one
    of the gateway's OWN plugs must still finalize AND (for a hardware cutoff)
    enter maintenance, and persist with the real plug_id — unchanged behaviour."""
    MQTTManager._instance = None
    db = _AlarmDB(owner_gateway_id="gw-9")  # plug 5 belongs to gw-9
    mgr = MQTTManager(db_session_factory=lambda: db)
    finalize_mock = AsyncMock()
    auto_maint_mock = AsyncMock()
    mgr._finalize_session_after_cutoff = finalize_mock
    mgr._auto_enter_maintenance = auto_maint_mock

    with patch("backend.services.socketio_manager.emit_gateway_alarm", AsyncMock()):
        await mgr._persist_gateway_event("gw-9", 5, "THERMAL_CUTOFF", "critical", None)

    finalize_mock.assert_awaited_once_with(5, "THERMAL_CUTOFF")
    auto_maint_mock.assert_awaited_once_with(5, "THERMAL_CUTOFF")
    assert db.added[0].plug_id == 5  # persisted with the real, owned plug_id
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_alarm_gateway_level_null_plug_still_persists_and_broadcasts():
    """A genuine gateway-level event (plug_id=None, e.g. an OTA notice) skips the
    ownership lookup entirely and still persists + broadcasts unchanged, with no
    plug-scoped action firing."""
    MQTTManager._instance = None
    db = _AlarmDB()
    mgr = MQTTManager(db_session_factory=lambda: db)
    finalize_mock = AsyncMock()
    auto_maint_mock = AsyncMock()
    mgr._finalize_session_after_cutoff = finalize_mock
    mgr._auto_enter_maintenance = auto_maint_mock

    emit_mock = AsyncMock()
    with patch("backend.services.socketio_manager.emit_gateway_alarm", emit_mock):
        await mgr._persist_gateway_event("gw-9", None, "OTA_STARTED", "info", None)

    finalize_mock.assert_not_called()
    auto_maint_mock.assert_not_called()
    assert len(db.added) == 1
    assert db.added[0].plug_id is None
    emit_mock.assert_awaited_once()
    assert emit_mock.await_args.args[0]["plug_id"] is None
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_auto_enter_maintenance_sets_status_and_broadcasts():
    """The coroutine itself: flips a non-MAINTENANCE plug to MAINTENANCE,
    commits, and broadcasts the new status (fw already force-OFF'd it)."""
    from backend.database.models import PlugStatus

    MQTTManager._instance = None
    plug = MagicMock()
    plug.status = PlugStatus.AVAILABLE
    session = _FakeSession([_FakeResult(scalar=plug)])
    mgr = MQTTManager(db_session_factory=lambda: session)

    emit_mock = AsyncMock()
    with patch("backend.services.socketio_manager.emit_plug_status", emit_mock):
        await mgr._auto_enter_maintenance(5, "THERMAL_CUTOFF")

    assert plug.status == PlugStatus.MAINTENANCE
    assert session.committed is True
    emit_mock.assert_awaited_once_with(5, "maintenance")
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_auto_enter_maintenance_noop_if_already_maintenance():
    """Idempotent: a plug already in MAINTENANCE isn't re-committed."""
    from backend.database.models import PlugStatus

    MQTTManager._instance = None
    plug = MagicMock()
    plug.status = PlugStatus.MAINTENANCE
    session = _FakeSession([_FakeResult(scalar=plug)])
    mgr = MQTTManager(db_session_factory=lambda: session)

    await mgr._auto_enter_maintenance(5, "THERMAL_CUTOFF")

    assert session.committed is False
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_auto_enter_maintenance_noop_if_plug_missing():
    """An unknown plug_id is logged and dropped, not crashed."""
    MQTTManager._instance = None
    session = _FakeSession([_FakeResult(scalar=None)])
    mgr = MQTTManager(db_session_factory=lambda: session)

    await mgr._auto_enter_maintenance(999, "OVERCURRENT_CUTOFF")

    assert session.committed is False
    MQTTManager._instance = None


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,exp_status,exp_fw", [
    ({"status": "online", "fw": "1.5.0-direct"}, "online", "1.5.0-direct"),
    ({"status": "online"}, "online", None),          # fw absent
    ({"status": "offline"}, "offline", None),        # LWT carries no fw
])
async def test_status_parses_fw(payload, exp_status, exp_fw):
    """
    _handle_gateway_status must forward the reported firmware version (from the
    `online` status payload) to _persist_gateway_status. The LWT/offline message
    has no fw, so None is forwarded and the stored value is left untouched.
    """
    MQTTManager._instance = None
    loop = asyncio.get_running_loop()
    mgr = MQTTManager(db_session_factory=lambda: None, event_loop=loop)

    captured = {}

    async def fake_persist(gateway_id, status, firmware_version=None):
        captured.update(gateway_id=gateway_id, status=status, firmware_version=firmware_version)

    mgr._persist_gateway_status = fake_persist
    mgr._handle_gateway_status("gw-1", payload)
    await asyncio.sleep(0.05)

    assert captured.get("status") == exp_status
    assert captured.get("firmware_version") == exp_fw
    MQTTManager._instance = None


# ---------------------------------------------------------------------------
# Plug discovery -> backend-authoritative plug_id assignment (docs/AMPHIVE_AGENT.md)
# ---------------------------------------------------------------------------


class _FakeResult:
    """Stands in for a SQLAlchemy Result: scalar_one_or_none() or all()."""

    def __init__(self, scalar="__unset__", rows=None):
        self._scalar = scalar
        self._rows = rows or []

    def scalar_one_or_none(self):
        return None if self._scalar == "__unset__" else self._scalar

    def scalar_one(self):
        return 0 if self._scalar == "__unset__" else self._scalar

    def all(self):
        return self._rows


class _FakeSession:
    """Async-context session that returns queued results and records adds."""

    def __init__(self, results):
        self._results = iter(results)
        self.added = []
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, *_a, **_k):
        return next(self._results)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True


@pytest.mark.asyncio
async def test_discovery_missing_unique_id_is_ignored():
    """A discovery announcement without unique_id must not touch the DB/loop."""
    MQTTManager._instance = None
    loop = asyncio.get_running_loop()
    mgr = MQTTManager(db_session_factory=lambda: None, event_loop=loop)

    scheduled = []
    mgr._persist_plug_discovery = lambda *a, **k: scheduled.append(a)  # would be awaited

    mgr._handle_gateway_plug_discovery("gw-1", {"provider": "kasa"})  # no unique_id
    await asyncio.sleep(0.05)

    assert scheduled == [], "discovery without unique_id should be ignored"
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_discovery_marshaled_onto_loop():
    """A valid discovery on the paho thread schedules _persist_plug_discovery."""
    MQTTManager._instance = None
    loop = asyncio.get_running_loop()
    mgr = MQTTManager(db_session_factory=lambda: None, event_loop=loop)

    captured = {}

    async def fake_persist(gateway_id, payload):
        captured.update(gateway_id=gateway_id, payload=payload)

    mgr._persist_plug_discovery = fake_persist

    payload = {"unique_id": "kasa:AA:BB", "provider": "kasa", "model": "KP115"}
    mgr._handle_gateway_plug_discovery("gw-9", payload)
    await asyncio.sleep(0.05)  # let the scheduled coroutine run

    assert captured.get("gateway_id") == "gw-9"
    assert captured.get("payload") == payload
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_persist_discovery_upserts_new_plug_and_publishes_assign_map():
    """
    For a known gateway, a new unique_id is inserted as a Plug and the backend
    publishes the retained {unique_id: plug_id} map so the agent adopts the
    DB-assigned id.
    """
    MQTTManager._instance = None
    loop = asyncio.get_running_loop()

    session = _FakeSession([
        _FakeResult(scalar=MagicMock()),                 # gateway exists
        _FakeResult(scalar=None),                        # plug is new
        _FakeResult(scalar=0),                           # plug count under the per-gateway cap
        _FakeResult(rows=[("kasa:AA:BB:CC", 5)]),        # rebuilt map after commit
    ])
    mgr = MQTTManager(db_session_factory=lambda: session, event_loop=loop)
    mgr.client = MagicMock()

    await mgr._persist_plug_discovery("gw-1", {
        "unique_id": "kasa:AA:BB:CC", "provider": "kasa",
        "model": "KP115", "alias": "Bay 3",
    })

    # A new Plug was inserted with the reported unique_id.
    assert len(session.added) == 1
    assert session.added[0].unique_id == "kasa:AA:BB:CC"
    assert session.added[0].name == "Bay 3"
    assert session.committed

    # The retained assign map was published with the DB-assigned id.
    mgr.client.publish.assert_called_once()
    args, kwargs = mgr.client.publish.call_args
    assert args[0] == "amphive/gateways/gw-1/assign"
    assert json.loads(args[1]) == {"kasa:AA:BB:CC": 5}
    assert kwargs.get("retain") is True
    MQTTManager._instance = None


# ---------------------------------------------------------------------------
# Plug roster (amphive/gateways/{gw}/config) — backend-pushed, retained
# ---------------------------------------------------------------------------


def test_publish_plug_roster_retained_config_topic():
    """publish_plug_roster emits a retained QoS-1 {"v":1,"plugs":[...]} message
    on the gateway's own config topic."""
    MQTTManager._instance = None
    mgr = MQTTManager(db_session_factory=lambda: None)
    mgr.client = MagicMock()

    mgr.publish_plug_roster("gw-1", [
        {"plug_id": 7, "local_ip": "10.0.0.7", "max_current_a": 16.0},
    ])

    args, kwargs = mgr.client.publish.call_args
    assert args[0] == "amphive/gateways/gw-1/config"
    assert kwargs.get("retain") is True
    assert kwargs.get("qos") == 1
    assert json.loads(args[1]) == {
        "v": 1,
        "plugs": [{"plug_id": 7, "local_ip": "10.0.0.7", "max_current_a": 16.0}],
    }
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_publish_roster_for_gateway_loads_plugs():
    """_publish_roster_for_gateway loads the gateway's plugs and serialises them
    into the roster (max_current_a=None passes through as null)."""
    MQTTManager._instance = None
    session = _FakeSession([
        _FakeResult(rows=[(7, "10.0.0.7", 16.0), (8, "10.0.0.8", None)]),
    ])
    mgr = MQTTManager(db_session_factory=lambda: session)
    mgr.client = MagicMock()

    await mgr._publish_roster_for_gateway("gw-1")

    args, kwargs = mgr.client.publish.call_args
    assert args[0] == "amphive/gateways/gw-1/config"
    assert kwargs.get("retain") is True
    payload = json.loads(args[1])
    assert payload["v"] == 1
    assert payload["plugs"] == [
        {"plug_id": 7, "local_ip": "10.0.0.7", "max_current_a": 16.0},
        {"plug_id": 8, "local_ip": "10.0.0.8", "max_current_a": None},
    ]
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_gateway_online_publishes_roster():
    """A gateway coming online triggers a retained roster republish."""
    from backend.database.models import GatewayStatus

    MQTTManager._instance = None
    loop = asyncio.get_running_loop()

    gw = MagicMock()
    gw.status = GatewayStatus.OFFLINE  # so this `online` is a real transition
    session = _FakeSession([_FakeResult(scalar=gw)])
    mgr = MQTTManager(db_session_factory=lambda: session, event_loop=loop)
    mgr.client = MagicMock()
    mgr._republish_off_for_orphaned_plugs = AsyncMock()
    mgr._publish_roster_for_gateway = AsyncMock()
    mgr._broadcast_plug_connectivity = AsyncMock()

    await mgr._persist_gateway_status("gw-1", "online", "2.0.0-direct")

    mgr._publish_roster_for_gateway.assert_awaited_once_with("gw-1")
    MQTTManager._instance = None


def _status_mgr(loop, gw_status):
    """MQTTManager wired for a `_persist_gateway_status` call against a
    gateway currently in `gw_status`, with the online-path collaborators
    mocked out."""
    from backend.database.models import GatewayStatus  # noqa: F401

    gw = MagicMock()
    gw.status = gw_status
    session = _FakeSession([_FakeResult(scalar=gw)])
    mgr = MQTTManager(db_session_factory=lambda: session, event_loop=loop)
    mgr.client = MagicMock()
    mgr._republish_off_for_orphaned_plugs = AsyncMock()
    mgr._publish_roster_for_gateway = AsyncMock()
    mgr._broadcast_plug_connectivity = AsyncMock()
    return mgr


@pytest.mark.asyncio
async def test_real_online_transition_alerts_operators_in_off_sweep():
    """OFFLINE->ONLINE is a genuine reconnect: the orphan-OFF sweep may alert
    the tenant's CPOs about force-OFF'd plugs."""
    from backend.database.models import GatewayStatus

    MQTTManager._instance = None
    mgr = _status_mgr(asyncio.get_running_loop(), GatewayStatus.OFFLINE)

    await mgr._persist_gateway_status("gw-1", "online")

    mgr._republish_off_for_orphaned_plugs.assert_awaited_once_with(
        "gw-1", alert_operators=True
    )
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_retained_online_replay_runs_off_sweep_without_operator_alert():
    """A retained `online` replay (gateway already ONLINE — re-delivered on
    every backend/broker reconnect) still runs the idempotent OFF sweep but
    must NOT re-alert operators: every backend restart was producing a fresh
    round of `orphan_off` bells (32 by the end of the 2026-08-03
    outage-recovery morning)."""
    from backend.database.models import GatewayStatus

    MQTTManager._instance = None
    mgr = _status_mgr(asyncio.get_running_loop(), GatewayStatus.ONLINE)

    await mgr._persist_gateway_status("gw-1", "online")

    mgr._republish_off_for_orphaned_plugs.assert_awaited_once_with(
        "gw-1", alert_operators=False
    )
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_persist_discovery_unknown_gateway_publishes_nothing():
    """Discovery for an unclaimed gateway is dropped (no plug, no assign map)."""
    MQTTManager._instance = None
    loop = asyncio.get_running_loop()

    session = _FakeSession([_FakeResult(scalar=None)])  # gateway does not exist
    mgr = MQTTManager(db_session_factory=lambda: session, event_loop=loop)
    mgr.client = MagicMock()

    await mgr._persist_plug_discovery("ghost-gw", {"unique_id": "kasa:ZZ"})

    assert session.added == []
    mgr.client.publish.assert_not_called()
    MQTTManager._instance = None


@pytest.mark.asyncio
@pytest.mark.parametrize("balance,energy_kwh,should_stop", [
    (Decimal("100"), 25.0, True),    # cost 25*5=125 >= 100 → exhausted, auto-stop
    (Decimal("100"), 19.0, False),   # cost 95 < 100 → still covered, no stop
    (Decimal("100"), 20.0, True),    # cost 100 == 100 → exhausted (>=)
])
async def test_auto_stop_on_balance_exhaustion(balance, energy_kwh, should_stop):
    """
    When the accrued energy cost (energy_kwh * COINS_PER_KWH, default 5) meets or
    exceeds the driver's wallet balance, the session is finalized via the shared
    finalize path; otherwise it keeps running.
    """
    MQTTManager._instance = None
    user = MagicMock()
    user.coin_balance = balance
    mgr = MQTTManager(db_session_factory=lambda: _FakeDB(user))

    finalize_mock = AsyncMock(return_value={"energy_kwh": energy_kwh, "coins_spent": 0})
    with patch("backend.services.session_lifecycle.finalize_charging_session", finalize_mock):
        await mgr._maybe_auto_stop_on_exhaustion(session_id=7, user_id=3, energy_kwh=energy_kwh)

    assert finalize_mock.called is should_stop
    if should_stop:
        args, kwargs = finalize_mock.call_args
        assert args[1] == 7  # session_id
        assert "exhaust" in kwargs.get("reason", "").lower()
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_auto_stop_disabled_by_flag(monkeypatch):
    """With AUTO_STOP_ON_BALANCE_EXHAUSTED off, the wallet check is skipped."""
    import backend.services.mqtt_manager as mm
    monkeypatch.setattr(mm, "AUTO_STOP_ON_BALANCE_EXHAUSTED", False)

    MQTTManager._instance = None
    user = MagicMock()
    user.coin_balance = Decimal("1")  # would be exhausted if the check ran
    mgr = MQTTManager(db_session_factory=lambda: _FakeDB(user))

    finalize_mock = AsyncMock()
    with patch("backend.services.session_lifecycle.finalize_charging_session", finalize_mock):
        await mgr._maybe_auto_stop_on_exhaustion(session_id=7, user_id=3, energy_kwh=100.0)

    finalize_mock.assert_not_called()
    MQTTManager._instance = None

# ---------------------------------------------------------------------------
# Telemetry ingestion guards (TD#25 + payload plug ownership, 2026-07-06 audit)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"plug_id": 1, "watts": "abc"},                 # non-numeric watts
    {"plug_id": 1, "kwh": {"nested": 1}},           # non-castable kwh
    {"plug_id": 1, "current": [1, 2]},              # non-castable current
    {"plug_id": "not-a-number", "watts": 10.0},     # non-integer plug_id
    {"plug_id": None, "watts": 10.0},               # explicit null plug_id
    {"plug_id": 1, "watts": "NaN"},                 # parses, but non-finite
    {"plug_id": 1, "voltage": "inf"},               # parses, but non-finite
])
async def test_malformed_telemetry_dropped_without_crashing(payload):
    """
    A malformed telemetry payload must be logged and dropped — it must not
    raise in the paho callback, feed the store, or reach persistence (TD#25:
    the old bare float() casts threw and silently killed the reading).
    """
    MQTTManager._instance = None
    loop = asyncio.get_running_loop()
    store = MagicMock()
    mgr = MQTTManager(telemetry_store=store, db_session_factory=lambda: None,
                      event_loop=loop)

    persists = []

    async def fake_persist(*a, **k):
        persists.append(a)

    mgr._persist_telemetry = fake_persist

    mgr._handle_gateway_telemetry("gw-1", payload)  # must not raise
    await asyncio.sleep(0.05)

    store.update.assert_not_called()
    assert persists == []
    MQTTManager._instance = None


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_kwh", [
    1e30,   # DoS: finite, so it sails past isfinite() — the monotonic max()
            # in _persist_telemetry would pin it onto active_session.energy_kwh,
            # then session_cost -> to_money's Decimal.quantize would raise a
            # raw decimal.InvalidOperation on every finalize path (a ~30-digit
            # number can't be rounded to 2dp in the default Decimal context),
            # wedging the session ACTIVE and the plug OCCUPIED forever.
    -5.0,   # negative energy is never plausible either.
])
async def test_implausible_kwh_dropped_before_persistence(bad_kwh):
    """
    A kwh far outside MAX_PLAUSIBLE_KWH (or negative) must be dropped by the
    handler itself, same as the non-finite/malformed cases above — it must
    never reach the TelemetryStore or get scheduled onto _persist_telemetry,
    so it can never be pinned onto a session's stored energy.
    """
    MQTTManager._instance = None
    loop = asyncio.get_running_loop()
    store = MagicMock()
    mgr = MQTTManager(telemetry_store=store, db_session_factory=lambda: None,
                      event_loop=loop)

    persists = []

    async def fake_persist(*a, **k):
        persists.append(a)

    mgr._persist_telemetry = fake_persist

    mgr._handle_gateway_telemetry("gw-1", {
        "plug_id": 1, "watts": 100.0, "kwh": bad_kwh,
        "voltage": 230.0, "current": 1.0, "status": "occupied",
    })
    await asyncio.sleep(0.05)

    store.update.assert_not_called()
    assert persists == []
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_kwh_at_the_plausible_ceiling_is_not_dropped():
    """The ceiling is inclusive: a reading exactly at MAX_PLAUSIBLE_KWH is a
    real (if extreme) value, not garbage, and must still be processed."""
    from backend.services.mqtt.telemetry import MAX_PLAUSIBLE_KWH

    MQTTManager._instance = None
    loop = asyncio.get_running_loop()
    store = MagicMock()
    mgr = MQTTManager(telemetry_store=store, db_session_factory=lambda: None,
                      event_loop=loop)

    persists = []

    async def fake_persist(gateway_id, plug_id, watts, kwh, session_id=None,
                           sample=None, relay_on=False, is_offline=False,
                           today_kwh=None, month_kwh=None):
        persists.append(kwh)

    mgr._persist_telemetry = fake_persist

    mgr._handle_gateway_telemetry("gw-1", {
        "plug_id": 1, "watts": 100.0, "kwh": MAX_PLAUSIBLE_KWH,
        "voltage": 230.0, "current": 1.0, "status": "occupied",
    })
    await asyncio.sleep(0.05)

    assert persists == [MAX_PLAUSIBLE_KWH]
    # [H1] On the DB-backed path the live-store feed now rides INSIDE
    # _persist_telemetry (behind the plug-ownership check), which is stubbed
    # here — so the handler itself no longer touches the store. The frame being
    # processed is proven by it reaching the (stubbed) persist above.
    store.update.assert_not_called()
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_implausible_kwh_frame_does_not_block_a_following_normal_frame():
    """
    A dropped 1e30 kwh frame must not wedge the handler for the next reading —
    e.g. the normal-sized frame that reports a session's actual stop must
    still be processed exactly as if the bad frame had never arrived.
    """
    MQTTManager._instance = None
    loop = asyncio.get_running_loop()
    store = MagicMock()
    mgr = MQTTManager(telemetry_store=store, db_session_factory=lambda: None,
                      event_loop=loop)

    persists = []

    async def fake_persist(gateway_id, plug_id, watts, kwh, session_id=None,
                           sample=None, relay_on=False, is_offline=False,
                           today_kwh=None, month_kwh=None):
        persists.append(kwh)

    mgr._persist_telemetry = fake_persist

    mgr._handle_gateway_telemetry("gw-1", {
        "plug_id": 1, "watts": 100.0, "kwh": 1e30,
        "voltage": 230.0, "current": 1.0, "status": "occupied",
    })
    mgr._handle_gateway_telemetry("gw-1", {
        "plug_id": 1, "watts": 0.0, "kwh": 0.75,
        "voltage": 230.0, "current": 0.0, "status": "available",
    })
    await asyncio.sleep(0.05)

    # Only the normal (stop) frame made it through.
    assert persists == [0.75]
    # [H1] The live-store feed now rides inside _persist_telemetry (stubbed
    # here), so the handler no longer touches the store on the DB-backed path;
    # `persists` above is the authoritative proof of what got processed.
    store.update.assert_not_called()
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_implausible_kwh_never_reaches_the_db_or_pins_session_energy():
    """
    End-to-end proof (handler -> scheduled persist, not just the isolated
    _persist_telemetry unit): a 1e30 kwh frame must never reach the DB layer
    at all, so it can never be pinned onto active_session.energy_kwh via the
    monotonic max() in _persist_telemetry. Plug/session wiring mirrors
    test_persist_telemetry_drops_foreign_plug below.
    """
    MQTTManager._instance = None
    loop = asyncio.get_running_loop()

    plug = MagicMock()
    plug.gateway_id = "gw-1"
    sess_row = MagicMock()
    sess_row.energy_kwh = 0.5  # the real, pre-frame stored energy

    session = _FakeSession([
        _FakeResult(scalar=plug),
        _FakeResult(scalar=sess_row),
    ])
    mgr = MQTTManager(db_session_factory=lambda: session, event_loop=loop)

    mgr._handle_gateway_telemetry("gw-1", {
        "plug_id": 5, "watts": 100.0, "kwh": 1e30,
        "voltage": 230.0, "current": 1.0, "status": "occupied",
        "session_id": "42",
    })
    await asyncio.sleep(0.05)

    # The frame never reached the DB layer: no query, no commit, and the
    # session's stored energy is untouched.
    assert session.committed is False
    assert sess_row.energy_kwh == 0.5
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_string_plug_id_coerced_to_int():
    """A numeric-string plug_id ("3") is coerced to int so downstream DB
    comparisons and store keys stay type-consistent."""
    MQTTManager._instance = None
    store = MagicMock()
    mgr = MQTTManager(telemetry_store=store)  # no loop: direct update path

    mgr._handle_gateway_telemetry("gw-1", {
        "plug_id": "3", "watts": 5.0, "kwh": 0.1,
        "voltage": 230.0, "current": 0.02, "status": "occupied",
    })

    kwargs = store.update.call_args.kwargs
    assert kwargs["plug_id"] == 3
    assert isinstance(kwargs["plug_id"], int)
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_persist_telemetry_drops_foreign_plug():
    """
    The payload's plug_id must belong to the topic's gateway: broker ACLs
    scope *topics*, not payload claims, so a compromised gateway could
    otherwise attribute energy/billing to another tenant's plug. Nothing is
    committed or enqueued for a foreign plug.
    """
    MQTTManager._instance = None
    plug = MagicMock()
    plug.gateway_id = "gw-other"
    session = _FakeSession([_FakeResult(scalar=plug)])
    fake_tp = MagicMock()
    mgr = MQTTManager(db_session_factory=lambda: session,
                      telemetry_persistence=fake_tp)

    await mgr._persist_telemetry("gw-1", 5, 100.0, 1.0, None, {"plug_id": 5})

    assert session.committed is False
    fake_tp.enqueue.assert_not_called()
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_persist_telemetry_drops_unknown_plug():
    """A plug_id that doesn't exist at all is dropped the same way."""
    MQTTManager._instance = None
    session = _FakeSession([_FakeResult(scalar=None)])
    fake_tp = MagicMock()
    mgr = MQTTManager(db_session_factory=lambda: session,
                      telemetry_persistence=fake_tp)

    await mgr._persist_telemetry("gw-1", 99, 100.0, 1.0, None, {"plug_id": 99})

    assert session.committed is False
    fake_tp.enqueue.assert_not_called()
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_persist_telemetry_accepts_own_plug_and_enqueues_sample():
    """For the gateway's own plug the snapshot commits and the raw sample is
    enqueued (the enqueue is deferred here from the handler so it sits behind
    the ownership check)."""
    MQTTManager._instance = None
    plug = MagicMock()
    plug.gateway_id = "gw-1"
    plug.last_telemetry_at = None
    session = _FakeSession([
        _FakeResult(scalar=plug),   # plug lookup: owned
        _FakeResult(scalar=None),   # no ACTIVE session on the plug
    ])
    fake_tp = MagicMock()
    mgr = MQTTManager(db_session_factory=lambda: session,
                      telemetry_persistence=fake_tp)

    sample = {"plug_id": 5, "power_w": 100.0}
    await mgr._persist_telemetry("gw-1", 5, 100.0, 1.0, None, sample)

    assert session.committed is True
    assert plug.current_power_w == 100.0
    fake_tp.enqueue.assert_called_once_with(sample)
    MQTTManager._instance = None


def _owned_plug():
    plug = MagicMock()
    plug.gateway_id = "gw-1"
    plug.local_ip = "10.0.0.5"
    plug.last_telemetry_at = None  # first frame — powered_since re-baselines
    return plug


@pytest.mark.asyncio
async def test_persist_telemetry_foreign_plug_does_not_feed_live_store():
    """[H1] The live TelemetryStore must NOT be fed for a plug the publishing
    gateway doesn't own. Otherwise any tenant with one provisioned gateway
    could publish a victim plug_id on its OWN topic and poison that plug's
    live snapshot — the figure the SSE stream shows, and the value the old
    finalize billed from via max(live, persisted). The store feed rides behind
    the same ownership check as the DB persist, so a foreign plug touches
    neither."""
    MQTTManager._instance = None
    plug = MagicMock()
    plug.gateway_id = "gw-other"   # NOT the publishing gateway
    session = _FakeSession([_FakeResult(scalar=plug)])
    store = MagicMock()
    mgr = MQTTManager(db_session_factory=lambda: session, telemetry_store=store)

    # A malicious max-magnitude frame aimed at another tenant's plug (id 5).
    sample = {"plug_id": 5, "power_w": 15000.0, "energy_kwh": 1000.0,
              "current_a": 65.0, "voltage_v": 230.0, "status": "occupied"}
    await mgr._persist_telemetry("gw-1", 5, 15000.0, 1000.0, None, sample,
                                 relay_on=True)

    store.update.assert_not_called()
    assert session.committed is False
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_persist_telemetry_owned_plug_feeds_live_store():
    """[H1] For the gateway's OWN plug the live TelemetryStore IS fed — the
    store feed rides behind the same ownership check as the DB persist, using
    the current/voltage/status carried on the handler-built `sample`. The raw
    frame kwh (live display value) is what the store shows."""
    MQTTManager._instance = None
    active = MagicMock()
    active.id = 10
    active.user_id = 1
    active.energy_kwh = 0.4
    active.peak_power_w = 0.0
    active.energy_counter_last_raw_kwh = None
    active.energy_reset_offset_kwh = 0.0
    session = _FakeSession([
        _FakeResult(scalar=_owned_plug()),
        _FakeResult(scalar=active),
    ])
    store = MagicMock()
    mgr = MQTTManager(db_session_factory=lambda: session, telemetry_store=store)
    mgr._maybe_auto_stop_on_exhaustion = AsyncMock()
    mgr._maybe_auto_stop_on_limits = AsyncMock()

    sample = {"plug_id": 5, "power_w": 1200.0, "energy_kwh": 0.45,
              "current_a": 5.2, "voltage_v": 231.0, "status": "occupied"}
    with patch("backend.services.pricing.reprice_session_if_due",
               AsyncMock(return_value=None)):
        await mgr._persist_telemetry("gw-1", 5, 1200.0, 0.45,
                                     session_id=10, sample=sample, relay_on=True)

    store.update.assert_called_once_with(
        plug_id=5, power_w=1200.0, current_a=5.2, energy_kwh=0.45,
        status="charging", voltage_v=231.0, relay_on=True,
    )
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_persist_telemetry_energy_is_monotonic():
    """[REC-01] A reading below the stored total (a meter reset / re-baseline)
    must NOT lower the session's billed energy — energy only ever climbs."""
    MQTTManager._instance = None
    active = MagicMock()
    active.id = 10
    active.user_id = 1
    active.energy_kwh = 5.0
    active.peak_power_w = 0.0
    active.energy_counter_last_raw_kwh = None  # first frame — no reset baseline yet
    active.energy_reset_offset_kwh = 0.0
    session = _FakeSession([
        _FakeResult(scalar=_owned_plug()),
        _FakeResult(scalar=active),
    ])
    mgr = MQTTManager(db_session_factory=lambda: session)
    mgr._maybe_auto_stop_on_exhaustion = AsyncMock()
    mgr._maybe_auto_stop_on_limits = AsyncMock()

    with patch("backend.services.pricing.reprice_session_if_due",
               AsyncMock(return_value=None)):
        # session_id matches the ACTIVE row → attributed; kwh drops 5.0 -> 2.0.
        await mgr._persist_telemetry("gw-1", 5, 100.0, 2.0,
                                     session_id=10, sample=None, relay_on=True)

    assert active.energy_kwh == 5.0
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_persist_telemetry_energy_climbs_on_higher_reading():
    """The monotonic guard still lets a genuinely higher reading through."""
    MQTTManager._instance = None
    active = MagicMock()
    active.id = 10
    active.user_id = 1
    active.energy_kwh = 5.0
    active.peak_power_w = 0.0
    active.energy_counter_last_raw_kwh = None  # first frame — no reset baseline yet
    active.energy_reset_offset_kwh = 0.0
    session = _FakeSession([
        _FakeResult(scalar=_owned_plug()),
        _FakeResult(scalar=active),
    ])
    mgr = MQTTManager(db_session_factory=lambda: session)
    mgr._maybe_auto_stop_on_exhaustion = AsyncMock()
    mgr._maybe_auto_stop_on_limits = AsyncMock()

    with patch("backend.services.pricing.reprice_session_if_due",
               AsyncMock(return_value=None)):
        await mgr._persist_telemetry("gw-1", 100.0, 100.0, 7.5,
                                     session_id=10, sample=None, relay_on=True)

    assert active.energy_kwh == 7.5
    MQTTManager._instance = None


# ---------------------------------------------------------------------------
# [REC-01 follow-up] Energy counter reset detection/re-baselining
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_telemetry_counter_reset_banks_offset_and_stays_monotonic():
    """A LIVE frame whose raw kwh drops well below the last-seen raw value is
    a genuine counter reset (device reboot/reflash). The LOST SLICE (how far
    the counter fell back) is banked into energy_reset_offset_kwh, and billed
    energy_kwh does not dip — it stays pinned at the pre-reset total (offset +
    tiny new raw kwh is still below the stored total right after the reset)."""
    MQTTManager._instance = None
    active = MagicMock()
    active.id = 10
    active.user_id = 1
    active.energy_kwh = 5.0
    active.peak_power_w = 0.0
    active.energy_counter_last_raw_kwh = 4.8
    active.energy_reset_offset_kwh = 0.0
    session = _FakeSession([
        _FakeResult(scalar=_owned_plug()),
        _FakeResult(scalar=active),
    ])
    mgr = MQTTManager(db_session_factory=lambda: session)
    mgr._maybe_auto_stop_on_exhaustion = AsyncMock()
    mgr._maybe_auto_stop_on_limits = AsyncMock()

    with patch("backend.services.pricing.reprice_session_if_due",
               AsyncMock(return_value=None)):
        # Raw counter dropped 4.8 -> 0.1 (reset), well past the tolerance.
        await mgr._persist_telemetry("gw-1", 100.0, 100.0, 0.1,
                                     session_id=10, sample=None, relay_on=True)

    # Banked the DROP (4.8 - 0.1), not the whole pre-reset reading: offset +
    # raw is continuous across the regression (4.7 + 0.1 == the 4.8 the counter
    # had reached), so nothing is billed twice as the counter re-climbs.
    assert active.energy_reset_offset_kwh == pytest.approx(4.7)
    assert active.energy_counter_last_raw_kwh == 0.1
    assert active.energy_kwh == 5.0  # offset(4.7) + raw(0.1) = 4.8 < stored 5.0
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_persist_telemetry_post_reset_climb_bills_offset_plus_raw():
    """Once the post-reset raw counter climbs enough that offset + raw exceeds
    the pre-reset stored total, billed energy resumes climbing from the
    banked offset rather than freezing or losing the pre-reset energy."""
    MQTTManager._instance = None
    active = MagicMock()
    active.id = 10
    active.user_id = 1
    active.energy_kwh = 5.0
    active.peak_power_w = 0.0
    active.energy_counter_last_raw_kwh = 0.1   # left off here after the reset above
    active.energy_reset_offset_kwh = 4.7       # already banked from the reset (the 4.8 -> 0.1 drop)
    session = _FakeSession([
        _FakeResult(scalar=_owned_plug()),
        _FakeResult(scalar=active),
    ])
    mgr = MQTTManager(db_session_factory=lambda: session)
    mgr._maybe_auto_stop_on_exhaustion = AsyncMock()
    mgr._maybe_auto_stop_on_limits = AsyncMock()

    with patch("backend.services.pricing.reprice_session_if_due",
               AsyncMock(return_value=None)):
        await mgr._persist_telemetry("gw-1", 100.0, 100.0, 0.5,
                                     session_id=10, sample=None, relay_on=True)

    # 0.5 is not a drop from 0.1, so no new reset — offset stays 4.7.
    assert active.energy_reset_offset_kwh == 4.7
    assert active.energy_counter_last_raw_kwh == 0.5
    assert active.energy_kwh == pytest.approx(5.2)  # offset(4.7) + raw(0.5) > stored 5.0
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_persist_telemetry_partial_counter_dip_is_not_double_billed():
    """Regression, session 80 (2026-08-13): a mid-session gateway reboot restores
    the firmware's energy meter from NVS, which is only persisted every
    ENERGY_PERSIST_THRESHOLD_WH (50 Wh), so the session counter comes back a few
    tens of Wh BEHIND and keeps climbing from there — it does NOT restart at zero.

    Banking the whole pre-drop reading (the old rule) re-added energy the counter
    then reported again: 1.2725 banked + a counter that climbed on to 1.7901 billed
    3.0626 kWh for 1.7901 kWh actually delivered. Banking only the drop keeps the
    billed total continuous across the reconnect."""
    MQTTManager._instance = None
    active = MagicMock()
    active.id = 80
    active.user_id = 9
    active.energy_kwh = 1.2725
    active.peak_power_w = 0.0
    active.energy_counter_last_raw_kwh = 1.2725
    active.energy_reset_offset_kwh = 0.0
    session = _FakeSession([
        _FakeResult(scalar=_owned_plug()),
        _FakeResult(scalar=active),
        _FakeResult(scalar=_owned_plug()),
        _FakeResult(scalar=active),
    ])
    mgr = MQTTManager(db_session_factory=lambda: session)
    mgr._maybe_auto_stop_on_exhaustion = AsyncMock()
    mgr._maybe_auto_stop_on_limits = AsyncMock()

    with patch("backend.services.pricing.reprice_session_if_due",
               AsyncMock(return_value=None)):
        # The real dip across the 12:13:46 reconnect: 1.2725 -> 1.2246 (47.9 Wh).
        await mgr._persist_telemetry("gw-1", 752.0, 752.0, 1.2246,
                                     session_id=80, sample=None, relay_on=True)

        assert active.energy_reset_offset_kwh == pytest.approx(0.0479)
        # Billed energy holds at the pre-dip total instead of jumping by 1.2725.
        assert active.energy_kwh == pytest.approx(1.2725)

        # The counter climbs on to its real final value — the session must bill
        # that, not that value plus the pre-dip reading again.
        await mgr._persist_telemetry("gw-1", 4.9, 4.9, 1.7901,
                                     session_id=80, sample=None, relay_on=True)

    assert active.energy_kwh == pytest.approx(1.838)  # 0.0479 + 1.7901, NOT 3.0626
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_persist_telemetry_small_drop_within_tolerance_is_not_a_reset():
    """A tiny drop consistent with %.4f rounding jitter on an essentially-flat
    reading must not be flagged as a counter reset (no offset banked)."""
    MQTTManager._instance = None
    active = MagicMock()
    active.id = 10
    active.user_id = 1
    active.energy_kwh = 5.0
    active.peak_power_w = 0.0
    active.energy_counter_last_raw_kwh = 5.0
    active.energy_reset_offset_kwh = 0.0
    session = _FakeSession([
        _FakeResult(scalar=_owned_plug()),
        _FakeResult(scalar=active),
    ])
    mgr = MQTTManager(db_session_factory=lambda: session)
    mgr._maybe_auto_stop_on_exhaustion = AsyncMock()
    mgr._maybe_auto_stop_on_limits = AsyncMock()

    with patch("backend.services.pricing.reprice_session_if_due",
               AsyncMock(return_value=None)):
        # 5.0 -> 4.998 is within ENERGY_COUNTER_RESET_DROP_KWH (default 0.005).
        await mgr._persist_telemetry("gw-1", 100.0, 100.0, 4.998,
                                     session_id=10, sample=None, relay_on=True)

    assert active.energy_reset_offset_kwh == 0.0  # not treated as a reset
    assert active.energy_counter_last_raw_kwh == 4.998  # still tracks the raw value
    assert active.energy_kwh == 5.0  # monotonic clamp, unrelated to reset logic
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_persist_telemetry_offline_resync_never_triggers_reset_detection():
    """[TD#24] An offline-resync frame replays historical readings out of
    order — a lower kwh than the last LIVE raw value must never be mistaken
    for a reset, and the offline frame must not clobber
    energy_counter_last_raw_kwh (which tracks only the live counter)."""
    MQTTManager._instance = None
    active = MagicMock()
    active.id = 11
    active.user_id = 1
    active.energy_kwh = 9.0
    active.peak_power_w = 0.0
    active.energy_counter_last_raw_kwh = 9.0  # last LIVE raw value
    active.energy_reset_offset_kwh = 0.0
    session = _FakeSession([
        _FakeResult(scalar=_owned_plug()),
        _FakeResult(scalar=active),
    ])
    mgr = MQTTManager(db_session_factory=lambda: session)
    mgr._maybe_auto_stop_on_exhaustion = AsyncMock()
    mgr._maybe_auto_stop_on_limits = AsyncMock()

    with patch("backend.services.pricing.reprice_session_if_due",
               AsyncMock(return_value=None)):
        # Buffered historical reading, far below the last live raw value.
        await mgr._persist_telemetry("gw-1", 5, 150.0, 1.0,
                                     session_id=11, sample=None,
                                     relay_on=True, is_offline=True)

    assert active.energy_reset_offset_kwh == 0.0        # no reset banked
    assert active.energy_counter_last_raw_kwh == 9.0     # untouched by the offline frame
    assert active.energy_kwh == 9.0                      # monotonic clamp holds
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_persist_telemetry_first_frame_of_session_never_flags_reset():
    """The very first frame of a session (energy_counter_last_raw_kwh NULL,
    the column's default) has no prior raw value to compare against, so it
    can never be flagged as a reset regardless of its kwh value."""
    MQTTManager._instance = None
    active = MagicMock()
    active.id = 12
    active.user_id = 1
    active.energy_kwh = 0.0
    active.peak_power_w = 0.0
    active.energy_counter_last_raw_kwh = None
    active.energy_reset_offset_kwh = 0.0
    session = _FakeSession([
        _FakeResult(scalar=_owned_plug()),
        _FakeResult(scalar=active),
    ])
    mgr = MQTTManager(db_session_factory=lambda: session)
    mgr._maybe_auto_stop_on_exhaustion = AsyncMock()
    mgr._maybe_auto_stop_on_limits = AsyncMock()

    with patch("backend.services.pricing.reprice_session_if_due",
               AsyncMock(return_value=None)):
        await mgr._persist_telemetry("gw-1", 100.0, 100.0, 0.05,
                                     session_id=12, sample=None, relay_on=True)

    assert active.energy_reset_offset_kwh == 0.0
    assert active.energy_counter_last_raw_kwh == 0.05
    assert active.energy_kwh == 0.05
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_persist_telemetry_idle_frame_not_attributed_to_active_session():
    """[REC-05] An idle frame (no session_id, relay off) reaches an ACTIVE
    session only via the plug-id fallback. It must not zero the session's
    energy or refresh its staleness clock — those belong to the real session
    this frame predates."""
    MQTTManager._instance = None
    active = MagicMock()
    active.energy_kwh = 5.0
    active.last_telemetry_at = "SENTINEL"
    session = _FakeSession([
        _FakeResult(scalar=_owned_plug()),
        _FakeResult(scalar=active),
    ])
    mgr = MQTTManager(db_session_factory=lambda: session)
    mgr._maybe_auto_stop_on_exhaustion = AsyncMock()
    mgr._maybe_auto_stop_on_limits = AsyncMock()
    mgr.send_plug_command = MagicMock()

    await mgr._persist_telemetry("gw-1", 5, 0.0, 0.0,
                                 session_id=None, sample=None, relay_on=False)

    assert active.energy_kwh == 5.0                 # not zeroed
    assert active.last_telemetry_at == "SENTINEL"   # staleness clock untouched
    mgr._maybe_auto_stop_on_exhaustion.assert_not_awaited()
    mgr.send_plug_command.assert_not_called()        # there IS an ACTIVE session
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_persist_telemetry_offline_resync_attributes_to_own_session():
    """[TD#24] A buffered reading drained on resync now echoes its own
    session_id. If that session is still ACTIVE, the reading updates its energy
    exactly like a live frame (buffered data isn't lost)."""
    MQTTManager._instance = None
    active = MagicMock()
    active.id = 11
    active.user_id = 1
    active.energy_kwh = 2.0
    active.peak_power_w = 0.0
    active.energy_counter_last_raw_kwh = None
    active.energy_reset_offset_kwh = 0.0
    session = _FakeSession([
        _FakeResult(scalar=_owned_plug()),
        _FakeResult(scalar=active),
    ])
    mgr = MQTTManager(db_session_factory=lambda: session)
    mgr._maybe_auto_stop_on_exhaustion = AsyncMock()
    mgr._maybe_auto_stop_on_limits = AsyncMock()

    with patch("backend.services.pricing.reprice_session_if_due",
               AsyncMock(return_value=None)):
        await mgr._persist_telemetry("gw-1", 5, 150.0, 4.0,
                                     session_id=11, sample=None,
                                     relay_on=True, is_offline=True)

    assert active.energy_kwh == 4.0
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_persist_telemetry_offline_resync_stale_session_id_is_inert():
    """[TD#24] If the buffered reading's session finalized while the gateway was
    offline (plug reused), the id-scoped lookup misses. The stale historical
    reading must neither bill another session nor — because it's an offline
    frame — trip the REC-02 OFF-republish against the plug's live relay."""
    MQTTManager._instance = None
    session = _FakeSession([
        _FakeResult(scalar=_owned_plug()),
        _FakeResult(scalar=None),   # session 11 no longer ACTIVE
    ])
    mgr = MQTTManager(db_session_factory=lambda: session)
    mgr._maybe_auto_stop_on_exhaustion = AsyncMock()
    mgr._maybe_auto_stop_on_limits = AsyncMock()
    mgr.send_plug_command = MagicMock()

    await mgr._persist_telemetry("gw-1", 5, 120.0, 9.9,
                                 session_id=11, sample=None,
                                 relay_on=True, is_offline=True)

    # relay_on True + no matching ACTIVE session would normally republish OFF;
    # is_offline suppresses it (contrast test_..._relay_on_no_session_republishes_off).
    mgr.send_plug_command.assert_not_called()
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_persist_telemetry_relay_on_no_session_republishes_off():
    """[REC-02] A frame reporting the relay ON for a plug with NO ACTIVE
    session re-sends OFF (level-triggered cleanup of a lost/failed OFF while
    the gateway stayed connected)."""
    MQTTManager._instance = None
    session = _FakeSession([
        _FakeResult(scalar=_owned_plug()),
        _FakeResult(scalar=None),   # no ACTIVE session (id-scoped/plug-scoped lookup)
        _FakeResult(scalar=None),   # [REC-02 race guard] plug-scoped recheck: still none
    ])
    mgr = MQTTManager(db_session_factory=lambda: session)
    mgr.send_plug_command = MagicMock()

    await mgr._persist_telemetry("gw-1", 7, 50.0, 0.0,
                                 session_id=None, sample=None, relay_on=True)

    mgr.send_plug_command.assert_called_once_with(
        "gw-1", 7, "OFF", local_ip="10.0.0.5", wait=False
    )
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_persist_telemetry_stale_session_id_does_not_off_a_different_active_session():
    """[REC-02 race guard] A frame carries a STALE claimed session_id (that
    session already finalized), so the id-scoped lookup misses — but a
    DIFFERENT session now legitimately owns the plug (a new one started on it
    since). The reconciliation OFF must not fire and cut power out from under
    the session that's really running."""
    MQTTManager._instance = None
    other_active = MagicMock()  # the NEW session that now legitimately owns the plug
    session = _FakeSession([
        _FakeResult(scalar=_owned_plug()),      # plug ownership
        _FakeResult(scalar=None),               # id-scoped lookup misses (stale id)
        _FakeResult(scalar=other_active),        # plug-scoped recheck: a DIFFERENT session IS active
    ])
    mgr = MQTTManager(db_session_factory=lambda: session)
    mgr.send_plug_command = MagicMock()

    await mgr._persist_telemetry("gw-1", 7, 50.0, 0.0,
                                 session_id=999, sample=None, relay_on=True)

    mgr.send_plug_command.assert_not_called()
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_persist_telemetry_relay_off_no_session_does_not_republish():
    """The level-triggered OFF is scoped to relay_on frames — an idle/off frame
    with no session must not spam OFF publishes."""
    MQTTManager._instance = None
    session = _FakeSession([
        _FakeResult(scalar=_owned_plug()),
        _FakeResult(scalar=None),   # no ACTIVE session
    ])
    mgr = MQTTManager(db_session_factory=lambda: session)
    mgr.send_plug_command = MagicMock()

    await mgr._persist_telemetry("gw-1", 7, 0.0, 0.0,
                                 session_id=None, sample=None, relay_on=False)

    mgr.send_plug_command.assert_not_called()
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_persist_telemetry_stamps_first_frame_power_clock():
    """[Plug power] The first frame (last_telemetry_at NULL) sets both
    powered_since and last_telemetry_at."""
    MQTTManager._instance = None
    plug = _owned_plug()  # last_telemetry_at = None
    session = _FakeSession([
        _FakeResult(scalar=plug),
        _FakeResult(scalar=None),   # no ACTIVE session
    ])
    mgr = MQTTManager(db_session_factory=lambda: session)

    await mgr._persist_telemetry("gw-1", 5, 100.0, 1.0, None, None)

    assert plug.powered_since is not None
    assert plug.last_telemetry_at is not None
    assert plug.last_telemetry_at == plug.powered_since
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_persist_telemetry_keeps_powered_since_within_window():
    """[Plug power] A frame following soon after the last one refreshes the
    freshness clock but keeps powered_since (no power gap)."""
    MQTTManager._instance = None
    plug = _owned_plug()
    recent = datetime.now(timezone.utc) - timedelta(seconds=5)
    plug.last_telemetry_at = recent
    plug.powered_since = recent
    session = _FakeSession([
        _FakeResult(scalar=plug),
        _FakeResult(scalar=None),
    ])
    mgr = MQTTManager(db_session_factory=lambda: session)

    await mgr._persist_telemetry("gw-1", 5, 100.0, 1.0, None, None)

    assert plug.powered_since == recent          # no gap -> unchanged
    assert plug.last_telemetry_at > recent       # freshness refreshed
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_persist_telemetry_rebaselines_powered_since_after_gap():
    """[Plug power] Telemetry resuming after a gap longer than
    PLUG_POWER_STALE_SEC re-baselines powered_since (a power-cycle)."""
    MQTTManager._instance = None
    plug = _owned_plug()
    old = datetime.now(timezone.utc) - timedelta(seconds=600)
    plug.last_telemetry_at = old
    plug.powered_since = old
    session = _FakeSession([
        _FakeResult(scalar=plug),
        _FakeResult(scalar=None),
    ])
    mgr = MQTTManager(db_session_factory=lambda: session)

    await mgr._persist_telemetry("gw-1", 5, 100.0, 1.0, None, None)

    assert plug.powered_since > old              # power resumed after a gap
    assert plug.last_telemetry_at > old
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_persist_telemetry_offline_frame_does_not_bump_freshness_clock():
    """[Plug power / TD#24] An offline-replay frame (buffered historical
    reading drained on resync) must NOT stamp last_telemetry_at/powered_since —
    those drive plug_is_powered()'s freshness check, and a buffered frame is
    not proof the plug is live right now. Without this, a backlog of replayed
    frames after a real power-cycle would make a de-powered plug look freshly
    powered."""
    MQTTManager._instance = None
    plug = _owned_plug()
    old = datetime.now(timezone.utc) - timedelta(seconds=600)  # older than PLUG_POWER_STALE_SEC
    plug.last_telemetry_at = old
    plug.powered_since = old
    session = _FakeSession([
        _FakeResult(scalar=plug),
        _FakeResult(scalar=None),   # no ACTIVE session
    ])
    mgr = MQTTManager(db_session_factory=lambda: session)

    await mgr._persist_telemetry("gw-1", 5, 100.0, 1.0, None, None,
                                 relay_on=False, is_offline=True)

    assert plug.last_telemetry_at == old   # untouched by the historical frame
    assert plug.powered_since == old
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_persist_telemetry_offline_first_frame_does_not_set_power_clock():
    """[Plug power / TD#24] Even as the very first frame ever seen for a plug
    (last_telemetry_at NULL), an offline-replay frame must not seed
    powered_since/last_telemetry_at — a historical reading proves nothing
    about the plug's live state now."""
    MQTTManager._instance = None
    plug = _owned_plug()  # last_telemetry_at = None
    plug.powered_since = None
    session = _FakeSession([
        _FakeResult(scalar=plug),
        _FakeResult(scalar=None),   # no ACTIVE session
    ])
    mgr = MQTTManager(db_session_factory=lambda: session)

    await mgr._persist_telemetry("gw-1", 5, 100.0, 1.0, None, None,
                                 relay_on=False, is_offline=True)

    assert plug.last_telemetry_at is None
    assert plug.powered_since is None
    MQTTManager._instance = None


def test_send_plug_command_includes_local_ip_in_payload():
    """ON/OFF carry the target plug's local_ip so a multi-plug gateway (TD#20)
    actuates the right plug and can learn an unseen one from the command."""
    MQTTManager._instance = None
    mgr = MQTTManager(db_session_factory=lambda: None)
    mgr.client = MagicMock()
    mgr.client.publish.return_value.is_published.return_value = True

    ok = mgr.send_plug_command("gw-1", 7, "ON", session_id=42, local_ip="10.0.0.7")

    assert ok is True
    args, _ = mgr.client.publish.call_args
    assert args[0] == "amphive/gateways/gw-1/plugs/7/commands"
    payload = json.loads(args[1])
    assert payload["action"] == "ON"
    assert payload["local_ip"] == "10.0.0.7"
    assert payload["session_id"] == "42"
    MQTTManager._instance = None


def test_send_plug_command_includes_max_current_a_on_on():
    """The ON payload carries max_current_a — the plug's effective current cap
    (amps) — for on-device enforcement against the plug's measured current."""
    MQTTManager._instance = None
    mgr = MQTTManager(db_session_factory=lambda: None)
    mgr.client = MagicMock()
    mgr.client.publish.return_value.is_published.return_value = True

    ok = mgr.send_plug_command("gw-1", 7, "ON", session_id=42,
                               local_ip="10.0.0.7", max_current_a=16.0)

    assert ok is True
    args, _ = mgr.client.publish.call_args
    payload = json.loads(args[1])
    assert payload["action"] == "ON"
    assert payload["max_current_a"] == 16.0
    MQTTManager._instance = None


def test_send_plug_command_omits_max_current_a_when_absent():
    """OFF / cleanup publishes omit max_current_a (backward-safe for firmware
    that doesn't read it)."""
    MQTTManager._instance = None
    mgr = MQTTManager(db_session_factory=lambda: None)
    mgr.client = MagicMock()
    mgr.client.publish.return_value.is_published.return_value = True

    mgr.send_plug_command("gw-1", 7, "OFF")

    args, _ = mgr.client.publish.call_args
    assert "max_current_a" not in json.loads(args[1])
    MQTTManager._instance = None


def test_send_plug_limits_includes_max_current_a():
    """SET_LIMITS carries max_current_a when given, so the firmware re-arms the
    running session's OVERCURRENT_CAP watchdog at the plug's effective cap."""
    MQTTManager._instance = None
    mgr = MQTTManager(db_session_factory=lambda: None)
    mgr.client = MagicMock()
    mgr.client.publish.return_value.is_published.return_value = True
    # Default wait=False path reports the publish rc (0 == MQTT_ERR_SUCCESS).
    mgr.client.publish.return_value.rc = 0

    ok = mgr.send_plug_limits("gw-1", 7, max_kwh=5.0, max_duration_seconds=3600,
                              local_ip="10.0.0.7", max_current_a=8.0)

    assert ok is True
    args, _ = mgr.client.publish.call_args
    assert args[0] == "amphive/gateways/gw-1/plugs/7/commands"
    payload = json.loads(args[1])
    assert payload["action"] == "SET_LIMITS"
    assert payload["max_kwh"] == 5.0
    assert payload["max_duration_seconds"] == 3600
    assert payload["max_current_a"] == 8.0
    MQTTManager._instance = None


def test_send_plug_limits_omits_max_current_a_when_absent():
    """A limits update without max_current_a omits the key — the firmware then
    leaves the running session's on-device cap untouched (backward-safe)."""
    MQTTManager._instance = None
    mgr = MQTTManager(db_session_factory=lambda: None)
    mgr.client = MagicMock()
    mgr.client.publish.return_value.is_published.return_value = True

    mgr.send_plug_limits("gw-1", 7, max_kwh=5.0, max_duration_seconds=3600)

    args, _ = mgr.client.publish.call_args
    payload = json.loads(args[1])
    assert payload["action"] == "SET_LIMITS"
    assert "max_current_a" not in payload
    MQTTManager._instance = None


def test_send_plug_command_omits_local_ip_when_absent():
    """Without local_ip the key is omitted, so old single-plug firmware falls
    back to its one provisioned target plug (backward-safe)."""
    MQTTManager._instance = None
    mgr = MQTTManager(db_session_factory=lambda: None)
    mgr.client = MagicMock()
    mgr.client.publish.return_value.is_published.return_value = True

    mgr.send_plug_command("gw-1", 7, "OFF")

    args, _ = mgr.client.publish.call_args
    assert "local_ip" not in json.loads(args[1])
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_set_plug_telemetry_interval_uses_lifespan_singleton(monkeypatch):
    """REC-13: set_plug_telemetry_interval must send through the singleton that
    lifespan built (state.mqtt_manager), never construct a fresh no-arg
    MQTTManager() that would pin a localhost/no-factory instance."""
    from types import SimpleNamespace

    from backend import state
    from backend.services import session_lifecycle

    MQTTManager._instance = None

    ts = MagicMock()
    ts.get_interval.return_value = 10000  # differs from target -> proceeds
    monkeypatch.setattr(state, "telemetry_store", ts)

    built = MagicMock()
    built.client = object()  # truthy client -> publish path taken
    monkeypatch.setattr(state, "mqtt_manager", built)

    db = _FakeDB(SimpleNamespace(id=7, gateway_id="gw-1"))
    await session_lifecycle.set_plug_telemetry_interval(db, 7, 1000)

    built.send_plug_interval.assert_called_once_with("gw-1", 7, 1000)
    # No fresh instance was pinned as the process singleton.
    assert MQTTManager._instance is None


@pytest.mark.asyncio
async def test_set_plug_telemetry_interval_noop_before_lifespan(monkeypatch):
    """REC-13: before lifespan binds state.mqtt_manager (None), the interval
    push is skipped gracefully rather than instantiating the singleton."""
    from types import SimpleNamespace

    from backend import state
    from backend.services import session_lifecycle

    MQTTManager._instance = None

    ts = MagicMock()
    ts.get_interval.return_value = 10000
    monkeypatch.setattr(state, "telemetry_store", ts)
    monkeypatch.setattr(state, "mqtt_manager", None)

    db = _FakeDB(SimpleNamespace(id=7, gateway_id="gw-1"))
    # Must not raise and must not pin a singleton.
    await session_lifecycle.set_plug_telemetry_interval(db, 7, 1000)

    assert MQTTManager._instance is None


# ---------------------------------------------------------------------------
# Gateway status restart-hygiene + connectivity push (REC-08/REC-09 + Lever 1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_duplicate_offline_notify_on_repeated_offline_status():
    """
    Subscriptions re-issue on every connect, so a backend/broker reconnect
    replays the retained LWT `offline`. _notify_drivers_gateway_offline must
    fire only on a real ONLINE->OFFLINE transition: once on the first offline,
    never again while the gateway is already stored OFFLINE (REC-08).
    """
    from backend.database.models import GatewayStatus

    MQTTManager._instance = None
    gateway = MagicMock()
    gateway.status = GatewayStatus.ONLINE
    session = _FakeSession([_FakeResult(scalar=gateway), _FakeResult(scalar=gateway)])
    mgr = MQTTManager(db_session_factory=lambda: session)
    mgr._notify_drivers_gateway_offline = AsyncMock()
    mgr._broadcast_plug_connectivity = AsyncMock()

    # First offline is a real ONLINE->OFFLINE transition → notify once.
    await mgr._persist_gateway_status("gw-1", "offline")
    # gateway.status is now OFFLINE; a replayed retained offline must NOT re-notify.
    await mgr._persist_gateway_status("gw-1", "offline")

    mgr._notify_drivers_gateway_offline.assert_awaited_once_with("gw-1")
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_status_online_does_not_bump_last_seen_at():
    """
    A retained `online` replayed on reconnect must not refresh liveness for a
    possibly-wedged gateway: _persist_gateway_status writes the status but must
    NOT stamp last_seen_at — telemetry (the real heartbeat) does that (REC-09).
    """
    from backend.database.models import GatewayStatus

    MQTTManager._instance = None
    sentinel = object()
    gateway = MagicMock()
    gateway.status = GatewayStatus.OFFLINE
    gateway.last_seen_at = sentinel
    session = _FakeSession([_FakeResult(scalar=gateway)])
    mgr = MQTTManager(db_session_factory=lambda: session)
    mgr._republish_off_for_orphaned_plugs = AsyncMock()
    mgr._broadcast_plug_connectivity = AsyncMock()

    await mgr._persist_gateway_status("gw-1", "online", firmware_version="1.8.0-direct")

    assert gateway.last_seen_at is sentinel  # untouched by the status message
    assert gateway.status == GatewayStatus.ONLINE  # the status write itself is kept
    assert gateway.firmware_version == "1.8.0-direct"
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_plug_connectivity_emitted_on_transition():
    """
    On a real online<->offline transition, plug_connectivity is broadcast for
    each of the gateway's plugs with the {plug_id, gateway_online} contract the
    frontend consumes (Faster-offline Lever 1).
    """
    from backend.database.models import GatewayStatus

    MQTTManager._instance = None
    gateway = MagicMock()
    gateway.status = GatewayStatus.ONLINE
    plug_result = MagicMock()
    plug_result.scalars.return_value.all.return_value = [10, 11]
    session = _FakeSession([_FakeResult(scalar=gateway), plug_result])
    mgr = MQTTManager(db_session_factory=lambda: session)
    mgr._notify_drivers_gateway_offline = AsyncMock()

    emit_mock = AsyncMock()
    with patch("backend.services.socketio_manager.emit_plug_connectivity", emit_mock):
        await mgr._persist_gateway_status("gw-1", "offline")  # ONLINE->OFFLINE

    assert emit_mock.await_count == 2
    emit_mock.assert_any_await(10, False)
    emit_mock.assert_any_await(11, False)
    MQTTManager._instance = None


# ---------------------------------------------------------------------------
# [Unmetered consumption] Continuous today/month reconciliation
# (services/mqtt/telemetry.py._persist_telemetry) — the backend-side half of
# the offline-consumption detector; see firmware/main/tapo_protocol.c's
# tapo_plug_reconcile_idle_baseline for the firmware's own one-shot report of
# the same signal (tested separately below, under "firmware alarm parsing").
# ---------------------------------------------------------------------------


def _plug_with_baseline(prev_today, prev_month):
    plug = _owned_plug()
    plug.last_today_energy_kwh = prev_today
    plug.last_month_energy_kwh = prev_month
    return plug


@pytest.mark.asyncio
async def test_persist_telemetry_unmetered_consumption_detected_on_idle_frame():
    """A jump in today/month energy with NO active session covering the plug
    must raise an UNMETERED_CONSUMPTION GatewayEvent -- this is what catches
    the owner-reported incident: the very first frame after a gateway that
    was fully offline reconnects."""
    MQTTManager._instance = None
    plug = _plug_with_baseline(1.0, 10.0)
    session = _FakeSession([
        _FakeResult(scalar=plug),   # plug lookup
        _FakeResult(scalar=None),   # no ACTIVE session on the plug
    ])
    mgr = MQTTManager(db_session_factory=lambda: session)
    mgr._persist_gateway_event = AsyncMock()

    await mgr._persist_telemetry(
        "gw-1", 5, 0.0, 0.0, session_id=None, sample=None, relay_on=False,
        today_kwh=1.05, month_kwh=10.05,
    )

    assert session.committed is True
    assert plug.last_today_energy_kwh == 1.05
    assert plug.last_month_energy_kwh == 10.05
    mgr._persist_gateway_event.assert_awaited_once()
    args = mgr._persist_gateway_event.await_args.args
    assert args[0] == "gw-1"
    assert args[1] == 5
    assert args[2] == "UNMETERED_CONSUMPTION"
    assert args[3] == "warning"
    assert "0.050" in args[4]  # the ~0.05 kWh estimate, formatted into the detail text

    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_persist_telemetry_unmetered_consumption_skipped_when_session_active():
    """A jump in today/month energy IS expected when an ACTIVE session
    already covers the plug -- billing already accounts for it, no alert."""
    MQTTManager._instance = None
    plug = _plug_with_baseline(1.0, 10.0)
    active = MagicMock()
    active.id = 10
    active.user_id = 1
    active.energy_kwh = 0.5
    active.peak_power_w = 0.0
    active.energy_counter_last_raw_kwh = None
    active.energy_reset_offset_kwh = 0.0
    session = _FakeSession([
        _FakeResult(scalar=plug),
        _FakeResult(scalar=active),
    ])
    mgr = MQTTManager(db_session_factory=lambda: session)
    mgr._persist_gateway_event = AsyncMock()
    mgr._maybe_auto_stop_on_exhaustion = AsyncMock()
    mgr._maybe_auto_stop_on_limits = AsyncMock()

    with patch("backend.services.pricing.reprice_session_if_due",
               AsyncMock(return_value=None)):
        await mgr._persist_telemetry(
            "gw-1", 5, 100.0, 0.5, session_id=10, sample=None, relay_on=True,
            today_kwh=1.5, month_kwh=10.5,
        )

    assert plug.last_today_energy_kwh == 1.5
    assert plug.last_month_energy_kwh == 10.5
    mgr._persist_gateway_event.assert_not_awaited()

    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_persist_telemetry_unmetered_consumption_reset_not_flagged():
    """Both counters regressing together (plug power-cycle / a full reset)
    must NOT be treated as unmetered consumption -- the baseline just
    re-seeds at the new (lower) reading, distinguishing a reset from real
    consumption the same way the firmware's own reconciliation does."""
    MQTTManager._instance = None
    plug = _plug_with_baseline(5.0, 50.0)
    session = _FakeSession([
        _FakeResult(scalar=plug),
        _FakeResult(scalar=None),
    ])
    mgr = MQTTManager(db_session_factory=lambda: session)
    mgr._persist_gateway_event = AsyncMock()

    await mgr._persist_telemetry(
        "gw-1", 5, 0.0, 0.0, session_id=None, sample=None, relay_on=False,
        today_kwh=0.1, month_kwh=0.2,
    )

    assert plug.last_today_energy_kwh == 0.1
    assert plug.last_month_energy_kwh == 0.2
    mgr._persist_gateway_event.assert_not_awaited()

    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_persist_telemetry_unmetered_consumption_today_rollover_falls_back_to_month():
    """today_energy alone regressing (midnight rollover mid-gap) must NOT
    suppress detection -- month_energy is still climbing and is used as the
    (slightly conservative) estimate, mirroring the firmware's fallback."""
    MQTTManager._instance = None
    plug = _plug_with_baseline(9.5, 10.0)  # today near its old max, month low
    session = _FakeSession([
        _FakeResult(scalar=plug),
        _FakeResult(scalar=None),
    ])
    mgr = MQTTManager(db_session_factory=lambda: session)
    mgr._persist_gateway_event = AsyncMock()

    # today rolled over (9.5 -> 0.2, a regression) but month kept climbing (10.0 -> 10.3).
    await mgr._persist_telemetry(
        "gw-1", 5, 0.0, 0.0, session_id=None, sample=None, relay_on=False,
        today_kwh=0.2, month_kwh=10.3,
    )

    mgr._persist_gateway_event.assert_awaited_once()
    args = mgr._persist_gateway_event.await_args.args
    assert args[2] == "UNMETERED_CONSUMPTION"
    assert "0.300" in args[4]  # month's delta (10.3 - 10.0), not today's

    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_persist_telemetry_unmetered_consumption_below_threshold_not_flagged():
    """A tiny delta (P110 standby-draw noise) below
    UNMETERED_CONSUMPTION_THRESHOLD_KWH must not alert."""
    MQTTManager._instance = None
    plug = _plug_with_baseline(1.0, 10.0)
    session = _FakeSession([
        _FakeResult(scalar=plug),
        _FakeResult(scalar=None),
    ])
    mgr = MQTTManager(db_session_factory=lambda: session)
    mgr._persist_gateway_event = AsyncMock()

    await mgr._persist_telemetry(
        "gw-1", 5, 0.0, 0.0, session_id=None, sample=None, relay_on=False,
        today_kwh=1.002, month_kwh=10.002,  # +2 Wh, under the 10 Wh default threshold
    )

    mgr._persist_gateway_event.assert_not_awaited()
    assert plug.last_today_energy_kwh == 1.002  # baseline still advances

    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_persist_telemetry_unmetered_consumption_first_frame_seeds_baseline():
    """No prior baseline (plug.last_*_energy_kwh both NULL, e.g. a freshly
    provisioned plug) -- nothing to compare against yet, so the first frame
    just seeds it and never alerts."""
    MQTTManager._instance = None
    plug = _plug_with_baseline(None, None)
    session = _FakeSession([
        _FakeResult(scalar=plug),
        _FakeResult(scalar=None),
    ])
    mgr = MQTTManager(db_session_factory=lambda: session)
    mgr._persist_gateway_event = AsyncMock()

    await mgr._persist_telemetry(
        "gw-1", 5, 0.0, 0.0, session_id=None, sample=None, relay_on=False,
        today_kwh=3.0, month_kwh=30.0,
    )

    assert plug.last_today_energy_kwh == 3.0
    assert plug.last_month_energy_kwh == 30.0
    mgr._persist_gateway_event.assert_not_awaited()

    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_persist_telemetry_unmetered_consumption_absent_fields_skip_reconciliation():
    """A frame that carries neither today_kwh nor month_kwh (older firmware,
    or a plug model that doesn't report them) must not touch the baseline or
    alert at all -- not even a bogus 0.0 comparison."""
    MQTTManager._instance = None
    plug = _plug_with_baseline(1.0, 10.0)
    session = _FakeSession([
        _FakeResult(scalar=plug),
        _FakeResult(scalar=None),
    ])
    mgr = MQTTManager(db_session_factory=lambda: session)
    mgr._persist_gateway_event = AsyncMock()

    await mgr._persist_telemetry(
        "gw-1", 5, 0.0, 0.0, session_id=None, sample=None, relay_on=False,
    )  # today_kwh/month_kwh both default to None

    assert plug.last_today_energy_kwh == 1.0    # untouched
    assert plug.last_month_energy_kwh == 10.0   # untouched
    mgr._persist_gateway_event.assert_not_awaited()

    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_persist_telemetry_unmetered_consumption_cooldown_suppresses_repeat_alert():
    """A second discrepancy on the SAME plug within UNMETERED_ALERT_COOLDOWN_SEC
    must not re-alert (avoids notification spam on an ongoing live drift); a
    fresh episode still raises promptly once the cooldown lapses (not tested
    here — would need monotonic-clock control — but the per-plug dict + a
    real elapsed-time gate is exercised structurally by this single-window
    check)."""
    MQTTManager._instance = None
    plug = _plug_with_baseline(1.0, 10.0)
    session = _FakeSession([
        _FakeResult(scalar=plug), _FakeResult(scalar=None),   # frame 1
        _FakeResult(scalar=plug), _FakeResult(scalar=None),   # frame 2
    ])
    mgr = MQTTManager(db_session_factory=lambda: session)
    mgr._persist_gateway_event = AsyncMock()

    await mgr._persist_telemetry("gw-1", 5, 0.0, 0.0, today_kwh=1.05, month_kwh=10.05)
    await mgr._persist_telemetry("gw-1", 5, 0.0, 0.0, today_kwh=1.10, month_kwh=10.10)

    assert mgr._persist_gateway_event.await_count == 1
    MQTTManager._instance = None


# ---------------------------------------------------------------------------
# [Unmetered consumption] Firmware's own one-shot offline report + the CPO
# bell-notify fan-out (services/mqtt/alarms.py) — the other half of the
# detector, and the delivery path shared by BOTH halves.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unmetered_consumption_alarm_detail_carries_kwh_estimate():
    """The firmware's UNMETERED_CONSUMPTION alarm carries a `kwh` estimate
    (plus today_kwh/month_kwh) that _handle_gateway_alarm must fold into a
    human-readable detail string (not just the static _EVENT_DETAIL fallback)."""
    MQTTManager._instance = None
    loop = asyncio.get_running_loop()
    mgr = MQTTManager(db_session_factory=lambda: None, event_loop=loop)

    captured = {}

    async def fake_persist(gateway_id, plug_id, event_type, severity, detail):
        captured.update(gateway_id=gateway_id, plug_id=plug_id,
                        event_type=event_type, severity=severity, detail=detail)

    mgr._persist_gateway_event = fake_persist

    mgr._handle_gateway_alarm("gw-9", {
        "error": "UNMETERED_CONSUMPTION", "plug_id": 5,
        "kwh": 1.234, "today_kwh": 2.5, "month_kwh": 12.5,
    })
    await asyncio.sleep(0.05)

    assert captured.get("event_type") == "UNMETERED_CONSUMPTION"
    assert captured.get("severity") == "warning"
    assert captured.get("plug_id") == 5
    assert "1.234" in captured.get("detail", "")

    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_unmetered_consumption_alarm_falls_back_to_static_detail_without_kwh():
    """A malformed/future payload missing `kwh` still gets SOME detail text
    (the static _EVENT_DETAIL fallback) rather than None."""
    MQTTManager._instance = None
    loop = asyncio.get_running_loop()
    mgr = MQTTManager(db_session_factory=lambda: None, event_loop=loop)

    captured = {}

    async def fake_persist(gateway_id, plug_id, event_type, severity, detail):
        captured.update(detail=detail)

    mgr._persist_gateway_event = fake_persist
    mgr._handle_gateway_alarm("gw-9", {"error": "UNMETERED_CONSUMPTION", "plug_id": 5})
    await asyncio.sleep(0.05)

    assert captured.get("detail")  # non-empty fallback text

    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_persist_gateway_event_notifies_cpos_for_unmetered_consumption():
    """_persist_gateway_event must bell-notify every CPO of the gateway's
    tenant for UNMETERED_CONSUMPTION (mirrors the orphan_off CPO-notify
    pattern in services/mqtt/status.py). Both call sites (the firmware's own
    alarm AND the backend's continuous check) funnel through this one
    function, so this single test covers the delivery path for both."""
    MQTTManager._instance = None
    gw = MagicMock()
    gw.tenant_id = 7

    owned_plug = MagicMock()
    owned_plug.gateway_id = "gw-9"  # plug 5 belongs to the publishing gateway
    plug_name_result = MagicMock()
    plug_name_result.scalar_one_or_none.return_value = "Sim Plug 1"
    cpo_ids_result = MagicMock()
    cpo_ids_result.scalars.return_value.all.return_value = [101, 102]

    session = _FakeSession([
        _FakeResult(scalar=gw),              # gateway lookup (event persist)
        _FakeResult(scalar=owned_plug),      # plug ownership check (event persist)
        plug_name_result,                    # plug-name lookup (CPO notify)
        cpo_ids_result,                      # CPO id lookup (CPO notify)
    ])
    mgr = MQTTManager(db_session_factory=lambda: session)

    notify_mock = AsyncMock()
    with patch("backend.services.socketio_manager.emit_gateway_alarm", AsyncMock()), \
         patch("backend.services.notifications.notify", notify_mock):
        await mgr._persist_gateway_event(
            "gw-9", 5, "UNMETERED_CONSUMPTION", "warning",
            "Plug consumed an estimated 1.234 kWh with no billed session covering it.",
        )

    assert notify_mock.await_count == 2
    notified_users = {c.args[0] for c in notify_mock.await_args_list}
    assert notified_users == {101, 102}
    for c in notify_mock.await_args_list:
        assert c.args[1] == "unmetered_consumption"
        assert c.kwargs.get("severity") == "warning"
        assert c.kwargs.get("plug_id") == 5

    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_persist_gateway_event_does_not_notify_cpos_for_other_event_types():
    """Only UNMETERED_CONSUMPTION triggers the CPO bell-notify fan-out — a
    routine safety alarm must not gain a second notification channel."""
    MQTTManager._instance = None
    db = _AlarmDB()
    mgr = MQTTManager(db_session_factory=lambda: db)
    mgr._finalize_session_after_cutoff = AsyncMock()
    mgr._auto_enter_maintenance = AsyncMock()

    notify_mock = AsyncMock()
    with patch("backend.services.socketio_manager.emit_gateway_alarm", AsyncMock()), \
         patch("backend.services.notifications.notify", notify_mock):
        await mgr._persist_gateway_event("gw-9", 5, "THERMAL_CUTOFF", "critical", None)

    notify_mock.assert_not_awaited()
    MQTTManager._instance = None


