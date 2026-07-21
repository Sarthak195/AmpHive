"""
Tariff / pricing tests — need a real PostgreSQL (CI's postgres:15 service,
exported as TEST_DATABASE_URL). Skipped locally: this repo's dev boxes run no
database by policy.

What's proven here:
1. resolve_rate_for_plug's fallback chain (services/pricing.py): plug's own
   tariff -> its charger group's tariff -> the tenant's default tariff -> the
   global COINS_PER_KWH env var. First match at each level wins.
2. Snapshot immutability: a session's rate_coins_per_kwh, resolved and
   written once at start, is unaffected by a later tariff price edit — and
   finalize_charging_session bills off that snapshot, not a fresh resolution
   against the now-edited tariff.
3. Cross-tenant tariff assignment is rejected by the CPO router's
   plug/group/tenant-default assignment endpoints, in both directions: a CPO
   can't attach another tenant's tariff, and can't reach a plug/group that
   isn't theirs even with their own tariff.
"""
import os
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

    # Mirror the app factory's expire_on_commit=False (backend/database/db.py).
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


# --- Seed helpers --------------------------------------------------------------

async def _seed_tenant(factory, name: str = "Tenant") -> int:
    from backend.database.models import Tenant

    async with factory() as db:
        tenant = Tenant(name=name)
        db.add(tenant)
        await db.commit()
        return tenant.id


async def _seed_gateway(factory, tenant_id: int, gateway_id: str) -> str:
    from backend.database.models import Gateway

    async with factory() as db:
        gw = Gateway(id=gateway_id, tenant_id=tenant_id, name=gateway_id, vpn_ip=gateway_id)
        db.add(gw)
        await db.commit()
        return gw.id


async def _seed_tariff(factory, tenant_id: int, price: str, name: str = "Tariff") -> int:
    from backend.database.models import Tariff

    async with factory() as db:
        tariff = Tariff(tenant_id=tenant_id, name=name, price_per_kwh=Decimal(price))
        db.add(tariff)
        await db.commit()
        return tariff.id


async def _seed_group(factory, tenant_id: int, tariff_id=None, name: str = "Group") -> int:
    from backend.database.models import ChargerGroup

    async with factory() as db:
        group = ChargerGroup(tenant_id=tenant_id, name=name, is_public=True, tariff_id=tariff_id)
        db.add(group)
        await db.commit()
        return group.id


async def _seed_plug(factory, gateway_id: str, group_id=None, tariff_id=None, name: str = "Plug") -> int:
    from backend.database.models import Plug

    async with factory() as db:
        plug = Plug(
            gateway_id=gateway_id, name=name, local_ip="10.0.0.5",
            group_id=group_id, tariff_id=tariff_id,
        )
        db.add(plug)
        await db.commit()
        return plug.id


async def _load_plug(factory, plug_id: int):
    from sqlalchemy import select

    from backend.database.models import Plug

    async with factory() as db:
        result = await db.execute(select(Plug).where(Plug.id == plug_id))
        return result.scalar_one()


def _cpo_user(tenant_id: int, user_id: int = 1):
    """A pre-authorized 'cpo' User stand-in — RBAC (require_role) is enforced
    by FastAPI's dependency injection at the HTTP layer, not exercised when
    calling the router function directly (matches the existing
    test_gateway_ota.py / test_session_start_plug_status.py convention)."""
    from unittest.mock import MagicMock

    u = MagicMock()
    u.id = user_id
    u.tenant_id = tenant_id
    u.email = "cpo@amphive.test"
    return u


# --- 1. resolve_rate_for_plug fallback chain -----------------------------------

@pytest.mark.asyncio
async def test_resolves_env_default_when_nothing_configured(factory, monkeypatch):
    """No plug tariff, no group tariff, no tenant default -> the global
    COINS_PER_KWH env fallback."""
    import backend.services.pricing as pricing_mod

    monkeypatch.setattr(pricing_mod, "COINS_PER_KWH", 5.0)

    tenant_id = await _seed_tenant(factory)
    gateway_id = await _seed_gateway(factory, tenant_id, "gw-1")
    plug_id = await _seed_plug(factory, gateway_id)

    plug = await _load_plug(factory, plug_id)
    async with factory() as db:
        rate = await pricing_mod.resolve_rate_for_plug(db, plug)

    assert rate == Decimal("5.00")


