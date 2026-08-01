"""
Tests for the CPO payout / settlement ledger (services/payouts.py +
routers/cpo.py GET /api/cpo/earnings, POST/GET /api/cpo/payouts,
POST /api/cpo/payouts/{id}/mark_paid|cancel).

Two tiers, mirroring the rest of this suite:

1. Pure-function + mocked-db tests (no DB, always run): fee-split math
   (services.payouts.compute_fee_and_net) and the RBAC/ownership branches
   that don't need real data — mark_paid's admin-only gate, cancel's
   cross-tenant block, and the "already settled" 409s. Same style as
   test_gateway_ota.py / test_max_active_sessions.py. Also the audit-trail
   shape: mark_paid/cancel stage a payout.* AuditLog row into the PAYOUT's
   tenant (not the admin actor's, which may be None), and the 409 paths
   stage none.
2. DB-gated tests (need a real PostgreSQL — CI's postgres:15 service,
   exported as TEST_DATABASE_URL; skipped locally, same as test_wallet.py /
   test_migrations.py): the earnings SQL aggregate itself (window + status
   filtering), the watermark (advances on request, CANCELLED frees it),
   double-request 409, and — the one that actually needs a real database —
   concurrent-request serialization via the tenant row lock, proving two
   simultaneous payout requests for the same tenant can't double-settle the
   same window. The lifecycle tests also assert the persisted audit trail:
   exactly one payout.request / payout.mark_paid / payout.cancel AuditLog
   row per successful transition (409 replays add none).
"""
import asyncio
import itertools
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastapi import HTTPException

from backend.database.models import PayoutStatus, SessionStatus, UserRole
from backend.routers.cpo import (
    cpo_cancel_payout,
    cpo_mark_payout_paid,
    cpo_request_payout,
)
from backend.services import payouts as payout_service
from backend.services.rbac import require_role

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
# Tier 1: pure math + mocked-db (no DB, always run)
# ===========================================================================

def test_compute_fee_and_net_default_rate(monkeypatch):
    monkeypatch.delenv("PLATFORM_FEE_PCT", raising=False)
    fee, net = payout_service.compute_fee_and_net(Decimal("100.00"))
    assert fee == Decimal("10.00")
    assert net == Decimal("90.00")
    assert fee + net == Decimal("100.00")


def test_compute_fee_and_net_rounds_half_up(monkeypatch):
    """0.05 coins at a 50% fee rate = 0.025 raw fee -- to_money's ROUND_HALF_UP
    must push that to 0.03, not truncate to 0.02, and fee + net must still
    foot to the original gross to the cent."""
    monkeypatch.setenv("PLATFORM_FEE_PCT", "50")
    fee, net = payout_service.compute_fee_and_net(Decimal("0.05"))
    assert fee == Decimal("0.03")
    assert net == Decimal("0.02")
    assert fee + net == Decimal("0.05")


def test_platform_fee_pct_default_when_unset(monkeypatch):
    monkeypatch.delenv("PLATFORM_FEE_PCT", raising=False)
    assert payout_service.platform_fee_pct() == Decimal("10.0")


def test_platform_fee_pct_falls_back_on_garbage_env(monkeypatch):
    monkeypatch.setenv("PLATFORM_FEE_PCT", "not-a-number")
    assert payout_service.platform_fee_pct() == Decimal("10.0")


def _payout_mock(id=1, tenant_id=1, status=PayoutStatus.REQUESTED):
    p = MagicMock()
    p.id = id
    p.tenant_id = tenant_id
    p.status = status
    p.period_start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    p.period_end = datetime(2026, 1, 2, tzinfo=timezone.utc)
    p.gross_coins = Decimal("10.00")
    p.platform_fee_coins = Decimal("1.00")
    p.net_coins = Decimal("9.00")
    p.requested_by_user_id = 1
    p.requested_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    p.paid_at = None
    p.note = None
    return p


def _stub_user(tenant_id, role=UserRole.CPO, user_id=1):
    u = MagicMock()
    u.id = user_id
    u.tenant_id = tenant_id
    u.role = role
    u.email = "user@example.com"
    return u


def _result(value):
    r = MagicMock()
    r.scalar_one_or_none.return_value = value
    return r


def _db(*results):
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=list(results))
    # Session.add is synchronous — a bare AsyncMock attribute would mint an
    # un-awaited coroutine per call (RuntimeWarning noise in the audit tests).
    db.add = MagicMock()
    return db


def _added_audit_rows(db):
    """AuditLog instances staged on the mocked session via db.add — what
    try_record_audit would persist (its commit is a separate transaction,
    so the staged row IS the audit write from the route's point of view)."""
    from backend.database.models import AuditLog
    return [c.args[0] for c in db.add.call_args_list if isinstance(c.args[0], AuditLog)]


@pytest.mark.asyncio
async def test_mark_paid_role_gate_rejects_cpo():
    """The security-relevant part of 'cpo can't mark_paid' is the
    require_role("admin") dependency the route is wired to -- calling the
    route function directly (as the tests below do) bypasses FastAPI's DI
    entirely, so this exercises the actual gate."""
    checker = require_role("admin")
    cpo_user = _stub_user(tenant_id=1, role=UserRole.CPO)
    with pytest.raises(HTTPException) as exc:
        await checker(user=cpo_user)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_mark_paid_role_gate_accepts_admin():
    checker = require_role("admin")
    admin_user = _stub_user(tenant_id=None, role=UserRole.ADMIN)
    result = await checker(user=admin_user)
    assert result is admin_user


@pytest.mark.asyncio
async def test_cancel_blocks_cross_tenant_cpo():
    payout = _payout_mock(tenant_id=2, status=PayoutStatus.REQUESTED)
    db = _db(_result(payout))
    intruder = _stub_user(tenant_id=1, role=UserRole.CPO)

    with pytest.raises(HTTPException) as exc:
        await cpo_cancel_payout(payout_id=payout.id, user=intruder, db=db)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_cancel_allows_admin_across_tenants():
    payout = _payout_mock(tenant_id=2, status=PayoutStatus.REQUESTED)
    # Two db.execute calls now: the payout SELECT ... FOR UPDATE, then the
    # free-claimed-sessions UPDATE charging_sessions (settlement marking) --
    # its return value is never read by the route, so a bare mock stands in.
    db = _db(_result(payout), MagicMock())
    admin = _stub_user(tenant_id=None, role=UserRole.ADMIN)

    result = await cpo_cancel_payout(payout_id=payout.id, user=admin, db=db)

    assert result["status"] == "cancelled"
    # Two commits: the cancel itself, then the payout.cancel audit row
    # (try_record_audit commits in its own transaction so an audit failure
    # can never undo the already-committed action).
    assert db.commit.await_count == 2


