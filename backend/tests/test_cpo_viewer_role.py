"""
F10: read-only CPO viewer (UserRole.CPO_VIEWER).

The guard lives in the single auth path (services/auth.get_current_user):
a cpo_viewer gets 403 "read_only_demo" on every request other than
GET/HEAD/OPTIONS, except POST /api/auth/logout. The generated test below walks
EVERY route the app serves, so a new write route is covered automatically,
and a new unauthenticated write route must be added to PUBLIC_WRITES on
purpose.

DB-free: the real get_current_user runs against a mocked session that
returns the viewer for the users lookup; tokens carry no sid, so the
revoked-sessions probe never queries.
"""
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from backend.database.models import SessionStatus, UserRole

SAFE = {"GET", "HEAD", "OPTIONS"}

# Write routes that take no bearer token at all (sign-in, sign-up, password
# reset, the payment webhook): a viewer token is irrelevant to them.
PUBLIC_WRITES = {
    ("POST", "/api/auth/register"),
    ("POST", "/api/auth/verify-email"),
    ("POST", "/api/auth/resend-verification"),
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/google/exchange"),
    ("POST", "/api/auth/forgot-password"),
    ("POST", "/api/auth/reset-password"),
    ("POST", "/api/payments/webhook"),
}
VIEWER_EXEMPT = {("POST", "/api/auth/logout")}


def _viewer(role=UserRole.CPO_VIEWER, tenant_id=5):
    return SimpleNamespace(
        id=42, email="viewer@amphive.test", full_name="Demo Viewer", role=role,
        tenant_id=tenant_id, token_version=0, is_disabled=False, is_active=True,
        deleted_at=None, coin_balance=0, email_verified=True,
    )


def _api():
    import backend.main as main

    return main, main.app.other_asgi_app  # main.app is the socket.io wrapper


def _walk(routes):
    """Every APIRoute, including those behind (nested) include_router."""
    for r in routes:
        if isinstance(r, APIRoute):
            yield r
        elif hasattr(r, "original_router"):
            yield from _walk(r.original_router.routes)


def _uses(dependant, fn) -> bool:
    return dependant.call is fn or any(_uses(d, fn) for d in dependant.dependencies)


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "1", path)


def _write_routes():
    _, api = _api()
    out = []
    for r in _walk(api.routes):
        for m in sorted(r.methods - SAFE):
            out.append((m, r.path, r))
    return out


@pytest.fixture
def viewer_client(monkeypatch):
    """TestClient whose bearer token resolves (through the REAL
    get_current_user) to the given user via a mocked DB session."""
    from backend.database.db import get_db
    from backend.services import rate_limit
    from backend.services.auth import create_access_token

    monkeypatch.setattr(rate_limit, "api_rate_limiter", None)
    main, api = _api()

    def _make(user):
        result = MagicMock()
        result.scalar_one_or_none.return_value = user
        db = MagicMock()
        db.execute = AsyncMock(return_value=result)
        db.commit = AsyncMock()
        db.rollback = AsyncMock()

        async def _db():
            yield db

        api.dependency_overrides[get_db] = _db
        token = create_access_token(user.id, user.role.value, user.email, 0)
        client = TestClient(main.app, raise_server_exceptions=False)
        client.headers["Authorization"] = f"Bearer {token}"
        return client, db

    yield _make
    api.dependency_overrides.pop(get_db, None)


def test_every_write_route_is_authenticated_or_deliberately_public():
    """A new write route that skips get_current_user must be listed here on
    purpose, or the viewer guard would not cover it."""
    from backend.services.auth import get_current_user, get_current_user_optional

    routes = _write_routes()
    assert len(routes) > 50, "route walk found too few routes"
    unguarded = {
        (m, p) for m, p, r in routes if not _uses(r.dependant, get_current_user)
    }
    assert unguarded == PUBLIC_WRITES
    # Optional auth never raises, so it must not back any write route.
    assert not [
        (m, p) for m, p, r in routes if _uses(r.dependant, get_current_user_optional)
    ]


@pytest.mark.parametrize(
    "method,path",
    [(m, p) for m, p, _ in _write_routes() if (m, p) not in PUBLIC_WRITES | VIEWER_EXEMPT],
)
def test_viewer_gets_403_on_every_write_route(viewer_client, method, path):
    client, db = viewer_client(_viewer())
    resp = client.request(method, _concrete(path), json={})
    assert resp.status_code == 403, (method, path, resp.status_code, resp.text)
    assert resp.json()["detail"] == "read_only_demo"
    db.commit.assert_not_awaited()


