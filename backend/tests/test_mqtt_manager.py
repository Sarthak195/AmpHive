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

    async def fake_persist(gateway_id, plug_id, watts, kwh, session_id=None, sample=None):
        captured.update(gateway_id=gateway_id, plug_id=plug_id, watts=watts,
                        kwh=kwh, session_id=session_id)

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
    succeeds; add()/commit() are no-ops so the function runs past the
    persist step to reach the finalize/auto-maintenance sequencing below it.
    """
    def __init__(self):
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, *_a, **_k):
        r = MagicMock()
        gw = MagicMock()
        gw.tenant_id = 1
        r.scalar_one_or_none.return_value = gw
        return r

    def add(self, row):
        row.id = 1
        row.created_at = None

    async def commit(self):
        self.committed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type,plug_id,should_trigger", [
    ("THERMAL_CUTOFF", 5, True),
    ("OVERCURRENT_CUTOFF", 5, True),
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