@pytest.mark.asyncio
async def test_cancel_audits_into_the_payouts_tenant_not_the_admins():
    """The payout.cancel audit row must land in the PAYOUT's tenant so the
    owning CPO sees it in GET /api/cpo/audit — the admin actor may have no
    tenant_id at all (AuditLog.tenant_id is NOT NULL, so using the actor's
    would also just break)."""
    payout = _payout_mock(id=7, tenant_id=2, status=PayoutStatus.REQUESTED)
    # See test_cancel_allows_admin_across_tenants: cancel now issues a second
    # db.execute for the free-claimed-sessions UPDATE (settlement marking).
    db = _db(_result(payout), MagicMock())
    admin = _stub_user(tenant_id=None, role=UserRole.ADMIN, user_id=42)

    await cpo_cancel_payout(payout_id=payout.id, user=admin, db=db)

    rows = _added_audit_rows(db)
    assert len(rows) == 1
    entry = rows[0]
    assert entry.action == "payout.cancel"
    assert entry.target_type == "payout"
    assert entry.target_id == "7"
    assert entry.tenant_id == 2  # the payout's tenant, not the admin's (None)
    assert entry.actor_user_id == 42
    # Detail carries the settlement window + net, per the key=value convention.
    assert payout.period_start.isoformat() in entry.detail
    assert payout.period_end.isoformat() in entry.detail
    assert "net=9.00" in entry.detail


@pytest.mark.asyncio
async def test_mark_paid_audits_payout_mark_paid():
    payout = _payout_mock(id=11, tenant_id=3, status=PayoutStatus.REQUESTED)
    db = _db(_result(payout))
    admin = _stub_user(tenant_id=None, role=UserRole.ADMIN, user_id=99)

    result = await cpo_mark_payout_paid(payout_id=payout.id, user=admin, db=db)
    assert result["status"] == "paid"

    rows = _added_audit_rows(db)
    assert len(rows) == 1
    entry = rows[0]
    assert entry.action == "payout.mark_paid"
    assert entry.target_type == "payout"
    assert entry.target_id == "11"
    assert entry.tenant_id == 3
    assert entry.actor_user_id == 99
    assert payout.period_start.isoformat() in entry.detail
    assert "net=9.00" in entry.detail


@pytest.mark.asyncio
async def test_mark_paid_409_when_already_paid():
    payout = _payout_mock(status=PayoutStatus.PAID)
    db = _db(_result(payout))
    admin = _stub_user(tenant_id=None, role=UserRole.ADMIN)

    with pytest.raises(HTTPException) as exc:
        await cpo_mark_payout_paid(payout_id=payout.id, user=admin, db=db)
    assert exc.value.status_code == 409
    assert _added_audit_rows(db) == []  # a rejected replay is not audited


@pytest.mark.asyncio
async def test_cancel_409_when_already_cancelled():
    payout = _payout_mock(tenant_id=1, status=PayoutStatus.CANCELLED)
    db = _db(_result(payout))
    owner = _stub_user(tenant_id=1, role=UserRole.CPO)

    with pytest.raises(HTTPException) as exc:
        await cpo_cancel_payout(payout_id=payout.id, user=owner, db=db)
    assert exc.value.status_code == 409
    assert _added_audit_rows(db) == []  # a rejected replay is not audited


@pytest.mark.asyncio
async def test_request_payout_400_without_a_tenant():
    """A bare admin (no tenant_id) hits the same guard cpo_setup uses."""
    admin = _stub_user(tenant_id=None, role=UserRole.ADMIN)
    with pytest.raises(HTTPException) as exc:
        await cpo_request_payout(user=admin, db=AsyncMock())
    assert exc.value.status_code == 400


# ===========================================================================
# Tier 2: DB-gated (real Postgres)
# ===========================================================================

_seed_counter = itertools.count(1)


@pytest_asyncio.fixture()
async def factory():
    """Engine + fresh schema per test; yields a session factory. Mirrors
    test_wallet.py's fixture exactly (including the belt-and-braces enum
    drop loop, extended here with payout_status)."""
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


async def _seed_tenant(factory, name):
    from backend.database.models import Tenant, User

    async with factory() as db:
        tenant = Tenant(name=name)
        db.add(tenant)
        await db.flush()
        cpo = User(
            tenant_id=tenant.id,
            email=f"cpo-{name.lower()}@example.com",
            hashed_password="x",
            full_name="CPO Test",
            role=UserRole.CPO,
        )
        db.add(cpo)
        await db.commit()
        return tenant.id, cpo.id


async def _seed_session(factory, tenant_id, coins_spent, status, ended_at=None):
    """A minimal session attributed to tenant_id. Gateway/plug/driver rows
    are throwaway, just to satisfy FK constraints -- the earnings query only
    reads ChargingSession.tenant_id/status/ended_at/coins_spent (see
    services/payouts.py's module docstring for why no join is needed)."""
    from backend.database.models import (
        ChargingSession,
        Gateway,
        GatewayStatus,
        Plug,
        PlugStatus,
        User,
    )

    n = next(_seed_counter)
    async with factory() as db:
        driver = User(
            email=f"driver-{n}@example.com",
            hashed_password="x",
            full_name="Driver Test",
            role=UserRole.DRIVER,
        )
        gw = Gateway(
            id=f"gw-{n}",
            tenant_id=tenant_id,
            name="GW",
            vpn_ip=f"10.0.{n // 250}.{n % 250 + 1}",
            status=GatewayStatus.ONLINE,
        )
        plug = Plug(
            gateway_id=gw.id,
            name="Plug",
            local_ip="10.0.1.1",
            status=PlugStatus.AVAILABLE,
        )
        started_at = (ended_at - timedelta(hours=1)) if ended_at else datetime.now(timezone.utc)
        session = ChargingSession(
            tenant_id=tenant_id,
            user=driver,
            plug=plug,
            started_at=started_at,
            ended_at=ended_at,
            energy_kwh=1.0,
            coins_spent=Decimal(coins_spent),
            status=status,
        )
        db.add(gw)
        db.add(driver)
        db.add(plug)
        db.add(session)
        await db.commit()
        return session.id


