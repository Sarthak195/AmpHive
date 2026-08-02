"""
Tests for the preflashed-unit claim-code onboarding flow (feat/easy-provisioning):
admin mint/list inventory (backend/routers/admin.py) and CPO claim
(backend/routers/cpo/_gateways.py cpo_claim_gateway).

Mirrors test_gateway_create.py's mocked-AsyncSession idiom for the pure
routing/validation logic (no DB needed), plus a DB-gated section (real
Postgres — CI's postgres:15 service, TEST_DATABASE_URL) for the two things a
mock can't prove: real tenant-scoped visibility after a claim, and that the
partial-unique claim_code index actually enforces one-code-one-gateway.
"""
import os
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastapi import HTTPException

from backend.routers.admin import (
    _CLAIM_CODE_ALPHABET,
    _CLAIM_CODE_LEN,
    admin_mint_inventory_gateway,
)
from backend.routers.cpo import cpo_claim_gateway, cpo_list_gateways
from backend.routers.cpo._gateways import _CLAIM_NOT_FOUND_DETAIL, _normalize_claim_code
from backend.schemas import AdminGatewayMintRequest, CpoGatewayClaimRequest

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
requires_db = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="needs TEST_DATABASE_URL pointing at a throwaway Postgres (CI service)",
)


# ===========================================================================
# Mocked-DB unit tests (no DB needed)
# ===========================================================================

def _admin_user():
    u = MagicMock()
    u.id = 99
    u.tenant_id = None
    u.email = "admin@amphive.test"
    return u


def _cpo_user(tenant_id=1):
    u = MagicMock()
    u.id = 7
    u.tenant_id = tenant_id
    u.email = "cpo@amphive.test"
    return u


def _result(value):
    r = MagicMock()
    r.scalar_one_or_none.return_value = value
    return r


def _db(*results):
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=list(results))
    db.add = MagicMock()          # add() is sync on AsyncSession
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    return db


def test_normalize_claim_code_strips_whitespace_dashes_and_uppercases():
    assert _normalize_claim_code(" h4kx-9q2p-fw ") == "H4KX9Q2PFW"
    assert _normalize_claim_code("") == ""


@pytest.mark.asyncio
async def test_mint_creates_unclaimed_gateway_with_claim_code():
    """No existing gateway, no claim-code collision on the first try → a
    fresh unclaimed (tenant_id None) row with a claim_code from the
    unambiguous alphabet."""
    db = _db(_result(None), _result(None))  # dup check, then claim-code uniqueness check
    res = await admin_mint_inventory_gateway(
        AdminGatewayMintRequest(gateway_id="aabbccddeeff"), _admin_user(), db,
    )
    assert res["status"] == "minted"
    assert res["gateway_id"] == "aabbccddeeff"
    code = res["claim_code"]
    assert len(code) == _CLAIM_CODE_LEN
    assert set(code) <= set(_CLAIM_CODE_ALPHABET)

    # db.add is called twice: the Gateway, then the audit log row.
    added = db.add.call_args_list[0][0][0]
    assert added.id == "aabbccddeeff"
    assert added.tenant_id is None
    assert added.claim_code == code
    assert added.vpn_ip == "aabbccddeeff"  # legacy overlay column fallback, same as cpo_create_gateway
    assert db.commit.await_count == 2


@pytest.mark.asyncio
async def test_mint_defaults_name_when_omitted():
    db = _db(_result(None), _result(None))
    res = await admin_mint_inventory_gateway(
        AdminGatewayMintRequest(gateway_id="gw-noname"), _admin_user(), db,
    )
    assert "gw-noname" in res["name"]


@pytest.mark.asyncio
async def test_mint_rejects_duplicate_gateway_id():
    db = _db(_result(MagicMock()))  # lookup finds an existing row
    with pytest.raises(HTTPException) as exc:
        await admin_mint_inventory_gateway(
            AdminGatewayMintRequest(gateway_id="dup"), _admin_user(), db,
        )
    assert exc.value.status_code == 400
    db.add.assert_not_called()


def _unclaimed_gateway(claim_code="H4KX9Q2PFW"):
    gw = MagicMock()
    gw.id = "aabbccddeeff"
    gw.name = "Unclaimed gateway aabbccddeeff"
    gw.vpn_ip = "aabbccddeeff"
    gw.claim_code = claim_code
    gw.claimed_at = None
    gw.tenant_id = None
    return gw


