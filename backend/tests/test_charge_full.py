"""
[Charge complete] Tests for the "car looks fully charged" detector and its
wiring (services/mqtt/charge_full.py, MQTTTelemetryMixin._on_charge_full).

DB-free: the detector is a pure state machine fed explicit timestamps; the
telemetry/stop wiring uses the fake-session and mocked-finalize patterns from
test_session_limits.py / test_mqtt_manager.py.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services.mqtt.charge_full import CHARGE_COMPLETE_REASON, ChargeFullDetector

T0 = datetime(2026, 11, 17, 22, 0, tzinfo=timezone.utc)   # session start
PLUG, SID = 5, 42


@pytest.fixture(autouse=True)
def _knobs(monkeypatch):
    """Pin the thresholds (defaults today, but env-overridable) and the
    reaper's stale window the gap rule uses."""
    import backend.services.mqtt_manager as mm
    import backend.services.session_reaper as reaper

    monkeypatch.setattr(mm, "CHARGE_FULL_ACTIVE_W", 200.0)
    monkeypatch.setattr(mm, "CHARGE_FULL_IDLE_W", 30.0)
    monkeypatch.setattr(mm, "CHARGE_FULL_GRACE_MIN", 5.0)
    monkeypatch.setattr(mm, "CHARGE_FULL_MINUTES", 10.0)
    monkeypatch.setattr(reaper, "STALE_TIMEOUT_SEC", 300.0)


def _feed(det, samples, peak=1500.0, sid=SID, started=T0):
    """samples: [(minutes_after_start, watts)] -> list of observe() results.
    `peak` is the session's persisted peak_power_w."""
    return [
        det.observe(PLUG, sid, started + timedelta(minutes=m), w, max(peak, w), started)
        for m, w in samples
    ]


def _every_minute(start_min, end_min, watts):
    return [(m, watts) for m in range(start_min, end_min + 1)]


def test_fires_once_after_sustained_low_draw():
    det = ChargeFullDetector()
    # Charges at 1.5 kW for 30 min, then drops to ~0 W.
    out = _feed(det, _every_minute(0, 30, 1500.0) + _every_minute(31, 60, 2.0))
    # Idle timer starts at minute 31 and needs 10 min -> fires at minute 41.
    assert out.index(True) == 41
    # Single fire: later idle samples never fire again.
    assert out.count(True) == 1


def test_never_fires_when_power_never_reached_the_active_threshold():
    """A car that never really drew power (EVSE standby only) is not 'full'."""
    det = ChargeFullDetector()
    out = _feed(det, _every_minute(0, 120, 5.0), peak=150.0)
    assert True not in out


def test_grace_window_low_samples_do_not_count():
    """Low draw inside the startup grace (car handshaking) is ignored even if
    the session already peaked; the timer only starts once grace is over."""
    det = ChargeFullDetector()
    out = _feed(det, [(0, 1500.0)] + _every_minute(1, 30, 0.0))
    # Grace ends at minute 5, timer starts at 5, fires at 15 (not at 11).
    assert out.index(True) == 15


def test_a_sample_at_or_above_idle_threshold_resets_the_timer():
    det = ChargeFullDetector()
    samples = (
        _every_minute(0, 20, 1500.0)
        + _every_minute(21, 29, 0.0)      # 8 min idle...
        + [(30, 30.0)]                    # ...a sample AT the threshold resets
        + _every_minute(31, 50, 0.0)
    )
    out = _feed(det, samples)
    assert out.index(True) == 41          # 31 + 10, not 21 + 10


def test_a_gap_longer_than_the_stale_window_resets_the_timer():
    """Gateway offline: no samples for > STALE_TIMEOUT_SEC. Nothing is known
    about that stretch, so the idle timer restarts on the next sample."""
    det = ChargeFullDetector()
    samples = (
        _every_minute(0, 20, 1500.0)
        + _every_minute(21, 25, 0.0)      # timer starts at 21
        + [(40, 0.0)]                     # 15-min gap (> 5 min): restart at 40
        + _every_minute(41, 55, 0.0)
    )
    out = _feed(det, samples)
    assert samples[out.index(True)][0] == 50   # 40 + 10, not 21 + 10
    assert det._plugs[PLUG].idle_since == T0 + timedelta(minutes=40)


def test_missing_samples_are_never_zero_filled():
    """Time only advances on samples that arrived: one low sample and then
    silence never fires, however long the silence."""
    det = ChargeFullDetector()
    out = _feed(det, _every_minute(0, 20, 1500.0) + [(21, 0.0)])
    assert True not in out


def test_relay_off_frame_interrupts_the_timer():
    det = ChargeFullDetector()
    _feed(det, _every_minute(0, 20, 1500.0) + _every_minute(21, 29, 0.0))
    det.interrupt(PLUG, SID)
    out = _feed(det, _every_minute(30, 45, 0.0))
    assert out.index(True) == 10          # minute 40 = 30 + 10