def _cpo_user(tenant_id, user_id, role=UserRole.CPO):
    u = MagicMock()
    u.id = user_id
    u.tenant_id = tenant_id
    u.role = role
    u.email = f"user{user_id}@example.com"
    return u


async def _seed_admin(factory):
    """A real platform-admin users row (no tenant), returning its id. Audit
    rows FK actor_user_id -> users.id, so a test actor must actually exist —
    a fabricated id makes every audit write FK-violate (which is exactly what
    test_failed_audit_write_never_breaks_the_action_or_session exercises on
    purpose; every other test should model production, where actors are real
    authenticated users)."""
    from backend.database.models import User

    n = next(_seed_counter)
    async with factory() as db:
        admin = User(
            email=f"admin-{n}@example.com",
            hashed_password="x",
            full_name="Platform Admin",
            role=UserRole.ADMIN,
        )
        db.add(admin)
        await db.commit()
        return admin.id


async def _audit_rows(factory, tenant_id, action):
    """Persisted AuditLog rows for one tenant + action (DB-gated tier)."""
    from sqlalchemy import and_, select

    from backend.database.models import AuditLog

    async with factory() as db:
        result = await db.execute(
            select(AuditLog).where(
                and_(AuditLog.tenant_id == tenant_id, AuditLog.action == action)
            )
        )
        return result.scalars().all()


@requires_db
@pytest.mark.asyncio
async def test_gross_earnings_sums_only_completed_sessions_in_window(factory):
    tenant_id, _ = await _seed_tenant(factory, "WindowCo")
    now = datetime.now(timezone.utc)
    in_window = now - timedelta(days=1)
    before_window = now - timedelta(days=10)

    await _seed_session(factory, tenant_id, "10.00", SessionStatus.COMPLETED, ended_at=in_window)
    await _seed_session(factory, tenant_id, "7.50", SessionStatus.COMPLETED, ended_at=in_window)
    # Outside the requested window -- must not be counted.
    await _seed_session(factory, tenant_id, "999.00", SessionStatus.COMPLETED, ended_at=before_window)
    # Not COMPLETED -- must not be counted even though it's "in window".
    await _seed_session(factory, tenant_id, "50.00", SessionStatus.ACTIVE, ended_at=None)

    async with factory() as db:
        gross = await payout_service.sum_completed_session_coins(
            db, tenant_id,
            window_start=now - timedelta(days=2),
            window_end=now + timedelta(minutes=1),
        )

    assert gross == Decimal("17.50")


@requires_db
@pytest.mark.asyncio
async def test_gross_earnings_includes_paid_status_alongside_completed(factory):
    """SessionStatus.PAID must count as finished revenue too, same as
    routers/admin.py's _REVENUE_STATUSES / routers/cpo/_analytics.py /
    services/invoices.py's _INVOICEABLE_STATUSES already treat (COMPLETED,
    PAID) -- payouts.py filtering on COMPLETED alone would silently
    under-report gross the moment anything transitions a session to PAID."""
    tenant_id, _ = await _seed_tenant(factory, "PaidStatusCo")
    now = datetime.now(timezone.utc)
    ended_at = now - timedelta(hours=1)

    await _seed_session(factory, tenant_id, "10.00", SessionStatus.COMPLETED, ended_at=ended_at)
    await _seed_session(factory, tenant_id, "6.00", SessionStatus.PAID, ended_at=ended_at)
    # Still excluded -- ACTIVE/CANCELLED are not finished revenue.
    await _seed_session(factory, tenant_id, "99.00", SessionStatus.CANCELLED, ended_at=ended_at)

    async with factory() as db:
        gross = await payout_service.sum_completed_session_coins(
            db, tenant_id,
            window_start=now - timedelta(days=1),
            window_end=now + timedelta(minutes=1),
        )

    assert gross == Decimal("16.00")


async def _seed_driver(factory, tag):
    """A standalone driver User row (no tenant), for FKs that need a real
    driver id -- e.g. SessionDispute.driver_user_id below. Mirrors
    _seed_admin's shape."""
    from backend.database.models import User

    n = next(_seed_counter)
    async with factory() as db:
        driver = User(
            email=f"{tag}-{n}@example.com",
            hashed_password="x",
            full_name="Disputing Driver",
            role=UserRole.DRIVER,
        )
        db.add(driver)
        await db.commit()
        return driver.id


@requires_db
@pytest.mark.asyncio
async def test_approved_dispute_refund_nets_out_of_gross_in_its_sessions_window(factory):
    """sum_completed_session_coins on its own: an APPROVED refund against a
    COMPLETED session must reduce the gross for the window that session's
    ended_at falls in, and a REJECTED dispute (no money moved) must not."""
    from backend.database.models import DisputeStatus, SessionDispute

    tenant_id, _ = await _seed_tenant(factory, "RefundGrossCo")
    driver_id = await _seed_driver(factory, "driver")
    now = datetime.now(timezone.utc)
    ended_at = now - timedelta(hours=1)

    approved_session_id = await _seed_session(
        factory, tenant_id, "20.00", SessionStatus.COMPLETED, ended_at=ended_at
    )
    rejected_session_id = await _seed_session(
        factory, tenant_id, "10.00", SessionStatus.COMPLETED, ended_at=ended_at
    )

    async with factory() as db:
        db.add(SessionDispute(
            session_id=approved_session_id,
            tenant_id=tenant_id,
            driver_user_id=driver_id,
            reason="Charger stopped early",
            status=DisputeStatus.APPROVED,
            refund_coins=Decimal("5.00"),
            resolved_at=now,
        ))
        db.add(SessionDispute(
            session_id=rejected_session_id,
            tenant_id=tenant_id,
            driver_user_id=driver_id,
            reason="Did not like the price",
            status=DisputeStatus.REJECTED,
            resolved_at=now,
        ))
        await db.commit()

    async with factory() as db:
        gross = await payout_service.sum_completed_session_coins(
            db, tenant_id,
            window_start=now - timedelta(days=1),
            window_end=now + timedelta(minutes=1),
        )

    # (20.00 - 5.00 approved refund) + 10.00 untouched by the rejected one.
    assert gross == Decimal("25.00")


