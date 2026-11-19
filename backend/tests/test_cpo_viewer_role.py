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


def test_wave3_write_routes_are_in_the_viewer_walk():
    """Integration pin (release/2026-11-17-w3): the write routes added or
    changed by C7 (notification prefs), C5 (charge routines) and C4 (plug
    hours: the CPO plug edit, the limits raise cap and the booking refusal)
    are reached by the route walk, so each answers 403 read_only_demo for a
    viewer. C10 (selftest) and C3 (estimate) add GET routes only."""
    walked = {(m, p) for m, p, _ in _write_routes()}
    expected = {
        ("PUT", "/api/me/notification-prefs"),  # C7
        ("POST", "/api/me/routines"),  # C5
        ("PATCH", "/api/me/routines/{routine_id}"),  # C5
        ("DELETE", "/api/me/routines/{routine_id}"),  # C5
        ("PUT", "/api/cpo/plugs/{plug_id}"),  # C4 hours and access fields
        ("PATCH", "/api/sessions/{session_id}/limits"),  # C4 cap on a raise
        ("POST", "/api/reservations"),  # C4 closed-slot refusal
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


@pytest.mark.parametrize(
    "role,email,note,hint",
    [
        (UserRole.CPO_VIEWER, "o***@example.com", "for b***@example.com", None),
        (UserRole.CPO, "owner@example.com", "for buyer@example.com", "PQRS"),
    ],
)
def test_promo_list_masks_issuer_email_and_note_for_viewer(as_user, role, email, note, hint):
    """Integration (F10 x F12): GET /api/cpo/promos redacts like other reads."""
    now = datetime.now(timezone.utc)
    promo = SimpleNamespace(
        id=1, tenant_id=5, code_hint="PQRS", amount_coins=50, expires_at=None,
        max_redemptions=1, per_user_limit=1, note="for buyer@example.com",
        created_at=now, disabled_at=None,
    )
    client, _ = as_user(_viewer(role=role), rows=[(promo, 0, "owner@example.com")])
    resp = client.get("/api/cpo/promos")
    assert resp.status_code == 200, resp.text
    item = resp.json()["items"][0]
    assert item["created_by_email"] == email
    assert item["note"] == note
    assert item["code_hint"] == hint


@pytest.mark.parametrize(
    "role,shown",
    [
        (UserRole.CPO_VIEWER, "(log text hidden in the read-only demo)"),
        (UserRole.CPO, 'WARN wifi "HomeNet-5G" broker mqtts://8.231.81.12:8883'),
    ],
)
def test_gateway_logs_hide_message_text_from_a_viewer(as_user, role, shown):
    """Firmware WARN lines carry the home SSID and broker URL (not covered by
    mask_pii): a viewer gets a placeholder, id/level/created_at stay."""
    client, db = as_user(_viewer(role=role))
    gateway = MagicMock()
    gateway.scalar_one_or_none.return_value = object()
    count = MagicMock()
    count.scalar.return_value = 1
    log = SimpleNamespace(
        id=9, gateway_id="gw1", level="warning", created_at=datetime.now(timezone.utc),
        message='WARN wifi "HomeNet-5G" broker mqtts://8.231.81.12:8883',
    )
    rows = MagicMock()
    rows.scalars.return_value.all.return_value = [log]
    db.execute = AsyncMock(side_effect=[gateway, count, rows])

    resp = client.get("/api/cpo/gateways/gw1/logs")

    assert resp.status_code == 200, resp.text
    item = resp.json()["items"][0]
    assert item["message"] == shown
    assert (item["id"], item["level"]) == (9, "warning")
    assert item["created_at"]
    if role == UserRole.CPO_VIEWER:
        assert "HomeNet" not in resp.text and "8.231.81.12" not in resp.text


def test_mask_pii_handles_free_text_and_phones():
    from backend.routers.cpo._common import mask_pii

    assert mask_pii("driver=ab@x.co, amount=5") == "driver=a***@x.co, amount=5"
    assert mask_pii("call +91 98765 43210") == "call [phone hidden]"
    assert mask_pii("9876543210") == "[phone hidden]"
    assert mask_pii("2026-11-17T10:00:00 plug 12") == "2026-11-17T10:00:00 plug 12"
    assert mask_pii(None) is None


def test_mask_pii_scrubs_ips_and_code_tails_but_keeps_versions():
    """Audit details carry vpn_ip, promo hints and claim code tails."""
    from backend.routers.cpo._common import mask_pii

    assert mask_pii("name=gw, vpn_ip=10.8.0.12") == "name=gw, vpn_ip=[ip hidden]"
    assert mask_pii("broker mqtts://8.231.81.12:8883") == "broker mqtts://[ip hidden]:8883"
    assert mask_pii("hint=PQRS, redemptions=3") == "hint=[hidden], redemptions=3"
    assert mask_pii("claim_code_tail=***WXYZ") == "claim_code_tail=[hidden]"
    assert mask_pii("OTA to 2.9.0 done") == "OTA to 2.9.0 done"
    assert mask_pii("plug is at 192.168.1.50.") == "plug is at [ip hidden]."
    assert mask_pii("oid 1.2.3.4.5") == "oid 1.2.3.4.5"


def test_mask_pii_scrubs_the_stored_promo_topup_note():
    """/api/wallet/redeem stores the top-up note as "Promo code ...<hint>"."""
    from backend.routers.cpo._common import mask_pii

    assert mask_pii("Promo code ...PQRS") == "Promo code ...[hidden]"
    assert mask_pii("Promo code credit from Acme") == "Promo code credit from Acme"


@pytest.mark.parametrize(
    "role,note,driver",
    [
        (UserRole.CPO_VIEWER, "Promo code ...[hidden]", "d***@example.com"),
        (UserRole.CPO, "Promo code ...PQRS", "driver@example.com"),
    ],
)
def test_topups_list_hides_promo_hint_from_viewer(as_user, role, note, driver):
    """A redeemed promo writes its code hint into the top-up note; the
    viewer must not read it there either (GET /api/cpo/promos hides it)."""
    topup = SimpleNamespace(
        id=3, tenant_id=5, actor_user_id=7, driver_user_id=8, amount_coins=50,
        note="Promo code ...PQRS", created_at=datetime.now(timezone.utc),
    )
    client, _ = as_user(
        _viewer(role=role), rows=[(topup, "driver@example.com", "owner@example.com")]
    )
    resp = client.get("/api/cpo/topups")
    assert resp.status_code == 200, resp.text
    item = resp.json()["items"][0]
    assert item["note"] == note
    assert item["driver_email"] == driver
    if role == UserRole.CPO_VIEWER:
        assert "PQRS" not in resp.text


@pytest.mark.parametrize(
    "role,detail",
    [
        (UserRole.CPO_VIEWER, "unsafe: call me on [phone hidden] or a***@example.com"),
        (UserRole.CPO, "unsafe: call me on 9876543210 or asha@example.com"),
    ],
)
def test_events_feed_masks_problem_report_detail_for_viewer(as_user, role, detail):
    """A driver problem report lands in the event feed as
    "<category>: <description>", free text a driver may fill with contacts."""
    client, db = as_user(_viewer(role=role))
    count = MagicMock()
    count.scalar.return_value = 1
    ev = SimpleNamespace(
        id=11, gateway_id="gw1", plug_id=2, event_type="DRIVER_PROBLEM_REPORT",
        severity="warning", acknowledged=False, created_at=datetime.now(timezone.utc),
        detail="unsafe: call me on 9876543210 or asha@example.com",
    )
    rows = MagicMock()
    rows.scalars.return_value.all.return_value = [ev]
    db.execute = AsyncMock(side_effect=[count, rows])

    resp = client.get("/api/cpo/events")

    assert resp.status_code == 200, resp.text
    item = resp.json()["items"][0]
    assert item["detail"] == detail
    assert (item["id"], item["gateway_id"], item["event_type"]) == (11, "gw1", "DRIVER_PROBLEM_REPORT")


_NOTE = "refunded, ask asha@example.com"
_MASKED_NOTE = "refunded, ask a***@example.com"


@pytest.mark.parametrize("role,note", [(UserRole.CPO_VIEWER, _MASKED_NOTE), (UserRole.CPO, _NOTE)])
def test_disputes_mask_resolution_note_for_viewer(as_user, role, note):
    from backend.database.models import DisputeStatus

    dispute = SimpleNamespace(
        id=1, session_id=2, tenant_id=5, driver_user_id=8, reason="Charged twice for one session",
        status=DisputeStatus.APPROVED, resolution_note=_NOTE, refund_coins=10,
        created_at=None, resolved_at=None, resolved_by_user_id=42,
    )
    client, _ = as_user(_viewer(role=role), rows=[(dispute, "driver@example.com", 10, "Plug A")])
    resp = client.get("/api/cpo/disputes")
    assert resp.status_code == 200, resp.text
    assert resp.json()[0]["resolution_note"] == note


@pytest.mark.parametrize("role,note", [(UserRole.CPO_VIEWER, _MASKED_NOTE), (UserRole.CPO, _NOTE)])
def test_plug_reports_mask_resolution_note_for_viewer(as_user, role, note):
    from backend.database.models import PlugReportStatus

    report = SimpleNamespace(
        id=1, plug_id=2, tenant_id=5, driver_user_id=8, category="unsafe",
        description="Sparks fly whenever the plug is inserted.",
        status=PlugReportStatus.RESOLVED, resolution_note=_NOTE,
        created_at=None, resolved_at=None, resolved_by_user_id=42,
    )
    client, db = as_user(_viewer(role=role))
    db.execute.return_value.scalars.return_value.all.return_value = [report]
    resp = client.get("/api/cpo/plug-reports")
    assert resp.status_code == 200, resp.text
    assert resp.json()[0]["resolution_note"] == note


@pytest.mark.parametrize("role,note", [(UserRole.CPO_VIEWER, _MASKED_NOTE), (UserRole.CPO, _NOTE)])
def test_payouts_mask_note_for_viewer(as_user, role, note):
    from backend.database.models import PayoutStatus

    payout = SimpleNamespace(
        id=1, tenant_id=5, period_start=None, period_end=None, gross_coins=100,
        platform_fee_coins=5, net_coins=95, status=PayoutStatus.PAID,
        requested_by_user_id=42, requested_at=None, paid_at=None, note=_NOTE,
    )
    client, db = as_user(_viewer(role=role))
    db.execute.return_value.scalars.return_value.all.return_value = [payout]
    resp = client.get("/api/cpo/payouts")
    assert resp.status_code == 200, resp.text
    assert resp.json()[0]["note"] == note


@pytest.mark.asyncio
async def test_problem_report_broadcast_carries_no_driver_free_text(monkeypatch):
    """gateway_alarm goes to every connected socket of every tenant, so the
    report's free-text description stays out of it: the live payload carries
    the category only. The stored GatewayEvent keeps the full detail."""
    from backend.database.models import GatewayEvent, PlugReport
    from backend.routers.plugs import report_plug_problem
    from backend.schemas import PlugReportCreateRequest
    from backend.services import socketio_manager

    plug = SimpleNamespace(id=2, gateway_id="gw1", group_id=None)
    gateway = SimpleNamespace(id="gw1", tenant_id=5)
    plug_res, gw_res = MagicMock(), MagicMock()
    plug_res.scalar_one_or_none.return_value = plug
    gw_res.scalar_one_or_none.return_value = gateway
    added = []
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[plug_res, gw_res])
    db.add = added.append
    db.commit = AsyncMock()

    async def _refresh(obj):
        obj.id = 1
        obj.created_at = datetime.now(timezone.utc)

    db.refresh = AsyncMock(side_effect=_refresh)
    emit = AsyncMock()
    monkeypatch.setattr(socketio_manager, "emit_gateway_alarm", emit)
    driver = SimpleNamespace(id=8, email="driver@example.com", tenant_id=None)
    description = "Call me on 9876543210, the cable is cut."

    await report_plug_problem(
        2, PlugReportCreateRequest(category="unsafe", description=description), driver, db,
    )

    payload = emit.await_args.args[0]
    assert payload["detail"] == "unsafe"
    assert payload["event_type"] == "DRIVER_PROBLEM_REPORT"
    assert "9876543210" not in str(payload)
    event = next(o for o in added if isinstance(o, GatewayEvent))
    assert event.detail == f"unsafe: {description}"
    assert any(isinstance(o, PlugReport) for o in added)


