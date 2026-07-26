"""
Tests for the groups router (backend/routers/groups.py):

- POST   /api/groups/join        join_group
- GET    /api/groups/my          get_my_groups
- DELETE /api/groups/{id}/leave  leave_group

DB-free: the mocked-AsyncSession pattern from test_driver_gap_endpoints.py /
test_admin_rbac.py — route functions are called directly with an AsyncMock
db whose execute() side-effects supply each query's result in call order.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from backend.database.models import UserRole
from backend.routers.groups import get_my_groups, join_group, leave_group
from backend.schemas import JoinGroupRequest


def _user(user_id=7, role=UserRole.DRIVER, tenant_id=None):
    u = MagicMock()
    u.id = user_id
    u.email = "driver@amphive.test"
    u.role = role
    u.tenant_id = tenant_id
    return u


def _group(group_id=2, name="Society", is_public=False, access_code="SUNRISE2024"):
    g = MagicMock()
    g.id = group_id
    g.name = name
    g.is_public = is_public
    g.access_code = access_code
    return g


def _scalar_one_or_none(value):
    r = MagicMock()
    r.scalar_one_or_none.return_value = value
    return r


def _all(rows):
    r = MagicMock()
    r.all.return_value = list(rows)
    return r


def _db(*results):
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=list(results))
    db.add = MagicMock()  # add() is sync on AsyncSession
    db.commit = AsyncMock()
    db.delete = AsyncMock()
    return db


# --- POST /api/groups/join ----------------------------------------------------


@pytest.mark.asyncio
async def test_join_group_success():
    group = _group()
    # First execute() = group-by-access-code lookup, second = existing-membership check.
    db = _db(_scalar_one_or_none(group), _scalar_one_or_none(None))

    resp = await join_group(
        JoinGroupRequest(access_code="SUNRISE2024"), _user(user_id=7), db
    )

    assert resp == {"status": "joined", "group_id": 2, "group_name": "Society"}
    db.add.assert_called_once()
    added = db.add.call_args[0][0]
    assert added.user_id == 7
    assert added.group_id == 2
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_join_group_invalid_access_code_404():
    db = _db(_scalar_one_or_none(None))

    with pytest.raises(HTTPException) as exc:
        await join_group(JoinGroupRequest(access_code="NOPE"), _user(), db)

    assert exc.value.status_code == 404
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_join_group_public_group_rejected_400():
    group = _group(is_public=True, access_code=None)
    db = _db(_scalar_one_or_none(group))

    with pytest.raises(HTTPException) as exc:
        await join_group(JoinGroupRequest(access_code="OPEN2024"), _user(), db)

    assert exc.value.status_code == 400
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_join_group_already_member_400():
    group = _group()
    existing_membership = MagicMock()
    db = _db(_scalar_one_or_none(group), _scalar_one_or_none(existing_membership))

    with pytest.raises(HTTPException) as exc:
        await join_group(
            JoinGroupRequest(access_code="SUNRISE2024"), _user(user_id=7), db
        )

    assert exc.value.status_code == 400
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


# --- GET /api/groups/my --------------------------------------------------------


@pytest.mark.asyncio
async def test_my_groups_shape_with_plug_counts():
    public_group = _group(group_id=1, name="Main Lot", is_public=True, access_code=None)
    private_group = _group(group_id=2, name="Society", is_public=False)
    # Single round-trip query: rows of (ChargerGroup, plug_count).
    db = _db(_all([(public_group, 3), (private_group, 0)]))

    resp = await get_my_groups(_user(user_id=7), db)

    assert [r.model_dump() for r in resp] == [
        {"id": 1, "name": "Main Lot", "is_public": True, "plug_count": 3},
        {"id": 2, "name": "Society", "is_public": False, "plug_count": 0},
    ]


@pytest.mark.asyncio
async def test_my_groups_empty_when_no_public_or_joined_groups():
    db = _db(_all([]))

    resp = await get_my_groups(_user(), db)

    assert resp == []


# --- DELETE /api/groups/{id}/leave ---------------------------------------------


@pytest.mark.asyncio
async def test_leave_group_deletes_membership():
    membership = MagicMock()
    db = _db(_scalar_one_or_none(membership))

    resp = await leave_group(2, _user(user_id=7), db)

    assert resp == {"status": "left", "group_id": 2}
    db.delete.assert_awaited_once_with(membership)
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_leave_group_not_a_member_404():
    db = _db(_scalar_one_or_none(None))

    with pytest.raises(HTTPException) as exc:
        await leave_group(2, _user(user_id=7), db)

    assert exc.value.status_code == 404
    db.delete.assert_not_awaited()
    db.commit.assert_not_awaited()
