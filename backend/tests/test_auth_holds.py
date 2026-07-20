"""
Session-sized authorization hold tests (MARKET_GAP_ANALYSIS.md §3) — need a
real PostgreSQL (CI's postgres:15 service, exported as TEST_DATABASE_URL).
Skipped locally: this repo's dev boxes run no database by policy.

What's proven here:

1. services/wallet.py available_balance(): coin_balance minus the SUM of
   hold_coins across the user's ACTIVE sessions. Legacy ACTIVE sessions with
   a NULL hold, and any session that isn't ACTIVE (regardless of its
   hold_coins), contribute nothing.
2. Hold sizing at POST /api/sessions/start (routers/sessions.py
   start_charging_session): hold = min(available_balance, max_kwh * rate),
   snapshotted onto ChargingSession.hold_coins. A second concurrent session
   only gets what's left of the AVAILABLE balance, not the raw wallet
   balance — the two starts serialize on the user-row lock so they can never
   double-reserve the same coins. 402 when the AVAILABLE (not raw) balance
   is below MIN_START_BALANCE_COINS, even when the raw wallet balance alone
   would have cleared it.
3. services/session_lifecycle.py finalize_charging_session debits at most
   the hold — any unspent remainder is released with no money movement (a
   hold never touched coin_balance to begin with) — and legacy NULL-hold
   sessions finalize with EXACTLY the pre-hold behavior (regression):
   min(final_cost, live balance), forgiven-shortfall logged.
4. services/mqtt_manager.py MQTTManager._maybe_auto_stop_on_exhaustion uses
   the session's OWN hold as its exhaustion threshold when set, never the
   driver's whole wallet balance (which a concurrent sibling session may
   also be counting on).
"""
import os
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="needs TEST_DATABASE_URL pointing at a throwaway Postgres (CI service)",
)

ENUM_TYPES = ["gateway_status", "plug_status", "session_status", "tx_type", "user_role"]


