"""
CPO offline (cash) top-up tests — need a real PostgreSQL (CI's postgres:15
service, exported as TEST_DATABASE_URL). Skipped locally, same as
test_payouts.py / test_disputes.py.

What's proven here:

1. Pool math (services/payouts.py tenant_earnings_summary's
   available_pool_coins): a fresh tenant's pool equals its unsettled net
   session earnings; issuing a top-up reduces the pool by exactly that
   amount (GET /api/cpo/earnings's topup_pool reflects it immediately).
2. The payout-watermark interaction (the core "no double draw" guarantee):
   a bank payout requested AFTER an offline top-up pays out the reduced
   figure (unsettled net minus top-ups already issued), not the raw
   unsettled net — and a payout request is a clean 400 (not a negative
   payout) when top-ups already consumed the entire window. Proven both
   sequentially and under real concurrency (two requests racing the same
   tenant row lock can never both draw the same earnings).
3. POST /api/cpo/topups: 409 (with the actual available figure) over the
   pool, 404 for an unknown/non-driver email, credits the driver's wallet
   and writes a matching CPO_TOPUP LedgerTransaction that reconciles
   (amount + balance_after), and a topup.create AuditLog row.
4. GET /api/cpo/topups: paginated {total, items} shape, tenant-scoped.
5. Role gating: require_role("cpo", "admin") rejects a driver.
"""
import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Optional

import pytest
import pytest_asyncio
from fastapi import HTTPException

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")

requires_db = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="needs TEST_DATABASE_URL pointing at a throwaway Postgres (CI service)",
)

ENUM_TYPES = [
    "gateway_status", "plug_status", "session_status", "tx_type", "user_role",
    "payout_status",
]


# ===========================================================================
# Schema validation (no DB needed)
# ===========================================================================

def test_topup_request_rejects_non_positive_amount():
    import pydantic

    from backend.schemas import CpoTopupCreateRequest

    with pytest.raises(pydantic.ValidationError):
        CpoTopupCreateRequest(driver_email="driver@example.com", amount_coins=0)
    with pytest.raises(pydantic.ValidationError):
        CpoTopupCreateRequest(driver_email="driver@example.com", amount_coins=-5)


def test_topup_request_accepts_any_email_string():
    # driver_email is deliberately a plain str (see CpoTopupCreateRequest's
    # docstring): it names an EXISTING account and the router 404s unknowns,
    # while EmailStr would reject the seeded @amphive.test accounts
    # (reserved TLD — caught by CI 2026-07-21). A malformed value is simply
    # an account that doesn't exist.
    from backend.schemas import CpoTopupCreateRequest

    req = CpoTopupCreateRequest(driver_email="not-an-email", amount_coins=10)
    assert req.driver_email == "not-an-email"


# ===========================================================================
# Role gating (no DB needed)
# ===========================================================================

@pytest.mark.asyncio
async def test_topup_role_gate_rejects_driver():
    from backend.database.models import UserRole
    from backend.services.rbac import require_role

    checker = require_role("cpo", "admin")
    driver = SimpleNamespace(role=UserRole.DRIVER)
    with pytest.raises(HTTPException) as exc:
        await checker(user=driver)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_topup_role_gate_accepts_cpo():
    from backend.database.models import UserRole
    from backend.services.rbac import require_role

    checker = require_role("cpo", "admin")
    cpo = SimpleNamespace(role=UserRole.CPO)
    assert await checker(user=cpo) is cpo


# ===========================================================================
# DB-gated tests
# ===========================================================================

@pytest_asyncio.fixture()
async def factory():
    """Engine + fresh schema per test; yields a session factory. Mirrors
    test_payouts.py's fixture exactly."""
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


def _cpo_user(tenant_id, user_id, email="cpo@example.com"):
    from backend.database.models import UserRole

    return SimpleNamespace(id=user_id, tenant_id=tenant_id, role=UserRole.CPO, email=email)


