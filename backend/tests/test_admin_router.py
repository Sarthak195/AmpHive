"""
Tests for the platform-admin API surface (backend/routers/admin.py,
redesign/ui-v3 contract §4) plus the users.is_disabled enforcement in
login and services/auth.get_current_user.

DB-free: mocked-AsyncSession pattern from test_audit_log.py; the RBAC gate
is asserted on the shared require_role dependency (and on every admin
route's declared dependencies) the same way test_direct_rbac.py does —
calling a route function directly bypasses FastAPI's DI entirely.
"""
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials

from backend.database.models import (
    GatewayStatus, TransactionType, UserRole,
)
from backend.routers.admin import (
    admin_adjust_balance, admin_list_gateways, admin_list_payouts,
    admin_stats_overview, admin_update_user, router as admin_router,
)
from backend.routers.auth import login
from backend.schemas import (
    AdminAdjustBalanceRequest, AdminUserUpdateRequest, LoginRequest,
)
from backend.services.auth import (
    create_access_token, get_current_user, hash_password,
)
from backend.services.rbac import require_role


def _admin(user_id=1, email="admin@amphive.test"):
    u = MagicMock()
    u.id = user_id
    u.email = email
    u.role = UserRole.ADMIN
    u.tenant_id = None  # platform admins have no tenant — that is the point
    return u


def _target(user_id=7, email="driver@amphive.test", role=UserRole.DRIVER,
            tenant_id=None, is_disabled=False):
    u = MagicMock()
    u.id = user_id
    u.email = email
    u.role = role
    u.tenant_id = tenant_id
    u.is_disabled = is_disabled
    return u


def _scalar_one_or_none(value):
    r = MagicMock()
    r.scalar_one_or_none.return_value = value
    return r


def _scalar(value):
    r = MagicMock()
    r.scalar.return_value = value
    return r


def _first(value):
    r = MagicMock()
    r.first.return_value = value
    return r


def _all(rows):
    r = MagicMock()
    r.all.return_value = list(rows)
    return r


def _one_or_none(value):
    r = MagicMock()
    r.one_or_none.return_value = value
    return r


def _db(*results):
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=list(results))
    db.add = MagicMock()  # add() is sync on AsyncSession
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.rollback = AsyncMock()
    return db


# --- RBAC gating -----------------------------------------------------------


def _allowed_roles(checker):
    """Extract require_role's allowed_roles tuple from the role_checker
    closure — asserts the gate's configuration, not just its presence."""
    for name, cell in zip(checker.__code__.co_freevars, checker.__closure__ or ()):
        if name == "allowed_roles":
            return cell.cell_contents
    return None


def test_every_admin_route_is_admin_only():
    routes = [r for r in admin_router.routes if isinstance(r, APIRoute)]
    assert len(routes) == 10  # contract §4: the full admin surface
    for route in routes:
        assert route.path.startswith("/api/admin/")
        gates = [
            d for d in route.dependant.dependencies
            if d.call is not None and getattr(d.call, "__name__", "") == "role_checker"
        ]
        assert gates, f"{route.path} is missing its require_role gate"
        for gate in gates:
            assert _allowed_roles(gate.call) == ("admin",), (
                f"{route.path} must be admin-only"
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [UserRole.DRIVER, UserRole.CPO])
async def test_admin_gate_rejects_non_admins(role):
    checker = require_role("admin")
    u = MagicMock()
    u.role = role
    with pytest.raises(HTTPException) as exc:
        await checker(user=u)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_admin_gate_accepts_admin():
    checker = require_role("admin")
    u = _admin()
    assert await checker(user=u) is u


# --- PATCH /api/admin/users/{id} -------------------------------------------


@pytest.mark.asyncio
async def test_patch_user_self_demote_forbidden():
    admin = _admin(user_id=1)
    me = _target(user_id=1, email=admin.email, role=UserRole.ADMIN)
    db = _db(_scalar_one_or_none(me))

    with pytest.raises(HTTPException) as exc:
        await admin_update_user(1, AdminUserUpdateRequest(role="driver"), admin, db)

    assert exc.value.status_code == 403
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_patch_user_self_disable_forbidden():
    admin = _admin(user_id=1)
    me = _target(user_id=1, email=admin.email, role=UserRole.ADMIN)
    db = _db(_scalar_one_or_none(me))

    with pytest.raises(HTTPException) as exc:
        await admin_update_user(1, AdminUserUpdateRequest(is_disabled=True), admin, db)

    assert exc.value.status_code == 403
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_patch_user_invalid_role_400():
    db = _db()
    with pytest.raises(HTTPException) as exc:
        await admin_update_user(7, AdminUserUpdateRequest(role="superuser"), _admin(), db)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_patch_user_empty_body_400():
    db = _db()
    with pytest.raises(HTTPException) as exc:
        await admin_update_user(7, AdminUserUpdateRequest(), _admin(), db)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_patch_user_unknown_404():
    db = _db(_scalar_one_or_none(None))
    with pytest.raises(HTTPException) as exc:
        await admin_update_user(999, AdminUserUpdateRequest(role="cpo"), _admin(), db)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_patch_user_role_change_bumps_tokens_and_audits():
    admin = _admin(user_id=1)
    target = _target(user_id=7, role=UserRole.DRIVER, tenant_id=None)
    db = _db(_scalar_one_or_none(target), MagicMock())  # select target, UPDATE

    resp = await admin_update_user(7, AdminUserUpdateRequest(role="cpo"), admin, db)

    assert resp == {
        "status": "updated", "id": 7, "role": "cpo",
        "is_disabled": target.is_disabled, "tokens_revoked": True,
    }
    # The UPDATE must carry the DB-side token_version increment.
    update_stmt = db.execute.call_args_list[1][0][0]
    assert "token_version" in str(update_stmt)
    # Audit row: platform-level (target has no tenant → tenant_id None).
    added = db.add.call_args[0][0]
    assert added.action == "user.update"
    assert added.target_type == "user"
    assert added.target_id == "7"
    assert added.tenant_id is None
    assert added.actor_user_id == 1
    assert "role: driver -> cpo" in added.detail
    assert db.commit.await_count == 2  # update + audit