@pytest.mark.asyncio
async def test_resolves_tenant_default_over_env(factory):
    """A tenant default tariff beats the env fallback when the plug is
    unassigned and ungrouped."""
    from sqlalchemy import select

    from backend.database.models import Tenant
    from backend.services.pricing import resolve_rate_for_plug

    tenant_id = await _seed_tenant(factory)
    gateway_id = await _seed_gateway(factory, tenant_id, "gw-2")
    tariff_id = await _seed_tariff(factory, tenant_id, "7.50", "Tenant Default")
    plug_id = await _seed_plug(factory, gateway_id)

    async with factory() as db:
        tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one()
        tenant.default_tariff_id = tariff_id
        await db.commit()

    plug = await _load_plug(factory, plug_id)
    async with factory() as db:
        rate = await resolve_rate_for_plug(db, plug)

    assert rate == Decimal("7.50")


@pytest.mark.asyncio
async def test_resolves_group_tariff_over_tenant_default(factory):
    """The plug's charger group's tariff beats the tenant default when the
    plug itself has no tariff of its own."""
    from sqlalchemy import select

    from backend.database.models import Tenant
    from backend.services.pricing import resolve_rate_for_plug

    tenant_id = await _seed_tenant(factory)
    gateway_id = await _seed_gateway(factory, tenant_id, "gw-3")
    tenant_tariff_id = await _seed_tariff(factory, tenant_id, "7.50", "Tenant Default")
    group_tariff_id = await _seed_tariff(factory, tenant_id, "6.00", "Group Rate")
    group_id = await _seed_group(factory, tenant_id, tariff_id=group_tariff_id)
    plug_id = await _seed_plug(factory, gateway_id, group_id=group_id)

    async with factory() as db:
        tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one()
        tenant.default_tariff_id = tenant_tariff_id
        await db.commit()

    plug = await _load_plug(factory, plug_id)
    async with factory() as db:
        rate = await resolve_rate_for_plug(db, plug)

    assert rate == Decimal("6.00")


@pytest.mark.asyncio
async def test_resolves_plug_tariff_over_group_and_tenant_default(factory):
    """The plug's own tariff wins over everything else in the chain."""
    from sqlalchemy import select

    from backend.database.models import Tenant
    from backend.services.pricing import resolve_rate_for_plug

    tenant_id = await _seed_tenant(factory)
    gateway_id = await _seed_gateway(factory, tenant_id, "gw-4")
    tenant_tariff_id = await _seed_tariff(factory, tenant_id, "7.50", "Tenant Default")
    group_tariff_id = await _seed_tariff(factory, tenant_id, "6.00", "Group Rate")
    plug_tariff_id = await _seed_tariff(factory, tenant_id, "4.25", "Plug Rate")
    group_id = await _seed_group(factory, tenant_id, tariff_id=group_tariff_id)
    plug_id = await _seed_plug(factory, gateway_id, group_id=group_id, tariff_id=plug_tariff_id)

    async with factory() as db:
        tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one()
        tenant.default_tariff_id = tenant_tariff_id
        await db.commit()

    plug = await _load_plug(factory, plug_id)
    async with factory() as db:
        rate = await resolve_rate_for_plug(db, plug)

    assert rate == Decimal("4.25")


# --- 2. Snapshot immutability ---------------------------------------------------