@requires_db
@pytest.mark.asyncio
async def test_approved_dispute_refund_nets_out_against_a_paid_status_session(factory):
    """The refund netting must apply to a PAID session too, not just
    COMPLETED -- sum_approved_refund_coins joins on the same
    FINISHED_SESSION_STATUSES set as the gross query, so a session that later
    transitions to PAID doesn't stop having its approved refunds subtracted."""
    from backend.database.models import DisputeStatus, SessionDispute

    tenant_id, _ = await _seed_tenant(factory, "RefundPaidCo")
    driver_id = await _seed_driver(factory, "paiddisputer")
    now = datetime.now(timezone.utc)
    ended_at = now - timedelta(hours=1)

    session_id = await _seed_session(
        factory, tenant_id, "20.00", SessionStatus.PAID, ended_at=ended_at
    )

    async with factory() as db:
        db.add(SessionDispute(
            session_id=session_id,
            tenant_id=tenant_id,
            driver_user_id=driver_id,
            reason="Charger stopped early",
            status=DisputeStatus.APPROVED,
            refund_coins=Decimal("5.00"),
            resolved_at=now,
        ))
        await db.commit()

    async with factory() as db:
        gross = await payout_service.sum_completed_session_coins(
            db, tenant_id,
            window_start=now - timedelta(days=1),
            window_end=now + timedelta(minutes=1),
        )

    assert gross == Decimal("15.00")


@requires_db
@pytest.mark.asyncio
async def test_approved_dispute_refund_reduces_available_pool_and_payable_net(factory):
    """End-to-end through tenant_earnings_summary: a session billed N=20.00
    coins with an APPROVED dispute refund R=5.00 must reduce both the
    payable net and the offline-topup available pool by R net of the
    platform's cut on R (fee is linear -- fee(A) - fee(A-R) == fee(R) at
    whole-rupee amounts) -- not by the raw R, and not at all until the
    dispute is actually approved. cpo_resolve_dispute never mutates
    ChargingSession.coins_spent (see routers/cpo/_disputes.py), so this
    netting-out has to happen in the earnings aggregate itself."""
    from backend.database.models import DisputeStatus, SessionDispute

    tenant_id, _ = await _seed_tenant(factory, "DisputePoolCo")
    driver_id = await _seed_driver(factory, "disputer")
    now = datetime.now(timezone.utc)
    ended_at = now - timedelta(hours=1)

    session_id = await _seed_session(
        factory, tenant_id, "20.00", SessionStatus.COMPLETED, ended_at=ended_at
    )

    # Baseline: no dispute yet -- the full 20.00 gross settles at the
    # default 10% platform fee (same default the sibling payout-lifecycle
    # tests above rely on).
    async with factory() as db:
        baseline = await payout_service.tenant_earnings_summary(db, tenant_id)
    assert baseline["unsettled_gross_coins"] == Decimal("20.00")
    assert baseline["unsettled_net_coins"] == Decimal("18.00")
    assert baseline["available_pool_coins"] == Decimal("18.00")

    async with factory() as db:
        db.add(SessionDispute(
            session_id=session_id,
            tenant_id=tenant_id,
            driver_user_id=driver_id,
            reason="Session cut off early",
            status=DisputeStatus.APPROVED,
            refund_coins=Decimal("5.00"),
            resolved_at=now,
        ))
        await db.commit()

    async with factory() as db:
        after_refund = await payout_service.tenant_earnings_summary(db, tenant_id)

    # Gross nets out the refund before the fee split: 20.00 - 5.00 = 15.00.
    assert after_refund["unsettled_gross_coins"] == Decimal("15.00")
    assert after_refund["unsettled_net_coins"] == Decimal("13.50")
    assert after_refund["available_pool_coins"] == Decimal("13.50")
    # Lifetime figures are the same underlying aggregate -- they move too.
    assert after_refund["lifetime_gross_coins"] == Decimal("15.00")
    assert after_refund["lifetime_net_coins"] == Decimal("13.50")

    # The payable net/pool dropped by exactly the refund's own after-fee
    # value -- i.e. by R net of the platform's cut on R, not by raw R.
    _, refund_net = payout_service.compute_fee_and_net(Decimal("5.00"))
    assert baseline["unsettled_net_coins"] - after_refund["unsettled_net_coins"] == refund_net
    assert baseline["available_pool_coins"] - after_refund["available_pool_coins"] == refund_net


@requires_db
@pytest.mark.asyncio
async def test_watermark_advances_after_payout_and_cancel_frees_window(factory):
    from backend.database.models import Payout

    tenant_id, user_id = await _seed_tenant(factory, "WatermarkCo")

    async with factory() as db:
        assert await payout_service.tenant_settlement_watermark(db, tenant_id) == payout_service.EPOCH

    period_end = datetime(2026, 1, 1, tzinfo=timezone.utc)
    async with factory() as db:
        payout = Payout(
            tenant_id=tenant_id,
            period_start=payout_service.EPOCH,
            period_end=period_end,
            gross_coins=Decimal("10.00"),
            platform_fee_coins=Decimal("1.00"),
            net_coins=Decimal("9.00"),
            status=PayoutStatus.REQUESTED,
            requested_by_user_id=user_id,
        )
        db.add(payout)
        await db.commit()
        payout_id = payout.id

    async with factory() as db:
        assert await payout_service.tenant_settlement_watermark(db, tenant_id) == period_end

    async with factory() as db:
        from sqlalchemy import select
        p = (await db.execute(select(Payout).where(Payout.id == payout_id))).scalar_one()
        p.status = PayoutStatus.CANCELLED
        await db.commit()

    async with factory() as db:
        # CANCELLED is excluded from the watermark -- the window is freed.
        assert await payout_service.tenant_settlement_watermark(db, tenant_id) == payout_service.EPOCH


@requires_db
@pytest.mark.asyncio
async def test_request_payout_400_when_nothing_unsettled(factory):
    tenant_id, user_id = await _seed_tenant(factory, "EmptyCo")
    user = _cpo_user(tenant_id, user_id)

    async with factory() as db:
        with pytest.raises(HTTPException) as exc:
            await cpo_request_payout(user=user, db=db)
    assert exc.value.status_code == 400