async def _seed_tenant_with_earnings(
    factory, *, gross_coins: str = "100.00", session_ended_hours_ago: float = 1.0
) -> dict:
    """A tenant with a CPO, a driver, and one COMPLETED session earning
    `gross_coins` (ended just under an hour ago, safely inside any
    [watermark, now) window a test computes moments later)."""
    from backend.database.models import (
        ChargingSession,
        Gateway,
        Plug,
        SessionStatus,
        Tenant,
        User,
        UserRole,
    )

    tag = uuid.uuid4().hex[:10]
    now = datetime.now(timezone.utc)

    async with factory() as db:
        tenant = Tenant(name=f"tenant-{tag}")
        db.add(tenant)
        await db.flush()

        cpo = User(
            email=f"cpo-{tag}@amphive.test", hashed_password="x",
            full_name="CPO Test", role=UserRole.CPO, tenant_id=tenant.id,
        )
        driver = User(
            email=f"driver-{tag}@amphive.test", hashed_password="x",
            full_name="Driver Test", role=UserRole.DRIVER,
        )
        db.add_all([cpo, driver])
        await db.flush()

        u = uuid.uuid4().int
        gateway = Gateway(
            id=f"gw-{tag}", tenant_id=tenant.id, name="Test GW",
            vpn_ip=f"10.{u % 250 + 1}.{(u >> 8) % 250 + 1}.{(u >> 16) % 250 + 1}",
        )
        db.add(gateway)
        await db.flush()

        plug = Plug(gateway_id=gateway.id, name="Test Plug", local_ip="10.0.1.1")
        db.add(plug)
        await db.flush()

        ended_at = now - timedelta(hours=session_ended_hours_ago)
        session = ChargingSession(
            tenant_id=tenant.id, user_id=driver.id, plug_id=plug.id,
            status=SessionStatus.COMPLETED, coins_spent=Decimal(gross_coins),
            started_at=ended_at - timedelta(hours=1), ended_at=ended_at,
        )
        db.add(session)
        await db.commit()

        return {
            "tenant_id": tenant.id,
            "cpo_id": cpo.id,
            "cpo_email": cpo.email,
            "driver_id": driver.id,
            "driver_email": driver.email,
        }


async def _seed_extra_user(factory, *, role):
    from backend.database.models import User

    async with factory() as db:
        user = User(
            email=f"extra-{uuid.uuid4().hex[:10]}@amphive.test",
            hashed_password="x", full_name="Extra", role=role,
        )
        db.add(user)
        await db.commit()
        return user.id, user.email


async def _fresh_balance(factory, user_id: int) -> Decimal:
    from sqlalchemy import select

    from backend.database.models import User

    async with factory() as db:
        result = await db.execute(select(User.coin_balance).where(User.id == user_id))
        return result.scalar_one()


async def _ledger_rows_for_user(factory, user_id: int):
    from sqlalchemy import select

    from backend.database.models import LedgerTransaction

    async with factory() as db:
        result = await db.execute(
            select(LedgerTransaction)
            .where(LedgerTransaction.user_id == user_id)
            .order_by(LedgerTransaction.id)
        )
        return list(result.scalars().all())


async def _audit_rows(factory, tenant_id, action):
    from sqlalchemy import and_, select

    from backend.database.models import AuditLog

    async with factory() as db:
        result = await db.execute(
            select(AuditLog).where(
                and_(AuditLog.tenant_id == tenant_id, AuditLog.action == action)
            )
        )
        return result.scalars().all()


async def _offline_topup_count(factory, tenant_id: Optional[int] = None) -> int:
    from sqlalchemy import func, select

    from backend.database.models import OfflineTopup

    async with factory() as db:
        stmt = select(func.count(OfflineTopup.id))
        if tenant_id is not None:
            stmt = stmt.where(OfflineTopup.tenant_id == tenant_id)
        return (await db.execute(stmt)).scalar() or 0


async def _payout_count(factory, tenant_id: int) -> int:
    from sqlalchemy import func, select

    from backend.database.models import Payout

    async with factory() as db:
        result = await db.execute(
            select(func.count(Payout.id)).where(Payout.tenant_id == tenant_id)
        )
        return result.scalar() or 0


# --- POST /api/cpo/topups: happy path + ledger reconciliation --------------