def test_mask_name_gives_initials():
    from backend.routers.cpo._common import mask_name

    assert mask_name("Sarthak Jain") == "S. J."
    # A Google signup without a name claim stores the email local part.
    assert mask_name("sarthak.jain") == "S."
    assert mask_name("asha  rao kumar extra") == "A. R. K."
    assert mask_name(None) is None
    assert mask_name("") == ""
    assert mask_name("---") == "?"


@pytest.mark.parametrize(
    "role,name,email",
    [
        (UserRole.CPO_VIEWER, "D. O.", "d***@example.com"),
        (UserRole.CPO, "Driver One", "driver1@example.com"),
    ],
)
def test_group_members_mask_driver_name_for_viewer(as_user, role, name, email):
    client, db = as_user(_viewer(role=role))
    group = MagicMock()
    group.scalar_one_or_none.return_value = object()
    members = MagicMock()
    member = SimpleNamespace(
        user_id=100, joined_at=datetime.now(timezone.utc), price_per_kwh_override=None
    )
    members.all.return_value = [(member, "driver1@example.com", "Driver One")]
    db.execute = AsyncMock(side_effect=[group, members])

    resp = client.get("/api/cpo/groups/1/members")

    assert resp.status_code == 200, resp.text
    assert (resp.json()[0]["full_name"], resp.json()[0]["email"]) == (name, email)