@requires_db
@pytest.mark.asyncio
async def test_second_payout_request_conflicts_while_first_pending(factory):
    tenant_id, user_id = await _seed_tenant(factory, "DoubleReqCo")
    now = datetime.now(timezone.utc)
    await _seed_session(factory, tenant_id, "20.00", SessionStatus.COMPLETED, ended_at=now - timedelta(hours=1))
    user = _cpo_user(tenant_id, user_id)

    async with factory() as db:
        first = await cpo_request_payout(user=user, db=db)
    assert first["status"] == "requested"
    assert first["net_coins"] == pytest.approx(18.00)

    async with factory() as db:
        with pytest.raises(HTTPException) as exc:
            await cpo_request_payout(user=user, db=db)
    assert exc.value.status_code == 409

    # Exactly one payout.request audit row: the successful request wrote it
    # (window + money in the detail), the 409 replay did not.
    rows = await _audit_rows(factory, tenant_id, "payout.request")
    assert len(rows) == 1
    assert rows[0].target_type == "payout"
    assert rows[0].target_id == str(first["id"])
    assert rows[0].actor_user_id == user_id
    assert "gross=20.00" in rows[0].detail
    assert "net=18.00" in rows[0].detail


@requires_db
@pytest.mark.asyncio
async def test_refund_approved_after_payout_still_claws_back_next_window(factory):
    """Regression for the pay-then-refund drain: a dispute approved AFTER its
    session was already settled into a PAID payout must still reduce the
    tenant's NEXT unsettled window, because the refund is windowed by the
    dispute's resolved_at, not the session's ended_at. Under the old ended_at
    windowing the refund fell behind the watermark and vanished, letting a CPO
    be paid in full and then approve the refund for free."""
    from sqlalchemy import update

    from backend.database.models import (
        ChargingSession,
        DisputeStatus,
        Payout,
        SessionDispute,
    )

    tenant_id, user_id = await _seed_tenant(factory, "PayThenRefundCo")
    driver_id = await _seed_driver(factory, "latedisputer")
    now = datetime.now(timezone.utc)

    # S1 ended 2h ago and was fully settled into a PAID payout whose period_end
    # (90m ago) becomes the tenant's watermark. Under settlement marking a
    # settled session is one the payout CLAIMED (settled_payout_id), so the
    # seeding stamps the claim explicitly — for a payout created through the
    # API that's what cpo_request_payout does, and for a pre-marking payout
    # like this fabricated one it's what migration 0028's backfill does
    # (test_backfill_claims_windowed_sessions_for_legacy_payouts proves that
    # SQL produces exactly this state).
    old_session_id = await _seed_session(
        factory, tenant_id, "20.00", SessionStatus.COMPLETED,
        ended_at=now - timedelta(hours=2),
    )
    settled_at = now - timedelta(minutes=90)
    async with factory() as db:
        paid_payout = Payout(
            tenant_id=tenant_id,
            period_start=payout_service.EPOCH,
            period_end=settled_at,
            gross_coins=Decimal("20.00"),
            platform_fee_coins=Decimal("2.00"),
            net_coins=Decimal("18.00"),
            status=PayoutStatus.PAID,
            requested_by_user_id=user_id,
        )
        db.add(paid_payout)
        await db.flush()
        await db.execute(
            update(ChargingSession)
            .where(ChargingSession.id == old_session_id)
            .values(settled_payout_id=paid_payout.id)
        )
        await db.commit()

    # Fresh earnings in the current unsettled window for the clawback to bite.
    await _seed_session(
        factory, tenant_id, "20.00", SessionStatus.COMPLETED,
        ended_at=now - timedelta(minutes=30),
    )

    # Only NOW is the dispute against the already-paid S1 approved.
    async with factory() as db:
        db.add(SessionDispute(
            session_id=old_session_id,
            tenant_id=tenant_id,
            driver_user_id=driver_id,
            reason="Approved long after the session was paid out",
            status=DisputeStatus.APPROVED,
            refund_coins=Decimal("5.00"),
            resolved_at=now,
        ))
        await db.commit()

    async with factory() as db:
        summary = await payout_service.tenant_earnings_summary(db, tenant_id)

    # Watermark sits at the PAID payout's period_end; the fresh 20.00 session is
    # the only in-window gross, but the late refund (resolved now) claws back
    # against it: 20.00 - 5.00 = 15.00 gross -> 13.50 net/pool. Old ended_at
    # windowing would have left gross=20.00 / net=18.00 (refund dropped).
    assert summary["watermark"] == settled_at
    assert summary["unsettled_gross_coins"] == Decimal("15.00")
    assert summary["unsettled_net_coins"] == Decimal("13.50")
    assert summary["available_pool_coins"] == Decimal("13.50")


@requires_db
@pytest.mark.asyncio
async def test_concurrent_payout_requests_serialize_no_double_settlement(factory):
    """The core race guarantee: two simultaneous requests for the same
    tenant must not both snapshot the unsettled window. The tenant row lock
    (SELECT ... FOR UPDATE in cpo_request_payout) forces one to wait for the
    other's commit; exactly one Payout row must exist afterwards."""
    from sqlalchemy import func, select

    from backend.database.models import Payout

    tenant_id, user_id = await _seed_tenant(factory, "ConcurrentCo")
    now = datetime.now(timezone.utc)
    await _seed_session(factory, tenant_id, "40.00", SessionStatus.COMPLETED, ended_at=now - timedelta(hours=1))
    user = _cpo_user(tenant_id, user_id)

    outcomes = []

    async def attempt():
        async with factory() as db:
            try:
                res = await cpo_request_payout(user=user, db=db)
                outcomes.append(("ok", res))
            except HTTPException as exc:
                outcomes.append(("err", exc.status_code))

    await asyncio.gather(attempt(), attempt())

    kinds = sorted(k for k, _ in outcomes)
    assert kinds == ["err", "ok"]
    assert [v for k, v in outcomes if k == "err"][0] == 409

    async with factory() as db:
        count = (
            await db.execute(select(func.count(Payout.id)).where(Payout.tenant_id == tenant_id))
        ).scalar()
    assert count == 1


