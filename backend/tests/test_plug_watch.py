"""
Tests for the one-shot "notify me when free" plug watches:

1. Endpoints (routers/plugs.py watch_plug / unwatch_plug): arm/disarm CRUD +
   idempotency (double-arm, double-disarm, the UNIQUE-race IntegrityError
   path), the shared single-plug access rule (403 for a non-member on a
   private-group plug), and the one rejected state (409 only when the plug
   is startable RIGHT NOW — available AND gateway live; watching an
   occupied/offline/maintenance plug is legitimate).
2. The fan-out service (services/plug_watch.py
   notify_watchers_plug_available): notifies each watcher (`plug_available`,
   carrying the plug_id) then deletes exactly those rows (one-shot), honors
   exclude_user_id, and NEVER raises — a db or notify() failure is swallowed
   and the shared session rolled back so the billing path stays intact.
3. Wiring: finalize_charging_session fans out with the stopping driver
   excluded and still completes when the fan-out's own DB read explodes;
   the CPO maintenance `clear` (and only `clear`) fans out too.
4. The `watching` response field on the plug list/detail endpoints (one
   extra query for the whole list — no N+1).

DB-free: mocked-AsyncSession pattern from test_plug_maintenance.py /
test_notifications.py.
"""
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from backend.database.models import PlugStatus, PlugWatch, SessionStatus
from backend.routers.plugs import (
    get_available_plugs,
    get_plug,
    unwatch_plug,
    watch_plug,
)
from backend.services.plug_watch import notify_watchers_plug_available

# ---------------------------------------------------------------- helpers ---

def _user(user_id=42):
    u = MagicMock()
    u.id = user_id
    return u


def _plug(plug_id=7, status=PlugStatus.OCCUPIED, group_id=None, name="Bay 1",
          gateway_id="gw-1"):
    p = MagicMock()
    p.id = plug_id
    p.name = name
    p.status = status
    p.group_id = group_id
    p.gateway_id = gateway_id
    p.local_ip = "10.0.0.9"
    p.current_power_w = 0.0
    p.plug_model = "tapo_p110"
    p.latitude = None
    p.longitude = None
    p.last_telemetry_at = None  # [Plug power] plug_powered = False for these tests
    # [Queued charge] No per-plug override -> inherits the tenant default.
    p.queued_charging_enabled = None
    p.auto_start_delay_min = None
    return p


def _group(group_id=5, is_public=False, name="Sunrise Apartments"):
    g = MagicMock()
    g.id = group_id
    g.is_public = is_public
    g.name = name
    return g


def _scalar_one_or_none(value):
    r = MagicMock()
    r.scalar_one_or_none.return_value = value
    return r


def _scalar_one(value):
    r = MagicMock()
    r.scalar_one.return_value = value
    return r


def _db(*results):
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=list(results))
    db.add = MagicMock()          # add() is sync on AsyncSession
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.refresh = AsyncMock()
    return db


# ------------------------------------------------- POST /watch (arm) --------

