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