@requires_db
@pytest.mark.asyncio
async def test_mark_paid_transitions_then_conflicts_on_replay(factory):
    tenant_id, user_id = await _seed_tenant(factory, "MarkPaidCo")
    now = datetime.now(timezone.utc)
    await _seed_session(factory, tenant_id, "30.00", SessionStatus.COMPLETED, ended_at=now - timedelta(hours=1))
    cpo_user = _cpo_user(tenant_id, user_id)
    # A real users row, not a fabricated id: mark_paid's audit write FKs
    # actor_user_id -> users.id (see _seed_admin's docstring).
    admin_id = await _seed_admin(factory)
    admin_user = _cpo_user(None, admin_id, role=UserRole.ADMIN)

    async with factory() as db:
        requested = await cpo_request_payout(user=cpo_user, db=db)

    async with factory() as db:
        paid = await cpo_mark_payout_paid(payout_id=requested["id"], user=admin_user, db=db)
    assert paid["status"] == "paid"
    assert paid["paid_at"] is not None

    async with factory() as db:
        with pytest.raises(HTTPException) as exc:
            await cpo_mark_payout_paid(payout_id=requested["id"], user=admin_user, db=db)
    assert exc.value.status_code == 409

    # Exactly one payout.mark_paid audit row — the 409 replay added none.
    rows = await _audit_rows(factory, tenant_id, "payout.mark_paid")
    assert len(rows) == 1
    assert rows[0].target_id == str(requested["id"])
    assert rows[0].actor_user_id == admin_user.id
    assert "net=" in rows[0].detail


@requires_db
@pytest.mark.asyncio
async def test_cancel_frees_window_for_a_new_request(factory):
    tenant_id, user_id = await _seed_tenant(factory, "CancelFreeCo")
    now = datetime.now(timezone.utc)
    await _seed_session(factory, tenant_id, "12.00", SessionStatus.COMPLETED, ended_at=now - timedelta(hours=1))
    user = _cpo_user(tenant_id, user_id)

    async with factory() as db:
        first = await cpo_request_payout(user=user, db=db)

    async with factory() as db:
        cancelled = await cpo_cancel_payout(payout_id=first["id"], user=user, db=db)
    assert cancelled["status"] == "cancelled"

    # Window freed -- a new request re-covers the same unsettled earnings.
    async with factory() as db:
        second = await cpo_request_payout(user=user, db=db)
    assert second["status"] == "requested"
    assert second["net_coins"] == pytest.approx(first["net_coins"])

    # Full audit trail of the lifecycle: two requests, one cancel.
    cancel_rows = await _audit_rows(factory, tenant_id, "payout.cancel")
    assert [r.target_id for r in cancel_rows] == [str(first["id"])]
    request_rows = await _audit_rows(factory, tenant_id, "payout.request")
    assert sorted(r.target_id for r in request_rows) == sorted(
        [str(first["id"]), str(second["id"])]
    )


@requires_db
@pytest.mark.asyncio
async def test_failed_audit_write_never_breaks_the_action_or_session(factory):
    """Regression (PR #27 CI failure): an audit INSERT that blows up at
    commit — here an actor_user_id FK violation, the shape of any transient
    audit-path failure — must (a) not fail the money op it documents, and
    (b) leave the session usable. Previously the route built its response
    from the Payout AFTER try_record_audit's failure-path rollback had
    expired it, so touching it raised MissingGreenlet on the AsyncSession;
    the routes now snapshot the response first, and try_record_audit's
    rollback is guarded."""
    from sqlalchemy import select

    from backend.database.models import Payout

    tenant_id, user_id = await _seed_tenant(factory, "AuditFailCo")
    now = datetime.now(timezone.utc)
    await _seed_session(factory, tenant_id, "20.00", SessionStatus.COMPLETED, ended_at=now - timedelta(hours=1))
    cpo = _cpo_user(tenant_id, user_id)

    async with factory() as db:
        requested = await cpo_request_payout(user=cpo, db=db)

    # Deliberately phantom actor: no users row with this id exists, so the
    # payout.mark_paid audit INSERT FK-violates inside try_record_audit.
    phantom_admin = _cpo_user(None, 999_999, role=UserRole.ADMIN)

    async with factory() as db:
        paid = await cpo_mark_payout_paid(payout_id=requested["id"], user=phantom_admin, db=db)

        # (a) The action itself succeeded, with a complete response.
        assert paid["status"] == "paid"
        assert paid["paid_at"] is not None
        assert paid["net_coins"] == pytest.approx(18.00)

        # (b) The SAME session is still usable after the failed audit write
        # (its aborted transaction was rolled back, not left poisoned).
        row = (
            await db.execute(select(Payout).where(Payout.id == requested["id"]))
        ).scalar_one()
        assert row.status == PayoutStatus.PAID

    # The failed audit write persisted nothing (and took nothing else with it).
    assert await _audit_rows(factory, tenant_id, "payout.mark_paid") == []
    request_rows = await _audit_rows(factory, tenant_id, "payout.request")
    assert [r.target_id for r in request_rows] == [str(requested["id"])]


# ===========================================================================
# Tier 2: settlement marking (ChargingSession.settled_payout_id) — the
# watermark-race fix. Row-ownership replaces the ended_at/watermark window
# for GROSS; refund/top-up windowing is untouched (covered by the tests
# above, still passing unmodified).
# ===========================================================================

@requires_db
@pytest.mark.asyncio
async def test_late_landing_session_is_claimed_by_next_payout_not_lost(factory):
    """Regression for the watermark race this fix closes: under the OLD
    ended_at/watermark scheme, a session whose ended_at falls BEFORE a
    payout's snapshot period_end but that only COMMITS after that payout was
    already requested would be silently dropped from every future payout
    (its ended_at is 'behind the watermark' forever). Under settlement
    marking a session's eligibility depends only on settled_payout_id, not
    on when its ended_at falls -- so it stays unsettled and is simply
    claimed by the NEXT payout instead of lost."""
    from backend.database.models import ChargingSession

    tenant_id, user_id = await _seed_tenant(factory, "LateLandCo")
    user = _cpo_user(tenant_id, user_id)
    now = datetime.now(timezone.utc)

    await _seed_session(
        factory, tenant_id, "10.00", SessionStatus.COMPLETED,
        ended_at=now - timedelta(hours=2),
    )

    async with factory() as db:
        first = await cpo_request_payout(user=user, db=db)
    assert first["gross_coins"] == pytest.approx(10.00)
    first_period_end = datetime.fromisoformat(first["period_end"])

    # Session B "lands" (its finalize commits) only now, AFTER the first
    # payout snapshot -- but its own ended_at predates that snapshot's
    # period_end, exactly the shape of the race: old windowing would already
    # consider it settled-and-gone.
    late_session_id = await _seed_session(
        factory, tenant_id, "6.00", SessionStatus.COMPLETED,
        ended_at=first_period_end - timedelta(seconds=1),
    )

    async with factory() as db:
        unsettled = await payout_service.sum_unsettled_session_coins(db, tenant_id)
    assert unsettled == Decimal("6.00")

    # Settle the first payout so a second request is allowed (only one
    # REQUESTED payout per tenant at a time).
    admin_id = await _seed_admin(factory)
    admin_user = _cpo_user(None, admin_id, role=UserRole.ADMIN)
    async with factory() as db:
        await cpo_mark_payout_paid(payout_id=first["id"], user=admin_user, db=db)

    async with factory() as db:
        second = await cpo_request_payout(user=user, db=db)
    assert second["gross_coins"] == pytest.approx(6.00)

    async with factory() as db:
        from sqlalchemy import select
        row = (
            await db.execute(select(ChargingSession).where(ChargingSession.id == late_session_id))
        ).scalar_one()
    assert row.settled_payout_id == second["id"]


