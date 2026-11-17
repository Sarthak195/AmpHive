"""
[Promo codes] CPO-issued promo codes redeemed by drivers (routers/cpo/_promos.py,
routers/payments.py redeem_promo_code, services/promos.py).

DB-free: code normalization/generation/hashing, status, request bounds, and
the redeem route's role gate and malformed-code short-circuit (no query).

DB-gated (CI's Postgres via TEST_DATABASE_URL, skipped locally):
1. A redemption credits the wallet, writes a CPO_TOPUP ledger line, an
   OfflineTopup tagged with promo_code_id, a PromoRedemption and an audit
   row, and sum_offline_topups / the pool include it (reconciliation).
2. Two concurrent redemptions of a one-use code: exactly one succeeds.
3. 409 "issuer pool exhausted" when the pool does not cover the amount.
4. Per-user limit, expiry and disable all return the one generic 404.
5. The CPO list carries redemption counts and status; the plain code is
   never stored.
"""
import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
# DB-free
# ===========================================================================

def test_generated_code_is_long_and_unambiguous():
    from backend.services import promos

    code = promos.generate_code()
    assert len(code) == promos.CODE_LEN >= 10
    assert set(code) <= set(promos.CODE_ALPHABET)
    assert not set("01ILO") & set(promos.CODE_ALPHABET)
    assert promos.generate_code() != code


def test_normalize_ignores_case_spaces_and_dashes():
    from backend.services import promos

    code = promos.generate_code()
    typed = " " + promos.format_code(code).lower() + " "
    assert promos.normalize_code(typed) == code
    assert promos.hash_code(promos.normalize_code(typed)) == promos.hash_code(code)


@pytest.mark.parametrize("raw", ["", "SHORT", "A" * 15, "A" * 17, "0" * 16, "IIIIOOOO11112222"])
def test_normalize_rejects_impossible_codes(raw):
    from backend.services import promos

    assert promos.normalize_code(raw) is None


def test_promo_status_precedence():
    from backend.services.promos import promo_status

    now = datetime.now(timezone.utc)
    past = now - timedelta(days=1)
    base = dict(disabled_at=None, expires_at=None, redemption_count=0, max_redemptions=1, now=now)
    assert promo_status(**base) == "active"
    assert promo_status(**{**base, "redemption_count": 1}) == "used_up"
    assert promo_status(**{**base, "expires_at": past, "redemption_count": 1}) == "expired"
    assert promo_status(**{**base, "disabled_at": past, "expires_at": past}) == "disabled"


def test_create_request_bounds():
    import pydantic

    from backend.schemas import PromoCodeCreateRequest

    req = PromoCodeCreateRequest(amount_coins=25)
    assert (req.max_redemptions, req.per_user_limit, req.expires_in_days) == (1, 1, 30)
    for bad in ({"amount_coins": 0}, {"amount_coins": 5, "max_redemptions": 0},
                {"amount_coins": 5, "per_user_limit": 0}, {"amount_coins": 5, "expires_in_days": 0}):
        with pytest.raises(pydantic.ValidationError):
            PromoCodeCreateRequest(**bad)


@pytest.mark.asyncio
async def test_redeem_refuses_non_driver_accounts():
    from backend.database.models import UserRole
    from backend.routers.payments import redeem_promo_code
    from backend.schemas import PromoRedeemRequest

    db = AsyncMock()
    cpo = SimpleNamespace(id=1, email="cpo@example.com", role=UserRole.CPO)
    with pytest.raises(HTTPException) as exc:
        await redeem_promo_code(PromoRedeemRequest(code="ABCD-EFGH-JKMN-PQRS"), cpo, db)
    assert exc.value.status_code == 403
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_redeem_malformed_code_is_generic_404_without_a_query():
    from backend.database.models import UserRole
    from backend.routers.payments import _PROMO_INVALID_DETAIL, redeem_promo_code
    from backend.schemas import PromoRedeemRequest

    db = AsyncMock()
    driver = SimpleNamespace(id=2, email="d@example.com", role=UserRole.DRIVER)
    with pytest.raises(HTTPException) as exc:
        await redeem_promo_code(PromoRedeemRequest(code="nope"), driver, db)
    assert (exc.value.status_code, exc.value.detail) == (404, _PROMO_INVALID_DETAIL)
    db.execute.assert_not_called()