@pytest.mark.asyncio
async def test_watch_occupied_plug_creates_row():
    user = _user(42)
    plug = _plug(plug_id=7, status=PlugStatus.OCCUPIED)  # ungrouped: no access queries
    db = _db(
        _scalar_one_or_none(plug),   # plug lookup
        _scalar_one_or_none(None),   # no existing watch
    )

    res = await watch_plug(7, user, db)

    assert res == {"status": "watching", "plug_id": 7, "watching": True}
    added = db.add.call_args[0][0]
    assert isinstance(added, PlugWatch)
    assert (added.user_id, added.plug_id) == (42, 7)
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_watch_is_idempotent_when_already_watching():
    user = _user(42)
    plug = _plug(plug_id=7, status=PlugStatus.OCCUPIED)
    existing = MagicMock()  # a PlugWatch row already there
    db = _db(
        _scalar_one_or_none(plug),
        _scalar_one_or_none(existing),
    )

    res = await watch_plug(7, user, db)

    assert res["watching"] is True
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_watch_race_on_unique_constraint_treated_as_watching():
    """Two concurrent arms: the loser's INSERT hits UNIQUE(user_id, plug_id).
    Same outcome as already-watching — rollback, 200, watching: true."""
    user = _user(42)
    plug = _plug(plug_id=7, status=PlugStatus.OCCUPIED)
    db = _db(
        _scalar_one_or_none(plug),
        _scalar_one_or_none(None),   # pre-check saw nothing (the race window)
    )
    db.commit = AsyncMock(side_effect=IntegrityError("stmt", {}, Exception("dup")))

    res = await watch_plug(7, user, db)

    assert res["watching"] is True
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_watch_404_for_unknown_plug():
    db = _db(_scalar_one_or_none(None))
    with pytest.raises(HTTPException) as exc:
        await watch_plug(999, _user(), db)
    assert exc.value.status_code == 404
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_watch_403_for_non_member_on_private_group_plug():
    """Same access rule as GET /api/plugs/{id} (shared helper): a plug in a
    private group is only watchable by members."""
    user = _user(42)
    plug = _plug(plug_id=7, status=PlugStatus.OCCUPIED, group_id=5)
    db = _db(
        _scalar_one_or_none(plug),                    # plug lookup
        _scalar_one_or_none(_group(is_public=False)), # its private group
        _scalar_one_or_none(None),                    # no membership
    )

    with pytest.raises(HTTPException) as exc:
        await watch_plug(7, user, db)

    assert exc.value.status_code == 403
    assert "private group" in exc.value.detail.lower()
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_watch_allowed_for_member_of_private_group():
    user = _user(42)
    plug = _plug(plug_id=7, status=PlugStatus.OCCUPIED, group_id=5)
    db = _db(
        _scalar_one_or_none(plug),
        _scalar_one_or_none(_group(is_public=False)),
        _scalar_one_or_none(MagicMock()),  # membership exists
        _scalar_one_or_none(None),         # no existing watch
    )

    res = await watch_plug(7, user, db)

    assert res["watching"] is True
    db.add.assert_called_once()
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_watch_409_when_plug_is_startable_right_now():
    """The single rejected state: AVAILABLE with a live gateway — there is
    nothing to wait for."""
    user = _user(42)
    plug = _plug(plug_id=7, status=PlugStatus.AVAILABLE)
    gateway = MagicMock()
    db = _db(
        _scalar_one_or_none(plug),
        _scalar_one_or_none(gateway),  # gateway lookup for the liveness gate
    )

    with patch("backend.routers.plugs.gateway_is_live", return_value=True):
        with pytest.raises(HTTPException) as exc:
            await watch_plug(7, user, db)

    assert exc.value.status_code == 409
    db.add.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [
    PlugStatus.OCCUPIED, PlugStatus.OFFLINE, PlugStatus.MAINTENANCE,
])
async def test_watch_allowed_for_every_non_available_status(status):
    """Don't over-restrict: occupied, offline, and maintenance plugs are all
    legitimately watchable (each can flip back to AVAILABLE later)."""
    user = _user(42)
    plug = _plug(plug_id=7, status=status)
    db = _db(
        _scalar_one_or_none(plug),
        _scalar_one_or_none(None),  # no existing watch (no gateway query: not AVAILABLE)
    )

    res = await watch_plug(7, user, db)

    assert res["watching"] is True
    db.add.assert_called_once()


@pytest.mark.asyncio
async def test_watch_allowed_when_available_but_gateway_dead():
    """AVAILABLE but unreachable is NOT startable (the driver UI shows it as
    offline), so watching it is allowed — it will fire on the next real
    occupied→available flip."""
    user = _user(42)
    plug = _plug(plug_id=7, status=PlugStatus.AVAILABLE)
    gateway = MagicMock()
    db = _db(
        _scalar_one_or_none(plug),
        _scalar_one_or_none(gateway),
        _scalar_one_or_none(None),  # no existing watch
    )

    with patch("backend.routers.plugs.gateway_is_live", return_value=False):
        res = await watch_plug(7, user, db)

    assert res["watching"] is True
    db.add.assert_called_once()


# ---------------------------------------------- DELETE /watch (disarm) ------

@pytest.mark.asyncio
async def test_unwatch_deletes_and_is_idempotent():
    """Disarm issues a scoped DELETE and returns the same 200 whether or not
    a watch existed — running it twice is harmless."""
    user = _user(42)
    for _ in range(2):
        db = _db(MagicMock())  # the DELETE's (row-count) result
        res = await unwatch_plug(7, user, db)
        assert res == {"status": "not_watching", "plug_id": 7, "watching": False}
        stmt = db.execute.await_args.args[0]
        sql = str(stmt)
        assert sql.startswith("DELETE FROM plug_watches")
        assert "user_id" in sql and "plug_id" in sql  # scoped to (user, plug)
        db.commit.assert_awaited_once()