@requires_db
@pytest.mark.asyncio
async def test_request_marks_exactly_its_claimed_sessions(factory):
    """A payout's claim UPDATE must stamp settled_payout_id on exactly the
    sessions its gross summed -- not more. A session seeded right after the
    request must stay untouched (settled_payout_id still NULL)."""
    from sqlalchemy import select

    from backend.database.models import ChargingSession

    tenant_id, user_id = await _seed_tenant(factory, "ExactClaimCo")
    user = _cpo_user(tenant_id, user_id)
    now = datetime.now(timezone.utc)

    s1 = await _seed_session(factory, tenant_id, "5.00", SessionStatus.COMPLETED, ended_at=now - timedelta(hours=1))
    s2 = await _seed_session(factory, tenant_id, "7.00", SessionStatus.COMPLETED, ended_at=now - timedelta(hours=1))

    async with factory() as db:
        payout = await cpo_request_payout(user=user, db=db)
    assert payout["gross_coins"] == pytest.approx(12.00)

    s3 = await _seed_session(factory, tenant_id, "3.00", SessionStatus.COMPLETED, ended_at=now)

    async with factory() as db:
        rows = {
            row.id: row.settled_payout_id
            for row in (await db.execute(select(ChargingSession))).scalars().all()
        }
    assert rows[s1] == payout["id"]
    assert rows[s2] == payout["id"]
    assert rows[s3] is None


@requires_db
@pytest.mark.asyncio
async def test_cancel_frees_claimed_sessions_for_next_request(factory):
    """cpo_cancel_payout must reset settled_payout_id back to NULL for every
    session it had claimed, so a cancelled payout's earnings aren't
    stranded -- they're claimable again by the very next request (the
    row-ownership replacement for the old watermark-excludes-CANCELLED
    window-freeing behavior)."""
    from sqlalchemy import select

    from backend.database.models import ChargingSession

    tenant_id, user_id = await _seed_tenant(factory, "CancelFreesSessionsCo")
    user = _cpo_user(tenant_id, user_id)
    now = datetime.now(timezone.utc)

    session_id = await _seed_session(
        factory, tenant_id, "9.00", SessionStatus.COMPLETED, ended_at=now - timedelta(hours=1)
    )

    async with factory() as db:
        first = await cpo_request_payout(user=user, db=db)

    async with factory() as db:
        row = (
            await db.execute(select(ChargingSession).where(ChargingSession.id == session_id))
        ).scalar_one()
    assert row.settled_payout_id == first["id"]

    async with factory() as db:
        await cpo_cancel_payout(payout_id=first["id"], user=user, db=db)

    async with factory() as db:
        row = (
            await db.execute(select(ChargingSession).where(ChargingSession.id == session_id))
        ).scalar_one()
    assert row.settled_payout_id is None

    async with factory() as db:
        second = await cpo_request_payout(user=user, db=db)
    assert second["gross_coins"] == pytest.approx(9.00)

    async with factory() as db:
        row = (
            await db.execute(select(ChargingSession).where(ChargingSession.id == session_id))
        ).scalar_one()
    assert row.settled_payout_id == second["id"]


@requires_db
@pytest.mark.asyncio
async def test_second_payout_gross_excludes_first_payouts_claimed_sessions(factory):
    """No double counting: once payout A claims S1, a later payout B (after
    A is settled) must never re-count S1's coins -- its gross must reflect
    only sessions that were still unsettled at B's claim time."""
    tenant_id, user_id = await _seed_tenant(factory, "NoDoubleCountCo")
    user = _cpo_user(tenant_id, user_id)
    now = datetime.now(timezone.utc)

    await _seed_session(factory, tenant_id, "20.00", SessionStatus.COMPLETED, ended_at=now - timedelta(hours=1))

    async with factory() as db:
        payout_a = await cpo_request_payout(user=user, db=db)
    assert payout_a["gross_coins"] == pytest.approx(20.00)

    admin_id = await _seed_admin(factory)
    admin_user = _cpo_user(None, admin_id, role=UserRole.ADMIN)
    async with factory() as db:
        await cpo_mark_payout_paid(payout_id=payout_a["id"], user=admin_user, db=db)

    await _seed_session(factory, tenant_id, "8.00", SessionStatus.COMPLETED, ended_at=datetime.now(timezone.utc))

    async with factory() as db:
        payout_b = await cpo_request_payout(user=user, db=db)
    assert payout_b["gross_coins"] == pytest.approx(8.00)


@requires_db
@pytest.mark.asyncio
async def test_payout_gross_matches_independent_sum_over_claimed_rows(factory):
    """payout.gross_coins must equal SUM(coins_spent) of exactly the rows
    that carry this payout's id in settled_payout_id -- the row-ownership
    invariant settlement marking is built on."""
    from sqlalchemy import func, select

    from backend.database.models import ChargingSession

    tenant_id, user_id = await _seed_tenant(factory, "InvariantCo")
    user = _cpo_user(tenant_id, user_id)
    now = datetime.now(timezone.utc)

    await _seed_session(factory, tenant_id, "4.25", SessionStatus.COMPLETED, ended_at=now - timedelta(hours=1))
    await _seed_session(factory, tenant_id, "11.75", SessionStatus.COMPLETED, ended_at=now - timedelta(hours=2))

    async with factory() as db:
        payout = await cpo_request_payout(user=user, db=db)

    async with factory() as db:
        independent_sum = (
            await db.execute(
                select(func.coalesce(func.sum(ChargingSession.coins_spent), 0)).where(
                    ChargingSession.settled_payout_id == payout["id"]
                )
            )
        ).scalar()

    assert Decimal(str(independent_sum)) == Decimal(str(payout["gross_coins"]))


