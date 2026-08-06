"""
Tests for services/session_lifecycle.finalize_charging_session — the one true
stop/billing path — focused on the 2026-08-06 security remediation:

- [H1] finalize bills STRICTLY from the ownership-guarded persisted
  session.energy_kwh, never from the mutable in-memory TelemetryStore (which a
  foreign gateway could poison via a victim plug_id on its own topic).
- [L8] finalize acquires the User row lock BEFORE the ChargingSession row lock,
  matching the walk-up start / update_session_limits (user -> session) order so
  the two paths can't AB-BA deadlock.

DB-free: the session/plug rows and the wallet debit are mocked; what's under
test is finalize's own billing arithmetic and lock ordering, not SQLAlchemy.
The behavioral deadlock needs two concurrent real DB txns, so the lock ordering
is asserted structurally (see test_finalize_locks_user_before_charging_session).
"""
import contextlib
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.database.models import SessionStatus


def _s_one_or_none(value):
    r = MagicMock()
    r.scalar_one_or_none.return_value = value
    return r


def _s_one(value):
    r = MagicMock()
    r.scalar_one.return_value = value
    return r


def _seq_db(*results):
    """A db whose execute() returns queued results in order (side_effect)."""
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=list(results))
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


def _active_session():
    session = MagicMock()
    session.id = 99
    session.status = SessionStatus.ACTIVE
    session.user_id = 3
    session.plug_id = 7
    session.hold_coins = None
    session.energy_kwh = 2.0                      # the guarded, persisted total
    session.rate_coins_per_kwh = Decimal("5.00")
    session.settled_cost_coins = None             # flat/legacy single-rate
    session.rate_segment_start_kwh = None
    session.peak_power_w = 0.0
    session.max_kwh = None
    session.max_duration_seconds = None
    session.started_at = datetime.now(timezone.utc)
    return session


def _plug():
    plug = MagicMock()
    plug.id = 7
    plug.name = "Bay 1"
    plug.gateway_id = "gw-1"
    plug.local_ip = "10.0.0.5"
    plug.group_id = None                          # ungrouped: capacity notify no-ops
    return plug


@pytest.mark.asyncio
async def test_finalize_bills_from_persisted_energy_not_the_live_store(monkeypatch):
    """[H1] finalize must bill from the ownership-guarded persisted
    session.energy_kwh, NEVER from the in-memory TelemetryStore. A live snapshot
    poisoned larger than persisted (the old max(live, persisted) input) must not
    inflate the bill — the store is no longer read on the money path at all."""
    import backend.services.session_lifecycle as sl_mod
    from backend import state as state_module

    session = _active_session()
    plug = _plug()
    db = _seq_db(
        _s_one_or_none(MagicMock()),   # [L8] user row locked first
        _s_one_or_none(session),       # session row locked
        _s_one(plug),                  # plug load
    )

    # A live store snapshot DELIBERATELY larger than the persisted 2.0 kWh —
    # the pre-fix max(live, persisted) would have billed 1000 kWh here.
    poison = MagicMock(energy_kwh=1000.0)
    store = MagicMock(
        get_latest=MagicMock(return_value=poison),
        end_session=MagicMock(),
    )
    monkeypatch.setattr(
        state_module, "mqtt_manager",
        MagicMock(send_plug_command=MagicMock(return_value=True)),
    )
    monkeypatch.setattr(state_module, "telemetry_store", store)
    monkeypatch.setattr(sl_mod, "set_plug_telemetry_interval", AsyncMock())
    # 2.0 kWh * 5.00 = 10.00 coins collected in full.
    monkeypatch.setattr(
        sl_mod, "debit_wallet_clamped",
        AsyncMock(return_value=(Decimal("10.00"), Decimal("90.00"))),
    )

    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch("backend.services.socketio_manager.emit_plug_status", AsyncMock())
        )
        stack.enter_context(
            patch("backend.services.notifications.notify", AsyncMock())
        )
        stack.enter_context(
            patch("backend.services.plug_watch.notify_watchers_plug_available",
                  AsyncMock(return_value=0))
        )
        stack.enter_context(
            patch("backend.services.capacity.notify_capacity_available", AsyncMock())
        )
        stack.enter_context(
            patch("backend.services.billing_emails.send_session_bill",
                  MagicMock(return_value=None))
        )
        stack.enter_context(
            patch("backend.services.billing_emails.schedule", MagicMock())
        )
        receipt = await sl_mod.finalize_charging_session(db, 99)

    assert receipt is not None
    # Billed 2.0 kWh (persisted), NOT 1000.0 (the poisoned live snapshot).
    assert receipt["energy_kwh"] == 2.0
    assert receipt["coins_spent"] == 10.0
    assert receipt["shortfall_coins"] == 0.0
    # The store must not be a billing input any more.
    store.get_latest.assert_not_called()


def test_finalize_locks_user_before_charging_session():
    """[L8] finalize must take the User row lock BEFORE the ChargingSession row
    lock, so it locks in the same user -> session order as the walk-up start
    and update_session_limits — the inverse order could AB-BA deadlock. The
    behavioral race needs two concurrent real DB txns, so this is a structural
    assertion on the source (mirrors test_session_limits' getsource sweeps)."""
    import inspect

    from backend.services import session_lifecycle

    src = inspect.getsource(session_lifecycle.finalize_charging_session)
    user_lock = src.find("select(User)")
    session_lock = src.find(
        "select(ChargingSession).where(and_(*filters)).with_for_update()"
    )
    assert user_lock != -1, "finalize no longer takes an explicit User FOR UPDATE"
    assert session_lock != -1, "finalize no longer locks the ChargingSession row"
    assert user_lock < session_lock, (
        "User row must be locked BEFORE the ChargingSession row (user -> session "
        "order) to avoid the AB-BA deadlock with update_session_limits"
    )
