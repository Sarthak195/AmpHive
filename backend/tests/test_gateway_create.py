"""
Tests for the CPO gateway-registration endpoint (POST /api/cpo/gateways).

Focus: vpn_ip is a legacy overlay field (NOT NULL + UNIQUE) that direct-MQTT
gateways don't set, so the endpoint falls back to the gateway_id — otherwise
two direct gateways would collide on the unique empty string. gateway_id is the
device MAC, which the firmware derives and shows in the setup portal.

Since 2026-11-16 the route is admin-only (gateway-ID squatting: a MAC is not a
secret, so any CPO could register a unit before its real owner). CPOs onboard
through the claim-code route instead; the role-gate tests at the bottom drive
the route's real `user` dependency, which calling the handler directly skips.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from backend.database.models import UserRole
from backend.routers.cpo import cpo_create_gateway
from backend.routers.cpo._gateways import router as gateways_router
from backend.schemas import CpoGatewayCreateRequest


def _user():
    u = MagicMock()
    u.tenant_id = 1
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


@pytest.mark.asyncio
async def test_create_gateway_defaults_vpn_ip_to_gateway_id():
    """No vpn_ip supplied → the persisted Gateway uses the gateway_id, so the
    UNIQUE/NOT NULL column is satisfied without an invented overlay IP."""
    db = _db(_result(None))  # no existing gateway with this id
    res = await cpo_create_gateway(
        CpoGatewayCreateRequest(gateway_id="aabbccddeeff", name="Site A - Bay 1"),
        _user(), db,
    )
    assert res["status"] == "registered"
    assert res["gateway_id"] == "aabbccddeeff"

    # db.add is now called twice: the Gateway, then the audit log row
    # (routers/cpo.py try_record_audit, TD#26) — the Gateway is the first call.
    added = db.add.call_args_list[0][0][0]
    assert added.id == "aabbccddeeff"
    assert added.tenant_id == 1
    assert added.vpn_ip == "aabbccddeeff"  # fell back to the (unique) gateway_id
    # Once for the gateway, once for the (best-effort) audit log write.
    assert db.commit.await_count == 2


@pytest.mark.asyncio
async def test_create_gateway_preserves_explicit_vpn_ip():
    """A caller that still provides vpn_ip (legacy overlay gateway) keeps it."""
    db = _db(_result(None))
    await cpo_create_gateway(
        CpoGatewayCreateRequest(gateway_id="gw-legacy", name="Old", vpn_ip="100.87.0.5"),
        _user(), db,
    )
    # First db.add() call is the Gateway; the second is the audit log row.
    assert db.add.call_args_list[0][0][0].vpn_ip == "100.87.0.5"


@pytest.mark.asyncio
async def test_create_gateway_rejects_duplicate():
    """An existing gateway_id → 400, nothing added."""
    db = _db(_result(MagicMock()))  # lookup finds an existing row
    with pytest.raises(HTTPException) as exc:
        await cpo_create_gateway(
            CpoGatewayCreateRequest(gateway_id="dup", name="Dup"),
            _user(), db,
        )
    assert exc.value.status_code == 400
    db.add.assert_not_called()


# ---------------------------------------------------------------------------
# Role gate (gateway-ID squatting fix, 2026-11-16)
# ---------------------------------------------------------------------------

def _user_dependency(path, method="POST"):
    """The route's own `user` dependency (the require_role checker), so the
    test fails if the route's role list is widened back to cpo."""
    route = next(
        r for r in gateways_router.routes
        if getattr(r, "path", None) == path and method in getattr(r, "methods", set())
    )
    return next(d.call for d in route.dependant.dependencies if d.name == "user")


def _role_user(role):
    u = MagicMock()
    u.role = role
    return u


@pytest.mark.asyncio
async def test_manual_register_refuses_cpo():
    """A CPO can no longer hand-register an arbitrary gateway_id: 403 from the
    role gate, before the handler (and its duplicate-id lookup) ever runs."""
    checker = _user_dependency("/api/cpo/gateways")
    with pytest.raises(HTTPException) as exc:
        await checker(user=_role_user(UserRole.CPO))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_manual_register_allows_admin():
    checker = _user_dependency("/api/cpo/gateways")
    admin = _role_user(UserRole.ADMIN)
    assert await checker(user=admin) is admin


@pytest.mark.asyncio
async def test_claim_route_still_open_to_cpo_and_admin():
    """The claim-code path is the CPO onboarding route and must stay open to
    both roles (drivers still refused)."""
    checker = _user_dependency("/api/cpo/gateways/claim")
    cpo = _role_user(UserRole.CPO)
    admin = _role_user(UserRole.ADMIN)
    assert await checker(user=cpo) is cpo
    assert await checker(user=admin) is admin
    with pytest.raises(HTTPException) as exc:
        await checker(user=_role_user(UserRole.DRIVER))
    assert exc.value.status_code == 403
