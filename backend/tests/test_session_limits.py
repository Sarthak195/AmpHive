"""
User-set charging limits ("only charge 1 kWh") — MARKET_GAP_ANALYSIS.md §5
"Stop at target kWh".

What's proven here:

1. [DB-gated — needs TEST_DATABASE_URL, CI's postgres:15 service; skipped
   locally by policy, same as test_auth_holds.py]
   POST /api/sessions/start persists the request's max_kwh /
   max_duration_seconds onto ChargingSession (always — including the schema
   defaults when the client sends none), echoes them in the start response,
   GET /api/sessions/active exposes them per session, and the finalize
   receipt carries them.
2. [DB-free — mirrors test_mqtt_manager.py]
   MQTTManager._maybe_auto_stop_on_limits: energy-limit auto-stop at
   energy >= max_kwh ("auto-stopped: energy limit reached"), time-limit
   auto-stop at elapsed >= max_duration_seconds ("auto-stopped: time limit
   reached"), energy reason wins when both trip on one frame, the
   AUTO_STOP_ON_LIMITS env toggle disables it, and legacy NULL-limit
   sessions are never limit-auto-stopped. Plus the telemetry path:
   _persist_telemetry forwards the session's OWN persisted limits into the
   check (this is what makes the mirror fire within ~1 s — telemetry arrives
   every ~1 s during an active session).
3. SessionReaperService.reap_time_limited_once: the 60 s duration backstop
   finalizes only sessions that outlived their own max_duration_seconds,
   honors the same env toggle, and skips NULL/naive-started_at edge rows
   correctly.

Background: the firmware already enforces both limits locally (relay OFF)
from the MQTT ON payload, but publishes NO alarm on those cutoffs — so
without the backend mirror the session would linger ACTIVE (plug pinned
OCCUPIED, driver unbilled) until the staleness reaper.
"""
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")

db_gated = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="needs TEST_DATABASE_URL pointing at a throwaway Postgres (CI service)",
)

ENUM_TYPES = ["gateway_status", "plug_status", "session_status", "tx_type", "user_role"]


# =============================================================================
# 1. DB-gated: limits persisted at start + exposed in API responses
# =============================================================================

