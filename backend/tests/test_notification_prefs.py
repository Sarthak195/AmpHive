"""
[Notification prefs C7] Push toggles, quiet hours and the three new session
notices. DB-free: the gate is a pure function plus a faked session factory,
the detector is a pure state machine fed explicit timestamps, and the
telemetry wiring reuses test_charge_full's fake-session frame helper.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

import backend.services.notifications as notif_mod
from backend.services.mqtt.session_events import SessionEventDetector
from backend.services.notification_prefs import (
    ALWAYS_PUSH_TYPES,
    SUPPRESSIBLE_TYPES,
    NotificationPrefs,
    QuietHours,
    in_quiet_hours,
    load_prefs,
    push_allowed,
)
from backend.tests.test_charge_full import (
    PLUG,
    SID,
    T0,
    _manager,
    _persist_frame,
    _session_row,
)

UTC = timezone.utc
NOON = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
IST = timezone(timedelta(hours=5, minutes=30))


def _muted_everything() -> NotificationPrefs:
    """Every toggle off and quiet hours covering the whole day but a minute."""
    return NotificationPrefs.model_validate({
        "push": {t: False for t in SUPPRESSIBLE_TYPES},
        "quiet_hours": {"enabled": True, "start": "00:00", "end": "23:59"},
    })


# ------------------------------------------------------------- allowlist ----

def test_allowlist_names_the_safety_billing_and_routine_types():
    for t in ("session_stopped", "topup_credited", "topup_refunded", "orphan_off",
              "gateway_fault", "gateway_offline", "gateway_online", "charge_full",
              "routine_started", "routine_disabled"):
        assert t in ALWAYS_PUSH_TYPES
    assert not (ALWAYS_PUSH_TYPES & SUPPRESSIBLE_TYPES)
    assert SUPPRESSIBLE_TYPES == {"session_started", "nearly_done", "power_dropped"}


@pytest.mark.parametrize("type_", sorted(ALWAYS_PUSH_TYPES))
def test_allowlisted_info_types_push_through_quiet_hours(type_):
    assert push_allowed(type_, "info", _muted_everything(), NOON, UTC)


@pytest.mark.parametrize("severity", ["warning", "critical"])
def test_warning_and_critical_push_even_for_a_muted_type(severity):
    assert push_allowed("session_started", severity, _muted_everything(), NOON, UTC)


@pytest.mark.parametrize("type_", sorted(ALWAYS_PUSH_TYPES))
def test_an_allowlisted_type_has_no_toggle_to_mute_it(type_):
    with pytest.raises(ValidationError):
        NotificationPrefs.model_validate({"push": {type_: False}})


def test_toggle_off_mutes_only_that_type():
    prefs = NotificationPrefs.model_validate({"push": {"power_dropped": False}})
    assert not push_allowed("power_dropped", "info", prefs, NOON, UTC)
    assert push_allowed("nearly_done", "info", prefs, NOON, UTC)


def test_null_or_garbage_stored_prefs_mean_defaults():
    assert load_prefs(None) == NotificationPrefs()
    assert load_prefs({"push": {"bogus": 1}}) == NotificationPrefs()
    assert push_allowed("session_started", "info", load_prefs(None), NOON, UTC)


# ----------------------------------------------------------- quiet hours ----

def _at(hhmm: str) -> datetime:
    h, m = hhmm.split(":")
    return datetime(2026, 11, 19, int(h), int(m), 30, tzinfo=UTC)


@pytest.mark.parametrize("now,quiet", [
    ("08:59", False), ("09:00", True), ("16:59", True), ("17:00", False),
])
def test_same_day_window_is_half_open(now, quiet):
    q = QuietHours(enabled=True, start="09:00", end="17:00")
    assert in_quiet_hours(q, _at(now)) is quiet


@pytest.mark.parametrize("now,quiet", [
    ("21:59", False), ("22:00", True), ("23:59", True), ("00:00", True),
    ("06:59", True), ("07:00", False), ("12:00", False),
])
def test_window_across_midnight(now, quiet):
    q = QuietHours(enabled=True, start="22:00", end="07:00")
    assert in_quiet_hours(q, _at(now)) is quiet


def test_disabled_quiet_hours_never_mute():
    assert not in_quiet_hours(QuietHours(enabled=False, start="00:00", end="23:59"), _at("12:00"))


@pytest.mark.parametrize("body", [
    {"enabled": True, "start": "22:00", "end": "22:00"},
    {"enabled": True, "start": "24:00", "end": "07:00"},
    {"enabled": True, "start": "7:00", "end": "08:00"},
    {"enabled": True, "start": "22:00", "end": "07:00", "zone": "UTC"},
])
def test_invalid_quiet_hours_are_rejected(body):
    with pytest.raises(ValidationError):
        QuietHours.model_validate(body)


def test_quiet_hours_use_the_tenant_zone():
    """17:00 UTC is 22:30 at +05:30 (the default zone's offset; a fixed
    offset so the test does not need the IANA database): quiet there, not
    in UTC."""
    prefs = NotificationPrefs(quiet_hours=QuietHours(enabled=True, start="22:00", end="07:00"))
    now = datetime(2026, 11, 19, 17, 0, tzinfo=UTC)
    assert not push_allowed("session_started", "info", prefs, now, IST)
    assert push_allowed("session_started", "info", prefs, now, UTC)


# ------------------------------------------------ notify() gate wiring ----

class _Session:
    """Async-context DB session: records added rows; execute() returns a
    result whose first() is the (prefs, tenant timezone) row."""
    def __init__(self, first=None):
        self.added = []
        self._first = first
        self.queried = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def add(self, row):
        row.id = 1
        self.added.append(row)

    async def commit(self):
        pass

    async def refresh(self, row):
        row.created_at = NOON
        row.read = False

    async def execute(self, *_a, **_k):
        self.queried = True
        r = MagicMock()
        r.first.return_value = self._first
        return r


async def _notify(type_, severity, prefs_row):
    persist, prefs_db = _Session(), _Session(prefs_row)
    dbs = iter([persist, prefs_db])
    emit, push = AsyncMock(), AsyncMock()
    with patch.object(notif_mod, "async_session_factory", lambda: next(dbs)), \
         patch.object(notif_mod, "VAPID_PRIVATE_KEY", "fake-key"), \
         patch.object(notif_mod, "_push_to_user", push), \
         patch("backend.services.socketio_manager.emit_notification", emit):
        payload = await notif_mod.notify(3, type_, "t", "b", severity=severity)
    return persist, prefs_db, emit, push, payload


@pytest.mark.asyncio
async def test_muted_type_still_writes_the_feed_row_and_live_event():
    muted = {"push": {"session_started": False}}
    persist, prefs_db, emit, push, payload = await _notify("session_started", "info", (muted, None))
    assert len(persist.added) == 1 and persist.added[0].type == "session_started"
    assert payload is not None
    emit.assert_awaited_once()
    assert prefs_db.queried
    push.assert_not_awaited()


@pytest.mark.asyncio
async def test_quiet_hours_suppress_the_push_but_not_the_feed_row():
    quiet = {"quiet_hours": {"enabled": True, "start": "00:00", "end": "23:59"}}
    with patch("backend.services.notifications.datetime") as dt:
        dt.now.return_value = NOON
        persist, _db, emit, push, _p = await _notify("nearly_done", "info", (quiet, "UTC"))
    assert len(persist.added) == 1
    emit.assert_awaited_once()
    push.assert_not_awaited()


@pytest.mark.asyncio
async def test_allowlisted_type_pushes_without_reading_prefs():
    muted = _muted_everything().model_dump()
    persist, prefs_db, _emit, push, _p = await _notify("session_stopped", "info", (muted, "UTC"))
    assert len(persist.added) == 1
    assert not prefs_db.queried
    push.assert_awaited_once()


@pytest.mark.asyncio
async def test_default_prefs_push():
    _persist, _db, _emit, push, _p = await _notify("power_dropped", "info", (None, None))
    push.assert_awaited_once()


# ------------------------------------------------- the new event detectors ----

def test_nearly_done_fires_once_at_ninety_percent():
    det = SessionEventDetector()
    out = [det.observe_energy(PLUG, SID, e, 10.0) for e in (5.0, 8.99, 9.0, 9.5, 9.9)]
    assert out == [False, False, True, False, False]


def test_nearly_done_not_on_the_frame_that_reaches_the_limit_or_without_one():
    det = SessionEventDetector()
    assert det.observe_energy(PLUG, SID, 10.0, 10.0) is False   # limit stop's receipt
    assert det.observe_energy(PLUG, SID + 1, 9.5, None) is False  # no max_kwh


def test_nearly_done_state_is_per_session():
    det = SessionEventDetector()
    assert det.observe_energy(PLUG, SID, 9.5, 10.0)
    assert det.observe_energy(PLUG, SID + 1, 9.5, 10.0)


@pytest.fixture
def _knobs(monkeypatch):
    import backend.services.mqtt_manager as mm
    import backend.services.session_reaper as reaper

    monkeypatch.setattr(mm, "CHARGE_FULL_ACTIVE_W", 200.0)
    monkeypatch.setattr(mm, "CHARGE_FULL_IDLE_W", 30.0)
    monkeypatch.setattr(reaper, "STALE_TIMEOUT_SEC", 300.0)


def _power(det, samples, peak=1500.0, sid=SID):
    """samples: [(minutes, watts)] -> observe_power results."""
    return [
        det.observe_power(PLUG, sid, T0 + timedelta(minutes=m), w, max(peak, w))
        for m, w in samples
    ]


def _minutes(a, b, w):
    return [(m, w) for m in range(a, b + 1)]


def test_power_dropped_fires_once_after_three_minutes(_knobs):
    det = SessionEventDetector()
    out = _power(det, _minutes(0, 20, 1500.0) + _minutes(21, 40, 600.0))
    assert out.index(True) == 24        # low from 21, 3 min later
    assert out.count(True) == 1


def test_power_dropped_never_fires_on_a_drop_to_idle(_knobs):
    det = SessionEventDetector()
    out = _power(det, _minutes(0, 20, 1500.0) + _minutes(21, 60, 5.0))
    assert True not in out


def test_power_dropped_never_fires_when_a_brief_dip_ends_in_idle(_knobs):
    det = SessionEventDetector()
    out = _power(det, _minutes(0, 20, 1500.0) + _minutes(21, 22, 600.0) + _minutes(23, 60, 5.0))
    assert True not in out


def test_power_dropped_needs_an_active_peak(_knobs):
    det = SessionEventDetector()
    out = _power(det, _minutes(0, 30, 80.0), peak=150.0)
    assert True not in out


def test_power_dropped_timer_resets_on_recovery_gap_and_relay_off(_knobs):
    det = SessionEventDetector()
    out = _power(det, _minutes(0, 10, 1500.0) + [(11, 600.0), (13, 600.0), (14, 1400.0),
                                                 (15, 600.0), (17, 600.0)])
    assert True not in out              # 14 recovered, so 15..17 is only 2 min
    det.interrupt(PLUG, SID)            # relay OFF frame
    assert _power(det, [(18, 600.0), (20, 600.0)]) == [False, False]
    assert _power(det, [(30, 600.0)]) == [False]   # 10 min gap > stale window restarts
    assert _power(det, [(33, 600.0)]) == [True]


# ----------------------------------------- telemetry wiring (persist path) ----

@pytest.mark.asyncio
async def test_persist_path_sends_nearly_done_and_power_dropped_once(_knobs):
    mgr = _manager()
    row = _session_row(auto_stop=False)
    row.max_kwh = 5.5                   # 5.0 kWh delivered = 91%
    with patch("backend.services.notifications.notify", AsyncMock()) as notify:
        for m in range(10, 20):
            await _persist_frame(mgr, row, m, 600.0)
    types = [c.args[1] for c in notify.await_args_list]
    assert types.count("nearly_done") == 1
    assert types.count("power_dropped") == 1
    mgr._on_charge_full.assert_not_awaited()


@pytest.mark.asyncio
async def test_persist_path_ignores_offline_replays(_knobs):
    mgr = _manager()
    row = _session_row(auto_stop=False)
    row.max_kwh = 5.5
    with patch("backend.services.notifications.notify", AsyncMock()) as notify:
        for m in range(10, 20):
            await _persist_frame(mgr, row, m, 600.0, is_offline=True)
    notify.assert_not_awaited()


# --------------------------------------------------------- session_started ----

async def _begin(publish_ok):
    from backend.services import session_start

    db = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    user = MagicMock(id=3, coin_balance=Decimal("500"))
    plug = MagicMock(id=PLUG, gateway_id="gw-1", local_ip="10.0.0.2")
    plug.name = "Bay 1"
    mqtt = MagicMock()
    mqtt.send_plug_command.return_value = publish_ok
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
         patch("backend.services.caps.effective_plug_cap", return_value=16.0), \
         patch("backend.services.notifications.notify", AsyncMock()) as notify:
        try:
            await session_start.begin_active_session(
                db, user, plug, MagicMock(tenant_id=1), max_kwh=None, max_duration_seconds=None,
            )
        except Exception:
            pass
    return notify


@pytest.mark.asyncio
async def test_session_started_sent_once_when_the_start_succeeds():
    notify = await _begin(publish_ok=True)
    notify.assert_awaited_once()
    assert notify.await_args.args[:2] == (3, "session_started")
    assert notify.await_args.kwargs["plug_id"] == PLUG


@pytest.mark.asyncio
async def test_no_session_started_when_the_on_publish_fails():
    notify = await _begin(publish_ok=False)
    notify.assert_not_awaited()


# ------------------------------------------------------------------- API ----

@pytest.mark.asyncio
async def test_get_returns_defaults_for_a_never_saved_user():
    from backend.routers.notifications import get_notification_prefs
    from backend.services.pricing import zone_name

    user = MagicMock(notification_prefs=None, tenant_id=None)
    out = await get_notification_prefs(user=user, db=MagicMock())
    assert out["push"] == {t: True for t in SUPPRESSIBLE_TYPES}
    assert out["quiet_hours"]["enabled"] is False
    assert out["timezone"] == zone_name(None)   # the pricing default zone


@pytest.mark.asyncio
async def test_put_stores_the_validated_dump():
    from backend.routers.notifications import put_notification_prefs

    db = MagicMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    db.scalar = AsyncMock(return_value="UTC")
    req = NotificationPrefs.model_validate({
        "push": {"nearly_done": False},
        "quiet_hours": {"enabled": True, "start": "23:00", "end": "06:30"},
    })
    out = await put_notification_prefs(req=req, user=MagicMock(id=3, tenant_id=1), db=db)
    stmt = db.execute.await_args.args[0]
    assert stmt.compile().params["notification_prefs"] == req.model_dump()
    db.commit.assert_awaited_once()
    assert out["push"]["nearly_done"] is False and out["timezone"] == "UTC"