def test_wave2_write_routes_are_in_the_viewer_walk():
    """Integration pin (release/2026-11-17-w2): the write routes added or
    changed by F1 (auto_stop_when_full on start/queue), F7 (personal tokens)
    and F12 (promo codes) must be reached by the route walk above, so each one
    is asserted to return 403 read_only_demo for a viewer."""
    walked = {(m, p) for m, p, _ in _write_routes()}
    expected = {
        ("POST", "/api/sessions/start"),  # F1
        ("POST", "/api/sessions/queue"),  # F1
        ("POST", "/api/me/tokens"),  # F7
        ("DELETE", "/api/me/tokens/{token_id}"),  # F7
        ("POST", "/api/wallet/redeem"),  # F12
        ("POST", "/api/cpo/promos"),  # F12
        ("POST", "/api/cpo/promos/{promo_id}/disable"),  # F12
    }
    assert expected <= walked, expected - walked
    assert not expected & (PUBLIC_WRITES | VIEWER_EXEMPT)


def test_viewer_may_sign_out(viewer_client):
    client, _ = viewer_client(_viewer())
    resp = client.post("/api/auth/logout")
    assert resp.status_code != 403, resp.text


def test_cpo_write_is_not_blocked_by_the_viewer_guard(viewer_client):
    """Control: the same write as a real CPO is not refused with read_only_demo."""
    client, _ = viewer_client(_viewer(role=UserRole.CPO))
    resp = client.post("/api/cpo/payouts/1/cancel")
    assert resp.status_code != 403 or resp.json()["detail"] != "read_only_demo"


@pytest.mark.parametrize(
    "path",
    sorted({
        p for m, p, _ in [
            (m, r.path, r) for r in _walk(_api()[1].routes) for m in r.methods
        ]
        if m == "GET" and p.startswith("/api/admin")
    }),
)
def test_viewer_gets_403_on_admin_reads(viewer_client, path):
    client, _ = viewer_client(_viewer())
    resp = client.get(_concrete(path))
    assert resp.status_code == 403, (path, resp.status_code, resp.text)


def _cpo_get_paths():
    from backend.routers import cpo

    return sorted({
        r.path for r in _walk(cpo.router.routes) if "GET" in r.methods
    })


@pytest.fixture
def as_user():
    """Like test_cpo_tenant_scope: get_current_user overridden, empty DB."""
    import backend.main as main
    from backend.database.db import get_db
    from backend.services.auth import get_current_user

    api = main.app.other_asgi_app

    def _make(user, rows=None, first=None):
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        result.scalar.return_value = 0
        result.scalar_one_or_none.return_value = None
        result.scalar_one.return_value = 0
        result.all.return_value = rows or []
        result.first.return_value = first
        db = MagicMock()
        db.execute = AsyncMock(return_value=result)
        db.commit = AsyncMock()

        async def _db():
            yield db

        api.dependency_overrides[get_db] = _db
        api.dependency_overrides[get_current_user] = lambda: user
        return TestClient(main.app, raise_server_exceptions=False), db

    yield _make
    api.dependency_overrides.pop(get_db, None)
    api.dependency_overrides.pop(get_current_user, None)


@pytest.mark.parametrize("path", _cpo_get_paths())
def test_viewer_reaches_every_cpo_read_route(as_user, path):
    client, _ = as_user(_viewer())
    resp = client.get(_concrete(path))
    assert resp.status_code not in (401, 403), (path, resp.status_code, resp.text)


@pytest.mark.asyncio
async def test_cpo_tenant_gate_refuses_viewer_writes_on_its_own():
    """Second layer: require_cpo_tenant refuses a viewer write even if the
    global guard were bypassed (e.g. get_current_user overridden)."""
    from backend.routers.cpo._common import require_cpo_tenant

    req = SimpleNamespace(method="POST", url=SimpleNamespace(path="/api/cpo/plugs"))
    with pytest.raises(HTTPException) as exc:
        await require_cpo_tenant(req, _viewer())
    assert exc.value.status_code == 403
    get = SimpleNamespace(method="GET", url=SimpleNamespace(path="/api/cpo/plugs"))
    assert await require_cpo_tenant(get, _viewer()) is not None