@pytest_asyncio.fixture()
async def factory():
    """Engine + fresh schema per test; yields a session factory (same shape
    as test_auth_holds.py)."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )
    from sqlalchemy.pool import NullPool

    from backend.database.models import Base

    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        for enum_name in ENUM_TYPES:
            await conn.execute(text(f"DROP TYPE IF EXISTS {enum_name} CASCADE"))
        await conn.run_sync(Base.metadata.create_all)

    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def _seed_tenant(factory, name: str = "Tenant") -> int:
    from backend.database.models import Tenant

    async with factory() as db:
        tenant = Tenant(name=f"{name}-{uuid.uuid4().hex[:8]}")
        db.add(tenant)
        await db.commit()
        return tenant.id


async def _seed_gateway(factory, tenant_id: int, gateway_id: str) -> str:
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
            status=PlugStatus.AVAILABLE,
        )
        db.add(plug)
        await db.commit()
        return plug.id


async def _seed_user(factory, balance: str, tenant_id=None) -> int:
    from backend.database.models import User

    async with factory() as db:
        user = User(
            email=f"limits-{uuid.uuid4().hex[:12]}@example.com",
            hashed_password="x", full_name="Limits Driver",
            tenant_id=tenant_id, coin_balance=Decimal(balance),
        )
        db.add(user)
        await db.commit()
        return user.id


def _fake_user(user_id: int):
    """Pre-authorized User stand-in (start_charging_session re-selects the
    real row under a lock, so only `.id` needs to be real)."""
    u = MagicMock()
    u.id = user_id
    return u


async def _start_session(factory, monkeypatch, *, plug_id: int, user_id: int,
                          max_kwh=None, max_duration=None):
    """Call the start handler directly against a real DB session, gateway/
    telemetry side-effects stubbed. max_kwh/max_duration None = omit from the
    request (schema defaults apply), matching a client that sends no limit."""
    import backend.routers.sessions as sessions_module
    from backend import state as state_module
    from backend.routers.sessions import start_charging_session
    from backend.schemas import SessionStartRequest

    monkeypatch.setattr(
        state_module, "mqtt_manager",
        MagicMock(send_plug_command=MagicMock(return_value=True)),
    )
    monkeypatch.setattr(sessions_module, "set_plug_telemetry_interval", AsyncMock())

    kwargs = {"plug_id": plug_id}
    if max_kwh is not None:
        kwargs["max_kwh"] = max_kwh
    if max_duration is not None:
        kwargs["max_duration_seconds"] = max_duration
    req = SessionStartRequest(**kwargs)
    async with factory() as db:
        return await start_charging_session(req, _fake_user(user_id), db)


@db_gated
@pytest.mark.asyncio
async def test_user_limits_persisted_at_start_and_echoed(factory, monkeypatch):
    """Explicit limits from the request land on the session row verbatim and
    come back in the start response."""
    from sqlalchemy import select

    from backend.database.models import ChargingSession

    tenant_id = await _seed_tenant(factory)
    gw = await _seed_gateway(factory, tenant_id, "gw-lim-1")
    plug_id = await _seed_plug(factory, gw)
    uid = await _seed_user(factory, "500.00", tenant_id)

    result = await _start_session(
        factory, monkeypatch, plug_id=plug_id, user_id=uid,
        max_kwh=1.0, max_duration=1800,
    )
    assert result["status"] == "started"
    assert result["max_kwh"] == 1.0
    assert result["max_duration_seconds"] == 1800

    async with factory() as db:
        session = (await db.execute(
            select(ChargingSession).where(ChargingSession.id == result["session_id"])
        )).scalar_one()
    assert session.max_kwh == 1.0
    assert session.max_duration_seconds == 1800


@db_gated
@pytest.mark.asyncio
async def test_default_limits_persisted_when_client_sends_none(factory, monkeypatch):
    """A bare {plug_id} start (what the pre-limit frontend always sent) still
    persists the schema defaults — every new session has enforceable limits."""
    from sqlalchemy import select

    from backend.database.models import ChargingSession

    tenant_id = await _seed_tenant(factory)
    gw = await _seed_gateway(factory, tenant_id, "gw-lim-2")
    plug_id = await _seed_plug(factory, gw)
    uid = await _seed_user(factory, "500.00", tenant_id)

    result = await _start_session(factory, monkeypatch, plug_id=plug_id, user_id=uid)

    async with factory() as db:
        session = (await db.execute(
            select(ChargingSession).where(ChargingSession.id == result["session_id"])
        )).scalar_one()
    assert session.max_kwh == 30.0            # SessionStartRequest default
    assert session.max_duration_seconds == 14400  # 4 h default


@db_gated
@pytest.mark.asyncio
async def test_active_sessions_expose_limits(factory, monkeypatch):
    from backend.routers.sessions import get_active_session

    tenant_id = await _seed_tenant(factory)
    gw = await _seed_gateway(factory, tenant_id, "gw-lim-3")
    plug_id = await _seed_plug(factory, gw)
    uid = await _seed_user(factory, "500.00", tenant_id)

    await _start_session(
        factory, monkeypatch, plug_id=plug_id, user_id=uid,
        max_kwh=2.5, max_duration=3600,
    )

    async with factory() as db:
        res = await get_active_session(_fake_user(uid), db)

    assert res["active"] is True
    assert res["sessions"][0]["max_kwh"] == 2.5
    assert res["sessions"][0]["max_duration_seconds"] == 3600


@db_gated
@pytest.mark.asyncio
async def test_finalize_receipt_carries_limits(factory, monkeypatch):
    """The stop receipt exposes the session's limits (and the reason), so the
    UI can say which limit an auto-stop hit. Legacy NULL-limit sessions
    surface None — additive, nothing else about finalize changes."""
    import backend.services.session_lifecycle as sl_mod
    from backend import state as state_module
    from backend.database.models import ChargingSession, SessionStatus

    tenant_id = await _seed_tenant(factory)
    gw = await _seed_gateway(factory, tenant_id, "gw-lim-4")
    plug_id = await _seed_plug(factory, gw)
    uid = await _seed_user(factory, "200.00", tenant_id)

    async with factory() as db:
        session = ChargingSession(
            tenant_id=tenant_id, user_id=uid, plug_id=plug_id,
            status=SessionStatus.ACTIVE, energy_kwh=1.0,
            rate_coins_per_kwh=Decimal("5.00"), hold_coins=Decimal("5.00"),
            max_kwh=1.0, max_duration_seconds=1800,
        )
        db.add(session)
        await db.commit()
        session_id = session.id

    monkeypatch.setattr(
        state_module, "mqtt_manager",
        MagicMock(send_plug_command=MagicMock(return_value=True)),
    )
    monkeypatch.setattr(sl_mod, "set_plug_telemetry_interval", AsyncMock())
    state_module.telemetry_store.start_session(plug_id)

    async with factory() as db:
        outcome = await sl_mod.finalize_charging_session(
            db, session_id, reason="auto-stopped: energy limit reached"
        )

    assert outcome is not None
    assert outcome["max_kwh"] == 1.0
    assert outcome["max_duration_seconds"] == 1800
    assert outcome["reason"] == "auto-stopped: energy limit reached"


# =============================================================================
# 1b. DB-gated: PATCH /api/sessions/{id}/limits — edit a running session
#     ("start now, set the target later"). Covers persistence, the auth-hold
#     re-size (grow/cap/shrink), and the 404/409 guards.
# =============================================================================

async def _seed_active_session(
    factory, *, tenant_id, user_id, plug_id,
    max_kwh, max_duration, hold="50.00", rate="5.00", status=None, energy=0.0,
):
    """Insert a charging session (ACTIVE by default) and return its id."""
    from backend.database.models import ChargingSession, SessionStatus

    async with factory() as db:
        s = ChargingSession(
            tenant_id=tenant_id, user_id=user_id, plug_id=plug_id,
            status=status or SessionStatus.ACTIVE, energy_kwh=energy,
            rate_coins_per_kwh=Decimal(rate),
            hold_coins=Decimal(hold) if hold is not None else None,
            max_kwh=max_kwh, max_duration_seconds=max_duration,
        )
        db.add(s)
        await db.commit()
        return s.id


async def _patch_limits(factory, *, session_id, user_id, max_kwh=None, max_duration=None):
    from backend.routers.sessions import update_session_limits
    from backend.schemas import SessionLimitsUpdateRequest

    kwargs = {}
    if max_kwh is not None:
        kwargs["max_kwh"] = max_kwh
    if max_duration is not None:
        kwargs["max_duration_seconds"] = max_duration
    req = SessionLimitsUpdateRequest(**kwargs)
    async with factory() as db:
        return await update_session_limits(session_id, req, _fake_user(user_id), db)


@db_gated
@pytest.mark.asyncio
async def test_patch_updates_limits_and_echoes(factory):
    """PATCH persists the new stop conditions and echoes them back."""
    from sqlalchemy import select

    from backend.database.models import ChargingSession

    tenant_id = await _seed_tenant(factory)
    gw = await _seed_gateway(factory, tenant_id, "gw-patch-1")
    plug_id = await _seed_plug(factory, gw)
    uid = await _seed_user(factory, "500.00", tenant_id)
    sid = await _seed_active_session(
        factory, tenant_id=tenant_id, user_id=uid, plug_id=plug_id,
        max_kwh=1.0, max_duration=1800,
    )

    result = await _patch_limits(factory, session_id=sid, user_id=uid, max_kwh=2.0, max_duration=3600)
    assert result["status"] == "updated"
    assert result["max_kwh"] == 2.0
    assert result["max_duration_seconds"] == 3600

    async with factory() as db:
        session = (await db.execute(
            select(ChargingSession).where(ChargingSession.id == sid)
        )).scalar_one()
    assert session.max_kwh == 2.0
    assert session.max_duration_seconds == 3600


@db_gated
@pytest.mark.asyncio
async def test_patch_pushes_set_limits_to_firmware(factory, monkeypatch):
    """A limit PATCH pushes the updated watchdogs to the gateway (SET_LIMITS)
    with BOTH current values, so raising a limit above the on-device value
    takes effect. A duration-only edit still sends the (unchanged) max_kwh."""
    from unittest.mock import MagicMock

    from backend import state as state_module

    tenant_id = await _seed_tenant(factory)
    gw = await _seed_gateway(factory, tenant_id, "gw-patch-fw")
    plug_id = await _seed_plug(factory, gw)
    uid = await _seed_user(factory, "500.00", tenant_id)
    sid = await _seed_active_session(
        factory, tenant_id=tenant_id, user_id=uid, plug_id=plug_id,
        max_kwh=1.0, max_duration=1800,
    )

    fake_mgr = MagicMock()
    monkeypatch.setattr(state_module, "mqtt_manager", fake_mgr)

    await _patch_limits(factory, session_id=sid, user_id=uid, max_duration=3600)

    fake_mgr.send_plug_limits.assert_called_once()
    kwargs = fake_mgr.send_plug_limits.call_args.kwargs
    assert kwargs["max_kwh"] == 1.0            # unchanged, still pushed
    assert kwargs["max_duration_seconds"] == 3600  # the raised value
    # The push also re-arms the on-device current cap at the plug's effective
    # cap (its own max_current_a, or DEFAULT_PLUG_CAP_A) — never omitted here.
    assert kwargs["max_current_a"] is not None


@db_gated
@pytest.mark.asyncio
async def test_patch_legacy_null_limit_session_skips_firmware_push(factory, monkeypatch):
    """A legacy session with NULL limits never carried firmware limits — a PATCH
    that leaves one side NULL doesn't push SET_LIMITS."""
    from unittest.mock import MagicMock

    from backend import state as state_module

    tenant_id = await _seed_tenant(factory)
    gw = await _seed_gateway(factory, tenant_id, "gw-patch-legacy")
    plug_id = await _seed_plug(factory, gw)
    uid = await _seed_user(factory, "500.00", tenant_id)
    sid = await _seed_active_session(
        factory, tenant_id=tenant_id, user_id=uid, plug_id=plug_id,
        max_kwh=None, max_duration=None,
    )

    fake_mgr = MagicMock()
    monkeypatch.setattr(state_module, "mqtt_manager", fake_mgr)

    # Set only duration — max_kwh stays NULL, so the firmware push is skipped.
    await _patch_limits(factory, session_id=sid, user_id=uid, max_duration=3600)
    fake_mgr.send_plug_limits.assert_not_called()