@pytest.mark.asyncio
async def test_snapshot_immutable_to_later_tariff_price_change(factory, monkeypatch):
    """
    A session's rate_coins_per_kwh, resolved + snapshotted at start (mirrors
    routers/sessions.py start_charging_session), must stay put through a
    later tariff price edit — and finalize_charging_session must bill off
    that snapshot, not a fresh resolution against the now-edited tariff.
    """
    from unittest.mock import AsyncMock, MagicMock

    from sqlalchemy import select

    import backend.services.session_lifecycle as sl_mod
    from backend import state as state_module
    from backend.database.models import ChargingSession, SessionStatus, Tariff, User
    from backend.services.pricing import resolve_rate_for_plug

    tenant_id = await _seed_tenant(factory)
    gateway_id = await _seed_gateway(factory, tenant_id, "gw-snap")
    tariff_id = await _seed_tariff(factory, tenant_id, "6.00", "Standard")
    plug_id = await _seed_plug(factory, gateway_id, tariff_id=tariff_id)

    plug = await _load_plug(factory, plug_id)
    async with factory() as db:
        rate_at_start = await resolve_rate_for_plug(db, plug)
    assert rate_at_start == Decimal("6.00")

    # Seed a user + an ACTIVE session snapshotting that rate, as
    # start_charging_session does at POST /api/sessions/start.
    async with factory() as db:
        user = User(
            email="driver-snap@example.com", hashed_password="x",
            full_name="Snapshot Driver", tenant_id=tenant_id,
            coin_balance=Decimal("1000.00"),
        )
        db.add(user)
        await db.flush()
        session = ChargingSession(
            tenant_id=tenant_id, user_id=user.id, plug_id=plug_id,
            status=SessionStatus.ACTIVE, rate_coins_per_kwh=rate_at_start,
            energy_kwh=2.0,
        )
        db.add(session)
        await db.commit()
        await db.refresh(session)
        session_id = session.id
        user_id = user.id

    # A CPO edits the tariff's price mid-session (PUT /api/cpo/tariffs/{id}).
    async with factory() as db:
        tariff = (await db.execute(select(Tariff).where(Tariff.id == tariff_id))).scalar_one()
        tariff.price_per_kwh = Decimal("9.00")
        await db.commit()

    # A fresh resolution for the same plug now sees the NEW price...
    plug = await _load_plug(factory, plug_id)
    async with factory() as db:
        rate_after_edit = await resolve_rate_for_plug(db, plug)
    assert rate_after_edit == Decimal("9.00")

    # ...but the ACTIVE session's own snapshot is untouched.
    async with factory() as db:
        reloaded = (await db.execute(
            select(ChargingSession).where(ChargingSession.id == session_id)
        )).scalar_one()
    assert reloaded.rate_coins_per_kwh == Decimal("6.00")

    # finalize_charging_session must bill at the OLD (snapshotted) rate:
    # 2.0 kWh * 6.00 = 12.00, NOT 2.0 kWh * 9.00 = 18.00.
    monkeypatch.setattr(
        state_module, "mqtt_manager",
        MagicMock(send_plug_command=MagicMock(return_value=True)),
    )
    # set_plug_telemetry_interval reaches into a real MQTTManager() singleton
    # internally, which isn't relevant to what this test is verifying — stub
    # it out rather than dragging paho/broker state into a pricing test.
    monkeypatch.setattr(sl_mod, "set_plug_telemetry_interval", AsyncMock())
    # Deterministic telemetry baseline for this plug_id, regardless of what
    # any other test in this process may have left in the shared singleton.
    state_module.telemetry_store.start_session(plug_id)

    async with factory() as db:
        outcome = await sl_mod.finalize_charging_session(db, session_id)

    assert outcome is not None
    assert outcome["price_per_kwh"] == 6.0
    assert outcome["coins_spent"] == 12.0
    assert outcome["shortfall_coins"] == 0.0

    async with factory() as db:
        final_user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
    assert final_user.coin_balance == Decimal("988.00")  # 1000.00 - 12.00


# --- 3. Cross-tenant tariff assignment rejected ---------------------------------

@pytest.mark.asyncio
async def test_cross_tenant_plug_tariff_assignment_rejected(factory):
    """A CPO must not be able to attach ANOTHER tenant's tariff to their own plug."""
    from fastapi import HTTPException

    from backend.routers.cpo import cpo_assign_plug_tariff
    from backend.schemas import CpoTariffAssignRequest

    tenant_a = await _seed_tenant(factory, "Tenant A")
    tenant_b = await _seed_tenant(factory, "Tenant B")
    gw_a = await _seed_gateway(factory, tenant_a, "gw-a")
    plug_a = await _seed_plug(factory, gw_a)
    tariff_b = await _seed_tariff(factory, tenant_b, "8.00", "Tenant B tariff")

    cpo_a = _cpo_user(tenant_a)

    async with factory() as db:
        with pytest.raises(HTTPException) as exc:
            await cpo_assign_plug_tariff(
                plug_a, CpoTariffAssignRequest(tariff_id=tariff_b), cpo_a, db
            )
    assert exc.value.status_code == 404

    plug = await _load_plug(factory, plug_a)
    assert plug.tariff_id is None  # rejected before any write


