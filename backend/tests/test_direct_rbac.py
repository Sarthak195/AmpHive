"""
Tests for the RBAC gate on the [Direct Mode] plug-actuating endpoints
(routers/direct.py: direct_plug_on / direct_plug_off).

These endpoints bypass the session/billing flow and energize the plug, so
they must be admin-only. The security-relevant part is the
require_role("admin") dependency the routes are wired to -- calling the route
function directly bypasses FastAPI's DI entirely, so we assert the gate on
the shared dependency the same way test_payouts.py does for mark_paid.
"""
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from backend.database.models import UserRole
from backend.services.rbac import require_role


def _stub_user(role):
    u = MagicMock()
    u.role = role
    return u


@pytest.mark.asyncio
async def test_direct_plug_on_role_gate_rejects_driver():
    checker = require_role("admin")
    driver_user = _stub_user(role=UserRole.DRIVER)
    with pytest.raises(HTTPException) as exc:
        await checker(user=driver_user)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_multi_role_gate_allows_admin_and_cpo_but_rejects_driver():
    """require_role("admin", "cpo") is the shape used to open an endpoint to
    more than one role (unlike direct.py's admin-only gate above) -- both
    permitted roles pass through unchanged, and the excluded role still 403s."""
    checker = require_role("admin", "cpo")

    admin_user = _stub_user(role=UserRole.ADMIN)
    assert await checker(user=admin_user) is admin_user

    cpo_user = _stub_user(role=UserRole.CPO)
    assert await checker(user=cpo_user) is cpo_user

    driver_user = _stub_user(role=UserRole.DRIVER)
    with pytest.raises(HTTPException) as exc:
        await checker(user=driver_user)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_role_gate_rejects_user_with_none_role_value():
    """A user whose role can't be resolved to one of the allowed strings --
    e.g. role.value is None/missing -- is rejected the same way an
    out-of-set role is: 403, not a crash."""
    checker = require_role("admin")
    user = _stub_user(role=MagicMock(value=None))
    with pytest.raises(HTTPException) as exc:
        await checker(user=user)
    assert exc.value.status_code == 403
