"""
Tests for driver notifications: the notify() service (persist + Socket.io +
push gating), dead-subscription pruning, the MQTT emit points (low-balance
warn-once, gateway-offline fan-out, safety-cutoff finalize), and the router
wiring.
"""
import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import backend.services.notifications as notif_mod
from backend.services.mqtt_manager import MQTTManager


# ---------------------------------------------------------------- fakes ----

class _FakeSession:
    """Async-context DB session that records added rows and assigns ids."""
    def __init__(self, execute_results=None):
        self.added = []
        self.committed = False
        self._results = list(execute_results or [])
        self.executed = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def add(self, row):
        row.id = 101
        self.added.append(row)

    async def commit(self):
        self.committed = True

    async def refresh(self, row):
        # Mimic the post-commit refresh pulling server defaults.
        from datetime import datetime, timezone
        if getattr(row, "created_at", None) is None:
            row.created_at = datetime.now(timezone.utc)
        if getattr(row, "read", None) is None:
            row.read = False

    async def execute(self, stmt, *a, **k):
        self.executed.append(stmt)
        if self._results:
            return self._results.pop(0)
        return MagicMock()


class _ScalarsResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        m = MagicMock()
        m.all.return_value = self._rows
        return m

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return self._rows


class _UserDB:
    """Mimics test_mqtt_manager's _FakeDB: execute() yields one scalar row."""
    def __init__(self, row):
        self._row = row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, *_a, **_k):
        r = MagicMock()
        r.scalar_one_or_none.return_value = self._row
        r.all.return_value = []
        return r


# ------------------------------------------------------------- notify() ----

@pytest.mark.asyncio
async def test_notify_persists_emits_and_returns_payload():
    fake = _FakeSession()
    emit = AsyncMock()
    with patch.object(notif_mod, "async_session_factory", lambda: fake), \
         patch("backend.services.socketio_manager.emit_notification", emit), \
         patch.object(notif_mod, "VAPID_PRIVATE_KEY", ""):
        payload = await notif_mod.notify(
            5, "session_stopped", "Charging complete", "0.5 kWh billed",
            severity="info", plug_id=2, session_id=9,
        )

    assert fake.committed
    assert len(fake.added) == 1
    row = fake.added[0]
    assert (row.user_id, row.type, row.plug_id, row.session_id) == (5, "session_stopped", 2, 9)
    assert payload["title"] == "Charging complete"
    assert payload["read"] is False
    assert payload["created_at"] is not None  # refresh pulled the server default
    emit.assert_awaited_once()
    assert emit.await_args.args[0] == 5


@pytest.mark.asyncio
async def test_notify_never_raises_on_persist_failure():
    class _Boom:
        async def __aenter__(self):
            raise RuntimeError("db down")

        async def __aexit__(self, *a):
            return False

    with patch.object(notif_mod, "async_session_factory", lambda: _Boom()):
        result = await notif_mod.notify(1, "t", "title", "body")
    assert result is None  # swallowed, not raised


@pytest.mark.asyncio
async def test_notify_truncates_oversized_fields():
    fake = _FakeSession()
    with patch.object(notif_mod, "async_session_factory", lambda: fake), \
         patch("backend.services.socketio_manager.emit_notification", AsyncMock()), \
         patch.object(notif_mod, "VAPID_PRIVATE_KEY", ""):
        await notif_mod.notify(1, "x" * 100, "t" * 300, "b" * 900)
    row = fake.added[0]
    assert len(row.type) == 32 and len(row.title) == 120 and len(row.body) == 500


@pytest.mark.asyncio
async def test_push_disabled_without_vapid_key():
    """No VAPID key → _push_to_user returns before touching the DB."""
    factory = MagicMock(side_effect=AssertionError("DB must not be queried"))
    with patch.object(notif_mod, "VAPID_PRIVATE_KEY", ""), \
         patch.object(notif_mod, "async_session_factory", factory):
        await notif_mod._push_to_user(1, {"title": "t"})


@pytest.mark.asyncio
async def test_push_prunes_gone_subscriptions():
    """A subscription the push service reports 404/410 for is deleted."""
    sub = MagicMock(id=7, endpoint="https://push/x", p256dh="k", auth="a", user_id=1)
    list_result = _ScalarsResult([sub])
    read_db = _FakeSession([list_result])
    delete_db = _FakeSession()
    dbs = iter([read_db, delete_db])

    with patch.object(notif_mod, "VAPID_PRIVATE_KEY", "fake-key"), \
         patch.object(notif_mod, "async_session_factory", lambda: next(dbs)), \
         patch.object(notif_mod, "_send_one_push", return_value=True):
        await notif_mod._push_to_user(1, {"title": "t"})

    assert delete_db.committed, "dead subscription was not deleted"


# ------------------------------------------------- low-balance warn-once ----