@requires_db
@pytest.mark.asyncio
async def test_topup_credits_wallet_and_writes_reconciling_ledger_row(factory):
    from backend.database.models import TransactionType
    from backend.routers.cpo import cpo_create_topup
    from backend.schemas import CpoTopupCreateRequest

    world = await _seed_tenant_with_earnings(factory, gross_coins="100.00")
    cpo = _cpo_user(world["tenant_id"], world["cpo_id"], world["cpo_email"])

    async with factory() as db:
        res = await cpo_create_topup(
            CpoTopupCreateRequest(
                driver_email=world["driver_email"], amount_coins=30.00, note="cash, pump 3",
            ),
            cpo, db,
        )

    assert res.tenant_id == world["tenant_id"]
    assert res.driver_user_id == world["driver_id"]
    assert res.driver_email == world["driver_email"]
    assert res.actor_email == world["cpo_email"]
    assert res.amount_coins == 30.00
    assert res.note == "cash, pump 3"
    assert res.created_at is not None

    assert await _fresh_balance(factory, world["driver_id"]) == Decimal("30.00")

    rows = await _ledger_rows_for_user(factory, world["driver_id"])
    assert len(rows) == 1
    assert rows[0].transaction_type == TransactionType.CPO_TOPUP
    assert rows[0].amount == Decimal("30.00")
    assert rows[0].balance_after == Decimal("30.00")

    audit = await _audit_rows(factory, world["tenant_id"], "topup.create")
    assert len(audit) == 1
    assert audit[0].actor_user_id == world["cpo_id"]
    assert world["driver_email"] in audit[0].detail
    assert "amount=30.00" in audit[0].detail


@requires_db
@pytest.mark.asyncio
async def test_topup_409_when_exceeding_available_pool(factory):
    from backend.routers.cpo import cpo_create_topup
    from backend.schemas import CpoTopupCreateRequest

    # gross=100, fee 10% -> net=90 available.
    world = await _seed_tenant_with_earnings(factory, gross_coins="100.00")
    cpo = _cpo_user(world["tenant_id"], world["cpo_id"], world["cpo_email"])

    async with factory() as db:
        with pytest.raises(HTTPException) as exc:
            await cpo_create_topup(
                CpoTopupCreateRequest(driver_email=world["driver_email"], amount_coins=90.01),
                cpo, db,
            )
    assert exc.value.status_code == 409
    assert "90.00" in exc.value.detail  # the actual available figure

    # Nothing moved: no balance change, no OfflineTopup row persisted.
    assert await _fresh_balance(factory, world["driver_id"]) == Decimal("0.00")
    assert await _offline_topup_count(factory, world["tenant_id"]) == 0


@requires_db
@pytest.mark.asyncio
async def test_topup_allows_exactly_the_available_pool(factory):
    from backend.routers.cpo import cpo_create_topup
    from backend.schemas import CpoTopupCreateRequest

    world = await _seed_tenant_with_earnings(factory, gross_coins="100.00")
    cpo = _cpo_user(world["tenant_id"], world["cpo_id"], world["cpo_email"])

    async with factory() as db:
        res = await cpo_create_topup(
            CpoTopupCreateRequest(driver_email=world["driver_email"], amount_coins=90.00),
            cpo, db,
        )
    assert res.amount_coins == 90.00
    assert await _fresh_balance(factory, world["driver_id"]) == Decimal("90.00")


@requires_db
@pytest.mark.asyncio
async def test_topup_404_for_unknown_driver_email(factory):
    from backend.routers.cpo import cpo_create_topup
    from backend.schemas import CpoTopupCreateRequest

    world = await _seed_tenant_with_earnings(factory)
    cpo = _cpo_user(world["tenant_id"], world["cpo_id"], world["cpo_email"])

    async with factory() as db:
        with pytest.raises(HTTPException) as exc:
            await cpo_create_topup(
                CpoTopupCreateRequest(driver_email="nobody@amphive.test", amount_coins=5.00),
                cpo, db,
            )
    assert exc.value.status_code == 404


@requires_db
@pytest.mark.asyncio
async def test_topup_404_when_email_belongs_to_a_non_driver(factory):
    """A CPO can only credit a driver — an email that resolves to a CPO or
    admin account is treated the same as "no such driver", not a 400/403, so
    a caller can't use this to probe which emails exist on the platform."""
    from backend.database.models import UserRole
    from backend.routers.cpo import cpo_create_topup
    from backend.schemas import CpoTopupCreateRequest

    world = await _seed_tenant_with_earnings(factory)
    cpo = _cpo_user(world["tenant_id"], world["cpo_id"], world["cpo_email"])
    _, other_cpo_email = await _seed_extra_user(factory, role=UserRole.CPO)

    async with factory() as db:
        with pytest.raises(HTTPException) as exc:
            await cpo_create_topup(
                CpoTopupCreateRequest(driver_email=other_cpo_email, amount_coins=5.00),
                cpo, db,
            )
    assert exc.value.status_code == 404


