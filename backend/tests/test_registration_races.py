"""
Regression tests for duplicate-insert races on /api/auth/register and
/api/cpo/setup.

Both endpoints do exists-check-then-insert. A concurrent duplicate slips past
the SELECT (it can't see the twin's uncommitted row) and hits the unique
index at commit/flush — which used to escape as a raw IntegrityError 500.
The race must map to the same 400 the sequential duplicate path returns.
"""
import pytest
from fastapi import HTTPException
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.exc import IntegrityError

from backend.routers.auth import register
from backend.routers.cpo import cpo_setup
from backend.schemas import CpoSetupRequest, RegisterRequest


def _integrity_error():
    return IntegrityError("INSERT ...", {}, Exception("duplicate key value"))


def _db_passing_exists_check():
    """Mock AsyncSession whose SELECTs find nothing (the race: the duplicate
    is not yet visible when the exists-check runs)."""
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    db.add = MagicMock()
    return db


@pytest.mark.asyncio
async def test_register_duplicate_race_maps_to_400():
    db = _db_passing_exists_check()
    db.commit = AsyncMock(side_effect=_integrity_error())

    req = RegisterRequest(email="dup@example.com", password="pw", full_name="Dup")
    with pytest.raises(HTTPException) as exc_info:
        await register(req, db)

    assert exc_info.value.status_code == 400
    assert "already exists" in exc_info.value.detail
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_cpo_setup_duplicate_tenant_race_maps_to_400():
    db = _db_passing_exists_check()
    db.flush = AsyncMock(side_effect=_integrity_error())

    user = MagicMock()
    user.tenant_id = None

    req = CpoSetupRequest(tenant_name="Acme Charging")
    with pytest.raises(HTTPException) as exc_info:
        await cpo_setup(req, user, db)

    assert exc_info.value.status_code == 400
    assert "already exists" in exc_info.value.detail
    db.rollback.assert_awaited_once()