@pytest_asyncio.fixture()
async def factory():
    """Engine + fresh schema per test; yields a session factory."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from backend.database.models import Base

    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        for enum_name in ENUM_TYPES:
            await conn.execute(text(f"DROP TYPE IF EXISTS {enum_name} CASCADE"))
        await conn.run_sync(Base.metadata.create_all)

    # Mirror the app factory's expire_on_commit=False (backend/database/db.py).
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


# --- Seed helpers --------------------------------------------------------------

async def _seed_tenant(factory, name: str = "Tenant") -> int:
    from backend.database.models import Tenant

    async with factory() as db:
        tenant = Tenant(name=f"{name}-{uuid.uuid4().hex[:8]}")
        db.add(tenant)
        await db.commit()
        return tenant.id


async def _seed_gateway(factory, tenant_id: int, gateway_id: str) -> str:
    """ONLINE + freshly seen, so gateway_is_live() clears the session-start gate."""
    from datetime import datetime, timezone

    from backend.database.models import Gateway, GatewayStatus

    gateway_id = f"{gateway_id}-{uuid.uuid4().hex[:8]}"
    async with factory() as db:
        gw = Gateway(
            id=gateway_id, tenant_id=tenant_id, name=gateway_id, vpn_ip=gateway_id,
            status=GatewayStatus.ONLINE, last_seen_at=datetime.now(timezone.utc),
        )
        db.add(gw)
        await db.commit()
        return gw.id


async def _seed_plug(factory, gateway_id: str, name: str = "Plug") -> int:
    from backend.database.models import Plug, PlugStatus

    async with factory() as db:
        plug = Plug(
            gateway_id=gateway_id, name=name, local_ip="10.0.0.5",
            status=PlugStatus.AVAILABLE,  # new plugs default OFFLINE (TD#22) — must be AVAILABLE to start
        )
        db.add(plug)
        await db.commit()
        return plug.id


async def _seed_user(factory, balance: str, tenant_id=None) -> int:
    from backend.database.models import User

    async with factory() as db:
        user = User(
            email=f"holds-{uuid.uuid4().hex[:12]}@example.com",
            hashed_password="x", full_name="Holds Driver",
            tenant_id=tenant_id, coin_balance=Decimal(balance),
        )
        db.add(user)
        await db.commit()
        return user.id


async def _seed_session(factory, *, tenant_id, user_id, plug_id, status,
                         hold_coins=None, energy_kwh: float = 0.0,
                         rate_coins_per_kwh=None) -> int:
    from backend.database.models import ChargingSession

    async with factory() as db:
        session = ChargingSession(
            tenant_id=tenant_id, user_id=user_id, plug_id=plug_id, status=status,
            hold_coins=Decimal(hold_coins) if hold_coins is not None else None,
            energy_kwh=energy_kwh,
            rate_coins_per_kwh=Decimal(rate_coins_per_kwh) if rate_coins_per_kwh is not None else None,
        )
        db.add(session)
        await db.commit()
        return session.id


def _fake_user(user_id: int):
    """A pre-authorized User stand-in for calling the router function
    directly — start_charging_session immediately re-selects the real row
    under a lock, so only `.id` needs to be real (matches the
    test_pricing.py/_cpo_user convention)."""
    from unittest.mock import MagicMock

    u = MagicMock()
    u.id = user_id
    return u


async def _start_session(factory, monkeypatch, *, plug_id: int, user_id: int,
                          max_kwh: float = 30.0, max_duration: int = 14400):
    """Call POST /api/sessions/start's handler directly against a real DB
    session, with the gateway/telemetry side-effects stubbed out (no real
    MQTT broker in this test process)."""
    from unittest.mock import AsyncMock, MagicMock

    import backend.routers.sessions as sessions_module
    from backend import state as state_module
    from backend.routers.sessions import start_charging_session
    from backend.schemas import SessionStartRequest

    monkeypatch.setattr(
        state_module, "mqtt_manager",
        MagicMock(send_plug_command=MagicMock(return_value=True)),
    )
    monkeypatch.setattr(sessions_module, "set_plug_telemetry_interval", AsyncMock())

    req = SessionStartRequest(plug_id=plug_id, max_duration_seconds=max_duration, max_kwh=max_kwh)
    async with factory() as db:
        return await start_charging_session(req, _fake_user(user_id), db)


class _NullDB:
    """Minimal async-context-manager stand-in for a db_session_factory()
    call that the code under test never actually queries through (used only
    where hold_coins is supplied, so no balance lookup happens, and
    finalize_charging_session itself is mocked out so the `db` it receives
    is never touched)."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


# --- 1. available_balance() -----------------------------------------------------

@pytest.mark.asyncio
async def test_available_balance_no_active_sessions_equals_coin_balance(factory):
    from backend.services.wallet import available_balance

    uid = await _seed_user(factory, "100.00")

    async with factory() as db:
        result = await available_balance(db, uid)

    assert result == Decimal("100.00")


@pytest.mark.asyncio
async def test_available_balance_subtracts_active_hold(factory):
    from backend.database.models import SessionStatus
    from backend.services.wallet import available_balance

    tenant_id = await _seed_tenant(factory)
    gw = await _seed_gateway(factory, tenant_id, "gw-avail-1")
    plug_id = await _seed_plug(factory, gw)
    uid = await _seed_user(factory, "100.00", tenant_id)
    await _seed_session(
        factory, tenant_id=tenant_id, user_id=uid, plug_id=plug_id,
        status=SessionStatus.ACTIVE, hold_coins="30.00",
    )

    async with factory() as db:
        result = await available_balance(db, uid)

    assert result == Decimal("70.00")


@pytest.mark.asyncio
async def test_available_balance_sums_multiple_active_holds(factory):
    from backend.database.models import SessionStatus
    from backend.services.wallet import available_balance

    tenant_id = await _seed_tenant(factory)
    gw = await _seed_gateway(factory, tenant_id, "gw-avail-2")
    plug_a = await _seed_plug(factory, gw, "Plug A")
    plug_b = await _seed_plug(factory, gw, "Plug B")
    uid = await _seed_user(factory, "100.00", tenant_id)
    await _seed_session(
        factory, tenant_id=tenant_id, user_id=uid, plug_id=plug_a,
        status=SessionStatus.ACTIVE, hold_coins="20.00",
    )
    await _seed_session(
        factory, tenant_id=tenant_id, user_id=uid, plug_id=plug_b,
        status=SessionStatus.ACTIVE, hold_coins="30.00",
    )

    async with factory() as db:
        result = await available_balance(db, uid)

    assert result == Decimal("50.00")  # 100 - 20 - 30