# ------------------------------------------------- fan-out service ----------

class _WatchDB:
    """Async-session stand-in for the service: records statements, returns a
    fixed watcher list for the first (SELECT) execute."""

    def __init__(self, rows, fail_on_execute=False):
        self.rows = rows
        self.executed = []
        self.committed = False
        self.rolled_back = False
        self._fail = fail_on_execute

    async def execute(self, stmt):
        if self._fail:
            raise RuntimeError("db down")
        self.executed.append(stmt)
        r = MagicMock()
        r.all.return_value = self.rows
        return r

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


@pytest.mark.asyncio
async def test_fanout_notifies_each_watcher_then_deletes_rows():
    plug = _plug(plug_id=7, name="Bay 1")
    db = _WatchDB([(101, 10), (102, 11)])  # (watch_id, user_id)
    notify_mock = AsyncMock()

    with patch("backend.services.notifications.notify", notify_mock):
        count = await notify_watchers_plug_available(db, plug)

    assert count == 2
    assert notify_mock.await_count == 2
    notified_users = [c.args[0] for c in notify_mock.await_args_list]
    assert notified_users == [10, 11]
    for c in notify_mock.await_args_list:
        assert c.args[1] == "plug_available"
        assert "Bay 1 is now free" in c.args[2]
        assert c.kwargs["plug_id"] == 7

    # One-shot: the delete targets exactly the fanned-out watch ids.
    assert len(db.executed) == 2
    delete_sql = str(db.executed[1])
    assert delete_sql.startswith("DELETE FROM plug_watches")
    assert "id IN" in delete_sql
    assert db.committed, "watch rows were not deleted after firing"


@pytest.mark.asyncio
async def test_fanout_excludes_the_given_user():
    plug = _plug(plug_id=7)
    db = _WatchDB([(101, 10)])
    notify_mock = AsyncMock()

    with patch("backend.services.notifications.notify", notify_mock):
        await notify_watchers_plug_available(db, plug, exclude_user_id=3)

    select_sql = str(db.executed[0])
    assert "user_id !=" in select_sql, "exclude_user_id filter missing from the watcher SELECT"


@pytest.mark.asyncio
async def test_fanout_without_exclusion_has_no_user_filter():
    plug = _plug(plug_id=7)
    db = _WatchDB([(101, 10)])

    with patch("backend.services.notifications.notify", AsyncMock()):
        await notify_watchers_plug_available(db, plug)

    assert "user_id !=" not in str(db.executed[0])


@pytest.mark.asyncio
async def test_fanout_noop_when_no_watchers():
    plug = _plug(plug_id=7)
    db = _WatchDB([])
    notify_mock = AsyncMock()

    with patch("backend.services.notifications.notify", notify_mock):
        count = await notify_watchers_plug_available(db, plug)

    assert count == 0
    notify_mock.assert_not_awaited()
    assert len(db.executed) == 1  # the SELECT only — no DELETE, no commit
    assert not db.committed


@pytest.mark.asyncio
async def test_fanout_never_raises_on_db_failure_and_rolls_back():
    plug = _plug(plug_id=7)
    db = _WatchDB([], fail_on_execute=True)

    count = await notify_watchers_plug_available(db, plug)  # must not raise

    assert count == 0
    assert db.rolled_back, "shared session not rolled back after a failed statement"


@pytest.mark.asyncio
async def test_fanout_never_raises_when_notify_explodes():
    """notify() never raises by contract, but even a broken monkey-wrench
    there must not escape into the billing path. The rows are deliberately
    NOT deleted in that case — the watch survives for the next flip."""
    plug = _plug(plug_id=7)
    db = _WatchDB([(101, 10)])

    with patch("backend.services.notifications.notify",
               AsyncMock(side_effect=RuntimeError("boom"))):
        count = await notify_watchers_plug_available(db, plug)  # must not raise

    assert count == 0
    assert len(db.executed) == 1  # SELECT happened, DELETE never reached
    assert not db.committed
    assert db.rolled_back


# ------------------------------------------- finalize / maintenance wiring --

