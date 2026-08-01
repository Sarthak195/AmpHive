"""
Tests for the "favorite this charger" star (discovery bundle):

1. Endpoints (routers/plugs.py favorite_plug / unfavorite_plug): arm/disarm
   CRUD + idempotency (double-star, double-unstar, the UNIQUE-race
   IntegrityError path), the shared single-plug access rule (403 for a
   non-member on a private-group plug), and — unlike watch_plug — NO
   state-based rejection: starring an AVAILABLE-right-now plug is fine (it's
   a bookmark, not a "notify me" arm).
2. The `is_favorite` response field on the plug list/detail endpoints (one
   extra query for the whole list — no N+1, mirrors `watching`).

DB-free: the mocked-AsyncSession pattern from test_plug_watch.py.
"""
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from backend.database.models import PlugStatus, UserFavorite
from backend.routers.plugs import (
    favorite_plug,
    get_available_plugs,
    get_plug,
    unfavorite_plug,
)

# ---------------------------------------------------------------- helpers ---

def _user(user_id=42):
    u = MagicMock()
    u.id = user_id
    return u


def _plug(plug_id=7, status=PlugStatus.AVAILABLE, group_id=None, name="Bay 1",
          gateway_id="gw-1"):
    p = MagicMock()
    p.id = plug_id
    p.name = name
    p.status = status
    p.group_id = group_id
    p.gateway_id = gateway_id
    p.local_ip = "10.0.0.9"
    p.current_power_w = 0.0
    p.plug_model = "tapo_p110"
    p.latitude = None
    p.longitude = None
    p.last_telemetry_at = None
    p.queued_charging_enabled = None
    p.auto_start_delay_min = None
    p.rated_power_w = None
    p.connector_type = None
    return p


def _group(group_id=5, is_public=False, name="Sunrise Apartments"):
    g = MagicMock()
    g.id = group_id
    g.is_public = is_public
    g.name = name
    return g


def _scalar_one_or_none(value):
    r = MagicMock()
    r.scalar_one_or_none.return_value = value
    return r


def _db(*results):
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=list(results))
    db.add = MagicMock()          # add() is sync on AsyncSession
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.refresh = AsyncMock()
    return db


# ------------------------------------------------- POST /favorite (arm) -----

@pytest.mark.asyncio
async def test_favorite_available_plug_creates_row():
    """Unlike watch_plug, an AVAILABLE-right-now plug is a perfectly normal
    thing to star — no 409, no gateway liveness check at all."""
    user = _user(42)
    plug = _plug(plug_id=7, status=PlugStatus.AVAILABLE)
    db = _db(
        _scalar_one_or_none(plug),   # plug lookup
        _scalar_one_or_none(None),   # no existing favorite
    )

    res = await favorite_plug(7, user, db)

    assert res == {"status": "favorited", "plug_id": 7, "is_favorite": True}
    added = db.add.call_args[0][0]
    assert isinstance(added, UserFavorite)
    assert (added.user_id, added.plug_id) == (42, 7)
    db.commit.assert_awaited_once()
    # Exactly two queries — plug lookup + existing-favorite check. No
    # gateway/liveness query, unlike watch_plug's AVAILABLE branch.
    assert db.execute.await_count == 2


@pytest.mark.asyncio
async def test_favorite_is_idempotent_when_already_favorited():
    user = _user(42)
    plug = _plug(plug_id=7)
    existing = MagicMock()  # a UserFavorite row already there
    db = _db(
        _scalar_one_or_none(plug),
        _scalar_one_or_none(existing),
    )

    res = await favorite_plug(7, user, db)

    assert res["is_favorite"] is True
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_favorite_race_on_unique_constraint_treated_as_favorited():
    """Two concurrent stars: the loser's INSERT hits UNIQUE(user_id, plug_id).
    Same outcome as already-favorited — rollback, 200, is_favorite: true."""
    user = _user(42)
    plug = _plug(plug_id=7)
    db = _db(
        _scalar_one_or_none(plug),
        _scalar_one_or_none(None),   # pre-check saw nothing (the race window)
    )
    db.commit = AsyncMock(side_effect=IntegrityError("stmt", {}, Exception("dup")))

    res = await favorite_plug(7, user, db)

    assert res["is_favorite"] is True
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_favorite_404_for_unknown_plug():
    db = _db(_scalar_one_or_none(None))
    with pytest.raises(HTTPException) as exc:
        await favorite_plug(999, _user(), db)
    assert exc.value.status_code == 404
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_favorite_403_for_non_member_on_private_group_plug():
    """Same access rule as GET /api/plugs/{id} (shared helper): a plug in a
    private group is only favoritable by members."""
    user = _user(42)
    plug = _plug(plug_id=7, group_id=5)
    db = _db(
        _scalar_one_or_none(plug),                     # plug lookup
        _scalar_one_or_none(_group(is_public=False)),  # its private group
        _scalar_one_or_none(None),                     # no membership
    )

    with pytest.raises(HTTPException) as exc:
        await favorite_plug(7, user, db)

    assert exc.value.status_code == 403
    assert "private group" in exc.value.detail.lower()
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_favorite_allowed_for_member_of_private_group():
    user = _user(42)
    plug = _plug(plug_id=7, group_id=5)
    db = _db(
        _scalar_one_or_none(plug),
        _scalar_one_or_none(_group(is_public=False)),
        _scalar_one_or_none(MagicMock()),  # membership exists
        _scalar_one_or_none(None),         # no existing favorite
    )

    res = await favorite_plug(7, user, db)

    assert res["is_favorite"] is True
    db.add.assert_called_once()
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [
    PlugStatus.AVAILABLE, PlugStatus.OCCUPIED, PlugStatus.OFFLINE,
    PlugStatus.MAINTENANCE,
])
async def test_favorite_allowed_for_every_status(status):
    """No state-based rejection anywhere — a bookmark, not a "notify me" arm."""
    user = _user(42)
    plug = _plug(plug_id=7, status=status)
    db = _db(
        _scalar_one_or_none(plug),
        _scalar_one_or_none(None),
    )

    res = await favorite_plug(7, user, db)

    assert res["is_favorite"] is True
    db.add.assert_called_once()