@pytest.mark.asyncio
async def test_direct_call_without_request_fails_closed_for_viewer():
    from backend.services.auth import create_access_token, get_current_user

    user = _viewer()
    result = MagicMock()
    result.scalar_one_or_none.return_value = user
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    creds = SimpleNamespace(credentials=create_access_token(42, "cpo_viewer", user.email, 0))
    with pytest.raises(HTTPException) as exc:
        await get_current_user(creds, db)
    assert exc.value.status_code == 403


# --- Privacy: emails masked for the viewer -------------------------------

def _session_row():
    now = datetime.now(timezone.utc)
    s = SimpleNamespace(
        id=9, plug_id=3, user_id=11, started_at=now - timedelta(hours=1), ended_at=now,
        energy_kwh=1.5, coins_spent=12.0, status=SessionStatus.COMPLETED,
    )
    return [(s, "Garage plug", "driver@example.com")]


@pytest.mark.parametrize(
    "role,expected",
    [(UserRole.CPO_VIEWER, "d***@example.com"), (UserRole.CPO, "driver@example.com")],
)
def test_sessions_list_masks_driver_email_for_viewer(as_user, role, expected):
    client, _ = as_user(_viewer(role=role), rows=_session_row(), first=(1, 1.5, 12.0))
    resp = client.get("/api/cpo/analytics/sessions")
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"][0]["user_email"] == expected


@pytest.mark.parametrize(
    "role,expected",
    [(UserRole.CPO_VIEWER, "d***@example.com"), (UserRole.CPO, "driver@example.com")],
)
def test_sessions_csv_masks_driver_email_for_viewer(as_user, role, expected):
    client, _ = as_user(_viewer(role=role), rows=_session_row())
    resp = client.get("/api/cpo/analytics/sessions.csv")
    assert resp.status_code == 200, resp.text
    assert expected in resp.text
    if role == UserRole.CPO_VIEWER:
        assert "driver@example.com" not in resp.text


def test_mask_pii_handles_free_text_and_phones():
    from backend.routers.cpo._common import mask_pii

    assert mask_pii("driver=ab@x.co, amount=5") == "driver=a***@x.co, amount=5"
    assert mask_pii("call +91 98765 43210") == "call [phone hidden]"
    assert mask_pii("9876543210") == "[phone hidden]"
    assert mask_pii("2026-11-17T10:00:00 plug 12") == "2026-11-17T10:00:00 plug 12"
    assert mask_pii(None) is None


@pytest.mark.parametrize("role,shown", [(UserRole.CPO_VIEWER, False), (UserRole.CPO, True)])
def test_viewer_never_sees_a_private_group_access_code(as_user, role, shown):
    group = SimpleNamespace(
        id=1, name="Home", is_public=False, access_code="SECRET12",
        max_current_a=None, tariff_id=None, created_at=None, tenant_id=5,
    )
    client, db = as_user(_viewer(role=role))
    db.execute.return_value.scalars.return_value.all.return_value = [group]
    resp = client.get("/api/cpo/groups")
    assert resp.status_code == 200, resp.text
    assert ("SECRET12" in resp.text) is shown


@pytest.mark.parametrize(
    "module,path",
    [
        ("backend.routers.cpo._reservations", "/api/cpo/reservations"),
        ("backend.routers.reservations", "/api/reservations/my"),
    ],
)
@pytest.mark.parametrize("role,persists", [(UserRole.CPO_VIEWER, False), (UserRole.CPO, True)])
def test_viewer_get_never_persists_lazy_reservation_expiry(
    as_user, monkeypatch, module, path, role, persists
):
    import importlib

    monkeypatch.setattr(
        importlib.import_module(module), "expire_lapsed_reservations", AsyncMock(return_value=1)
    )
    client, db = as_user(_viewer(role=role))
    resp = client.get(path)
    assert resp.status_code == 200, resp.text
    assert db.commit.await_count == (1 if persists else 0)


@pytest.mark.parametrize("role,persists", [(UserRole.CPO_VIEWER, False), (UserRole.CPO, True)])
def test_viewer_get_plug_schedule_never_persists_lazy_expiry(as_user, monkeypatch, role, persists):
    import backend.routers.reservations as res_router

    monkeypatch.setattr(res_router, "expire_lapsed_reservations", AsyncMock(return_value=1))
    monkeypatch.setattr(res_router, "user_can_access_plug", AsyncMock(return_value=True))
    client, db = as_user(_viewer(role=role))
    db.execute.return_value.scalar_one_or_none.return_value = SimpleNamespace(id=1, group_id=None)
    resp = client.get("/api/plugs/1/reservations")
    assert resp.status_code == 200, resp.text
    assert db.commit.await_count == (1 if persists else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("role,persists", [(UserRole.CPO_VIEWER, False), (UserRole.CPO, True)])
async def test_plug_reservation_fields_never_persist_for_a_viewer(monkeypatch, role, persists):
    """The helper behind GET /api/plugs/available and GET /api/plugs/{id}."""
    import backend.services.reservations as res_service
    from backend.routers.plugs import _reservation_fields_for_plugs

    monkeypatch.setattr(res_service, "expire_lapsed_reservations", AsyncMock(return_value=1))
    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock())
    db.execute.return_value.scalars.return_value.all.return_value = []
    db.commit = AsyncMock()
    await _reservation_fields_for_plugs(db, [1], _viewer(role=role), datetime.now(timezone.utc))
    assert db.commit.await_count == (1 if persists else 0)