@pytest.mark.asyncio
async def test_cross_tenant_plug_target_not_owned_rejected(factory):
    """The inverse direction: a CPO must not be able to touch a PLUG that
    isn't theirs, even to assign their OWN tariff to it."""
    from fastapi import HTTPException

    from backend.routers.cpo import cpo_assign_plug_tariff
    from backend.schemas import CpoTariffAssignRequest

    tenant_a = await _seed_tenant(factory, "Tenant A")
    tenant_b = await _seed_tenant(factory, "Tenant B")
    gw_b = await _seed_gateway(factory, tenant_b, "gw-b")
    plug_b = await _seed_plug(factory, gw_b)  # owned by tenant B
    tariff_a = await _seed_tariff(factory, tenant_a, "6.50", "Tenant A tariff")

    cpo_a = _cpo_user(tenant_a)

    async with factory() as db:
        with pytest.raises(HTTPException) as exc:
            await cpo_assign_plug_tariff(
                plug_b, CpoTariffAssignRequest(tariff_id=tariff_a), cpo_a, db
            )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_cross_tenant_group_tariff_assignment_rejected(factory):
    from fastapi import HTTPException

    from backend.routers.cpo import cpo_assign_group_tariff
    from backend.schemas import CpoTariffAssignRequest

    tenant_a = await _seed_tenant(factory, "Tenant A")
    tenant_b = await _seed_tenant(factory, "Tenant B")
    group_a = await _seed_group(factory, tenant_a)
    tariff_b = await _seed_tariff(factory, tenant_b, "8.00", "Tenant B tariff")

    cpo_a = _cpo_user(tenant_a)

    async with factory() as db:
        with pytest.raises(HTTPException) as exc:
            await cpo_assign_group_tariff(
                group_a, CpoTariffAssignRequest(tariff_id=tariff_b), cpo_a, db
            )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_cross_tenant_tenant_default_assignment_rejected(factory):
    from fastapi import HTTPException

    from backend.routers.cpo import cpo_assign_tenant_default_tariff
    from backend.schemas import CpoTariffAssignRequest

    tenant_a = await _seed_tenant(factory, "Tenant A")
    tenant_b = await _seed_tenant(factory, "Tenant B")
    tariff_b = await _seed_tariff(factory, tenant_b, "8.00", "Tenant B tariff")

    cpo_a = _cpo_user(tenant_a)

    async with factory() as db:
        with pytest.raises(HTTPException) as exc:
            await cpo_assign_tenant_default_tariff(
                CpoTariffAssignRequest(tariff_id=tariff_b), cpo_a, db
            )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_same_tenant_plug_tariff_assignment_succeeds(factory):
    """Positive control: same-tenant assignment (plug, tariff both owned by
    the caller) must actually succeed — proves the 404s above are really
    about cross-tenant isolation, not a rejection of everything."""
    from sqlalchemy import select

    from backend.database.models import Plug
    from backend.routers.cpo import cpo_assign_plug_tariff
    from backend.schemas import CpoTariffAssignRequest

    tenant_a = await _seed_tenant(factory, "Tenant A")
    gw_a = await _seed_gateway(factory, tenant_a, "gw-a")
    plug_a = await _seed_plug(factory, gw_a)
    tariff_a = await _seed_tariff(factory, tenant_a, "6.50", "Tenant A tariff")

    cpo_a = _cpo_user(tenant_a)

    async with factory() as db:
        result = await cpo_assign_plug_tariff(
            plug_a, CpoTariffAssignRequest(tariff_id=tariff_a), cpo_a, db
        )
    assert result == {"status": "updated", "plug_id": plug_a, "tariff_id": tariff_a}

    async with factory() as db:
        plug = (await db.execute(select(Plug).where(Plug.id == plug_a))).scalar_one()
    assert plug.tariff_id == tariff_a