@db_gated
@pytest.mark.asyncio
async def test_patch_only_updates_the_field_provided(factory):
    """A duration-only PATCH leaves max_kwh (and its hold) untouched."""
    from sqlalchemy import select

    from backend.database.models import ChargingSession

    tenant_id = await _seed_tenant(factory)
    gw = await _seed_gateway(factory, tenant_id, "gw-patch-2")
    plug_id = await _seed_plug(factory, gw)
    uid = await _seed_user(factory, "500.00", tenant_id)
    sid = await _seed_active_session(
        factory, tenant_id=tenant_id, user_id=uid, plug_id=plug_id,
        max_kwh=1.0, max_duration=1800, hold="5.00",
    )

    await _patch_limits(factory, session_id=sid, user_id=uid, max_duration=7200)

    async with factory() as db:
        session = (await db.execute(
            select(ChargingSession).where(ChargingSession.id == sid)
        )).scalar_one()
    assert session.max_duration_seconds == 7200
    assert session.max_kwh == 1.0                     # unchanged
    assert session.hold_coins == Decimal("5.00")      # hold untouched (no max_kwh change)


@db_gated
@pytest.mark.asyncio
async def test_patch_grows_hold_when_max_kwh_raised(factory):
    """Raising max_kwh grows the hold to min(available + own hold, kwh * rate)."""
    from sqlalchemy import select

    from backend.database.models import ChargingSession

    tenant_id = await _seed_tenant(factory)
    gw = await _seed_gateway(factory, tenant_id, "gw-patch-3")
    plug_id = await _seed_plug(factory, gw)
    uid = await _seed_user(factory, "500.00", tenant_id)
    # hold 5 (= 1 kWh * 5). Raise to 10 kWh → 50 coins, well within the 500 wallet.
    sid = await _seed_active_session(
        factory, tenant_id=tenant_id, user_id=uid, plug_id=plug_id,
        max_kwh=1.0, max_duration=1800, hold="5.00", rate="5.00",
    )

    await _patch_limits(factory, session_id=sid, user_id=uid, max_kwh=10.0)

    async with factory() as db:
        session = (await db.execute(
            select(ChargingSession).where(ChargingSession.id == sid)
        )).scalar_one()
    assert session.hold_coins == Decimal("50.00")