@pytest.mark.asyncio
async def test_available_balance_ignores_legacy_null_hold_active_session(factory):
    """An ACTIVE session with hold_coins=NULL (pre-migration legacy) must
    contribute 0 to the held sum — coin_balance itself was never reduced for
    it, so available_balance must not either."""
    from backend.database.models import SessionStatus
    from backend.services.wallet import available_balance

    tenant_id = await _seed_tenant(factory)
    gw = await _seed_gateway(factory, tenant_id, "gw-avail-3")
    plug_id = await _seed_plug(factory, gw)
    uid = await _seed_user(factory, "100.00", tenant_id)
    await _seed_session(
        factory, tenant_id=tenant_id, user_id=uid, plug_id=plug_id,
        status=SessionStatus.ACTIVE, hold_coins=None,
    )

    async with factory() as db:
        result = await available_balance(db, uid)

    assert result == Decimal("100.00")


@pytest.mark.asyncio
async def test_available_balance_ignores_non_active_session_holds(factory):
    """A COMPLETED session's hold_coins must not count — only ACTIVE
    sessions hold anything (a completed session's hold already stopped
    applying when it left ACTIVE)."""
    from backend.database.models import SessionStatus
    from backend.services.wallet import available_balance

    tenant_id = await _seed_tenant(factory)
    gw = await _seed_gateway(factory, tenant_id, "gw-avail-4")
    plug_id = await _seed_plug(factory, gw)
    uid = await _seed_user(factory, "100.00", tenant_id)
    await _seed_session(
        factory, tenant_id=tenant_id, user_id=uid, plug_id=plug_id,
        status=SessionStatus.COMPLETED, hold_coins="40.00",
    )

    async with factory() as db:
        result = await available_balance(db, uid)

    assert result == Decimal("100.00")


@pytest.mark.asyncio
async def test_available_balance_unknown_user_returns_zero(factory):
    from backend.services.wallet import available_balance

    await _seed_user(factory, "100.00")  # some other user exists
    async with factory() as db:
        result = await available_balance(db, 999999)

    assert result == Decimal("0.00")


# --- 2. Hold sizing at session start ---------------------------------------------

@pytest.mark.asyncio
async def test_hold_sized_to_max_kwh_cost_when_available_balance_is_larger(factory, monkeypatch):
    """hold = min(available, max_kwh * rate) — bounded by the session's own
    worst-case energy cost when the wallet has plenty to spare."""
    import backend.services.pricing as pricing_mod
    from sqlalchemy import select

    from backend.database.models import ChargingSession

    monkeypatch.setattr(pricing_mod, "COINS_PER_KWH", 5.0)

    tenant_id = await _seed_tenant(factory)
    gw = await _seed_gateway(factory, tenant_id, "gw-hold-1")
    plug_id = await _seed_plug(factory, gw)
    uid = await _seed_user(factory, "1000.00", tenant_id)

    result = await _start_session(factory, monkeypatch, plug_id=plug_id, user_id=uid, max_kwh=10.0)
    assert result["status"] == "started"

    async with factory() as db:
        session = (await db.execute(
            select(ChargingSession).where(ChargingSession.id == result["session_id"])
        )).scalar_one()

    assert session.hold_coins == Decimal("50.00")  # min(1000, 10 * 5.00)


