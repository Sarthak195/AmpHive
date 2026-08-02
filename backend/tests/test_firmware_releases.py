"""
Tests for the firmware-release registry (feat/ota-version-picker):
admin register/list/deactivate (backend/routers/admin.py) and the
semver-aware ordering (backend/services/versioning.py) both endpoints rely
on. The CPO-facing list endpoint and the OTA-trigger integration are
covered by test_gateway_ota.py.

Mirrors test_gateway_claim.py's mocked-AsyncSession idiom for the pure
routing/validation logic, plus a DB-gated section (real Postgres — CI's
postgres:15 service, TEST_DATABASE_URL) for what a mock can't prove: the
UNIQUE constraint on `version` and a real end-to-end register -> list ->
deactivate round trip.
"""
import os
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastapi import HTTPException

from backend.routers.admin import (
    admin_create_firmware_release,
    admin_deactivate_firmware_release,
    admin_list_firmware_releases,
)
from backend.schemas import AdminFirmwareReleaseCreateRequest
from backend.services.versioning import is_newer_version, version_sort_key

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
requires_db = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="needs TEST_DATABASE_URL pointing at a throwaway Postgres (CI service)",
)


# ===========================================================================
# services/versioning.py — the semver-aware sort key
# ===========================================================================

def test_version_sort_key_orders_numeric_not_lexical():
    # A plain string sort would put "2.9.0" above "2.10.0" ('9' > '1').
    versions = ["2.9.0", "2.10.0", "2.2.0", "10.0.0", "2.10.10"]
    ordered = sorted(versions, key=version_sort_key, reverse=True)
    assert ordered == ["10.0.0", "2.10.10", "2.10.0", "2.9.0", "2.2.0"]


def test_version_sort_key_handles_suffix():
    assert version_sort_key("2.3.0-direct") == (2, 3, 0, "direct")
    assert version_sort_key("2.3.0") == (2, 3, 0, "")
    assert version_sort_key("2.3.0-direct") > version_sort_key("2.3.0")


def test_version_sort_key_malformed_sorts_last():
    ordered = sorted(["2.3.0", "not-a-version", ""], key=version_sort_key, reverse=True)
    assert ordered[0] == "2.3.0"


def test_is_newer_version():
    assert is_newer_version("2.10.0", "2.9.0") is True
    assert is_newer_version("2.9.0", "2.10.0") is False
    assert is_newer_version("2.3.0", "2.3.0") is False  # equal, not newer


# ===========================================================================
# Mocked-DB unit tests (no DB needed)
# ===========================================================================

def _admin_user():
    u = MagicMock()
    u.id = 99
    u.tenant_id = None
    u.email = "admin@amphive.test"
    return u


def _result(value):
    r = MagicMock()
    r.scalar_one_or_none.return_value = value
    return r


def _db(*results):
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=list(results))
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    return db


def _release_row(release_id=1, version="2.3.0-direct", url="https://storage.googleapis.com/amphive-fw/x.bin", is_active=True):
    r = MagicMock()
    r.id = release_id
    r.version = version
    r.url = url
    r.notes = None
    r.is_active = is_active
    from datetime import datetime, timezone
    r.created_at = datetime.now(timezone.utc)
    return r


@pytest.mark.asyncio
async def test_create_release_registers_a_new_row():
    db = _db(_result(None))  # uniqueness check finds nothing
    res = await admin_create_firmware_release(
        AdminFirmwareReleaseCreateRequest(
            version="2.4.0-direct",
            url="https://storage.googleapis.com/amphive-fw/amphive-gateway-2.4.0.bin",
            notes="adds current-cap sub-16A enforcement",
        ),
        _admin_user(), db,
    )
    assert res["version"] == "2.4.0-direct"
    assert res["is_active"] is True

    added = db.add.call_args_list[0][0][0]
    assert added.version == "2.4.0-direct"
    assert added.url.startswith("https://")
    assert db.commit.await_count == 2  # release insert, then the audit row


@pytest.mark.asyncio
async def test_create_release_rejects_duplicate_version():
    db = _db(_result(_release_row()))  # uniqueness check finds an existing row
    with pytest.raises(HTTPException) as exc:
        await admin_create_firmware_release(
            AdminFirmwareReleaseCreateRequest(
                version="2.3.0-direct",
                url="https://storage.googleapis.com/amphive-fw/dup.bin",
            ),
            _admin_user(), db,
        )
    assert exc.value.status_code == 400
    db.add.assert_not_called()


def test_create_release_request_rejects_plain_http_url():
    import pydantic
    with pytest.raises(pydantic.ValidationError):
        AdminFirmwareReleaseCreateRequest(version="2.4.0", url="http://insecure/x.bin")


def test_create_release_request_rejects_malformed_version():
    import pydantic
    with pytest.raises(pydantic.ValidationError):
        AdminFirmwareReleaseCreateRequest(version="not-a-version", url="https://x/y.bin")