@db_gated
@pytest.mark.asyncio
async def test_patch_hold_capped_by_available_balance(factory):
    """A raised ceiling the wallet can't back caps the hold at what's available
    (this session's own hold added back in)."""
    from sqlalchemy import select

    from backend.database.models import ChargingSession

    tenant_id = await _seed_tenant(factory)
    gw = await _seed_gateway(factory, tenant_id, "gw-patch-4")
    plug_id = await _seed_plug(factory, gw)
    uid = await _seed_user(factory, "20.00", tenant_id)  # thin wallet
    sid = await _seed_active_session(
        factory, tenant_id=tenant_id, user_id=uid, plug_id=plug_id,
        max_kwh=1.0, max_duration=1800, hold="5.00", rate="5.00",
    )

    # 100 kWh * 5 = 500 wanted, but only 20 coins exist → hold caps at 20.
    await _patch_limits(factory, session_id=sid, user_id=uid, max_kwh=100.0)

    async with factory() as db:
        session = (await db.execute(
            select(ChargingSession).where(ChargingSession.id == sid)
        )).scalar_one()
    assert session.hold_coins == Decimal("20.00")


@db_gated
@pytest.mark.asyncio
async def test_patch_shrinks_hold_when_max_kwh_lowered(factory):
    """Lowering max_kwh shrinks the hold, freeing the difference."""
    from sqlalchemy import select

    from backend.database.models import ChargingSession

    tenant_id = await _seed_tenant(factory)
    gw = await _seed_gateway(factory, tenant_id, "gw-patch-5")
    plug_id = await _seed_plug(factory, gw)
    uid = await _seed_user(factory, "500.00", tenant_id)
    sid = await _seed_active_session(
        factory, tenant_id=tenant_id, user_id=uid, plug_id=plug_id,
        max_kwh=10.0, max_duration=1800, hold="50.00", rate="5.00",
    )

    await _patch_limits(factory, session_id=sid, user_id=uid, max_kwh=2.0)

    async with factory() as db:
        session = (await db.execute(
            select(ChargingSession).where(ChargingSession.id == sid)
        )).scalar_one()
    assert session.hold_coins == Decimal("10.00")     # 2 kWh * 5


@db_gated
@pytest.mark.asyncio
async def test_patch_409_when_max_kwh_below_energy_already_delivered(factory):
    """[Security] The driver-exploitable hold-forgiveness bug (backend/routers/
    sessions.py update_session_limits): charge most of the way to the target,
    then PATCH max_kwh down to a sliver so the naive resize (and the
    telemetry-path auto-stop mirror) would let finalize collect almost
    nothing for energy already consumed. The PATCH itself must be rejected —
    not merely re-sized — so a 200 here can never be followed within ~1 s by
    an auto-stop at a target the driver never actually charged down to."""
    from fastapi import HTTPException
    from sqlalchemy import select

    from backend.database.models import ChargingSession

    tenant_id = await _seed_tenant(factory)
    gw = await _seed_gateway(factory, tenant_id, "gw-patch-exploit")
    plug_id = await _seed_plug(factory, gw)
    uid = await _seed_user(factory, "500.00", tenant_id)
    # 29 kWh already delivered against a 30 kWh target; hold sized for the
    # original target (30 kWh * 5.00 = 150.00) — mirrors the reported exploit
    # ("charge 29 kWh, PATCH max_kwh to 0.1, pay ~0.1 kWh").
    sid = await _seed_active_session(
        factory, tenant_id=tenant_id, user_id=uid, plug_id=plug_id,
        max_kwh=30.0, max_duration=14400, hold="150.00", rate="5.00",
        energy=29.0,
    )

    with pytest.raises(HTTPException) as exc:
        await _patch_limits(factory, session_id=sid, user_id=uid, max_kwh=0.1)
    assert exc.value.status_code == 409
    assert "already delivered" in exc.value.detail

    # Rejected outright — neither max_kwh nor the hold moved.
    async with factory() as db:
        session = (await db.execute(
            select(ChargingSession).where(ChargingSession.id == sid)
        )).scalar_one()
    assert session.max_kwh == 30.0
    assert session.hold_coins == Decimal("150.00")


@db_gated
@pytest.mark.asyncio
async def test_patch_hold_floored_at_accrued_cost_even_when_kwh_target_allowed(factory):
    """[Security] Even a max_kwh PATCH that CLEARS the 409 guard (new target
    still >= energy already delivered) must not let the hold resize dip below
    what's already owed. A segmented (TOD) session can carry settled cost from
    an earlier, HIGHER rate than the plug's current/future rate, so naively
    re-deriving the hold as energy_cost(new_max_kwh, max_rate) can undershoot
    the real accrued cost even though new_max_kwh > energy delivered. The
    floor — billing.session_cost at the session's own energy_kwh, the same
    function finalize bills with — must win."""
    from sqlalchemy import select

    from backend.database.models import ChargingSession

    tenant_id = await _seed_tenant(factory)
    gw = await _seed_gateway(factory, tenant_id, "gw-patch-floor")
    plug_id = await _seed_plug(factory, gw)
    uid = await _seed_user(factory, "500.00", tenant_id)
    sid = await _seed_active_session(
        factory, tenant_id=tenant_id, user_id=uid, plug_id=plug_id,
        max_kwh=30.0, max_duration=14400, hold="150.00", rate="5.00",
        energy=5.0,
    )
    # Fabricate a closed high-rate segment: 5 kWh already settled at a rate
    # that nets 100.00 coins, with the open segment starting fresh at the
    # current energy (nothing accrued in it yet). No tariff on the plug, so
    # max_rate_over_window resolves to the 5.00 env default for the future
    # window — well under the 100.00 already locked into settled_cost_coins.
    async with factory() as db:
        session = (await db.execute(
            select(ChargingSession).where(ChargingSession.id == sid)
        )).scalar_one()
        session.settled_cost_coins = Decimal("100.00")
        session.rate_segment_start_kwh = 5.0
        await db.commit()

    # New target (6 kWh) is above the 5 kWh already delivered — clears the 409
    # guard — but energy_cost(6.0, 5.00) = 30.00, far under the 100.00 already
    # owed for the closed segment. session_cost(session, 5.0) = 100.00 exactly
    # (settled 100.00 + 0 open-segment energy), so the floor must hold at 100.
    await _patch_limits(factory, session_id=sid, user_id=uid, max_kwh=6.0)

    async with factory() as db:
        session = (await db.execute(
            select(ChargingSession).where(ChargingSession.id == sid)
        )).scalar_one()
    assert session.max_kwh == 6.0
    assert session.hold_coins == Decimal("100.00")   # floored, not shrunk to 30.00