@pytest.mark.asyncio
async def test_hold_sized_to_available_balance_when_smaller_than_max_kwh_cost(factory, monkeypatch):
    """hold = min(available, max_kwh * rate) — bounded by the AVAILABLE
    balance when it's the tighter constraint."""
    import backend.services.pricing as pricing_mod
    from sqlalchemy import select

    from backend.database.models import ChargingSession

    monkeypatch.setattr(pricing_mod, "COINS_PER_KWH", 5.0)

    tenant_id = await _seed_tenant(factory)
    gw = await _seed_gateway(factory, tenant_id, "gw-hold-2")
    plug_id = await _seed_plug(factory, gw)
    uid = await _seed_user(factory, "60.00", tenant_id)  # available = 60

    # max_kwh=30 (default cap) * 5.00 = 150, far more than the 60 available.
    result = await _start_session(factory, monkeypatch, plug_id=plug_id, user_id=uid, max_kwh=30.0)

    async with factory() as db:
        session = (await db.execute(
            select(ChargingSession).where(ChargingSession.id == result["session_id"])
        )).scalar_one()

    assert session.hold_coins == Decimal("60.00")  # min(60, 150)


@pytest.mark.asyncio
async def test_second_concurrent_session_gets_remaining_available_balance(factory, monkeypatch):
    """Two concurrent sessions for the same user must never double-reserve
    the same coins: the second start only gets what's left of the AVAILABLE
    balance after the first session's hold, not a fresh read of the raw
    wallet balance."""
    import backend.services.pricing as pricing_mod
    from sqlalchemy import select

    from backend.database.models import ChargingSession

    monkeypatch.setattr(pricing_mod, "COINS_PER_KWH", 5.0)

    tenant_id = await _seed_tenant(factory)
    gw = await _seed_gateway(factory, tenant_id, "gw-hold-3")
    plug_a = await _seed_plug(factory, gw, "Plug A")
    plug_b = await _seed_plug(factory, gw, "Plug B")
    uid = await _seed_user(factory, "120.00", tenant_id)

    # Session A: hold = min(120, 10 * 5.00) = 50.
    result_a = await _start_session(factory, monkeypatch, plug_id=plug_a, user_id=uid, max_kwh=10.0)
    async with factory() as db:
        session_a = (await db.execute(
            select(ChargingSession).where(ChargingSession.id == result_a["session_id"])
        )).scalar_one()
    assert session_a.hold_coins == Decimal("50.00")

    # Session B starts while A is still ACTIVE: available = 120 - 50 = 70.
    # B's own cap (30 kWh * 5.00 = 150) is larger, so B is bounded by the
    # remaining AVAILABLE balance, not its own max_kwh, and NOT by the raw
    # 120-coin wallet balance either.
    result_b = await _start_session(factory, monkeypatch, plug_id=plug_b, user_id=uid, max_kwh=30.0)
    async with factory() as db:
        session_b = (await db.execute(
            select(ChargingSession).where(ChargingSession.id == result_b["session_id"])
        )).scalar_one()
    assert session_b.hold_coins == Decimal("70.00")


@pytest.mark.asyncio
async def test_small_session_starts_with_hold_below_floor(factory, monkeypatch):
    """A modest session whose worst-case cost is BELOW the floor must still
    start when the driver's available balance clears the floor: the floor
    gates the AVAILABLE balance, never the hold itself. Regression for the
    2026-07-12 prod find where a fully-funded wallet was 402'd because its
    5 kWh session only reserved 25 coins (< the 50 floor)."""
    import backend.services.pricing as pricing_mod
    from sqlalchemy import select

    from backend.database.models import ChargingSession

    monkeypatch.setattr(pricing_mod, "COINS_PER_KWH", 5.0)

    tenant_id = await _seed_tenant(factory)
    gw = await _seed_gateway(factory, tenant_id, "gw-hold-small")
    plug = await _seed_plug(factory, gw, "Plug Small")
    uid = await _seed_user(factory, "497.72", tenant_id)

    # 5 kWh * 5.00 = 25.00 hold — below the 50 floor, but available (497.72)
    # clears the floor, so the start must succeed with the small hold.
    result = await _start_session(factory, monkeypatch, plug_id=plug, user_id=uid, max_kwh=5.0)
    assert result["status"] == "started"

    async with factory() as db:
        session = (await db.execute(
            select(ChargingSession).where(ChargingSession.id == result["session_id"])
        )).scalar_one()
    assert session.hold_coins == Decimal("25.00")