@pytest.mark.parametrize(
    "role,name",
    [(UserRole.CPO_VIEWER, "A. R."), (UserRole.CPO, "Asha Rao")],
)
def test_cpo_reservations_mask_driver_name_for_viewer(as_user, monkeypatch, role, name):
    import backend.routers.cpo._reservations as cpo_res
    from backend.database.models import ReservationStatus

    monkeypatch.setattr(cpo_res, "expire_lapsed_reservations", AsyncMock(return_value=0))
    now = datetime.now(timezone.utc)
    r = SimpleNamespace(
        id=1, plug_id=3, user_id=11, start_at=now, end_at=now + timedelta(hours=1),
        status=ReservationStatus.BOOKED, session_id=None, created_at=now,
    )
    client, _ = as_user(_viewer(role=role), rows=[(r, "Garage plug", "asha@example.com", "Asha Rao")])

    resp = client.get("/api/cpo/reservations")

    assert resp.status_code == 200, resp.text
    assert resp.json()["items"][0]["user_name"] == name


@pytest.mark.parametrize("role,hidden", [(UserRole.CPO_VIEWER, True), (UserRole.CPO, False)])
def test_cpo_plugs_hide_lan_address_and_coordinates_from_viewer(as_user, role, hidden):
    plug = SimpleNamespace(
        id=1, name="Bay 1", gateway_id="gw1", local_ip="192.168.1.10", plug_model="P110",
        status=SimpleNamespace(value="available"), current_power_w=0.0, group_id=5,
        tariff_id=None, latitude=12.97, longitude=77.59, max_current_a=None,
        rated_power_w=None, connector_type=None, queued_charging_enabled=None,
        auto_start_delay_min=None, last_seen_at=None, created_at=None,
    )
    client, _ = as_user(_viewer(role=role), rows=[(plug, "Society A")])

    resp = client.get("/api/cpo/plugs")

    assert resp.status_code == 200, resp.text
    item = resp.json()[0]
    if hidden:
        assert (item["local_ip"], item["latitude"], item["longitude"]) == (None, None, None)
        assert "192.168.1.10" not in resp.text
    else:
        assert (item["local_ip"], item["latitude"]) == ("192.168.1.10", 12.97)
    assert (item["name"], item["gateway_id"]) == ("Bay 1", "gw1")