@db_gated
@pytest.mark.asyncio
async def test_finalize_bills_full_accrued_cost_with_no_forgiven_overage_after_blocked_patch(
    factory, monkeypatch,
):
    """End-to-end version of the exploit report: a rejected max_kwh PATCH must
    leave the hold intact, so finalize goes on to collect the FULL accrued
    cost for energy already delivered — none of it forgiven."""
    from unittest.mock import AsyncMock, MagicMock

    from fastapi import HTTPException
    from sqlalchemy import select

    import backend.services.session_lifecycle as sl_mod
    from backend import state as state_module
    from backend.database.models import ChargingSession, User

    tenant_id = await _seed_tenant(factory)
    gw = await _seed_gateway(factory, tenant_id, "gw-patch-finalize")
    plug_id = await _seed_plug(factory, gw)
    uid = await _seed_user(factory, "500.00", tenant_id)
    sid = await _seed_active_session(
        factory, tenant_id=tenant_id, user_id=uid, plug_id=plug_id,
        max_kwh=30.0, max_duration=14400, hold="150.00", rate="5.00",
        energy=29.0,
    )

    # The exploit attempt: rejected, not merely re-sized.
    with pytest.raises(HTTPException) as exc:
        await _patch_limits(factory, session_id=sid, user_id=uid, max_kwh=0.1)
    assert exc.value.status_code == 409

    monkeypatch.setattr(
        state_module, "mqtt_manager",
        MagicMock(send_plug_command=MagicMock(return_value=True)),
    )
    monkeypatch.setattr(sl_mod, "set_plug_telemetry_interval", AsyncMock())
    state_module.telemetry_store.start_session(plug_id)

    async with factory() as db:
        outcome = await sl_mod.finalize_charging_session(db, sid)

    assert outcome is not None
    # 29 kWh * 5.00 coins/kWh = 145.00 — the FULL accrued cost, none forgiven
    # (the pre-fix bug would have left the hold at ~0.10-kWh's worth after
    # the malicious PATCH, capping this at pennies instead).
    assert outcome["coins_spent"] == 145.0
    assert outcome["shortfall_coins"] == 0.0

    async with factory() as db:
        session = (await db.execute(
            select(ChargingSession).where(ChargingSession.id == sid)
        )).scalar_one()
        user = (await db.execute(select(User).where(User.id == uid))).scalar_one()
    assert session.coins_spent == Decimal("145.00")
    assert user.coin_balance == Decimal("355.00")   # 500.00 - 145.00, no shortfall


@db_gated
@pytest.mark.asyncio
async def test_patch_resizes_hold_when_only_max_duration_raised_into_higher_rate(factory):
    """REC-07: a PATCH raising ONLY max_duration into a window that crosses a
    higher-rate TOD slot must re-size the hold — max_rate_over_window rises with
    the longer window, so the hold can't be left under-covering."""
    from sqlalchemy import select

    from backend.database.models import (
        ChargingSession,
        Plug,
        TariffSlot,
    )
    from backend.database.models import (
        Tariff as TariffModel,
    )

    tenant_id = await _seed_tenant(factory)
    gw = await _seed_gateway(factory, tenant_id, "gw-patch-tod")
    plug_id = await _seed_plug(factory, gw)
    uid = await _seed_user(factory, "500.00", tenant_id)

    # Base 5.00 tariff + an all-week slot at 10.00. A >=24h window necessarily
    # touches the slot, so max_rate_over_window returns 10.00 for the raised
    # window (vs 5.00 base) — the whole point of the resize.
    async with factory() as db:
        tariff = TariffModel(tenant_id=tenant_id, name="TOD", price_per_kwh=Decimal("5.00"))
        db.add(tariff)
        await db.commit()
        tariff_id = tariff.id
        db.add(TariffSlot(
            tariff_id=tariff_id, start_min=540, end_min=1020,
            price_per_kwh=Decimal("10.00"), days_mask=127,
        ))
        plug = (await db.execute(select(Plug).where(Plug.id == plug_id))).scalar_one()
        plug.tariff_id = tariff_id
        await db.commit()

    # Seeded hold is the short-window sizing (1 kWh * 5.00 base = 5.00).
    sid = await _seed_active_session(
        factory, tenant_id=tenant_id, user_id=uid, plug_id=plug_id,
        max_kwh=1.0, max_duration=1800, hold="5.00", rate="5.00",
    )

    # Raise ONLY max_duration to a full day → window now crosses the 10.00 slot.
    await _patch_limits(factory, session_id=sid, user_id=uid, max_duration=24 * 3600)

    async with factory() as db:
        session = (await db.execute(
            select(ChargingSession).where(ChargingSession.id == sid)
        )).scalar_one()
    assert session.max_duration_seconds == 24 * 3600
    assert session.max_kwh == 1.0                     # unchanged
    assert session.hold_coins == Decimal("10.00")     # 1 kWh * 10.00 worst-case