@pytest.mark.asyncio
async def test_claim_binds_unclaimed_gateway_to_caller_tenant():
    gw = _unclaimed_gateway()
    db = _db(_result(gw))
    user = _cpo_user(tenant_id=42)
    res = await cpo_claim_gateway(
        CpoGatewayClaimRequest(claim_code="h4kx-9q2p-fw"), user, db,
    )
    assert res == {
        "status": "claimed",
        "gateway_id": "aabbccddeeff",
        "name": "Unclaimed gateway aabbccddeeff",
        "vpn_ip": "aabbccddeeff",
    }
    assert gw.tenant_id == 42
    assert gw.claimed_at is not None
    assert db.commit.await_count == 2  # the claim itself, then the audit row


@pytest.mark.asyncio
async def test_claim_renames_gateway_when_name_given():
    gw = _unclaimed_gateway()
    db = _db(_result(gw))
    await cpo_claim_gateway(
        CpoGatewayClaimRequest(claim_code="H4KX9Q2PFW", name="Basement gateway"),
        _cpo_user(), db,
    )
    assert gw.name == "Basement gateway"


@pytest.mark.asyncio
async def test_claim_rejects_unknown_code_with_generic_404():
    db = _db(_result(None))
    with pytest.raises(HTTPException) as exc:
        await cpo_claim_gateway(CpoGatewayClaimRequest(claim_code="NOPE"), _cpo_user(), db)
    assert exc.value.status_code == 404
    assert exc.value.detail == _CLAIM_NOT_FOUND_DETAIL


@pytest.mark.asyncio
async def test_claim_rejects_already_claimed_code_with_the_same_generic_404():
    """Double-claim: a code whose gateway.claimed_at is already set must 404
    with the EXACT SAME detail as an unknown code — no enumeration oracle."""
    gw = _unclaimed_gateway()
    from datetime import datetime, timezone
    gw.claimed_at = datetime.now(timezone.utc)
    db = _db(_result(gw))
    with pytest.raises(HTTPException) as exc:
        await cpo_claim_gateway(CpoGatewayClaimRequest(claim_code="H4KX9Q2PFW"), _cpo_user(), db)
    assert exc.value.status_code == 404
    assert exc.value.detail == _CLAIM_NOT_FOUND_DETAIL


@pytest.mark.asyncio
async def test_claim_rejects_malformed_code_with_the_same_generic_404():
    db = _db(_result(None))
    with pytest.raises(HTTPException) as exc:
        await cpo_claim_gateway(CpoGatewayClaimRequest(claim_code="   "), _cpo_user(), db)
    assert exc.value.status_code == 404
    assert exc.value.detail == _CLAIM_NOT_FOUND_DETAIL


# ===========================================================================
# DB-gated tests (real Postgres — the flow's tenant-scoping + uniqueness
# guarantees are the part a mock can't prove)
# ===========================================================================

ENUM_TYPES = ["gateway_status", "plug_status", "session_status", "tx_type", "user_role"]


@pytest_asyncio.fixture()
async def factory():
    """Engine + fresh schema per test; yields a session factory. Mirrors
    test_offline_topups.py's fixture exactly."""
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


async def _seed_admin(factory):
    from backend.database.models import User, UserRole

    async with factory() as db:
        admin = User(
            email=f"admin-{uuid.uuid4().hex[:10]}@amphive.test",
            hashed_password="x", full_name="Admin", role=UserRole.ADMIN, tenant_id=None,
        )
        db.add(admin)
        await db.commit()
        await db.refresh(admin)
        return admin.id


async def _seed_tenant_with_cpo(factory):
    from backend.database.models import Tenant, User, UserRole

    tag = uuid.uuid4().hex[:10]
    async with factory() as db:
        tenant = Tenant(name=f"tenant-{tag}")
        db.add(tenant)
        await db.flush()
        cpo = User(
            email=f"cpo-{tag}@amphive.test", hashed_password="x",
            full_name="CPO Test", role=UserRole.CPO, tenant_id=tenant.id,
        )
        db.add(cpo)
        await db.commit()
        await db.refresh(cpo)
        return tenant.id, cpo.id