@pytest.mark.asyncio
async def test_patch_user_disable_bumps_tokens():
    target = _target(user_id=7, is_disabled=False)
    db = _db(_scalar_one_or_none(target), MagicMock())

    resp = await admin_update_user(
        7, AdminUserUpdateRequest(is_disabled=True), _admin(), db)

    assert resp["is_disabled"] is True
    assert resp["tokens_revoked"] is True
    assert "token_version" in str(db.execute.call_args_list[1][0][0])


@pytest.mark.asyncio
async def test_patch_user_reenable_does_not_bump_tokens():
    target = _target(user_id=7, is_disabled=True)
    db = _db(_scalar_one_or_none(target), MagicMock())

    resp = await admin_update_user(
        7, AdminUserUpdateRequest(is_disabled=False), _admin(), db)

    assert resp["is_disabled"] is False
    assert resp["tokens_revoked"] is False
    assert "token_version" not in str(db.execute.call_args_list[1][0][0])


# --- POST /api/admin/users/{id}/adjust-balance ------------------------------


@pytest.mark.asyncio
async def test_adjust_balance_credit_writes_topup_ledger_row():
    db = _db(
        _one_or_none((Decimal("10.00"), "driver@amphive.test", None)),  # lock read
        MagicMock(),                                                    # UPDATE
    )

    resp = await admin_adjust_balance(
        7, AdminAdjustBalanceRequest(amount_coins=5, reason="goodwill credit"),
        _admin(), db)

    assert resp == {"new_balance": 15.0}
    ledger = db.add.call_args_list[0][0][0]
    assert ledger.user_id == 7
    assert ledger.session_id is None
    assert ledger.amount == Decimal("5.00")
    assert ledger.transaction_type == TransactionType.TOPUP
    assert ledger.balance_after == Decimal("15.00")
    assert "goodwill credit" in ledger.description
    audit = db.add.call_args_list[1][0][0]
    assert audit.action == "user.adjust_balance"
    assert audit.target_id == "7"
    assert db.commit.await_count == 2  # wallet write + audit


@pytest.mark.asyncio
async def test_adjust_balance_debit_floors_at_zero():
    """A clawback larger than the balance clamps to 0 (the DB CHECK forbids
    negative balances) and the ledger records the ACTUAL applied delta."""
    db = _db(
        _one_or_none((Decimal("3.00"), "driver@amphive.test", 2)),
        MagicMock(),
    )

    resp = await admin_adjust_balance(
        7, AdminAdjustBalanceRequest(amount_coins=-10, reason="fraud clawback"),
        _admin(), db)

    assert resp == {"new_balance": 0.0}
    ledger = db.add.call_args_list[0][0][0]
    assert ledger.amount == Decimal("-3.00")
    assert ledger.transaction_type == TransactionType.SESSION_DEBIT
    assert ledger.balance_after == Decimal("0.00")
    audit = db.add.call_args_list[1][0][0]
    assert audit.tenant_id == 2  # audited into the target's tenant when it has one


@pytest.mark.asyncio
async def test_adjust_balance_zero_amount_400():
    db = _db()
    with pytest.raises(HTTPException) as exc:
        await admin_adjust_balance(
            7, AdminAdjustBalanceRequest(amount_coins=0.001, reason="rounds to zero"),
            _admin(), db)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_adjust_balance_debit_from_empty_wallet_400():
    db = _db(_one_or_none((Decimal("0.00"), "driver@amphive.test", None)))
    with pytest.raises(HTTPException) as exc:
        await admin_adjust_balance(
            7, AdminAdjustBalanceRequest(amount_coins=-5, reason="nothing there"),
            _admin(), db)
    assert exc.value.status_code == 400
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_adjust_balance_unknown_user_404():
    db = _db(_one_or_none(None))
    with pytest.raises(HTTPException) as exc:
        await admin_adjust_balance(
            999, AdminAdjustBalanceRequest(amount_coins=5, reason="whatever"),
            _admin(), db)
    assert exc.value.status_code == 404


# --- List shapes ------------------------------------------------------------