def test_promo_routes_are_registered():
    from backend.routers import cpo, payments

    routes = list(payments.router.routes) + list(cpo.router.routes)
    paths = {(m, r.path) for r in routes if hasattr(r, "methods") for m in r.methods}
    assert ("POST", "/api/wallet/redeem") in paths
    assert ("POST", "/api/cpo/promos") in paths
    assert ("GET", "/api/cpo/promos") in paths
    assert ("POST", "/api/cpo/promos/{promo_id}/disable") in paths


# ===========================================================================
# DB-gated
# ===========================================================================

@pytest_asyncio.fixture()
async def factory():
    """Engine + fresh schema per test; mirrors test_offline_topups.py."""
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

    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def _seed(factory, *, gross_coins: str = "100.00") -> dict:
    """A tenant with a CPO and one COMPLETED session earning `gross_coins`
    (net = gross minus the 10% default platform fee is the pool)."""
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
            full_name="CPO", role=UserRole.CPO, tenant_id=tenant.id,
        )
        owner = User(
            email=f"owner-{tag}@amphive.test", hashed_password="x",
            full_name="Session driver", role=UserRole.DRIVER,
        )
        db.add_all([cpo, owner])
        await db.flush()
        u = uuid.uuid4().int
        gateway = Gateway(
            id=f"gw-{tag}", tenant_id=tenant.id, name="GW",
            vpn_ip=f"10.{u % 250 + 1}.{(u >> 8) % 250 + 1}.{(u >> 16) % 250 + 1}",
        )
        db.add(gateway)
        await db.flush()
        plug = Plug(gateway_id=gateway.id, name="Plug", local_ip="10.0.1.1")
        db.add(plug)
        await db.flush()
        ended_at = now - timedelta(hours=1)
        db.add(ChargingSession(
            tenant_id=tenant.id, user_id=owner.id, plug_id=plug.id,
            status=SessionStatus.COMPLETED, coins_spent=Decimal(gross_coins),
            started_at=ended_at - timedelta(hours=1), ended_at=ended_at,
        ))
        await db.commit()
        cpo_ns = SimpleNamespace(id=cpo.id, tenant_id=tenant.id, role=UserRole.CPO, email=cpo.email)
        return {"tenant_id": tenant.id, "cpo": cpo_ns, "owner_email": owner.email}


async def _driver(factory):
    from backend.database.models import User, UserRole

    async with factory() as db:
        user = User(
            email=f"driver-{uuid.uuid4().hex[:10]}@amphive.test", hashed_password="x",
            full_name="Driver", role=UserRole.DRIVER,
        )
        db.add(user)
        await db.commit()
        return SimpleNamespace(id=user.id, email=user.email, role=UserRole.DRIVER)


async def _issue(factory, cpo, **kwargs):
    from backend.routers.cpo import cpo_create_promo
    from backend.schemas import PromoCodeCreateRequest

    async with factory() as db:
        return await cpo_create_promo(PromoCodeCreateRequest(**kwargs), cpo, db)


async def _redeem(factory, driver, code):
    """Returns ("ok", response) or ("err", (status, detail))."""
    from backend.routers.payments import redeem_promo_code
    from backend.schemas import PromoRedeemRequest

    async with factory() as db:
        try:
            return "ok", await redeem_promo_code(PromoRedeemRequest(code=code), driver, db)
        except HTTPException as exc:
            return "err", (exc.status_code, exc.detail)


async def _scalar(factory, stmt):
    async with factory() as db:
        return (await db.execute(stmt)).scalar()


async def _balance(factory, user_id):
    from sqlalchemy import select

    from backend.database.models import User

    return await _scalar(factory, select(User.coin_balance).where(User.id == user_id))