@pytest.mark.asyncio
async def test_list_releases_orders_semver_descending_not_lexical():
    rows = [_release_row(1, "2.9.0"), _release_row(2, "2.10.0"), _release_row(3, "1.9.0-direct")]
    scalars_result = MagicMock()
    scalars_result.scalars.return_value.all.return_value = rows
    db = AsyncMock()
    db.execute = AsyncMock(return_value=scalars_result)

    res = await admin_list_firmware_releases(_admin_user(), db)

    assert [item["version"] for item in res["items"]] == ["2.10.0", "2.9.0", "1.9.0-direct"]
    assert res["total"] == 3


@pytest.mark.asyncio
async def test_deactivate_release_flips_the_flag():
    release = _release_row(is_active=True)
    db = _db(_result(release))
    res = await admin_deactivate_firmware_release(release.id, _admin_user(), db)
    assert res["status"] == "deactivated"
    assert release.is_active is False


@pytest.mark.asyncio
async def test_deactivate_release_404_when_missing():
    db = _db(_result(None))
    with pytest.raises(HTTPException) as exc:
        await admin_deactivate_firmware_release(999, _admin_user(), db)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_deactivate_release_is_idempotent():
    """Deactivating an already-inactive release doesn't error."""
    release = _release_row(is_active=False)
    db = _db(_result(release))
    res = await admin_deactivate_firmware_release(release.id, _admin_user(), db)
    assert res["status"] == "deactivated"
    assert release.is_active is False


# NOTE: RBAC gating for these routes (require_role("admin"), and the exact
# allowed-roles tuple) is asserted generically by
# test_admin_router.py::test_every_admin_route_is_admin_only, which walks
# every /api/admin/* route — no need to duplicate that here.


# ===========================================================================
# DB-gated tests (real Postgres)
# ===========================================================================

ENUM_TYPES = ["gateway_status", "plug_status", "session_status", "tx_type", "user_role"]


@pytest_asyncio.fixture()
async def factory():
    """Engine + fresh schema per test; yields a session factory. Mirrors
    test_gateway_claim.py's fixture exactly."""
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


@requires_db
@pytest.mark.asyncio
async def test_register_list_deactivate_end_to_end(factory):
    from sqlalchemy import select

    from backend.database.models import FirmwareRelease, User
    from backend.routers.cpo import cpo_list_firmware_releases

    admin_id = await _seed_admin(factory)

    async with factory() as db:
        admin = (await db.execute(select(User).where(User.id == admin_id))).scalar_one()
        await admin_create_firmware_release(
            AdminFirmwareReleaseCreateRequest(version="2.2.0-direct", url="https://storage.googleapis.com/amphive-fw/a.bin"),
            admin, db,
        )
        await admin_create_firmware_release(
            AdminFirmwareReleaseCreateRequest(version="2.10.0-direct", url="https://storage.googleapis.com/amphive-fw/b.bin"),
            admin, db,
        )
        res3 = await admin_create_firmware_release(
            AdminFirmwareReleaseCreateRequest(version="2.9.0-direct", url="https://storage.googleapis.com/amphive-fw/c.bin"),
            admin, db,
        )

    # CPO-facing list: active only, semver-descending (2.10.0 above 2.9.0).
    async with factory() as db:
        admin = (await db.execute(select(User).where(User.id == admin_id))).scalar_one()
        cpo_view = await cpo_list_firmware_releases(admin, db)
    assert [r["version"] for r in cpo_view] == ["2.10.0-direct", "2.9.0-direct", "2.2.0-direct"]

    # Deactivate one: it disappears from the CPO list but stays in the admin list.
    async with factory() as db:
        admin = (await db.execute(select(User).where(User.id == admin_id))).scalar_one()
        await admin_deactivate_firmware_release(res3["id"], admin, db)

    async with factory() as db:
        admin = (await db.execute(select(User).where(User.id == admin_id))).scalar_one()
        cpo_view_after = await cpo_list_firmware_releases(admin, db)
        admin_view_after = await admin_list_firmware_releases(admin, db)
    assert [r["version"] for r in cpo_view_after] == ["2.10.0-direct", "2.2.0-direct"]
    assert admin_view_after["total"] == 3  # deactivated row still visible to admins

    async with factory() as db:
        deactivated = (
            await db.execute(select(FirmwareRelease).where(FirmwareRelease.id == res3["id"]))
        ).scalar_one()
        assert deactivated.is_active is False


@requires_db
@pytest.mark.asyncio
async def test_version_unique_constraint_rejects_a_forced_collision(factory):
    from sqlalchemy.exc import IntegrityError

    from backend.database.models import FirmwareRelease

    async with factory() as db:
        db.add(FirmwareRelease(version="2.3.0-direct", url="https://storage.googleapis.com/amphive-fw/a.bin"))
        await db.commit()

    async with factory() as db:
        db.add(FirmwareRelease(version="2.3.0-direct", url="https://storage.googleapis.com/amphive-fw/b.bin"))
        with pytest.raises(IntegrityError):
            await db.commit()