@pytest.mark.asyncio
async def test_start_rejected_when_available_balance_below_floor_despite_raw_balance(factory, monkeypatch):
    """A driver with a raw wallet balance well above MIN_START_BALANCE_COINS
    must still get the 402 once another active session already holds enough
    of it to push the AVAILABLE balance below the floor — proving the check
    gates on available_balance, not user.coin_balance."""
    import backend.services.pricing as pricing_mod
    from fastapi import HTTPException

    monkeypatch.setattr(pricing_mod, "COINS_PER_KWH", 5.0)

    tenant_id = await _seed_tenant(factory)
    gw = await _seed_gateway(factory, tenant_id, "gw-hold-4")
    plug_a = await _seed_plug(factory, gw, "Plug A")
    plug_b = await _seed_plug(factory, gw, "Plug B")
    uid = await _seed_user(factory, "100.00", tenant_id)  # raw balance clears the 50 floor alone

    # Session A reserves nearly everything: 18 kWh * 5.00 = 90.
    result_a = await _start_session(factory, monkeypatch, plug_id=plug_a, user_id=uid, max_kwh=18.0)
    assert result_a["status"] == "started"

    # Available is now 100 - 90 = 10, below the 50 floor, even though
    # user.coin_balance (100) alone would have cleared it. Session B's own
    # max_kwh cap (30 * 5.00 = 150) is deliberately large enough that it
    # would NOT itself be the binding constraint — isolating that the 402
    # here comes from the available-balance check, not from B's own cap.
    with pytest.raises(HTTPException) as exc_info:
        await _start_session(factory, monkeypatch, plug_id=plug_b, user_id=uid, max_kwh=30.0)

    assert exc_info.value.status_code == 402
    assert "available" in exc_info.value.detail.lower()


# --- 3. finalize_charging_session caps the debit at the hold ---------------------

@pytest.mark.asyncio
async def test_finalize_debits_at_most_the_hold_and_releases_remainder(factory, monkeypatch):
    """A session that used LESS energy than its hold reserved is only
    debited for what it actually used — the unspent remainder of the hold is
    released with no money movement (a hold is a logical reservation, never
    a real debit — see services/wallet.py available_balance's docstring)."""
    from unittest.mock import AsyncMock, MagicMock

    from sqlalchemy import select

    import backend.services.session_lifecycle as sl_mod
    from backend import state as state_module
    from backend.database.models import ChargingSession, SessionStatus, User

    tenant_id = await _seed_tenant(factory)
    gw = await _seed_gateway(factory, tenant_id, "gw-fin-1")
    plug_id = await _seed_plug(factory, gw)
    uid = await _seed_user(factory, "200.00", tenant_id)

    # Reserved a 50-coin hold but only actually drew 2 kWh -> final_cost = 10.00.
    session_id = await _seed_session(
        factory, tenant_id=tenant_id, user_id=uid, plug_id=plug_id,
        status=SessionStatus.ACTIVE, hold_coins="50.00",
        energy_kwh=2.0, rate_coins_per_kwh="5.00",
    )

    monkeypatch.setattr(
        state_module, "mqtt_manager",
        MagicMock(send_plug_command=MagicMock(return_value=True)),
    )
    monkeypatch.setattr(sl_mod, "set_plug_telemetry_interval", AsyncMock())
    # Deterministic telemetry baseline for this plug_id (0 kWh live reading),
    # regardless of what any other test in this process left in the shared
    # TelemetryStore singleton — finalize takes max(live, persisted), so this
    # makes session.energy_kwh (2.0, set above) the value that wins.
    state_module.telemetry_store.start_session(plug_id)

    async with factory() as db:
        outcome = await sl_mod.finalize_charging_session(db, session_id)

    assert outcome is not None
    assert outcome["coins_spent"] == 10.0  # 2.0 kWh * 5.00, not the 50-coin hold
    assert outcome["shortfall_coins"] == 0.0

    async with factory() as db:
        user = (await db.execute(select(User).where(User.id == uid))).scalar_one()
    # Only the ACTUAL cost was debited — the other 40 coins of the hold were
    # never moved, so the wallet holds 200 - 10 = 190, not 200 - 50.
    assert user.coin_balance == Decimal("190.00")

    async with factory() as db:
        reloaded = (await db.execute(
            select(ChargingSession).where(ChargingSession.id == session_id)
        )).scalar_one()
    assert reloaded.status == SessionStatus.COMPLETED
    assert reloaded.coins_spent == Decimal("10.00")