# ---------------------------------------------- DELETE /favorite (disarm) ---

@pytest.mark.asyncio
async def test_unfavorite_deletes_and_is_idempotent():
    """Disarm issues a scoped DELETE and returns the same 200 whether or not
    a favorite existed — running it twice is harmless."""
    user = _user(42)
    for _ in range(2):
        db = _db(MagicMock())  # the DELETE's (row-count) result
        res = await unfavorite_plug(7, user, db)
        assert res == {"status": "unfavorited", "plug_id": 7, "is_favorite": False}
        stmt = db.execute.await_args.args[0]
        sql = str(stmt)
        assert sql.startswith("DELETE FROM user_favorites")
        assert "user_id" in sql and "plug_id" in sql  # scoped to (user, plug)
        db.commit.assert_awaited_once()


# ------------------------------------------------- `is_favorite` field ------

@pytest.mark.asyncio
async def test_available_plugs_carry_is_favorite_via_one_extra_query():
    """The list endpoint marks favorited plugs from ONE per-user favorites
    query — a constant extra query for any list size (no N+1), mirroring
    `watching`."""
    user = _user(42)
    plug_a = _plug(plug_id=1, status=PlugStatus.AVAILABLE, name="Plug A")
    plug_b = _plug(plug_id=2, status=PlugStatus.OCCUPIED, name="Plug B")
    gateway = MagicMock(latitude=None, longitude=None)
    tenant = MagicMock(queued_charging_enabled=False)

    rows_result = MagicMock()
    rows_result.all.return_value = [
        (plug_a, None, None, None, gateway, tenant),
        (plug_b, None, None, None, gateway, tenant),
    ]
    watched_result = MagicMock()
    watched_result.scalars.return_value.all.return_value = []  # no watches
    favorited_result = MagicMock()
    favorited_result.scalars.return_value.all.return_value = [2]  # favorited plug B

    expire_result = MagicMock(rowcount=0)
    reservations_result = MagicMock()
    reservations_result.scalars.return_value = []

    db = _db(rows_result, watched_result, favorited_result, expire_result, reservations_result)

    with patch("backend.routers.plugs.gateway_is_live", return_value=True), \
         patch("backend.routers.plugs.resolve_price_display_batch",
               AsyncMock(return_value={1: (Decimal("5.00"), None, None),
                                       2: (Decimal("5.00"), None, None)})):
        responses = await get_available_plugs(user, db)

    assert db.execute.await_count == 5
    by_id = {r.id: r for r in responses}
    assert by_id[1].is_favorite is False
    assert by_id[2].is_favorite is True


@pytest.mark.asyncio
async def test_get_plug_reports_is_favorite_true():
    user = _user(42)
    plug = _plug(plug_id=7, status=PlugStatus.OCCUPIED)  # ungrouped

    expire_result = MagicMock(rowcount=0)
    reservations_result = MagicMock()
    reservations_result.scalars.return_value = []

    db = _db(
        _scalar_one_or_none(plug),        # plug lookup
        _scalar_one_or_none(None),        # gateway lookup (none — offline)
        expire_result,                    # reservation lazy-expiry UPDATE
        reservations_result,              # grouped reservation SELECT
        _scalar_one_or_none(101),         # a watch row id (none) — watching
        _scalar_one_or_none(202),         # a favorite row id exists
    )

    with patch("backend.routers.plugs.resolve_price_display",
               AsyncMock(return_value=(Decimal("5.00"), None, None))):
        res = await get_plug(7, user, db)

    assert res.is_favorite is True
    assert res.status == "occupied"