@pytest.mark.parametrize("role,vpn_ip", [(UserRole.CPO_VIEWER, None), (UserRole.CPO, "10.8.0.12")])
def test_cpo_gateways_hide_vpn_ip_from_viewer(as_user, role, vpn_ip):
    from backend.database.models import GatewayStatus

    gw = SimpleNamespace(
        id="gw1", name="Home", vpn_ip="10.8.0.12", status=GatewayStatus.OFFLINE,
        firmware_version="2.9.0", last_seen_at=None, created_at=None,
    )
    client, db = as_user(_viewer(role=role))
    db.execute.return_value.scalars.return_value.all.return_value = [gw]

    resp = client.get("/api/cpo/gateways")

    assert resp.status_code == 200, resp.text
    assert resp.json()[0]["vpn_ip"] == vpn_ip


# --- No existence oracle on {id} reads ---------------------------------------

def _invoice_get(as_user, monkeypatch, role, session, invoice=None, query=""):
    import backend.routers.sessions as sessions_router

    issue = AsyncMock(return_value=object())
    monkeypatch.setattr(sessions_router, "issue_invoice_for_session", issue)
    monkeypatch.setattr(sessions_router, "invoice_to_dict", lambda inv: {"ok": True})
    lookup = AsyncMock(return_value=invoice)
    monkeypatch.setattr(sessions_router, "get_invoice_by_session", lookup)
    client, db = as_user(_viewer(role=role))
    db.execute.return_value.scalar_one_or_none.return_value = session
    resp = client.get(f"/api/sessions/9/invoice{query}")
    return resp, issue, lookup