def test_new_session_on_the_plug_starts_fresh():
    det = ChargeFullDetector()
    assert True in _feed(det, _every_minute(0, 20, 1500.0) + _every_minute(21, 40, 0.0))
    t1 = T0 + timedelta(hours=2)
    out = _feed(det, _every_minute(0, 20, 1500.0) + _every_minute(21, 40, 0.0),
                sid=SID + 1, started=t1)
    assert out.count(True) == 1


def test_persisted_peak_counts_as_having_charged_after_a_restart():
    """After a backend restart the detector is empty, but the session's
    persisted peak_power_w still proves it charged: low draw alone can fire."""
    det = ChargeFullDetector()
    out = _feed(det, _every_minute(60, 75, 0.0), peak=3300.0)
    assert out.index(True) == 10


# --------------------------------------------------------------------------
# Telemetry wiring
# --------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("payload_watts,expected", [(None, False), (0.0, True), (1200.0, True)])
async def test_handler_flags_frames_without_a_watts_reading(payload_watts, expected):
    """The handler defaults a missing `watts` to 0.0; it must tell the persist
    path that frame is not a real power sample (else it reads as an idle car)."""
    from backend.services.mqtt_manager import MQTTManager

    MQTTManager._instance = None
    mgr = MQTTManager(db_session_factory=lambda: None, event_loop=asyncio.get_running_loop())
    captured = {}

    async def fake_persist(*_a, **kw):
        captured.update(kw)

    mgr._persist_telemetry = fake_persist
    payload = {"plug_id": 3, "kwh": 0.5, "status": "occupied", "relay": True}
    if payload_watts is not None:
        payload["watts"] = payload_watts
    mgr._handle_gateway_telemetry("gw-1", payload)
    await asyncio.sleep(0.05)

    assert captured.get("watts_reported") is expected
    MQTTManager._instance = None


class _FakeResult:
    def __init__(self, scalar=None):
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._scalar


class _FakeSession:
    def __init__(self, results):
        self._results = iter(results)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, *_a, **_k):
        return next(self._results)

    async def commit(self):
        pass


def _session_row(auto_stop):
    row = MagicMock()
    row.id = SID
    row.user_id = 3
    row.rate_coins_per_kwh = Decimal("5.00")
    row.hold_coins = Decimal("500.00")
    row.max_kwh = None
    row.max_duration_seconds = None
    row.started_at = T0
    row.energy_kwh = 5.0
    row.peak_power_w = 1500.0
    row.rate_valid_until = None
    row.settled_cost_coins = None
    row.rate_segment_start_kwh = None
    row.energy_counter_last_raw_kwh = 5.0
    row.energy_reset_offset_kwh = 0.0
    row.auto_stop_when_full = auto_stop
    return row


async def _persist_frame(mgr, row, minute, watts, relay_on=True, watts_reported=True,
                         is_offline=False):
    plug = MagicMock()
    plug.gateway_id = "gw-1"
    plug.last_telemetry_at = None
    mgr.db_session_factory = lambda: _FakeSession([_FakeResult(plug), _FakeResult(row)])
    sample = {"recorded_at": T0 + timedelta(minutes=minute)}
    await mgr._persist_telemetry(
        "gw-1", PLUG, watts, 5.0, SID, sample, relay_on=relay_on, is_offline=is_offline,
        watts_reported=watts_reported,
    )


def _manager():
    from backend.services.mqtt_manager import MQTTManager

    MQTTManager._instance = None
    mgr = MQTTManager(db_session_factory=lambda: None)
    mgr._maybe_auto_stop_on_exhaustion = AsyncMock()
    mgr._maybe_auto_stop_on_limits = AsyncMock()
    mgr._on_charge_full = AsyncMock()
    return mgr


@pytest.mark.asyncio
async def test_persist_path_fires_once_from_real_live_samples():
    mgr = _manager()
    row = _session_row(auto_stop=True)
    for m in range(10, 31):
        await _persist_frame(mgr, row, m, 0.0)
    mgr._on_charge_full.assert_awaited_once_with(SID, 3, PLUG, True)


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [
    {"watts_reported": False},   # frame had no power reading
    {"is_offline": True},        # buffered replay while the gateway was offline
])
async def test_persist_path_ignores_non_real_samples(kwargs):
    mgr = _manager()
    row = _session_row(auto_stop=True)
    for m in range(10, 31):
        await _persist_frame(mgr, row, m, 0.0, **kwargs)
    mgr._on_charge_full.assert_not_awaited()
    assert PLUG not in mgr._charge_full._plugs