@db_gated
@pytest.mark.asyncio
async def test_patch_409_when_session_not_active(factory):
    from fastapi import HTTPException

    from backend.database.models import SessionStatus

    tenant_id = await _seed_tenant(factory)
    gw = await _seed_gateway(factory, tenant_id, "gw-patch-6")
    plug_id = await _seed_plug(factory, gw)
    uid = await _seed_user(factory, "500.00", tenant_id)
    sid = await _seed_active_session(
        factory, tenant_id=tenant_id, user_id=uid, plug_id=plug_id,
        max_kwh=1.0, max_duration=1800, status=SessionStatus.COMPLETED,
    )

    with pytest.raises(HTTPException) as exc:
        await _patch_limits(factory, session_id=sid, user_id=uid, max_kwh=2.0)
    assert exc.value.status_code == 409


@db_gated
@pytest.mark.asyncio
async def test_patch_404_when_not_owner(factory):
    from fastapi import HTTPException

    tenant_id = await _seed_tenant(factory)
    gw = await _seed_gateway(factory, tenant_id, "gw-patch-7")
    plug_id = await _seed_plug(factory, gw)
    owner = await _seed_user(factory, "500.00", tenant_id)
    other = await _seed_user(factory, "500.00", tenant_id)
    sid = await _seed_active_session(
        factory, tenant_id=tenant_id, user_id=owner, plug_id=plug_id,
        max_kwh=1.0, max_duration=1800,
    )

    with pytest.raises(HTTPException) as exc:
        await _patch_limits(factory, session_id=sid, user_id=other, max_kwh=2.0)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_patch_400_when_no_fields_given():
    """The empty-request guard fires before any DB work — so this needs no DB."""
    from fastapi import HTTPException

    from backend.routers.sessions import update_session_limits
    from backend.schemas import SessionLimitsUpdateRequest

    with pytest.raises(HTTPException) as exc:
        await update_session_limits(1, SessionLimitsUpdateRequest(), _fake_user(1), None)
    assert exc.value.status_code == 400


# =============================================================================
# 2. DB-free: the telemetry-path auto-stop mirror
# =============================================================================