@pytest.mark.asyncio
async def test_finalize_caps_debit_at_hold_even_when_wallet_could_cover_more(factory, monkeypatch):
    """Defense-in-depth: even if a session's recorded energy would bill more
    than its OWN hold (shouldn't normally happen — max_kwh is an external
    hard cap — but the hold must still win as the ceiling), the debit never
    exceeds the hold. The wallet balance here is deliberately large enough
    to cover the FULL bill, proving the cap comes from the hold, not from
    debit_wallet_clamped's balance clamp."""
    from unittest.mock import AsyncMock, MagicMock

    from sqlalchemy import select

    import backend.services.session_lifecycle as sl_mod
    from backend import state as state_module
    from backend.database.models import SessionStatus, User

    tenant_id = await _seed_tenant(factory)
    gw = await _seed_gateway(factory, tenant_id, "gw-fin-2")
    plug_id = await _seed_plug(factory, gw)
    uid = await _seed_user(factory, "500.00", tenant_id)  # plenty — could cover the full 50.00 bill

    session_id = await _seed_session(
        factory, tenant_id=tenant_id, user_id=uid, plug_id=plug_id,
        status=SessionStatus.ACTIVE, hold_coins="20.00",
        energy_kwh=10.0, rate_coins_per_kwh="5.00",  # cost would be 50.00
    )

    monkeypatch.setattr(
        state_module, "mqtt_manager",
        MagicMock(send_plug_command=MagicMock(return_value=True)),
    )
    monkeypatch.setattr(sl_mod, "set_plug_telemetry_interval", AsyncMock())
    state_module.telemetry_store.start_session(plug_id)

    async with factory() as db:
        outcome = await sl_mod.finalize_charging_session(db, session_id)

    assert outcome["coins_spent"] == 20.0    # capped at the hold, not the 50.00 bill
    assert outcome["shortfall_coins"] == 30.0  # 50 - 20, reported on the receipt

    async with factory() as db:
        user = (await db.execute(select(User).where(User.id == uid))).scalar_one()
    # 500 - 20, NOT 500 - 50: the wallet could have paid the full bill, so
    # the cap really is the hold, not a balance shortfall.
    assert user.coin_balance == Decimal("480.00")


@pytest.mark.asyncio
async def test_legacy_null_hold_session_finalizes_with_pre_hold_behavior(factory, monkeypatch):
    """Regression: a session with hold_coins=NULL (pre-migration legacy)
    must debit min(final_cost, live balance) exactly as before this
    feature — including forgiving (and logging/recording) a shortfall when
    the bill exceeds the wallet, with the ledger still reconciling."""
    from unittest.mock import AsyncMock, MagicMock

    from sqlalchemy import select

    import backend.services.session_lifecycle as sl_mod
    from backend import state as state_module
    from backend.database.models import (
        LedgerTransaction, SessionStatus, User,
    )

    tenant_id = await _seed_tenant(factory)
    gw = await _seed_gateway(factory, tenant_id, "gw-fin-3")
    plug_id = await _seed_plug(factory, gw)
    uid = await _seed_user(factory, "8.00", tenant_id)  # less than the bill

    session_id = await _seed_session(
        factory, tenant_id=tenant_id, user_id=uid, plug_id=plug_id,
        status=SessionStatus.ACTIVE, hold_coins=None,  # legacy: no hold
        energy_kwh=3.0, rate_coins_per_kwh="5.00",  # cost = 15.00, exceeds the 8.00 wallet
    )

    monkeypatch.setattr(
        state_module, "mqtt_manager",
        MagicMock(send_plug_command=MagicMock(return_value=True)),
    )
    monkeypatch.setattr(sl_mod, "set_plug_telemetry_interval", AsyncMock())
    state_module.telemetry_store.start_session(plug_id)

    async with factory() as db:
        outcome = await sl_mod.finalize_charging_session(db, session_id)

    assert outcome["coins_spent"] == 8.0    # clamped to the live balance — same as pre-hold behavior
    assert outcome["shortfall_coins"] == 7.0  # 15 - 8, forgiven (legacy path only)

    async with factory() as db:
        user = (await db.execute(select(User).where(User.id == uid))).scalar_one()
    assert user.coin_balance == Decimal("0.00")

    async with factory() as db:
        ledger = (await db.execute(
            select(LedgerTransaction).where(LedgerTransaction.session_id == session_id)
        )).scalar_one()
    # Ledger must still reconcile: amount == the real balance delta.
    assert ledger.amount == Decimal("-8.00")
    assert ledger.balance_after == Decimal("0.00")
    assert "shortfall" in (ledger.description or "").lower()