async def _count(factory, model, *where):
    from sqlalchemy import func, select

    return await _scalar(factory, select(func.count(model.id)).where(*where))


def _generic_404():
    from backend.routers.payments import _PROMO_INVALID_DETAIL

    return "err", (404, _PROMO_INVALID_DETAIL)


@requires_db
@pytest.mark.asyncio
async def test_redeem_credits_wallet_and_reconciles_with_offline_topups(factory):
    from sqlalchemy import select

    from backend.database.models import (
        AuditLog,
        LedgerTransaction,
        OfflineTopup,
        PromoCode,
        PromoRedemption,
        TransactionType,
    )
    from backend.routers.cpo import cpo_create_topup
    from backend.schemas import CpoTopupCreateRequest
    from backend.services import payouts as payout_service
    from backend.services import promos

    world = await _seed(factory)
    driver = await _driver(factory)
    issued = await _issue(factory, world["cpo"], amount_coins=20, max_redemptions=3, note="flyer")
    assert issued.code and issued.code_hint == issued.code[-4:]
    assert issued.status == "active" and issued.redemption_count == 0

    # Hash only: the plain code is not in the table.
    async with factory() as db:
        row = (await db.execute(select(PromoCode).where(PromoCode.id == issued.id))).scalar_one()
    assert row.code_hash == promos.hash_code(promos.normalize_code(issued.code))
    assert promos.normalize_code(issued.code) not in (row.code_hash, row.note)

    kind, res = await _redeem(factory, driver, issued.code.lower())
    assert kind == "ok", res
    assert (res.amount_coins, res.new_balance) == (20.0, 20.0)
    assert await _balance(factory, driver.id) == Decimal("20.00")

    async with factory() as db:
        ledger = (await db.execute(
            select(LedgerTransaction).where(LedgerTransaction.user_id == driver.id)
        )).scalars().all()
        topup = (await db.execute(
            select(OfflineTopup).where(OfflineTopup.promo_code_id == issued.id)
        )).scalar_one()
        redemption = (await db.execute(select(PromoRedemption))).scalar_one()
        audit = (await db.execute(
            select(AuditLog).where(AuditLog.action == "promo.redeem")
        )).scalars().all()
    assert [(r.transaction_type, r.amount, r.balance_after) for r in ledger] == [
        (TransactionType.CPO_TOPUP, Decimal("20.00"), Decimal("20.00"))
    ]
    assert topup.tenant_id == world["tenant_id"]
    assert topup.actor_user_id == world["cpo"].id
    assert topup.driver_user_id == driver.id
    assert (redemption.code_id, redemption.user_id, redemption.offline_topup_id) == (
        issued.id, driver.id, topup.id
    )
    assert len(audit) == 1 and audit[0].tenant_id == world["tenant_id"]

    # A cash top-up next to it: the window sum carries both, and the pool
    # (net 90) is reduced by both.
    async with factory() as db:
        await cpo_create_topup(
            CpoTopupCreateRequest(driver_email=driver.email, amount_coins=10), world["cpo"], db,
        )
    async with factory() as db:
        summary = await payout_service.tenant_earnings_summary(db, world["tenant_id"])
        window_sum = await payout_service.sum_offline_topups(
            db, world["tenant_id"], window_start=summary["watermark"], window_end=summary["now"],
        )
    assert window_sum == Decimal("30.00")
    assert summary["topups_since_watermark_coins"] == Decimal("30.00")
    assert summary["available_pool_coins"] == Decimal("60.00")


@requires_db
@pytest.mark.asyncio
async def test_concurrent_redemptions_of_a_one_use_code_only_one_wins(factory):
    from backend.database.models import OfflineTopup, PromoRedemption

    world = await _seed(factory)  # pool 90, two redemptions of 20 would both fit
    a, b = await _driver(factory), await _driver(factory)
    issued = await _issue(factory, world["cpo"], amount_coins=20, max_redemptions=1)

    results = await asyncio.gather(_redeem(factory, a, issued.code), _redeem(factory, b, issued.code))

    assert sorted(kind for kind, _ in results) == ["err", "ok"], results
    loser = next(r for r in results if r[0] == "err")
    assert loser == _generic_404()
    assert await _count(factory, PromoRedemption) == 1
    assert await _count(factory, OfflineTopup, OfflineTopup.promo_code_id == issued.id) == 1
    assert sorted([await _balance(factory, a.id), await _balance(factory, b.id)]) == [
        Decimal("0.00"), Decimal("20.00")
    ]