@requires_db
@pytest.mark.asyncio
async def test_sum_unsettled_session_coins_matches_what_a_request_would_claim(factory):
    """The read-only earnings-preview aggregate must agree with what
    mark_unsettled_sessions_and_sum_gross actually claims -- GET
    /api/cpo/earnings and POST /api/cpo/payouts must never disagree, and the
    preview call itself must not have claimed anything (still unsettled
    afterwards, claimable by the real request). Seeds an APPROVED refund so
    the agreement covers the refund-clawback subtraction too, on BOTH paths:
    a preview that skipped the subtraction would over-show, and a claim that
    skipped it (e.g. by recomputing the watermark after the new payout row
    is flushed, seeing its own period_end, and windowing the refund out --
    the exact bug this guards against) would over-PAY."""
    from backend.database.models import DisputeStatus, SessionDispute

    tenant_id, user_id = await _seed_tenant(factory, "PreviewMatchesCo")
    driver_id = await _seed_driver(factory, "previewdisputer")
    user = _cpo_user(tenant_id, user_id)
    now = datetime.now(timezone.utc)

    session_id = await _seed_session(
        factory, tenant_id, "13.50", SessionStatus.COMPLETED, ended_at=now - timedelta(hours=1)
    )
    async with factory() as db:
        db.add(SessionDispute(
            session_id=session_id,
            tenant_id=tenant_id,
            driver_user_id=driver_id,
            reason="Partial refund before any payout",
            status=DisputeStatus.APPROVED,
            refund_coins=Decimal("2.00"),
            resolved_at=now,
        ))
        await db.commit()

    # Preview: 13.50 session gross minus the 2.00 approved refund.
    async with factory() as db:
        preview = await payout_service.sum_unsettled_session_coins(db, tenant_id)
    assert preview == Decimal("11.50")

    # The claim must compute the exact same post-refund gross.
    async with factory() as db:
        payout = await cpo_request_payout(user=user, db=db)
    assert payout["gross_coins"] == pytest.approx(11.50)

    # The preview read didn't claim anything -- a second preview right after
    # the real request correctly shows zero unsettled left (and the already-
    # subtracted refund, now behind the new watermark, is not re-subtracted).
    async with factory() as db:
        after = await payout_service.sum_unsettled_session_coins(db, tenant_id)
    assert after == Decimal("0.00")


@requires_db
@pytest.mark.asyncio
async def test_backfill_claims_windowed_sessions_for_legacy_payouts(factory):
    """Migration 0028's backfill: payouts that predate settlement marking
    never claimed their sessions, so without a one-time backfill the first
    post-deploy request would re-pay all of history. The backfill stamps
    each FINISHED session whose ended_at falls in a non-CANCELLED payout's
    [period_start, period_end) window with that payout's id -- and nothing
    else: out-of-window sessions and CANCELLED payouts' windows stay NULL.
    Executes the migration's own _BACKFILL_SQL verbatim (imported from the
    version file) against a seeded schema, since the alembic-driven
    migration tests only ever run against an empty database."""
    import importlib.util
    from pathlib import Path

    from sqlalchemy import select, text

    from backend.database.models import ChargingSession, Payout

    spec = importlib.util.spec_from_file_location(
        "migration_0028",
        Path(__file__).parents[1] / "migrations" / "versions" / "0028_payout_settlement_marking.py",
    )
    migration_0028 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration_0028)

    tenant_id, user_id = await _seed_tenant(factory, "BackfillCo")
    now = datetime.now(timezone.utc)

    in_window_id = await _seed_session(
        factory, tenant_id, "20.00", SessionStatus.COMPLETED, ended_at=now - timedelta(hours=2)
    )
    paid_in_window_id = await _seed_session(
        factory, tenant_id, "5.00", SessionStatus.PAID, ended_at=now - timedelta(hours=3)
    )
    after_window_id = await _seed_session(
        factory, tenant_id, "8.00", SessionStatus.COMPLETED, ended_at=now - timedelta(minutes=30)
    )
    cancelled_window_id = await _seed_session(
        factory, tenant_id, "7.00", SessionStatus.COMPLETED, ended_at=now - timedelta(minutes=70)
    )

    settled_at = now - timedelta(minutes=90)
    async with factory() as db:
        legacy_paid = Payout(
            tenant_id=tenant_id,
            period_start=payout_service.EPOCH,
            period_end=settled_at,
            gross_coins=Decimal("25.00"),
            platform_fee_coins=Decimal("2.50"),
            net_coins=Decimal("22.50"),
            status=PayoutStatus.PAID,
            requested_by_user_id=user_id,
        )
        # A CANCELLED payout whose window covers cancelled_window_id: its
        # sessions must NOT be claimed (cancel always freed its window).
        legacy_cancelled = Payout(
            tenant_id=tenant_id,
            period_start=settled_at,
            period_end=now - timedelta(minutes=60),
            gross_coins=Decimal("7.00"),
            platform_fee_coins=Decimal("0.70"),
            net_coins=Decimal("6.30"),
            status=PayoutStatus.CANCELLED,
            requested_by_user_id=user_id,
        )
        db.add(legacy_paid)
        db.add(legacy_cancelled)
        await db.commit()
        legacy_paid_id = legacy_paid.id

    async with factory() as db:
        await db.execute(text(migration_0028._BACKFILL_SQL))
        await db.commit()

    async with factory() as db:
        rows = {
            row.id: row.settled_payout_id
            for row in (await db.execute(select(ChargingSession))).scalars().all()
        }
    assert rows[in_window_id] == legacy_paid_id
    assert rows[paid_in_window_id] == legacy_paid_id
    assert rows[after_window_id] is None
    assert rows[cancelled_window_id] is None

    # Post-backfill, the unsettled preview shows only the truly-unsettled
    # sessions -- history is not re-payable.
    async with factory() as db:
        unsettled = await payout_service.sum_unsettled_session_coins(db, tenant_id)
    assert unsettled == Decimal("15.00")  # 8.00 + 7.00, not the settled 25.00