@requires_db
@pytest.mark.asyncio
async def test_topup_400_without_a_tenant(factory):
    from backend.database.models import UserRole
    from backend.routers.cpo import cpo_create_topup
    from backend.schemas import CpoTopupCreateRequest

    admin = SimpleNamespace(id=1, tenant_id=None, role=UserRole.ADMIN, email="admin@amphive.test")
    async with factory() as db:
        with pytest.raises(HTTPException) as exc:
            await cpo_create_topup(
                CpoTopupCreateRequest(driver_email="driver@amphive.test", amount_coins=5.00),
                admin, db,
            )
    assert exc.value.status_code == 400


# --- GET /api/cpo/topups: pagination + tenant scoping -----------------------

@requires_db
@pytest.mark.asyncio
async def test_list_topups_paginated_and_tenant_scoped(factory):
    from backend.routers.cpo import cpo_create_topup, cpo_list_topups
    from backend.schemas import CpoTopupCreateRequest

    world1 = await _seed_tenant_with_earnings(factory, gross_coins="200.00")
    world2 = await _seed_tenant_with_earnings(factory, gross_coins="200.00")
    cpo1 = _cpo_user(world1["tenant_id"], world1["cpo_id"], world1["cpo_email"])
    cpo2 = _cpo_user(world2["tenant_id"], world2["cpo_id"], world2["cpo_email"])

    async with factory() as db:
        await cpo_create_topup(
            CpoTopupCreateRequest(driver_email=world1["driver_email"], amount_coins=10.00), cpo1, db,
        )
    async with factory() as db:
        await cpo_create_topup(
            CpoTopupCreateRequest(driver_email=world1["driver_email"], amount_coins=5.00), cpo1, db,
        )
    async with factory() as db:
        await cpo_create_topup(
            CpoTopupCreateRequest(driver_email=world2["driver_email"], amount_coins=7.00), cpo2, db,
        )

    async with factory() as db:
        page = await cpo_list_topups(cpo1, db, limit=1, offset=0)
    assert page["total"] == 2
    assert len(page["items"]) == 1
    assert page["items"][0].amount_coins == 5.00  # newest first
    assert page["items"][0].driver_email == world1["driver_email"]
    assert page["items"][0].actor_email == world1["cpo_email"]

    async with factory() as db:
        page2 = await cpo_list_topups(cpo2, db, limit=50, offset=0)
    assert page2["total"] == 1
    assert page2["items"][0].amount_coins == 7.00


# --- The payout-watermark interaction (the core guarantee) ------------------

@requires_db
@pytest.mark.asyncio
async def test_earnings_pool_reflects_topups_issued_this_window(factory):
    from backend.routers.cpo import cpo_create_topup, cpo_earnings
    from backend.schemas import CpoTopupCreateRequest

    world = await _seed_tenant_with_earnings(factory, gross_coins="100.00")
    cpo = _cpo_user(world["tenant_id"], world["cpo_id"], world["cpo_email"])

    async with factory() as db:
        before = await cpo_earnings(cpo, db)
    assert before["topup_pool"]["available_coins"] == pytest.approx(90.00)
    assert before["topup_pool"]["already_issued_coins"] == pytest.approx(0.0)

    async with factory() as db:
        await cpo_create_topup(
            CpoTopupCreateRequest(driver_email=world["driver_email"], amount_coins=40.00), cpo, db,
        )

    async with factory() as db:
        after = await cpo_earnings(cpo, db)
    assert after["topup_pool"]["available_coins"] == pytest.approx(50.00)
    assert after["topup_pool"]["already_issued_coins"] == pytest.approx(40.00)
    # The unsettled leg itself is unaffected -- topups don't touch session earnings.
    assert after["unsettled"]["net_coins"] == pytest.approx(90.00)


@requires_db
@pytest.mark.asyncio
async def test_payout_pays_out_net_minus_topups_already_issued(factory):
    """The core 'no double draw' guarantee: a payout requested after a
    top-up must pay out unsettled net MINUS the top-up, not the raw net."""
    from backend.routers.cpo import cpo_create_topup, cpo_request_payout
    from backend.schemas import CpoTopupCreateRequest

    world = await _seed_tenant_with_earnings(factory, gross_coins="100.00")
    cpo = _cpo_user(world["tenant_id"], world["cpo_id"], world["cpo_email"])

    async with factory() as db:
        await cpo_create_topup(
            CpoTopupCreateRequest(driver_email=world["driver_email"], amount_coins=40.00), cpo, db,
        )

    async with factory() as db:
        payout = await cpo_request_payout(user=cpo, db=db)

    # gross/fee still reflect the TRUE session earnings for the window...
    assert payout["gross_coins"] == pytest.approx(100.00)
    assert payout["platform_fee_coins"] == pytest.approx(10.00)
    # ...but net is reduced by the 40 already handed out as cash.
    assert payout["net_coins"] == pytest.approx(50.00)