class _NullDB:
    """Async-context stand-in for a db_session_factory() call the code under
    test never actually queries through (finalize itself is mocked)."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeResult:
    def __init__(self, scalar=None):
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._scalar


class _FakeSession:
    """Async-context session returning queued results (test_mqtt_manager.py
    convention)."""

    def __init__(self, results):
        self._results = iter(results)
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, *_a, **_k):
        return next(self._results)

    async def commit(self):
        self.committed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("max_kwh,energy_kwh,should_stop", [
    (1.0, 0.5, False),   # under the limit → keep charging
    (1.0, 1.0, True),    # exactly at the limit → stop (>=)
    (1.0, 1.2, True),    # past the limit → stop
    (30.0, 29.99, False),  # default cap, not yet reached
])
async def test_energy_limit_auto_stop_via_finalize(max_kwh, energy_kwh, should_stop):
    from backend.services.mqtt_manager import MQTTManager

    MQTTManager._instance = None
    mgr = MQTTManager(db_session_factory=lambda: _NullDB())

    finalize_mock = AsyncMock(return_value={"energy_kwh": energy_kwh, "coins_spent": 0})
    with patch("backend.services.session_lifecycle.finalize_charging_session", finalize_mock):
        await mgr._maybe_auto_stop_on_limits(
            session_id=7, user_id=3, energy_kwh=energy_kwh,
            max_kwh=max_kwh, max_duration_seconds=None, started_at=None,
        )

    assert finalize_mock.called is should_stop
    if should_stop:
        args, kwargs = finalize_mock.call_args
        assert args[1] == 7  # session_id
        assert kwargs.get("reason") == "auto-stopped: energy limit reached"
    MQTTManager._instance = None


@pytest.mark.asyncio
@pytest.mark.parametrize("elapsed_sec,max_duration,should_stop", [
    (3600, 1800, True),    # ran twice the limit → stop
    (1800, 1800, True),    # exactly at the limit → stop (>=)
    (600, 1800, False),    # still inside the window → keep charging
])
async def test_time_limit_auto_stop_via_finalize(elapsed_sec, max_duration, should_stop):
    from backend.services.mqtt_manager import MQTTManager

    MQTTManager._instance = None
    mgr = MQTTManager(db_session_factory=lambda: _NullDB())
    started_at = datetime.now(timezone.utc) - timedelta(seconds=elapsed_sec)

    finalize_mock = AsyncMock(return_value={"energy_kwh": 0.1, "coins_spent": 0})
    with patch("backend.services.session_lifecycle.finalize_charging_session", finalize_mock):
        await mgr._maybe_auto_stop_on_limits(
            session_id=8, user_id=3, energy_kwh=0.1,
            max_kwh=None, max_duration_seconds=max_duration, started_at=started_at,
        )

    assert finalize_mock.called is should_stop
    if should_stop:
        assert finalize_mock.call_args.kwargs.get("reason") == "auto-stopped: time limit reached"
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_time_limit_handles_legacy_naive_started_at():
    """A naive (tz-less) started_at from a legacy row is treated as UTC —
    same convention as gateway_is_live/finalize — not crashed on."""
    from backend.services.mqtt_manager import MQTTManager

    MQTTManager._instance = None
    mgr = MQTTManager(db_session_factory=lambda: _NullDB())
    # Naive on purpose (what a legacy pre-tz row reflects back).
    naive_started = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=3600)
    assert naive_started.tzinfo is None

    finalize_mock = AsyncMock(return_value={"energy_kwh": 0.1, "coins_spent": 0})
    with patch("backend.services.session_lifecycle.finalize_charging_session", finalize_mock):
        await mgr._maybe_auto_stop_on_limits(
            session_id=8, user_id=3, energy_kwh=0.1,
            max_kwh=None, max_duration_seconds=1800, started_at=naive_started,
        )

    assert finalize_mock.called is True
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_energy_reason_wins_when_both_limits_trip():
    """When one frame crosses both limits, the recorded reason is the energy
    one (energy is measured; elapsed time is merely implied) — and finalize
    still runs exactly once."""
    from backend.services.mqtt_manager import MQTTManager

    MQTTManager._instance = None
    mgr = MQTTManager(db_session_factory=lambda: _NullDB())

    finalize_mock = AsyncMock(return_value={"energy_kwh": 2.0, "coins_spent": 0})
    with patch("backend.services.session_lifecycle.finalize_charging_session", finalize_mock):
        await mgr._maybe_auto_stop_on_limits(
            session_id=9, user_id=3, energy_kwh=2.0,
            max_kwh=1.0, max_duration_seconds=60,
            started_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )

    finalize_mock.assert_awaited_once()
    assert finalize_mock.call_args.kwargs.get("reason") == "auto-stopped: energy limit reached"
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_limits_auto_stop_disabled_by_flag(monkeypatch):
    """With AUTO_STOP_ON_LIMITS off, no limit check runs at all — even a
    blatantly exceeded limit never finalizes."""
    import backend.services.mqtt_manager as mm
    from backend.services.mqtt_manager import MQTTManager

    monkeypatch.setattr(mm, "AUTO_STOP_ON_LIMITS", False)

    MQTTManager._instance = None
    mgr = MQTTManager(db_session_factory=lambda: _NullDB())

    finalize_mock = AsyncMock()
    with patch("backend.services.session_lifecycle.finalize_charging_session", finalize_mock):
        await mgr._maybe_auto_stop_on_limits(
            session_id=7, user_id=3, energy_kwh=100.0,
            max_kwh=1.0, max_duration_seconds=60,
            started_at=datetime.now(timezone.utc) - timedelta(hours=5),
        )

    finalize_mock.assert_not_called()
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_legacy_null_limit_session_never_limit_auto_stops():
    """A legacy session (NULL max_kwh AND NULL max_duration_seconds —
    predating the 0014 columns) must be completely unaffected: no finalize,
    regardless of how much energy/time has accrued."""
    from backend.services.mqtt_manager import MQTTManager

    MQTTManager._instance = None
    mgr = MQTTManager(db_session_factory=lambda: _NullDB())

    finalize_mock = AsyncMock()
    with patch("backend.services.session_lifecycle.finalize_charging_session", finalize_mock):
        await mgr._maybe_auto_stop_on_limits(
            session_id=7, user_id=3, energy_kwh=999.0,
            max_kwh=None, max_duration_seconds=None,
            started_at=datetime.now(timezone.utc) - timedelta(days=2),
        )

    finalize_mock.assert_not_called()
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_persist_telemetry_forwards_session_limits_into_the_check():
    """The telemetry path: _persist_telemetry must call the limit check with
    the ACTIVE session's OWN persisted limits + started_at (this is the
    ~1 s-latency mirror; the reaper is only a backstop). Mirrors
    test_mqtt_manager.py's _persist_telemetry fake-session pattern."""
    from backend.services.mqtt_manager import MQTTManager

    MQTTManager._instance = None
    plug = MagicMock()
    plug.gateway_id = "gw-1"
    plug.last_telemetry_at = None  # [Plug power] first frame stamps the clock
    started = datetime.now(timezone.utc) - timedelta(minutes=10)
    sess_row = MagicMock()
    sess_row.id = 42
    sess_row.user_id = 3
    sess_row.rate_coins_per_kwh = Decimal("5.00")
    sess_row.hold_coins = Decimal("50.00")
    sess_row.max_kwh = 1.0
    sess_row.max_duration_seconds = 1800
    sess_row.started_at = started
    sess_row.energy_kwh = 0.0    # real float: monotonic max() reads it
    sess_row.peak_power_w = 0.0  # real float: compared against watts
    # [Pricing v2] Flat session: no TOD boundary, no segment accrual — so the
    # in-frame reprice hook is a clean no-op (rate_valid_until None).
    sess_row.rate_valid_until = None
    sess_row.settled_cost_coins = None
    sess_row.rate_segment_start_kwh = None

    session = _FakeSession([
        _FakeResult(scalar=plug),      # ownership lookup
        _FakeResult(scalar=sess_row),  # ACTIVE session on the plug
    ])
    mgr = MQTTManager(db_session_factory=lambda: session)
    mgr._maybe_auto_stop_on_exhaustion = AsyncMock()
    mgr._maybe_auto_stop_on_limits = AsyncMock()

    # relay_on=True: a real charging frame reports the relay on, so this is
    # attributed to the ACTIVE session (not treated as an idle frame).
    await mgr._persist_telemetry("gw-1", 5, 100.0, 1.2, None, None, relay_on=True)

    assert session.committed is True
    mgr._maybe_auto_stop_on_limits.assert_awaited_once_with(
        42, 3, 1.2, 1.0, 1800, started
    )
    # The pre-existing exhaustion check still runs (and first).
    mgr._maybe_auto_stop_on_exhaustion.assert_awaited_once()
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_persist_telemetry_no_active_session_skips_limit_check():
    """Idle telemetry (no ACTIVE session on the plug) never reaches the
    limit check."""
    from backend.services.mqtt_manager import MQTTManager

    MQTTManager._instance = None
    plug = MagicMock()
    plug.gateway_id = "gw-1"
    session = _FakeSession([
        _FakeResult(scalar=plug),
        _FakeResult(scalar=None),  # no ACTIVE session
    ])
    mgr = MQTTManager(db_session_factory=lambda: session)
    mgr._maybe_auto_stop_on_exhaustion = AsyncMock()
    mgr._maybe_auto_stop_on_limits = AsyncMock()

    await mgr._persist_telemetry("gw-1", 5, 100.0, 1.2, None, None)

    mgr._maybe_auto_stop_on_limits.assert_not_called()
    mgr._maybe_auto_stop_on_exhaustion.assert_not_called()
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_persist_telemetry_post_reset_frame_still_triggers_max_kwh_stop():
    """[Fix] A frame arriving right after a mid-session gateway reboot reports
    a tiny raw kwh (the device counter reset to ~0), but the session's
    PERSISTED total already exceeds max_kwh. The auto-stop mirror must be
    driven by the session's MONOTONIC total (active_session.energy_kwh, post
    the REC-01 max() clamp) — not the raw per-frame value — or a reset would
    silently defeat the limit and let charging continue unbilled/uncapped."""
    from backend.services.mqtt_manager import MQTTManager

    MQTTManager._instance = None
    plug = MagicMock()
    plug.gateway_id = "gw-1"
    plug.last_telemetry_at = None
    sess_row = MagicMock()
    sess_row.id = 42
    sess_row.user_id = 3
    sess_row.rate_coins_per_kwh = Decimal("5.00")
    sess_row.hold_coins = None
    sess_row.max_kwh = 1.0  # already exceeded by the persisted total below
    sess_row.max_duration_seconds = None
    sess_row.started_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    sess_row.energy_kwh = 5.0    # real float: the PRE-frame persisted total (past max_kwh)
    sess_row.peak_power_w = 0.0
    sess_row.rate_valid_until = None
    sess_row.settled_cost_coins = None
    sess_row.rate_segment_start_kwh = None

    session = _FakeSession([
        _FakeResult(scalar=plug),
        _FakeResult(scalar=sess_row),
    ])
    mgr = MQTTManager(db_session_factory=lambda: session)
    mgr._maybe_auto_stop_on_exhaustion = AsyncMock()

    finalize_mock = AsyncMock(return_value={"energy_kwh": 5.0, "coins_spent": 0})
    with patch("backend.services.session_lifecycle.finalize_charging_session", finalize_mock), \
         patch("backend.services.pricing.reprice_session_if_due", AsyncMock(return_value=None)):
        # Raw frame kwh is tiny (post-reboot device counter) — on its own it
        # would be far under max_kwh, but the persisted total must win.
        await mgr._persist_telemetry("gw-1", 5, 100.0, 0.05,
                                     session_id=42, sample=None, relay_on=True)

    finalize_mock.assert_awaited_once()
    assert finalize_mock.call_args.args[1] == 42  # session_id
    assert finalize_mock.call_args.kwargs.get("reason") == "auto-stopped: energy limit reached"
    MQTTManager._instance = None