def _finalize_fixtures():
    """A DB-free finalize_charging_session run: real function, mocked
    session/plug rows and side-effect surfaces."""
    session = MagicMock()
    session.id = 99
    session.status = SessionStatus.ACTIVE
    session.user_id = 3
    session.plug_id = 7
    session.hold_coins = None
    session.energy_kwh = 1.0
    session.rate_coins_per_kwh = Decimal("5.00")
    # [Pricing v2] Flat/legacy single-rate session: NULL segment accrual, so
    # session_cost bills energy_cost(1.0, 5.00) = 5.00 (the legacy path).
    session.settled_cost_coins = None
    session.rate_segment_start_kwh = None
    session.peak_power_w = 0.0
    session.started_at = datetime.now(timezone.utc)

    plug = _plug(plug_id=7, name="Bay 1")
    return session, plug


async def _run_finalize(db, monkeypatch, watchers_mock=None):
    """watchers_mock=None leaves the REAL fan-out service in place."""
    from contextlib import ExitStack

    import backend.services.session_lifecycle as sl_mod
    from backend import state as state_module

    monkeypatch.setattr(
        state_module, "mqtt_manager",
        MagicMock(send_plug_command=MagicMock(return_value=True)),
    )
    monkeypatch.setattr(
        state_module, "telemetry_store",
        MagicMock(get_latest=MagicMock(return_value=None), end_session=MagicMock()),
    )
    monkeypatch.setattr(sl_mod, "set_plug_telemetry_interval", AsyncMock())
    monkeypatch.setattr(
        sl_mod, "debit_wallet_clamped",
        AsyncMock(return_value=(Decimal("5.00"), Decimal("95.00"))),
    )

    with ExitStack() as stack:
        stack.enter_context(
            patch("backend.services.socketio_manager.emit_plug_status", AsyncMock())
        )
        stack.enter_context(
            patch("backend.services.notifications.notify", AsyncMock())
        )
        if watchers_mock is not None:
            stack.enter_context(
                patch("backend.services.plug_watch.notify_watchers_plug_available",
                      watchers_mock)
            )
        return await sl_mod.finalize_charging_session(db, 99)


@pytest.mark.asyncio
async def test_finalize_notifies_watchers_excluding_the_stopping_driver(monkeypatch):
    session, plug = _finalize_fixtures()
    db = _db(
        _scalar_one_or_none(session),  # locked session select
        _scalar_one(plug),             # plug select
    )
    watchers_mock = AsyncMock(return_value=1)

    receipt = await _run_finalize(db, monkeypatch, watchers_mock)

    assert receipt is not None and receipt["status"] == "completed"
    watchers_mock.assert_awaited_once()
    args, kwargs = watchers_mock.await_args
    assert args[0] is db
    assert args[1] is plug
    assert kwargs["exclude_user_id"] == 3  # the driver whose session just ended


@pytest.mark.asyncio
async def test_finalize_survives_watch_fanout_db_failure(monkeypatch):
    """The REAL fan-out service runs against a db whose watcher SELECT (the
    third execute) explodes — the service swallows + rolls back, and finalize
    still returns a complete receipt (billing unharmed)."""
    session, plug = _finalize_fixtures()
    db = _db(
        _scalar_one_or_none(session),
        _scalar_one(plug),
        RuntimeError("watch select exploded"),  # raised by the service's SELECT
    )

    receipt = await _run_finalize(db, monkeypatch, watchers_mock=None)

    assert receipt is not None
    assert receipt["status"] == "completed"
    assert receipt["coins_spent"] == 5.0
    db.rollback.assert_awaited_once()  # the service restored the shared session


@pytest.mark.asyncio
async def test_maintenance_clear_notifies_watchers():
    from backend.routers.cpo import cpo_plug_maintenance
    from backend.schemas import CpoPlugMaintenanceRequest

    user = _user(42)
    user.tenant_id = 1
    user.email = "cpo@amphive.test"
    plug = _plug(plug_id=7, status=PlugStatus.MAINTENANCE)
    db = _db(
        _scalar_one_or_none(plug),  # plug ownership lookup
        _scalar_one(0),             # no ACTIVE sessions on this plug
    )

    watchers_mock = AsyncMock(return_value=0)
    with patch("backend.services.socketio_manager.emit_plug_status", AsyncMock()), \
         patch("backend.services.plug_watch.notify_watchers_plug_available", watchers_mock):
        res = await cpo_plug_maintenance(
            7, CpoPlugMaintenanceRequest(action="clear"), user, db,
        )

    assert res["plug_status"] == "available"
    watchers_mock.assert_awaited_once()
    args, kwargs = watchers_mock.await_args
    assert args[1] is plug
    assert kwargs.get("exclude_user_id") is None  # nobody excluded on a CPO clear