# --- 4. Auto-stop threshold uses the hold ----------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("hold_coins,energy_kwh,should_stop", [
    (Decimal("20.00"), 3.0, False),  # cost 15.00 < hold 20.00 -> still covered
    (Decimal("20.00"), 4.0, True),   # cost 20.00 == hold 20.00 -> exhausted (>=)
    (Decimal("20.00"), 5.0, True),   # cost 25.00 >= hold 20.00 -> exhausted
])
async def test_auto_stop_threshold_uses_the_session_hold_not_wallet_balance(hold_coins, energy_kwh, should_stop):
    """
    A session's own hold_coins — not the driver's whole wallet balance — is
    the auto-stop threshold when set: a concurrent SECOND session may be
    holding the rest of that balance, and this session must only stop when
    IT exhausts its own reservation. No DB read of the user's balance should
    even be needed in this path (db_session_factory returns a stand-in that
    would blow up on a real query), proving the threshold really is the
    hold, not a fresh balance lookup.
    """
    from unittest.mock import AsyncMock, patch

    from backend.services.mqtt_manager import MQTTManager

    MQTTManager._instance = None
    mgr = MQTTManager(db_session_factory=lambda: _NullDB())

    finalize_mock = AsyncMock(return_value={"energy_kwh": energy_kwh, "coins_spent": 0})
    with patch("backend.services.session_lifecycle.finalize_charging_session", finalize_mock):
        await mgr._maybe_auto_stop_on_exhaustion(
            session_id=7, user_id=3, energy_kwh=energy_kwh,
            rate_coins_per_kwh=Decimal("5.00"), hold_coins=hold_coins,
        )

    assert finalize_mock.called is should_stop
    if should_stop:
        args, kwargs = finalize_mock.call_args
        assert args[1] == 7  # session_id
        assert "hold" in kwargs.get("reason", "").lower()
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_auto_stop_falls_back_to_wallet_balance_when_hold_is_none():
    """Legacy sessions (hold_coins=NULL) keep the pre-hold behavior exactly:
    the exhaustion threshold falls back to the live wallet balance, read
    from the DB (matches the pre-existing test_auto_stop_on_balance_exhaustion
    in test_mqtt_manager.py, exercised here for the explicit hold_coins=None
    call signature added by this feature)."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from backend.services.mqtt_manager import MQTTManager

    class _FakeResult:
        def __init__(self, val):
            self._val = val

        def scalar_one_or_none(self):
            return self._val

    class _FakeDB:
        def __init__(self, row):
            self._row = row

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, *_a, **_k):
            return _FakeResult(self._row)

    MQTTManager._instance = None
    user = MagicMock()
    user.coin_balance = Decimal("100")
    mgr = MQTTManager(db_session_factory=lambda: _FakeDB(user))

    finalize_mock = AsyncMock(return_value={"energy_kwh": 25.0, "coins_spent": 0})
    with patch("backend.services.session_lifecycle.finalize_charging_session", finalize_mock):
        # cost = 25 * 5 = 125 >= balance 100 -> exhausted
        await mgr._maybe_auto_stop_on_exhaustion(
            session_id=9, user_id=3, energy_kwh=25.0,
            rate_coins_per_kwh=Decimal("5.00"), hold_coins=None,
        )

    assert finalize_mock.called is True
    args, kwargs = finalize_mock.call_args
    assert "wallet balance" in kwargs.get("reason", "").lower()
    MQTTManager._instance = None