def test_viewer_invoice_missing_foreign_and_unissued_answer_identically(as_user, monkeypatch):
    foreign = SimpleNamespace(id=9, user_id=11, tenant_id=6)
    own = SimpleNamespace(id=9, user_id=11, tenant_id=5)
    answers = []
    for session in (None, foreign, own):
        # own has no issued invoice: the lookup answers None.
        resp, issue, _ = _invoice_get(as_user, monkeypatch, UserRole.CPO_VIEWER, session)
        answers.append((resp.status_code, resp.json()))
        issue.assert_not_awaited()
    assert answers[0] == (404, {"detail": "Session not found."})
    assert answers[0] == answers[1] == answers[2]


def test_viewer_gets_an_existing_invoice_of_own_tenant_and_never_issues(as_user, monkeypatch):
    own = SimpleNamespace(id=9, user_id=11, tenant_id=5)
    resp, issue, lookup = _invoice_get(
        as_user, monkeypatch, UserRole.CPO_VIEWER, own, invoice=object()
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True}
    lookup.assert_awaited_once()
    issue.assert_not_awaited()


def test_viewer_foreign_tenant_invoice_is_never_looked_up(as_user, monkeypatch):
    foreign = SimpleNamespace(id=9, user_id=11, tenant_id=6)
    resp, issue, lookup = _invoice_get(
        as_user, monkeypatch, UserRole.CPO_VIEWER, foreign, invoice=object()
    )
    assert resp.status_code == 404
    lookup.assert_not_awaited()
    issue.assert_not_awaited()


def _real_invoice():
    from decimal import Decimal

    from backend.database.models import Invoice

    return Invoice(
        id=3, tenant_id=5, session_id=9, driver_user_id=11, invoice_number="INV-0003",
        seller_legal_name="Volt Co", seller_gstin="29ABCDE1234F1Z5",
        amount_coins=Decimal("17.50"), taxable_value_inr=Decimal("14.83"),
        gst_rate_pct=Decimal("18.00"), gst_amount_inr=Decimal("2.67"),
        total_inr=Decimal("17.50"), energy_kwh=3.5, rate_coins_per_kwh=Decimal("5.00"),
        issued_at=datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc),
    )


def _html_invoice(as_user, monkeypatch, role):
    """GET ?format=html through the REAL render_invoice_html: the DB mock
    answers session, then (for the viewer) the invoice lookup, then the
    driver row the Bill To line is built from."""
    import backend.routers.sessions as sessions_router

    invoice = _real_invoice()
    monkeypatch.setattr(sessions_router, "issue_invoice_for_session", AsyncMock(return_value=invoice))
    monkeypatch.setattr(sessions_router, "get_invoice_by_session", AsyncMock(return_value=invoice))
    client, db = as_user(_viewer(role=role))

    def _row(value):
        r = MagicMock()
        r.scalar_one_or_none.return_value = value
        return r

    driver = SimpleNamespace(full_name="Real Name", email="driver@example.com")
    db.execute = AsyncMock(side_effect=[
        _row(SimpleNamespace(id=9, user_id=11, tenant_id=5)), _row(driver),
    ])
    return client.get("/api/sessions/9/invoice?format=html")


def test_viewer_invoice_html_masks_the_bill_to_name_and_email(as_user, monkeypatch):
    resp = _html_invoice(as_user, monkeypatch, UserRole.CPO_VIEWER)
    assert resp.status_code == 200, resp.text
    assert "INV-0003" in resp.text
    assert "Real Name" not in resp.text and "driver@example.com" not in resp.text
    assert "R. N." in resp.text and "d***@example.com" in resp.text


def test_cpo_invoice_html_still_shows_the_full_bill_to(as_user, monkeypatch):
    resp = _html_invoice(as_user, monkeypatch, UserRole.CPO)
    assert resp.status_code == 200, resp.text
    assert "Real Name (driver@example.com)" in resp.text