@requires_db
@pytest.mark.asyncio
async def test_payout_400_when_topups_already_consumed_the_entire_pool(factory):
    from backend.routers.cpo import cpo_create_topup, cpo_request_payout
    from backend.schemas import CpoTopupCreateRequest

    world = await _seed_tenant_with_earnings(factory, gross_coins="50.00")
    cpo = _cpo_user(world["tenant_id"], world["cpo_id"], world["cpo_email"])

    async with factory() as db:
        await cpo_create_topup(
            CpoTopupCreateRequest(driver_email=world["driver_email"], amount_coins=45.00), cpo, db,
        )

    async with factory() as db:
        with pytest.raises(HTTPException) as exc:
            await cpo_request_payout(user=cpo, db=db)
    assert exc.value.status_code == 400
    assert "45.00" in exc.value.detail
    assert await _payout_count(factory, world["tenant_id"]) == 0


@requires_db
@pytest.mark.asyncio
async def test_topup_after_a_payout_is_capped_by_the_new_empty_window(factory):
    """Once a payout snapshots [watermark, now) forward, that same window's
    earnings can no longer fund a NEW top-up -- the watermark has advanced,
    so a fresh top-up request sees an empty (or session-only) new window."""
    from backend.routers.cpo import cpo_create_topup, cpo_request_payout
    from backend.schemas import CpoTopupCreateRequest

    world = await _seed_tenant_with_earnings(factory, gross_coins="100.00")
    cpo = _cpo_user(world["tenant_id"], world["cpo_id"], world["cpo_email"])

    async with factory() as db:
        payout = await cpo_request_payout(user=cpo, db=db)
    assert payout["net_coins"] == pytest.approx(90.00)

    # The watermark has advanced past the session that funded the payout --
    # a top-up attempt now has nothing left to draw from.
    async with factory() as db:
        with pytest.raises(HTTPException) as exc:
            await cpo_create_topup(
                CpoTopupCreateRequest(driver_email=world["driver_email"], amount_coins=1.00), cpo, db,
            )
    assert exc.value.status_code == 409
    assert "0.00" in exc.value.detail


@requires_db
@pytest.mark.asyncio
async def test_concurrent_topup_and_payout_never_both_draw_the_full_pool(factory):
    """Race-safety proof: a top-up for the FULL pool and a payout request
    fired concurrently against the same tenant must not both succeed --
    the tenant row lock (shared by both endpoints) serializes them, and
    whichever runs second sees the pool the first one already consumed."""
    from backend.routers.cpo import cpo_create_topup, cpo_request_payout
    from backend.schemas import CpoTopupCreateRequest

    world = await _seed_tenant_with_earnings(factory, gross_coins="100.00")
    cpo = _cpo_user(world["tenant_id"], world["cpo_id"], world["cpo_email"])

    outcomes = {}

    async def do_topup():
        async with factory() as db:
            try:
                res = await cpo_create_topup(
                    CpoTopupCreateRequest(driver_email=world["driver_email"], amount_coins=90.00),
                    cpo, db,
                )
                outcomes["topup"] = ("ok", res)
            except HTTPException as exc:
                outcomes["topup"] = ("err", exc.status_code)

    async def do_payout():
        async with factory() as db:
            try:
                res = await cpo_request_payout(user=cpo, db=db)
                outcomes["payout"] = ("ok", res)
            except HTTPException as exc:
                outcomes["payout"] = ("err", exc.status_code)

    await asyncio.gather(do_topup(), do_payout())

    kinds = sorted(kind for kind, _ in outcomes.values())
    assert kinds == ["err", "ok"], f"exactly one of topup/payout must succeed, got {outcomes}"

    if outcomes["topup"][0] == "ok":
        assert outcomes["payout"] == ("err", 400)
        assert await _fresh_balance(factory, world["driver_id"]) == Decimal("90.00")
        assert await _payout_count(factory, world["tenant_id"]) == 0
    else:
        assert outcomes["payout"][0] == "ok"
        assert outcomes["topup"] == ("err", 409)
        assert outcomes["payout"][1]["net_coins"] == pytest.approx(90.00)
        assert await _fresh_balance(factory, world["driver_id"]) == Decimal("0.00")
        assert await _offline_topup_count(factory, world["tenant_id"]) == 0