def _gateway(gw_id="gw-1", tenant_id=1, status=GatewayStatus.ONLINE):
    gw = MagicMock()
    gw.id = gw_id
    gw.name = "Site A"
    gw.tenant_id = tenant_id
    gw.status = status
    gw.firmware_version = "2.1.0-direct"
    gw.last_seen_at = datetime(2026, 7, 21, tzinfo=timezone.utc)
    return gw


@pytest.mark.asyncio
async def test_list_gateways_shape_and_online_derivation():
    db = _db(
        _scalar(2),  # total
        _all([
            (_gateway("gw-1", status=GatewayStatus.ONLINE), "Acme Charging", 3),
            (_gateway("gw-2", status=GatewayStatus.OFFLINE), "Beta Homes", 0),
        ]),
    )

    resp = await admin_list_gateways(_admin(), db, online=None, limit=50, offset=0)

    assert resp["total"] == 2
    first = resp["items"][0]
    assert first == {
        "id": "gw-1",
        "gateway_id": "gw-1",
        "name": "Site A",
        "tenant_id": 1,
        "tenant_name": "Acme Charging",
        "online": True,
        "last_seen_at": "2026-07-21T00:00:00+00:00",
        "firmware_version": "2.1.0-direct",
        "plug_count": 3,
    }
    assert resp["items"][1]["online"] is False
    assert resp["items"][1]["plug_count"] == 0


@pytest.mark.asyncio
async def test_list_payouts_invalid_status_400():
    db = _db()
    with pytest.raises(HTTPException) as exc:
        await admin_list_payouts(_admin(), db, status="pending", limit=50, offset=0)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_stats_overview_shape():
    db = _db(
        _scalar(3),                                              # tenants
        _all([(UserRole.DRIVER, 5), (UserRole.CPO, 2), (UserRole.ADMIN, 1)]),
        _scalar(4),                                              # gateways total
        _scalar(2),                                              # gateways online
        _scalar(6),                                              # plugs
        _scalar(1),                                              # sessions active
        _scalar(2),                                              # sessions today
        _scalar(10),                                             # sessions total
        _first((1.5, Decimal("20.00"))),                         # today energy/revenue
        _first((100.0, Decimal("1234.50"))),                     # total energy/revenue
        _first((2, Decimal("55.50"))),                           # requested payouts
        _scalar(1),                                              # open disputes
    )

    resp = await admin_stats_overview(_admin(), db)

    assert resp == {
        "tenants": 3,
        "users": {"total": 8, "drivers": 5, "cpos": 2, "admins": 1},
        "gateways": {"total": 4, "online": 2},
        "plugs": {"total": 6},
        "sessions": {"active": 1, "today": 2, "total": 10},
        "energy_kwh": {"today": 1.5, "total": 100.0},
        "revenue_coins": {"today": 20.0, "total": 1234.5},
        "payouts": {"requested_count": 2, "requested_net_coins": 55.5},
        "disputes": {"open": 1},
    }


# --- is_disabled enforcement (login + get_current_user) ---------------------


def _auth_user(password="correct-horse", is_disabled=False, token_version=0):
    u = MagicMock()
    u.id = 7
    u.email = "driver@amphive.test"
    u.hashed_password = hash_password(password)
    u.full_name = "Test Driver"
    u.role = MagicMock()
    u.role.value = "driver"
    u.coin_balance = 0.0
    u.token_version = token_version
    u.is_disabled = is_disabled
    return u


def _db_returning(user):
    result = MagicMock()
    result.scalar_one_or_none.return_value = user
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    return db


@pytest.mark.asyncio
async def test_login_disabled_account_403():
    user = _auth_user(is_disabled=True)
    db = _db_returning(user)

    with pytest.raises(HTTPException) as exc:
        await login(LoginRequest(email=user.email, password="correct-horse"), db)

    assert exc.value.status_code == 403
    assert exc.value.detail == "account_disabled"


@pytest.mark.asyncio
async def test_login_disabled_check_runs_after_password():
    """Wrong password on a disabled account still 401s — no oracle telling a
    guesser the account exists but is disabled."""
    user = _auth_user(is_disabled=True)
    db = _db_returning(user)

    with pytest.raises(HTTPException) as exc:
        await login(LoginRequest(email=user.email, password="wrong"), db)

    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_login_enabled_account_still_works():
    user = _auth_user(is_disabled=False)
    db = _db_returning(user)

    with patch("backend.routers.auth.check_and_speed_up_active_session",
               new=AsyncMock()):
        resp = await login(LoginRequest(email=user.email, password="correct-horse"), db)

    assert resp.token


@pytest.mark.asyncio
async def test_get_current_user_rejects_disabled_with_valid_token():
    """Existing tokens must die the moment an account is disabled."""
    user = _auth_user(is_disabled=True, token_version=0)
    token = create_access_token(user.id, "driver", user.email, token_version=0)
    db = _db_returning(user)

    with pytest.raises(HTTPException) as exc:
        await get_current_user(
            HTTPAuthorizationCredentials(scheme="Bearer", credentials=token), db)

    assert exc.value.status_code == 403
    assert exc.value.detail == "account_disabled"