def test_cpo_invoice_access_is_unchanged(as_user, monkeypatch):
    resp, issue, _ = _invoice_get(
        as_user, monkeypatch, UserRole.CPO, SimpleNamespace(id=9, user_id=11, tenant_id=6)
    )
    assert resp.status_code == 404
    issue.assert_not_awaited()
    resp, issue, _ = _invoice_get(
        as_user, monkeypatch, UserRole.CPO, SimpleNamespace(id=9, user_id=11, tenant_id=5)
    )
    assert resp.status_code == 200, resp.text
    issue.assert_awaited_once()


def _private_plug_results():
    plug = MagicMock()
    plug.scalar_one_or_none.return_value = SimpleNamespace(id=1, group_id=7, gateway_id="gw1")
    group = MagicMock()
    group.scalar_one_or_none.return_value = SimpleNamespace(id=7, name="Society", is_public=False)
    membership = MagicMock()
    membership.scalar_one_or_none.return_value = None
    return [plug, group, membership]


def test_viewer_private_plug_and_missing_plug_answer_identically(as_user):
    client, db = as_user(_viewer())
    missing = MagicMock()
    missing.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(side_effect=[missing])
    gone = client.get("/api/plugs/1")
    db.execute = AsyncMock(side_effect=_private_plug_results())
    private = client.get("/api/plugs/1")
    assert gone.status_code == private.status_code == 404
    assert gone.json() == private.json()


def test_cpo_private_plug_still_gets_403(as_user):
    client, db = as_user(_viewer(role=UserRole.CPO))
    db.execute = AsyncMock(side_effect=_private_plug_results())
    resp = client.get("/api/plugs/1")
    assert resp.status_code == 403


@pytest.mark.parametrize("role,status", [(UserRole.CPO_VIEWER, 404), (UserRole.CPO, 403)])
def test_plug_schedule_private_plug_answer(as_user, monkeypatch, role, status):
    import backend.routers.reservations as res_router

    monkeypatch.setattr(res_router, "user_can_access_plug", AsyncMock(return_value=False))
    client, db = as_user(_viewer(role=role))
    db.execute.return_value.scalar_one_or_none.return_value = SimpleNamespace(id=1, group_id=7)
    resp = client.get("/api/plugs/1/reservations")
    assert resp.status_code == status
    if role == UserRole.CPO_VIEWER:
        db.execute.return_value.scalar_one_or_none.return_value = None
        gone = client.get("/api/plugs/1/reservations")
        assert (gone.status_code, gone.json()) == (resp.status_code, resp.json())


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


# --- Pending plug photos stay hidden from a viewer --------------------------

def _photo_get(as_user, monkeypatch, role, status):
    import backend.routers.plugs as plugs_router
    from backend.database.models import PlugPhotoStatus

    download = MagicMock(return_value=b"image-bytes")
    monkeypatch.setattr(plugs_router, "download_object", download)
    photo = SimpleNamespace(
        id=4, plug_id=1, tenant_id=5, object_name="plug-1/p.jpg", status=PlugPhotoStatus(status),
    )
    from backend.services.auth import get_current_user_optional

    user = _viewer(role=role)
    client, db = as_user(user)
    # The image route authenticates through the optional dependency.
    monkeypatch.setitem(
        _api()[1].dependency_overrides, get_current_user_optional, lambda: user
    )
    db.execute.return_value.scalar_one_or_none.return_value = photo
    return client.get("/api/plugs/1/photos/4/image"), download


def test_viewer_cannot_load_a_pending_photo_even_in_its_own_tenant(as_user, monkeypatch):
    resp, download = _photo_get(as_user, monkeypatch, UserRole.CPO_VIEWER, "pending")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Photo not found."}
    download.assert_not_called()


def test_viewer_cannot_load_a_rejected_photo(as_user, monkeypatch):
    resp, download = _photo_get(as_user, monkeypatch, UserRole.CPO_VIEWER, "rejected")
    assert resp.status_code == 404
    download.assert_not_called()


def test_cpo_still_loads_a_pending_photo_and_viewer_loads_an_approved_one(as_user, monkeypatch):
    resp, _ = _photo_get(as_user, monkeypatch, UserRole.CPO, "pending")
    assert resp.status_code == 200 and resp.headers["cache-control"] == "private, no-store"
    resp, _ = _photo_get(as_user, monkeypatch, UserRole.CPO_VIEWER, "approved")
    assert resp.status_code == 200 and "public" in resp.headers["cache-control"]