@pytest.mark.asyncio
async def test_maintenance_enter_does_not_notify_watchers():
    from backend.routers.cpo import cpo_plug_maintenance
    from backend.schemas import CpoPlugMaintenanceRequest

    user = _user(42)
    user.tenant_id = 1
    user.email = "cpo@amphive.test"
    plug = _plug(plug_id=7, status=PlugStatus.AVAILABLE)
    db = _db(_scalar_one_or_none(plug))

    watchers_mock = AsyncMock()
    with patch("backend.services.socketio_manager.emit_plug_status", AsyncMock()), \
         patch("backend.services.plug_watch.notify_watchers_plug_available", watchers_mock):
        await cpo_plug_maintenance(
            7, CpoPlugMaintenanceRequest(action="enter"), user, db,
        )

    watchers_mock.assert_not_awaited()


# ------------------------------------------------- `watching` field ---------

@pytest.mark.asyncio
async def test_available_plugs_carry_watching_via_one_extra_query():
    """The list endpoint marks watched plugs from ONE per-user watches query
    (2 queries total for any list size — the endpoint's N+1-free shape)."""
    user = _user(42)
    plug_a = _plug(plug_id=1, status=PlugStatus.AVAILABLE, name="Plug A")
    plug_b = _plug(plug_id=2, status=PlugStatus.OCCUPIED, name="Plug B")
    gateway = MagicMock(latitude=None, longitude=None)
    # [Queued charge] The list query now also joins Tenant (for queue_available).
    tenant = MagicMock(queued_charging_enabled=False)

    rows_result = MagicMock()
    # Row shape: (plug, group name, group is_public, group tariff_id, gateway,
    # tenant) — the group columns are None for ungrouped plugs (outer join).
    rows_result.all.return_value = [
        (plug_a, None, None, None, gateway, tenant),
        (plug_b, None, None, None, gateway, tenant),
    ]
    watched_result = MagicMock()
    watched_result.scalars.return_value.all.return_value = [2]  # watching plug B

    # [Reservations] The list endpoint also runs its grouped reservation
    # lookup: one lazy-expiry UPDATE (rowcount=0 → nothing flipped → no
    # commit) + one grouped SELECT — a constant 2 more queries for any list
    # size, so the endpoint stays N+1-free overall.
    expire_result = MagicMock(rowcount=0)
    reservations_result = MagicMock()
    reservations_result.scalars.return_value = []

    db = _db(rows_result, watched_result, expire_result, reservations_result)

    with patch("backend.routers.plugs.gateway_is_live", return_value=True), \
         patch("backend.routers.plugs.resolve_price_display_batch",
               AsyncMock(return_value={1: (Decimal("5.00"), None, None),
                                       2: (Decimal("5.00"), None, None)})):
        responses = await get_available_plugs(user, db)

    # plugs+joins, the watches, then the reservation expiry+grouped pair —
    # constant query count regardless of list size (no N+1).
    assert db.execute.await_count == 4
    by_id = {r.id: r for r in responses}
    assert by_id[1].watching is False
    assert by_id[2].watching is True


@pytest.mark.asyncio
async def test_get_plug_reports_watching_true():
    user = _user(42)
    plug = _plug(plug_id=7, status=PlugStatus.OCCUPIED)  # ungrouped

    # [Reservations] The detail endpoint runs the same expiry UPDATE +
    # grouped SELECT the list does, before the watching lookup.
    expire_result = MagicMock(rowcount=0)
    reservations_result = MagicMock()
    reservations_result.scalars.return_value = []

    db = _db(
        _scalar_one_or_none(plug),        # plug lookup
        _scalar_one_or_none(None),        # gateway lookup (none — offline)
        expire_result,                    # reservation lazy-expiry UPDATE
        reservations_result,              # grouped reservation SELECT
        _scalar_one_or_none(101),         # a watch row id exists
    )

    with patch("backend.routers.plugs.resolve_price_display",
               AsyncMock(return_value=(Decimal("5.00"), None, None))):
        res = await get_plug(7, user, db)

    assert res.watching is True
    assert res.status == "occupied"