@requires_db
@pytest.mark.asyncio
async def test_mint_then_claim_end_to_end_binds_tenant(factory):
    from sqlalchemy import select

    from backend.database.models import Gateway, User

    admin_id = await _seed_admin(factory)
    tenant_id, cpo_id = await _seed_tenant_with_cpo(factory)
    gw_id = f"gw-{uuid.uuid4().hex[:10]}"

    async with factory() as db:
        admin = (await db.execute(select(User).where(User.id == admin_id))).scalar_one()
        mint_res = await admin_mint_inventory_gateway(
            AdminGatewayMintRequest(gateway_id=gw_id), admin, db,
        )
    assert mint_res["status"] == "minted"
    claim_code = mint_res["claim_code"]

    # Unclaimed row is invisible to the tenant fleet view (inner-joined on Tenant).
    async with factory() as db:
        cpo = (await db.execute(select(User).where(User.id == cpo_id))).scalar_one()
        before = await cpo_list_gateways(cpo, db)
    assert all(g["id"] != gw_id for g in before)

    async with factory() as db:
        cpo = (await db.execute(select(User).where(User.id == cpo_id))).scalar_one()
        claim_res = await cpo_claim_gateway(
            CpoGatewayClaimRequest(claim_code=claim_code, name="Claimed gateway"), cpo, db,
        )
    assert claim_res["status"] == "claimed"
    assert claim_res["name"] == "Claimed gateway"

    async with factory() as db:
        gw = (await db.execute(select(Gateway).where(Gateway.id == gw_id))).scalar_one()
        assert gw.tenant_id == tenant_id
        assert gw.claimed_at is not None
        assert gw.claim_code == claim_code  # not cleared — kept for admin audit/reprint

    # Now visible in the claiming tenant's fleet view.
    async with factory() as db:
        cpo = (await db.execute(select(User).where(User.id == cpo_id))).scalar_one()
        after = await cpo_list_gateways(cpo, db)
    assert any(g["id"] == gw_id for g in after)


@requires_db
@pytest.mark.asyncio
async def test_double_claim_is_rejected(factory):
    from sqlalchemy import select

    from backend.database.models import User

    admin_id = await _seed_admin(factory)
    tenant_a, cpo_a_id = await _seed_tenant_with_cpo(factory)
    tenant_b, cpo_b_id = await _seed_tenant_with_cpo(factory)
    gw_id = f"gw-{uuid.uuid4().hex[:10]}"

    async with factory() as db:
        admin = (await db.execute(select(User).where(User.id == admin_id))).scalar_one()
        mint_res = await admin_mint_inventory_gateway(
            AdminGatewayMintRequest(gateway_id=gw_id), admin, db,
        )
    claim_code = mint_res["claim_code"]

    async with factory() as db:
        cpo_a = (await db.execute(select(User).where(User.id == cpo_a_id))).scalar_one()
        first = await cpo_claim_gateway(CpoGatewayClaimRequest(claim_code=claim_code), cpo_a, db)
    assert first["status"] == "claimed"

    # A second claim of the SAME code — even by a different tenant — 404s
    # with the generic detail, and does not steal the gateway.
    async with factory() as db:
        cpo_b = (await db.execute(select(User).where(User.id == cpo_b_id))).scalar_one()
        with pytest.raises(HTTPException) as exc:
            await cpo_claim_gateway(CpoGatewayClaimRequest(claim_code=claim_code), cpo_b, db)
    assert exc.value.status_code == 404
    assert exc.value.detail == _CLAIM_NOT_FOUND_DETAIL

    async with factory() as db:
        cpo_b = (await db.execute(select(User).where(User.id == cpo_b_id))).scalar_one()
        b_view = await cpo_list_gateways(cpo_b, db)
    assert all(g["id"] != gw_id for g in b_view)  # foreign tenant never sees it


@requires_db
@pytest.mark.asyncio
async def test_claim_rejects_a_code_that_was_never_minted(factory):
    from sqlalchemy import select

    from backend.database.models import User

    _, cpo_id = await _seed_tenant_with_cpo(factory)
    async with factory() as db:
        cpo = (await db.execute(select(User).where(User.id == cpo_id))).scalar_one()
        with pytest.raises(HTTPException) as exc:
            await cpo_claim_gateway(CpoGatewayClaimRequest(claim_code="ZZZZZZZZZZ"), cpo, db)
    assert exc.value.status_code == 404
    assert exc.value.detail == _CLAIM_NOT_FOUND_DETAIL


@requires_db
@pytest.mark.asyncio
async def test_claim_code_unique_index_rejects_a_forced_collision(factory):
    """Belt-and-braces on the partial unique index itself (not just the
    mint-time retry loop): two gateways can never share a claim_code."""
    from sqlalchemy.exc import IntegrityError

    from backend.database.models import Gateway

    async with factory() as db:
        db.add(Gateway(id="gw-one", tenant_id=None, name="One", vpn_ip="gw-one", claim_code="SAMECODE01"))
        await db.commit()

    async with factory() as db:
        db.add(Gateway(id="gw-two", tenant_id=None, name="Two", vpn_ip="gw-two", claim_code="SAMECODE01"))
        with pytest.raises(IntegrityError):
            await db.commit()