@pytest.mark.asyncio
async def test_on_charge_full_notifies_once_and_stops_through_the_driver_stop_path():
    """Opted in: one notify(), then finalize_charging_session exactly as the
    driver's stop route calls it (own user id) plus the charge-complete reason."""
    from backend.services.mqtt_manager import MQTTManager

    MQTTManager._instance = None
    store = MagicMock()
    db = MagicMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=db)
    cm.__aexit__ = AsyncMock(return_value=False)
    mgr = MQTTManager(telemetry_store=store, db_session_factory=lambda: cm)

    finalize = AsyncMock(return_value={"energy_kwh": 5.0, "coins_spent": 25.0})
    with patch("backend.services.session_lifecycle.finalize_charging_session", finalize), \
         patch("backend.services.notifications.notify", AsyncMock()) as notify:
        await mgr._on_charge_full(SID, 3, PLUG, True)

    store.mark_charge_full.assert_called_once_with(PLUG)
    notify.assert_awaited_once()
    assert notify.await_args.args[:3] == (3, "charge_full", "Your car looks fully charged")
    finalize.assert_awaited_once_with(db, SID, expected_user_id=3, reason=CHARGE_COMPLETE_REASON)
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_on_charge_full_without_opt_in_only_notifies():
    from backend.services.mqtt_manager import MQTTManager

    MQTTManager._instance = None
    mgr = MQTTManager(telemetry_store=MagicMock(), db_session_factory=lambda: None)
    finalize = AsyncMock()
    with patch("backend.services.session_lifecycle.finalize_charging_session", finalize), \
         patch("backend.services.notifications.notify", AsyncMock()) as notify:
        await mgr._on_charge_full(SID, 3, PLUG, False)

    notify.assert_awaited_once()
    finalize.assert_not_awaited()
    MQTTManager._instance = None


def test_reason_does_not_collide_with_finalize_title_keywords():
    for word in ("exhausted", "limit reached", "reserved", "telemetry lost", "]"):
        assert word not in CHARGE_COMPLETE_REASON


def test_telemetry_store_snapshot_carries_charge_full_until_session_ends():
    from backend.services.telemetry import TelemetryStore

    store = TelemetryStore()
    store.start_session(77)
    store.update(77, 1500.0, 6.5, 1.0)
    assert store.get_latest(77).charge_full is False
    store.mark_charge_full(77)
    assert store.get_latest(77).charge_full is True
    store.update(77, 0.0, 0.0, 1.0)
    assert store.get_latest(77).to_dict()["charge_full"] is True
    store.end_session(77)
    store.start_session(77)
    store.update(77, 1500.0, 6.5, 0.1)
    assert store.get_latest(77).charge_full is False
    store.end_session(77)


# --------------------------------------------------------------------------
# Opt-in carried to the session
# --------------------------------------------------------------------------

def test_start_and_queue_requests_default_the_opt_in_off():
    from backend.schemas import QueueChargeRequest, SessionStartRequest

    assert SessionStartRequest(plug_id=1).auto_stop_when_full is False
    assert SessionStartRequest(plug_id=1, auto_stop_when_full=True).auto_stop_when_full is True
    assert QueueChargeRequest(plug_id=1, auto_stop_when_full=True).auto_stop_when_full is True


@pytest.mark.asyncio
@pytest.mark.parametrize("opt_in,stored", [(True, True), (False, None)])
async def test_begin_active_session_persists_the_opt_in(opt_in, stored):
    from backend.database.models import ChargingSession
    from backend.services import session_start

    db = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    user = MagicMock(id=3, coin_balance=Decimal("500"))
    plug = MagicMock(id=PLUG, gateway_id="gw-1", local_ip="10.0.0.2")
    gateway = MagicMock(tenant_id=1)
    mqtt = MagicMock()
    mqtt.send_plug_command.return_value = True

    with patch.object(session_start, "check_circuit_admission", AsyncMock()), \
         patch.object(session_start, "resolve_rate_window",
                      AsyncMock(return_value=(Decimal("5.00"), None))), \
         patch.object(session_start, "available_balance", AsyncMock(return_value=Decimal("500"))), \
         patch.object(session_start, "max_rate_over_window",
                      AsyncMock(return_value=Decimal("5.00"))), \
         patch.object(session_start, "set_plug_telemetry_interval", AsyncMock()), \
         patch.object(session_start.state, "mqtt_manager", mqtt), \
         patch.object(session_start.state, "telemetry_store", MagicMock()), \
         patch("backend.services.socketio_manager.emit_plug_status", AsyncMock()), \
         patch("backend.services.caps.effective_plug_cap", return_value=16.0):
        session = await session_start.begin_active_session(
            db, user, plug, gateway, max_kwh=None, max_duration_seconds=None,
            auto_stop_when_full=opt_in,
        )

    assert isinstance(session, ChargingSession)
    assert session.auto_stop_when_full is stored
