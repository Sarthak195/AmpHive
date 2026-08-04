"""
Tests for the platform-admin API surface (backend/routers/admin.py,
redesign/ui-v3 contract §4) plus the users.is_disabled enforcement in
login and services/auth.get_current_user.

DB-free: mocked-AsyncSession pattern from test_audit_log.py; the RBAC gate
is asserted on the shared require_role dependency (and on every admin
route's declared dependencies) the same way test_direct_rbac.py does —
calling a route function directly bypasses FastAPI's DI entirely.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials

from backend.database.models import (
    GatewayStatus,
    PlugStatus,
    TransactionType,
    UserRole,
)
from backend.routers.admin import (
    admin_adjust_balance,
    admin_gateway_ota,
    admin_list_gateways,
    admin_list_payouts,
    admin_list_plugs,
    admin_stats_overview,
    admin_update_user,
)
from backend.routers.admin import (
    router as admin_router,
)
from backend.routers.auth import login
from backend.schemas import (
    AdminAdjustBalanceRequest,
    AdminUserUpdateRequest,
    CpoGatewayOtaRequest,
    LoginRequest,
)
from backend.services.auth import (
    create_access_token,
    get_current_user,
    hash_password,
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
    # contract §4 + GET /api/admin/gateway-logs (TD#28) + POST/GET
    # /api/admin/gateways/inventory (feat/easy-provisioning claim-code onboarding)
    # + POST/GET /api/admin/firmware-releases + POST .../deactivate
    # (feat/ota-version-picker) + GET /api/admin/plugs + POST
    # /api/admin/gateways/{id}/ota + POST /api/admin/firmware-releases/upload
    # (feat/admin-dashboard completion). NOTE: the public GET
    # /api/firmware/images/{filename} deliberately lives in a SEPARATE router
    # (routers/firmware_images.py), not here — it must be reachable without the
    # admin gate — so it is not counted among these admin-only routes.
    assert len(routes) == 19
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


def _gateway(gw_id="gw-1", tenant_id=1, status=GatewayStatus.ONLINE,
             last_seen_at=None):
    gw = MagicMock()
    gw.id = gw_id
    gw.name = "Site A"
    gw.tenant_id = tenant_id
    gw.status = status
    gw.firmware_version = "2.1.0-direct"
    # Fresh by default: `online` derives from gateway_is_live (status AND a
    # fresh last_seen_at), so a dated fixture timestamp reads as offline.
    gw.last_seen_at = last_seen_at or datetime.now(timezone.utc)
    return gw


@pytest.mark.asyncio
async def test_list_gateways_shape_and_online_derivation():
    seen = datetime.now(timezone.utc)
    db = _db(
        _scalar(2),  # total
        _all([
            (_gateway("gw-1", status=GatewayStatus.ONLINE, last_seen_at=seen),
             "Acme Charging", 3),
            (_gateway("gw-2", status=GatewayStatus.OFFLINE, last_seen_at=seen),
             "Beta Homes", 0),
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
        "last_seen_at": seen.isoformat(),
        "firmware_version": "2.1.0-direct",
        "plug_count": 3,
    }
    assert resp["items"][1]["online"] is False
    assert resp["items"][1]["plug_count"] == 0


@pytest.mark.asyncio
async def test_list_gateways_stale_online_flag_reads_offline():
    """Crash-outage case: the DB flag is stuck ONLINE (LWT never delivered,
    retained `online` replayed on broker restart) but nothing has been seen
    for hours — the fleet list must not report it online."""
    stale = datetime.now(timezone.utc) - timedelta(hours=9)
    db = _db(
        _scalar(1),
        _all([
            (_gateway("gw-1", status=GatewayStatus.ONLINE, last_seen_at=stale),
             "Acme Charging", 3),
        ]),
    )

    resp = await admin_list_gateways(_admin(), db, online=None, limit=50, offset=0)

    assert resp["items"][0]["online"] is False


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
        # [Admin dashboard completion] the three APPENDED queries, in order:
        _scalar(4),                                              # public plugs
        _scalar(2),                                              # private plugs
        _first((50.0, Decimal("600.00"))),                       # last-30d energy/revenue
    )

    resp = await admin_stats_overview(_admin(), db)

    assert resp == {
        "tenants": 3,
        "users": {"total": 8, "drivers": 5, "cpos": 2, "admins": 1},
        "gateways": {"total": 4, "online": 2},
        "plugs": {"total": 6, "public": 4, "private": 2},
        "sessions": {"active": 1, "today": 2, "total": 10},
        "energy_kwh": {"today": 1.5, "total": 100.0, "last_30d": 50.0},
        "revenue_coins": {"today": 20.0, "total": 1234.5, "last_30d": 600.0},
        "payouts": {"requested_count": 2, "requested_net_coins": 55.5},
        "disputes": {"open": 1},
    }


# --- GET /api/admin/plugs ---------------------------------------------------


def _plug(plug_id=1, name="Plug A", status=PlugStatus.AVAILABLE, gateway_id="gw-1",
          group_id=None, local_ip="10.0.0.5", plug_model="tapo_p110",
          connector_type="Type2", rated_power_w=7400, current_power_w=0.0,
          last_telemetry_at=None, tariff_id=None):
    p = MagicMock()
    p.id = plug_id
    p.name = name
    p.status = status
    p.gateway_id = gateway_id
    p.group_id = group_id
    p.local_ip = local_ip
    p.plug_model = plug_model
    p.connector_type = connector_type
    p.rated_power_w = rated_power_w
    p.current_power_w = current_power_w
    p.last_telemetry_at = last_telemetry_at
    p.tariff_id = tariff_id
    return p


@pytest.mark.asyncio
async def test_list_plugs_shape_and_visibility_derivation():
    """Ungrouped and public-group plugs read `public`; a private-group plug
    reads `private` — the derived per-item value must track the (row's group)
    is_public exactly, the same rule the SQL visibility filter uses."""
    seen = datetime.now(timezone.utc)
    p_ungrouped = _plug(1, "Ungrouped", group_id=None, last_telemetry_at=seen)
    p_public = _plug(2, "In public group", group_id=5)
    p_private = _plug(3, "In private group", group_id=9)
    db = _db(
        _scalar(3),
        _all([
            # (plug, gateway_name, tenant_id, tenant_name, group_name, is_public)
            (p_ungrouped, "GW One", 1, "Acme", None, None),
            (p_public, "GW One", 1, "Acme", "Public Lot", True),
            (p_private, "GW Two", 2, "Beta", "Private Lot", False),
        ]),
    )

    resp = await admin_list_plugs(
        _admin(), db, q=None, visibility=None, status=None, limit=50, offset=0)

    assert resp["total"] == 3
    assert [i["visibility"] for i in resp["items"]] == ["public", "public", "private"]
    assert resp["items"][0] == {
        "id": 1,
        "name": "Ungrouped",
        "status": "available",
        "gateway_id": "gw-1",
        "gateway_name": "GW One",
        "tenant_id": 1,
        "tenant_name": "Acme",
        "group_id": None,
        "group_name": None,
        "visibility": "public",
        "local_ip": "10.0.0.5",
        "plug_model": "tapo_p110",
        "connector_type": "Type2",
        "rated_power_w": 7400,
        "current_power_w": 0.0,
        "last_telemetry_at": seen.isoformat(),
        "tariff_id": None,
    }
    assert resp["items"][1]["group_id"] == 5
    assert resp["items"][1]["group_name"] == "Public Lot"
    assert resp["items"][2]["group_id"] == 9
    assert resp["items"][2]["group_name"] == "Private Lot"


@pytest.mark.asyncio
async def test_list_plugs_visibility_and_status_filter_accepted():
    p = _plug(2, "Public plug", group_id=5)
    db = _db(_scalar(1), _all([(p, "GW", 1, "Acme", "Lot", True)]))

    resp = await admin_list_plugs(
        _admin(), db, q="pub", visibility="public", status="available", limit=50, offset=0)

    assert resp["total"] == 1
    assert resp["items"][0]["visibility"] == "public"


@pytest.mark.asyncio
async def test_list_plugs_invalid_visibility_400():
    db = _db()  # rejected before any query runs
    with pytest.raises(HTTPException) as exc:
        await admin_list_plugs(
            _admin(), db, q=None, visibility="secret", status=None, limit=50, offset=0)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_list_plugs_invalid_status_400():
    db = _db()
    with pytest.raises(HTTPException) as exc:
        await admin_list_plugs(
            _admin(), db, q=None, visibility=None, status="frying", limit=50, offset=0)
    assert exc.value.status_code == 400


# --- POST /api/admin/gateways/{id}/ota --------------------------------------

OTA_URL = "https://storage.googleapis.com/amphive-fw/amphive-gateway-2.4.0.bin"


def _release_row(release_id=7, version="2.4.0-direct", url=OTA_URL):
    r = MagicMock()
    r.id = release_id
    r.version = version
    r.url = url
    r.is_active = True
    return r


@pytest.mark.asyncio
async def test_admin_ota_via_release_id_audits_into_gateway_tenant():
    gw = _gateway("gw-1", tenant_id=3, status=GatewayStatus.ONLINE)
    db = _db(
        _scalar_one_or_none(gw),                 # gateway lookup (no tenant filter)
        _scalar_one_or_none(_release_row(7)),    # release lookup
        _scalar_one_or_none(5),                  # lowest plug id
    )
    with patch("backend.routers.admin.state") as st:
        st.mqtt_manager.send_gateway_ota.return_value = True
        resp = await admin_gateway_ota(
            "gw-1", CpoGatewayOtaRequest(release_id=7), _admin(), db)

    assert resp["status"] == "ota_triggered"
    assert resp["firmware_url"] == OTA_URL
    assert resp["release_version"] == "2.4.0-direct"
    st.mqtt_manager.send_gateway_ota.assert_called_once_with("gw-1", 5, OTA_URL)
    audit = db.add.call_args[0][0]
    assert audit.action == "gateway.ota"
    assert audit.target_type == "gateway"
    assert audit.target_id == "gw-1"
    assert audit.tenant_id == 3


@pytest.mark.asyncio
async def test_admin_ota_via_firmware_url_on_unclaimed_gateway():
    # tenant_id None: unclaimed inventory gateways are permitted for admin OTA.
    gw = _gateway("gw-9", tenant_id=None, status=GatewayStatus.ONLINE)
    db = _db(_scalar_one_or_none(gw), _scalar_one_or_none(2))
    with patch("backend.routers.admin.state") as st:
        st.mqtt_manager.send_gateway_ota.return_value = True
        resp = await admin_gateway_ota(
            "gw-9", CpoGatewayOtaRequest(firmware_url=OTA_URL), _admin(), db)

    assert resp["release_version"] is None
    assert resp["firmware_url"] == OTA_URL
    st.mqtt_manager.send_gateway_ota.assert_called_once_with("gw-9", 2, OTA_URL)


@pytest.mark.asyncio
async def test_admin_ota_unknown_gateway_404():
    db = _db(_scalar_one_or_none(None))
    with pytest.raises(HTTPException) as exc:
        await admin_gateway_ota(
            "nope", CpoGatewayOtaRequest(firmware_url=OTA_URL), _admin(), db)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_admin_ota_offline_gateway_409():
    gw = _gateway("gw-1", status=GatewayStatus.OFFLINE)
    db = _db(_scalar_one_or_none(gw))
    with pytest.raises(HTTPException) as exc:
        await admin_gateway_ota(
            "gw-1", CpoGatewayOtaRequest(firmware_url=OTA_URL), _admin(), db)
    assert exc.value.status_code == 409
    assert "offline" in exc.value.detail.lower()


@pytest.mark.asyncio
async def test_admin_ota_no_plugs_409():
    gw = _gateway("gw-1", status=GatewayStatus.ONLINE)
    db = _db(_scalar_one_or_none(gw), _scalar_one_or_none(None))  # live, no plugs
    with pytest.raises(HTTPException) as exc:
        await admin_gateway_ota(
            "gw-1", CpoGatewayOtaRequest(firmware_url=OTA_URL), _admin(), db)
    assert exc.value.status_code == 409
    assert "no registered plugs" in exc.value.detail


@pytest.mark.asyncio
async def test_admin_ota_publish_failure_502():
    gw = _gateway("gw-1", status=GatewayStatus.ONLINE)
    db = _db(_scalar_one_or_none(gw), _scalar_one_or_none(1))
    with patch("backend.routers.admin.state") as st:
        st.mqtt_manager.send_gateway_ota.return_value = False
        with pytest.raises(HTTPException) as exc:
            await admin_gateway_ota(
                "gw-1", CpoGatewayOtaRequest(firmware_url=OTA_URL), _admin(), db)
    assert exc.value.status_code == 502


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