@pytest.mark.asyncio
async def test_low_balance_warns_once_then_not_again():
    """Crossing the warn fraction notifies exactly once per session and does
    not finalize; a second reading above the threshold stays silent."""
    MQTTManager._instance = None
    user = MagicMock()
    user.coin_balance = Decimal("100")
    mgr = MQTTManager(db_session_factory=lambda: _UserDB(user))

    notify_mock = AsyncMock()
    finalize_mock = AsyncMock()
    with patch("backend.services.notifications.notify", notify_mock), \
         patch("backend.services.session_lifecycle.finalize_charging_session", finalize_mock):
        # 17 kWh * 5 coins = 85 >= 80% of 100 → warn
        await mgr._maybe_auto_stop_on_exhaustion(session_id=7, user_id=3, energy_kwh=17.0)
        await mgr._maybe_auto_stop_on_exhaustion(session_id=7, user_id=3, energy_kwh=18.0)

    assert notify_mock.await_count == 1
    assert notify_mock.await_args.args[1] == "low_balance"
    finalize_mock.assert_not_awaited()
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_low_balance_no_warning_below_threshold():
    MQTTManager._instance = None
    user = MagicMock()
    user.coin_balance = Decimal("100")
    mgr = MQTTManager(db_session_factory=lambda: _UserDB(user))

    notify_mock = AsyncMock()
    with patch("backend.services.notifications.notify", notify_mock):
        # 10 kWh * 5 = 50 < 80 → silent
        await mgr._maybe_auto_stop_on_exhaustion(session_id=7, user_id=3, energy_kwh=10.0)

    notify_mock.assert_not_awaited()
    MQTTManager._instance = None


# ------------------------------------------------- gateway offline notify ---

@pytest.mark.asyncio
async def test_gateway_offline_notifies_each_active_driver():
    MQTTManager._instance = None
    rows = [(11, 3, 1, "Plug A"), (12, 4, 2, "Plug B")]

    class _RowsDB(_UserDB):
        async def execute(self, *_a, **_k):
            r = MagicMock()
            r.all.return_value = rows
            return r

    mgr = MQTTManager(db_session_factory=lambda: _RowsDB(None))
    notify_mock = AsyncMock()
    with patch("backend.services.notifications.notify", notify_mock):
        await mgr._notify_drivers_gateway_offline("gw-1")

    assert notify_mock.await_count == 2
    notified_users = [c.args[0] for c in notify_mock.await_args_list]
    assert notified_users == [3, 4]
    assert all(c.args[1] == "charger_offline" for c in notify_mock.await_args_list)
    MQTTManager._instance = None


# ------------------------------------------------ safety-cutoff finalize ----

@pytest.mark.asyncio
@pytest.mark.parametrize("event_type,should_finalize", [
    ("THERMAL_CUTOFF", True),
    ("OVERCURRENT_CUTOFF", True),
    ("OVERCURRENT_CAP", True),     # soft cap trip also finalizes (firmware already stopped)
    ("UNAUTHORIZED_ON", False),   # no session by definition
    ("OTA_STARTED", False),
])
async def test_cutoff_alarm_finalizes_active_session(event_type, should_finalize):
    MQTTManager._instance = None
    gw = MagicMock()
    gw.tenant_id = 1

    class _SeqDB(_UserDB):
        """gateway lookup → event insert → session-id lookup → finalize db."""
        def __init__(self):
            super().__init__(gw)
            self.added = []

        def add(self, row):
            row.id = 55
            row.created_at = None
            self.added.append(row)

        async def execute(self, *_a, **_k):
            r = MagicMock()
            r.scalar_one_or_none.return_value = gw if not self.added else 42
            return r

        async def commit(self):
            pass

    db = _SeqDB()
    mgr = MQTTManager(db_session_factory=lambda: db)
    finalize_mock = AsyncMock(return_value={"status": "completed"})
    with patch("backend.services.session_lifecycle.finalize_charging_session", finalize_mock), \
         patch("backend.services.socketio_manager.emit_gateway_alarm", AsyncMock()):
        await mgr._persist_gateway_event("gw-1", 2, event_type, "critical", None)

    assert finalize_mock.called is should_finalize
    if should_finalize:
        reason = finalize_mock.call_args.kwargs["reason"]
        if event_type == "OVERCURRENT_CAP":
            assert "current cap exceeded" in reason   # soft cap → own notification title
        else:
            assert "safety cutoff" in reason
    MQTTManager._instance = None


# ---------------------------------------------------------------- wiring ----

def test_notification_routes_registered():
    from backend.routers.notifications import router
    paths = {r.path for r in router.routes}
    assert {"/api/notifications",
            "/api/notifications/{notification_id}/read",
            "/api/notifications/read-all",
            "/api/notifications/push/public-key",
            "/api/notifications/push/subscribe"} <= paths


def test_finalize_notifies_on_stop_reasons():
    """The stop-notification title mapping covers every finalize reason."""
    import inspect
    from backend.services import session_lifecycle
    src = inspect.getsource(session_lifecycle.finalize_charging_session)
    assert "session_stopped" in src
    for marker in ("balance exhausted", "telemetry lost", "safety cutoff", "current cap exceeded"):
        assert marker in src, f"finalize no longer maps reason {marker!r}"