@requires_db
@pytest.mark.asyncio
async def test_redeem_409_when_issuer_pool_is_short(factory):
    from backend.database.models import OfflineTopup

    world = await _seed(factory)  # pool 90
    driver = await _driver(factory)
    issued = await _issue(factory, world["cpo"], amount_coins=100)

    assert await _redeem(factory, driver, issued.code) == ("err", (409, "issuer pool exhausted"))
    assert await _balance(factory, driver.id) == Decimal("0.00")
    assert await _count(factory, OfflineTopup) == 0


@requires_db
@pytest.mark.asyncio
async def test_per_user_limit_is_enforced(factory):
    world = await _seed(factory)
    a, b = await _driver(factory), await _driver(factory)
    issued = await _issue(factory, world["cpo"], amount_coins=10, max_redemptions=5, per_user_limit=1)

    assert (await _redeem(factory, a, issued.code))[0] == "ok"
    assert await _redeem(factory, a, issued.code) == _generic_404()
    assert (await _redeem(factory, b, issued.code))[0] == "ok"
    assert await _balance(factory, a.id) == Decimal("10.00")


@requires_db
@pytest.mark.asyncio
async def test_expired_disabled_and_unknown_codes_share_one_error(factory):
    from sqlalchemy import update

    from backend.database.models import PromoCode
    from backend.routers.cpo import cpo_disable_promo, cpo_list_promos
    from backend.services import promos

    world = await _seed(factory)
    driver = await _driver(factory)
    expired = await _issue(factory, world["cpo"], amount_coins=10)
    disabled = await _issue(factory, world["cpo"], amount_coins=10)
    async with factory() as db:
        await db.execute(
            update(PromoCode).where(PromoCode.id == expired.id)
            .values(expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
        )
        await db.commit()
    async with factory() as db:
        res = await cpo_disable_promo(disabled.id, world["cpo"], db)
    assert res.status == "disabled"

    assert await _redeem(factory, driver, expired.code) == _generic_404()
    assert await _redeem(factory, driver, disabled.code) == _generic_404()
    assert await _redeem(factory, driver, promos.format_code(promos.generate_code())) == _generic_404()
    assert await _balance(factory, driver.id) == Decimal("0.00")

    async with factory() as db:
        listing = await cpo_list_promos(world["cpo"], db)
    by_id = {p.id: p for p in listing["items"]}
    assert listing["total"] == 2
    assert by_id[expired.id].status == "expired"
    assert by_id[disabled.id].status == "disabled"
    assert all(p.code is None for p in listing["items"])


@requires_db
@pytest.mark.asyncio
async def test_list_counts_redemptions_and_is_tenant_scoped(factory):
    from backend.routers.cpo import cpo_disable_promo, cpo_list_promos

    world = await _seed(factory)
    other = await _seed(factory)
    driver = await _driver(factory)
    issued = await _issue(factory, world["cpo"], amount_coins=10, max_redemptions=1)
    assert (await _redeem(factory, driver, issued.code))[0] == "ok"

    async with factory() as db:
        mine = await cpo_list_promos(world["cpo"], db)
    async with factory() as db:
        theirs = await cpo_list_promos(other["cpo"], db)
    assert [(p.redemption_count, p.status) for p in mine["items"]] == [(1, "used_up")]
    assert theirs == {"total": 0, "items": []}

    async with factory() as db:
        with pytest.raises(HTTPException) as exc:
            await cpo_disable_promo(issued.id, other["cpo"], db)
    assert exc.value.status_code == 404