def test_finalize_maps_limit_reasons_into_the_stop_notification():
    """The session-stopped notification (emitted inside finalize) must map
    the limit reasons — the reason string itself lands in the body (extends
    test_notifications.py's marker sweep)."""
    import inspect

    from backend.services import session_lifecycle

    src = inspect.getsource(session_lifecycle.finalize_charging_session)
    assert "limit reached" in src, "finalize no longer maps the limit-reached reasons"
    assert "your limit was reached" in src


# =============================================================================
# 3. Reaper duration backstop
# =============================================================================

def _factory_yielding(db):
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=db)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


def _db_with_limit_rows(rows):
    result = MagicMock()
    result.all.return_value = list(rows)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    return db


@pytest.mark.asyncio
async def test_reaper_backstop_finalizes_only_overdue_sessions():
    from backend.services.session_reaper import (
        TIME_LIMIT_REAP_REASON,
        SessionReaperService,
    )

    now = datetime.now(timezone.utc)
    rows = [
        (41, now - timedelta(hours=2), 3600),   # overdue → reap
        (42, now - timedelta(minutes=10), 3600),  # inside its window → leave
        (43, None, 60),                          # no started_at → skip safely
        # naive legacy → treated as UTC, overdue
        (44, now.replace(tzinfo=None) - timedelta(hours=2), 3600),
    ]
    db = _db_with_limit_rows(rows)
    finalize = AsyncMock(return_value={"energy_kwh": 0.5, "coins_spent": 2.5})
    svc = SessionReaperService(_factory_yielding(db), finalize)

    assert await svc.reap_time_limited_once() == 2

    assert [c.args[1] for c in finalize.await_args_list] == [41, 44]
    assert all(
        c.kwargs == {"reason": TIME_LIMIT_REAP_REASON}
        for c in finalize.await_args_list
    )


@pytest.mark.asyncio
async def test_reaper_backstop_race_loser_not_counted():
    """finalize -> None (the telemetry-path mirror or a user stop won the
    row-lock race) must not count as reaped."""
    from backend.services.session_reaper import SessionReaperService

    now = datetime.now(timezone.utc)
    db = _db_with_limit_rows([(51, now - timedelta(hours=2), 3600)])
    finalize = AsyncMock(return_value=None)
    svc = SessionReaperService(_factory_yielding(db), finalize)

    assert await svc.reap_time_limited_once() == 0
    finalize.assert_awaited_once()


@pytest.mark.asyncio
async def test_reaper_backstop_disabled_by_flag(monkeypatch):
    import backend.services.session_reaper as sr
    from backend.services.session_reaper import SessionReaperService

    monkeypatch.setattr(sr, "AUTO_STOP_ON_LIMITS", False)

    factory = MagicMock()  # would explode if the sweep even opened a session
    finalize = AsyncMock()
    svc = SessionReaperService(factory, finalize)

    assert await svc.reap_time_limited_once() == 0
    finalize.assert_not_called()
    factory.assert_not_called()


def test_reaper_backstop_query_filters_active_with_limit():
    """The backstop query must restrict to ACTIVE sessions that actually
    carry a duration limit (legacy NULL-limit sessions excluded in SQL)."""
    from backend.services.session_reaper import SessionReaperService

    svc = SessionReaperService(MagicMock(), AsyncMock())
    sql = str(svc._time_limited_sessions_query()).lower()
    assert "max_duration_seconds" in sql
    assert "status" in sql
    assert "is not null" in sql
