"""
Regression tests for the 2026-08-18 production-readiness hardening batch.

Each test pins one behaviour that was a real defect before this batch, so a
future refactor that quietly undoes it fails CI rather than shipping:

- app-level security response headers (previously proxy-only)
- interactive API docs / OpenAPI schema off unless explicitly enabled
- email addresses masked in log output
- caller-supplied `days` windows clamped (they used to 500 on OverflowError)
- oversized free-text fields rejected as 422 (they used to 500 on DataError)
- `local_ip` constrained before it is republished to a gateway
- the Socket.io handshake carries a per-IP cap

No database required — everything here is pure or TestClient-level.
"""
import logging

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend import main as main_module
from backend.logging_config import PiiRedactionFilter, mask_email
from backend.routers.cpo._common import MAX_LOOKBACK_DAYS, clamp_days, clamp_list_window
from backend.schemas import (
    CpoGroupCreateRequest,
    CpoPlugCreateRequest,
    CpoPlugUpdateRequest,
    CpoSetupRequest,
    LoginRequest,
    validate_local_ip,
)


@pytest.fixture
def client():
    # main.app is the socketio ASGIApp wrapper; the FastAPI instance it wraps is
    # what TestClient needs for plain HTTP assertions.
    return TestClient(main_module.app)


# --------------------------------------------------------------------------
# Security response headers
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "header,expected",
    [
        ("X-Content-Type-Options", "nosniff"),
        ("X-Frame-Options", "DENY"),
        ("Referrer-Policy", "strict-origin-when-cross-origin"),
        ("Cache-Control", "no-store"),
    ],
)
def test_api_responses_carry_security_headers(client, header, expected):
    """These used to exist ONLY on the nginx/Caddy edge, so anything reaching
    uvicorn directly (the VM-local :8000 port, a self-hoster's own proxy) got
    none of them."""
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.headers.get(header) == expected


def test_api_csp_locks_down_the_json_surface(client):
    """An API response should never be able to load or frame anything."""
    csp = client.get("/api/health").headers.get("Content-Security-Policy", "")
    assert "default-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp


def test_permissions_policy_denies_device_access(client):
    pp = client.get("/api/health").headers.get("Permissions-Policy", "")
    for feature in ("geolocation=()", "camera=()", "microphone=()", "payment=()"):
        assert feature in pp


def test_interactive_docs_are_off_by_default(client):
    """/docs, /redoc and /openapi.json publish a complete map of every route
    and sit OUTSIDE the blanket /api/ rate limiter's prefix. Off unless
    ENABLE_API_DOCS is set."""
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404


# --------------------------------------------------------------------------
# PII redaction in logs
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,masked",
    [
        ("sarthak@example.com", "s***k@example.com"),
        ("ab@x.com", "a***@x.com"),
        ("a@y.io", "a***@y.io"),
        ("very.long.name+tag@sub.domain.co.uk", "v***g@sub.domain.co.uk"),
        ("not-an-email", "not-an-email"),
    ],
)
def test_mask_email(raw, masked):
    assert mask_email(raw) == masked


def _record(msg, args=(), **extra):
    record = logging.LogRecord(
        name="amphive.api", level=logging.INFO, pathname=__file__, lineno=1,
        msg=msg, args=args, exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_pii_filter_masks_structured_email_extra():
    record = _record("login ok", email="driver1@gmail.com", user_id=4)
    assert PiiRedactionFilter().filter(record) is True
    assert record.email == "d***1@gmail.com"
    assert record.user_id == 4, "the real correlator must survive untouched"


def test_pii_filter_masks_addresses_interpolated_into_the_message():
    record = _record("CPO group created by %s (tenant 3)", ("operator@amphive.app",))
    PiiRedactionFilter().filter(record)
    rendered = record.getMessage()
    assert "operator@amphive.app" not in rendered
    assert "o***r@amphive.app" in rendered


def test_pii_filter_leaves_email_free_messages_lazily_formatted():
    record = _record("session %s finalized", (42,))
    PiiRedactionFilter().filter(record)
    assert record.args == (42,), "no mask applied => stdlib lazy interpolation kept"
    assert record.getMessage() == "session 42 finalized"


# --------------------------------------------------------------------------
# Bounded query windows
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        (30, 30),
        (0, 1),
        (-5, 1),
        (10**9, MAX_LOOKBACK_DAYS),
        (10**12, MAX_LOOKBACK_DAYS),
    ],
)
def test_clamp_days(raw, expected):
    """`datetime.now() - timedelta(days=10**9)` raises OverflowError, which with
    no exception handler registered surfaced as an opaque 500 that any
    authenticated CPO could trigger with one request."""
    assert clamp_days(raw) == expected