def test_session_detail_admits_a_viewer_of_the_owning_tenant_only():
    from backend.routers.sessions import _assert_session_viewer

    session = SimpleNamespace(user_id=11, tenant_id=5)
    _assert_session_viewer(session, _viewer())  # same tenant: allowed
    with pytest.raises(HTTPException) as exc:
        _assert_session_viewer(session, _viewer(tenant_id=6))
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_socket_subscribe_refused_for_viewer(monkeypatch):
    from backend.services import socketio_manager as sm

    monkeypatch.setattr(sm, "_socket_user_still_valid", AsyncMock(return_value=True))
    monkeypatch.setattr(sm.sio, "get_session", AsyncMock(
        return_value={"user_id": 42, "tv": 0, "read_only": True}
    ))
    emit = AsyncMock()
    monkeypatch.setattr(sm.sio, "emit", emit)
    factory = MagicMock()
    monkeypatch.setattr(sm, "async_session_factory", factory)
    await sm.subscribe_session("sid1", {"session_id": 9})
    emit.assert_awaited_once()
    assert emit.await_args.args[1] == {"detail": "read_only_demo"}
    factory.assert_not_called()


# --- Seed script (imported, never run against a database) -----------------

def _seed_db(tenant, existing):
    results = []
    for value in (tenant, existing):
        r = MagicMock()
        r.scalar_one_or_none.return_value = value
        results.append(r)
    db = MagicMock()
    db.execute = AsyncMock(side_effect=results)
    db.add = MagicMock()
    return db


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [UserRole.DRIVER, UserRole.CPO, UserRole.ADMIN])
async def test_seed_refuses_to_convert_a_real_account(role):
    from backend.seed_cpo_viewer import SeedError, upsert_viewer

    existing = _viewer(role=role)
    db = _seed_db(SimpleNamespace(id=5), existing)
    with pytest.raises(SeedError):
        await upsert_viewer(db, 5, "Viewer@AmpHive.test", "Demo")
    assert existing.role == role
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_seed_creates_a_viewer_and_resets_an_existing_one():
    from backend.seed_cpo_viewer import upsert_viewer

    db = _seed_db(SimpleNamespace(id=5), None)
    pw = await upsert_viewer(db, 5, "Viewer@AmpHive.test", "Demo")
    created = db.add.call_args.args[0]
    assert created.role == UserRole.CPO_VIEWER and created.tenant_id == 5
    assert created.email == "viewer@amphive.test" and len(pw) >= 20

    existing = _viewer()
    existing.hashed_password = "old"
    db = _seed_db(SimpleNamespace(id=5), existing)
    pw2 = await upsert_viewer(db, 5, "viewer@amphive.test", "Demo")
    assert pw2 != pw and existing.hashed_password != "old"
    assert existing.token_version == 1


@pytest.mark.asyncio
async def test_socket_connect_marks_a_viewer_read_only(monkeypatch):
    from backend.services import socketio_manager as sm

    monkeypatch.setattr(sm.socketio_connect_rate_limiter, "check", lambda ip: None)
    monkeypatch.setattr(sm, "decode_access_token", lambda t: {"sub": "42", "tv": 0})
    monkeypatch.setattr(sm, "is_sid_revoked", AsyncMock(return_value=False))
    result = MagicMock()
    result.scalar_one_or_none.return_value = _viewer()
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=db)
    cm.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(sm, "async_session_factory", lambda: cm)
    save = AsyncMock()
    monkeypatch.setattr(sm.sio, "save_session", save)
    monkeypatch.setattr(sm.sio, "enter_room", AsyncMock())
    assert await sm.connect("sid1", {}, {"token": "t"}) is True
    assert save.await_args.args[1]["read_only"] is True
