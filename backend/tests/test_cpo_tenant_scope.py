"""
U4: a tenant-less caller (a platform admin, or a CPO not yet attached to a
tenant) must match nothing on tenant-scoped /api/cpo/* routes.

Root cause: the CPO routes filter with `X.tenant_id == user.tenant_id`, and
SQLAlchemy compiles `column == None` to `column IS NULL`. For a caller whose
tenant_id is None that selects every row with a NULL tenant, i.e. unclaimed
inventory gateways (Gateway.tenant_id is nullable) and the plugs on them, and
platform-level AuditLog rows. The fix is one shared dependency
(routers/cpo/_common.require_cpo_tenant) that refuses a tenant-less caller
before any query runs; platform admins use the /api/admin/* routes instead.

These tests resolve dependencies through the real app (the handlers are not
called directly), with get_current_user and get_db overridden, so no
database is needed.
"""
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from backend.database.models import UserRole

# Routes on the CPO router that deliberately stay reachable without a tenant:
# onboarding (creates the tenant), the tenant profile (looks up Tenant by its
# non-null primary key, so a None tenant already 404s), the global firmware
# catalog, and the admin payout actions (admin cross-tenant by design).
TENANTLESS_ALLOWED = {
    ("POST", "/api/cpo/setup"),
    ("GET", "/api/cpo/profile"),
    ("PUT", "/api/cpo/profile"),
    ("GET", "/api/cpo/firmware-releases"),
    ("POST", "/api/cpo/payouts/{payout_id}/mark_paid"),
    ("POST", "/api/cpo/payouts/{payout_id}/cancel"),
}


def _cpo_routes():
    from backend.routers import cpo

    out = []
    for route in cpo.router.routes:
        if isinstance(route, APIRoute):
            for method in sorted(route.methods):
                out.append((method, route.path, route))
    return out


def _uses_guard(dependant, guard) -> bool:
    if dependant.call is guard:
        return True
    return any(_uses_guard(d, guard) for d in dependant.dependencies)


def test_every_tenant_scoped_cpo_route_requires_a_tenant():
    """Structural: each /api/cpo route is either guarded or explicitly
    allow-listed. Adding a new unguarded CPO route fails here."""
    from backend.routers.cpo._common import require_cpo_tenant

    routes = _cpo_routes()
    assert routes, "no CPO routes found"
    unguarded = [
        (m, p) for m, p, r in routes
        if (m, p) not in TENANTLESS_ALLOWED and not _uses_guard(r.dependant, require_cpo_tenant)
    ]
    assert unguarded == []
    # The allow-list must not go stale either.
    present = {(m, p) for m, p, _ in routes}
    assert TENANTLESS_ALLOWED <= present


def _recording_db():
    statements = []
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    result.scalar.return_value = 0
    result.scalar_one_or_none.return_value = None
    result.scalar_one.return_value = 0
    result.all.return_value = []
    result.first.return_value = None

    async def _execute(stmt, *a, **kw):
        statements.append(stmt)
        return result

    db = MagicMock()
    db.execute = AsyncMock(side_effect=_execute)
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.refresh = AsyncMock()
    db.flush = AsyncMock()
    db.add = MagicMock()
    return db, statements


def _user(role: UserRole, tenant_id):
    return SimpleNamespace(
        id=7, email="u@amphive.test", full_name="U", role=role, tenant_id=tenant_id,
        is_active=True, deleted_at=None,
    )


@pytest.fixture
def client_as():
    import backend.main as main
    from backend.database.db import get_db
    from backend.services.auth import get_current_user

    api = main.app.other_asgi_app  # main.app is the socket.io wrapper
    holder = {}

    def _make(user):
        db, statements = _recording_db()
        holder["statements"] = statements

        async def _db():
            yield db

        api.dependency_overrides[get_db] = _db
        api.dependency_overrides[get_current_user] = lambda: user
        return TestClient(main.app, raise_server_exceptions=False), statements

    yield _make
    api.dependency_overrides.pop(get_db, None)
    api.dependency_overrides.pop(get_current_user, None)


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "1", path)


# One representative per endpoint family named in U4 (gateways, plugs,
# groups, analytics, events/audit, disputes, plug reports, plug photos,
# tariffs, payouts/earnings, invoices, reservations, top-ups).
FAMILY_ROUTES = [
    ("GET", "/api/cpo/gateways"),
    ("POST", "/api/cpo/gateways/{gateway_id}/ota"),
    ("GET", "/api/cpo/gateways/{gateway_id}/logs"),
    ("GET", "/api/cpo/plugs"),
    ("PUT", "/api/cpo/plugs/{plug_id}"),
    ("POST", "/api/cpo/plugs/{plug_id}/maintenance"),
    ("GET", "/api/cpo/groups"),
    ("GET", "/api/cpo/analytics/overview"),
    ("GET", "/api/cpo/analytics/sessions"),
    ("GET", "/api/cpo/events"),
    ("GET", "/api/cpo/audit"),
    ("GET", "/api/cpo/disputes"),
    ("GET", "/api/cpo/plug-reports"),
    ("GET", "/api/cpo/plug-photos"),
    ("GET", "/api/cpo/tariffs"),
    ("PUT", "/api/cpo/plugs/{plug_id}/tariff"),
    ("GET", "/api/cpo/earnings"),
    ("GET", "/api/cpo/invoices"),
    ("GET", "/api/cpo/reservations"),
    ("GET", "/api/cpo/topups"),
]


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.CPO])
@pytest.mark.parametrize("method,path", FAMILY_ROUTES)
def test_tenantless_caller_is_refused_before_any_query(client_as, role, method, path):
    client, statements = client_as(_user(role, None))
    resp = client.request(method, _concrete(path), json={})
    leaked = [str(s) for s in statements]
    assert resp.status_code == 400, (resp.status_code, resp.text, leaked)
    assert resp.json()["detail"] == "You are not associated with a tenant."
    assert statements == [], f"tenant-less caller reached a query: {leaked}"


def test_tenant_user_still_reaches_the_scoped_query(client_as):
    """The guard passes a CPO with a tenant through, and the list query is
    still bound to that tenant id (not IS NULL)."""
    client, statements = client_as(_user(UserRole.CPO, 5))
    resp = client.get("/api/cpo/gateways")
    assert resp.status_code == 200
    sql = str(statements[0].compile(compile_kwargs={"literal_binds": True}))
    assert "gateways.tenant_id = 5" in sql
    assert "IS NULL" not in sql


def test_guarded_routes_never_compile_a_null_tenant_match(client_as):
    """Every guarded route, called by a tenant-less admin, is refused with no
    SQL executed (so no `tenant_id IS NULL` match can ever run)."""
    for method, path, _ in _cpo_routes():
        if (method, path) in TENANTLESS_ALLOWED:
            continue
        client, statements = client_as(_user(UserRole.ADMIN, None))
        resp = client.request(method, _concrete(path), json={})
        assert resp.status_code == 400, (method, path, resp.status_code, resp.text)
        assert statements == [], (method, path, [str(s) for s in statements])