def test_clamp_days_output_is_always_usable_as_a_timedelta():
    from datetime import datetime, timedelta, timezone
    for raw in (-1, 0, 1, 10**9, 10**12, 2**63):
        # The point of the clamp: this must never raise.
        datetime.now(timezone.utc) - timedelta(days=clamp_days(raw))


@pytest.mark.parametrize(
    "limit,offset,expected",
    [(500, 0, (500, 0)), (0, 0, (1, 0)), (99999, 0, (1000, 0)), (10, -3, (10, 0))],
)
def test_clamp_list_window(limit, offset, expected):
    assert clamp_list_window(limit, offset) == expected


# --------------------------------------------------------------------------
# Bounded free-text input (422 instead of a DataError 500)
# --------------------------------------------------------------------------

def test_group_name_over_column_width_is_rejected():
    """charger_groups.name is String(100); an oversize value used to reach
    asyncpg and raise StringDataRightTruncation -> unhandled 500."""
    with pytest.raises(ValidationError):
        CpoGroupCreateRequest(name="x" * 101)


def test_tenant_name_over_column_width_is_rejected():
    with pytest.raises(ValidationError):
        CpoSetupRequest(tenant_name="x" * 101)


def test_login_fields_are_memory_bounded_but_not_policied():
    """Login stays deliberately unvalidated for pre-rule accounts (TD#30) — the
    bounds are a ceiling on unauthenticated input, not a password policy."""
    LoginRequest(email="legacy-user@example.com", password="short")  # still fine
    with pytest.raises(ValidationError):
        LoginRequest(email="a" * 400 + "@x.com", password="pw")


# --------------------------------------------------------------------------
# local_ip: it is republished into the retained MQTT roster and used by the
# gateway firmware as a network target, so it must not be free text.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["192.168.1.50", "10.0.0.1", "fe80::1", "plug-a.local"])
def test_local_ip_accepts_addresses_and_plain_hostnames(value):
    assert validate_local_ip(value) == value


@pytest.mark.parametrize(
    "value",
    ["http://1.2.3.4", "1.2.3.4:80", "1.2.3.4/path", "has space", '"quoted"', "a\nb", ""],
)
def test_local_ip_rejects_anything_with_structure(value):
    with pytest.raises(ValueError):
        validate_local_ip(value)


def test_plug_create_and_update_both_validate_local_ip():
    with pytest.raises(ValidationError):
        CpoPlugCreateRequest(gateway_id="gw1", name="Plug", local_ip="http://evil/")
    with pytest.raises(ValidationError):
        CpoPlugUpdateRequest(local_ip="http://evil/")
    # None on update means "leave unchanged" and must stay legal.
    assert CpoPlugUpdateRequest(local_ip=None).local_ip is None


# --------------------------------------------------------------------------
# Socket.io handshake cap
# --------------------------------------------------------------------------

def test_socketio_connect_limiter_exists_and_bounds_a_single_ip():
    """`api_rate_limit_middleware` only engages for /api/ paths, so the
    Socket.io namespace had no cap at all — and each attempt costs a JWT decode
    plus a users SELECT before an invalid token is refused."""
    from backend.services.rate_limit import SlidingWindowRateLimiter

    limiter = SlidingWindowRateLimiter(3, 60, clock=lambda: 1000.0)
    assert [limiter.check("1.2.3.4") for _ in range(3)] == [None, None, None]
    assert limiter.check("1.2.3.4") is not None      # over budget
    assert limiter.check("5.6.7.8") is None          # a different IP is unaffected


def test_socketio_client_ip_ignores_the_spoofable_left_of_the_chain(monkeypatch):
    """X-Forwarded-For is appended to on the RIGHT, so the leftmost entry is
    client-supplied. Counting a fixed number of hops from the right is what
    stops an attacker minting unlimited fresh rate-limit buckets."""
    from backend.services.rate_limit import client_ip_from_forwarded

    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "2")
    # attacker-forged, real client, then the two honest proxy hops
    assert client_ip_from_forwarded("9.9.9.9, 203.0.113.5, 172.18.0.4", "172.18.0.9") == "203.0.113.5"
    # chain shorter than the trusted hop count => nothing forwarded is trusted
    assert client_ip_from_forwarded("9.9.9.9", "172.18.0.9") == "172.18.0.9"
    assert client_ip_from_forwarded("", None) == "unknown"
