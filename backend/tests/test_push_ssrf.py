"""
SSRF guard for Web-Push subscription endpoints.

The `endpoint` on a push subscription is a browser-supplied URL that the
backend later POSTs to (pywebpush -> requests) on every notification. Without
validation an authenticated driver could register a loopback/internal target
and turn the notification fan-out into a blind-SSRF primitive.

These tests pin the shared validator's rule set and both enforcement layers:
  (a) the pydantic field validator rejects a bad subscribe request at the edge;
  (b) the send-path guard skips an unsafe *stored* endpoint (a row saved before
      the guard existed) without raising and without POSTing to it.
"""
import pytest
from pydantic import ValidationError

import backend.services.notifications as notif_mod
from backend.schemas import PushSubscribeRequest
from backend.services.notifications import is_safe_push_endpoint

# Real-world public push services must be accepted.
SAFE_ENDPOINTS = [
    "https://fcm.googleapis.com/fcm/send/abcDEF123",
    "https://updates.push.services.mozilla.com/wpush/v2/gAAAA",
    "https://web.push.apple.com/QABc123",
    "https://fcm.googleapis.com:443/fcm/send/xyz",  # explicit standard port ok
    "https://fcm.googleapis.com./fcm/send/xyz",     # trailing-dot FQDN ok
]

# Anything internal / non-https / IP-literal must be rejected.
UNSAFE_ENDPOINTS = [
    "http://fcm.googleapis.com/fcm/send/x",   # not https
    "file:///etc/passwd",                     # not https
    "gopher://fcm.googleapis.com/x",          # not https
    "https://127.0.0.1/x",                    # IPv4 loopback
    "https://127.0.0.1:8000/api/internal",    # loopback + port
    "https://localhost/x",                    # loopback name
    "https://localhost:8000/x",
    "https://169.254.169.254/latest/meta-data/",  # cloud metadata (link-local)
    "https://metadata.google.internal/x",     # metadata by name
    "https://[::1]/x",                         # IPv6 loopback
    "https://[fe80::1]/x",                     # IPv6 link-local
    "https://[fc00::1]/x",                     # IPv6 unique-local (private)
    "https://[::ffff:127.0.0.1]/x",            # IPv4-mapped IPv6 loopback
    "https://10.0.0.5/x",                      # RFC1918 private
    "https://192.168.1.1/x",                   # RFC1918 private
    "https://172.16.0.1/x",                    # RFC1918 private
    "https://0.0.0.0/x",                       # unspecified
    "https://foo.local/x",                     # internal suffix
    "https://printer.internal/x",              # internal suffix
    "https://bareword/x",                      # bare single-label host
    "https:///x",                              # no host
    "not-a-url",                               # unparseable / no scheme
    "",                                        # empty
]


@pytest.mark.parametrize("url", SAFE_ENDPOINTS)
def test_is_safe_push_endpoint_accepts_public_services(url):
    assert is_safe_push_endpoint(url) is True


@pytest.mark.parametrize("url", UNSAFE_ENDPOINTS)
def test_is_safe_push_endpoint_rejects_unsafe(url):
    assert is_safe_push_endpoint(url) is False


def test_is_safe_push_endpoint_non_str_is_rejected():
    assert is_safe_push_endpoint(None) is False  # type: ignore[arg-type]


# --- Layer (a): schema field validator rejects at the edge ----------------

def test_schema_accepts_valid_endpoint():
    req = PushSubscribeRequest(
        endpoint="https://fcm.googleapis.com/fcm/send/abc",
        keys={"p256dh": "k", "auth": "a"},
    )
    assert req.endpoint == "https://fcm.googleapis.com/fcm/send/abc"


@pytest.mark.parametrize("url", [
    "http://fcm.googleapis.com/x",
    "https://127.0.0.1/x",
    "https://localhost/x",
    "https://169.254.169.254/x",
    "https://[::1]/x",
    "https://10.0.0.5/x",
    "https://192.168.1.1/x",
    "https://foo.local/x",
    "https://bareword/x",
])
def test_schema_rejects_unsafe_endpoint(url):
    with pytest.raises(ValidationError):
        PushSubscribeRequest(endpoint=url, keys={"p256dh": "k", "auth": "a"})


# --- Layer (b): send-path guard skips an unsafe stored endpoint -----------

def test_send_path_skips_unsafe_stored_endpoint_without_raising():
    """A row persisted before the guard (unsafe endpoint) must be skipped:
    _send_one_push returns False (not pruned) and never reaches pywebpush."""
    called = {"webpush": False}

    def _boom(*a, **k):  # pragma: no cover - must never run
        called["webpush"] = True
        raise AssertionError("webpush must not be called for an unsafe endpoint")

    # If the guard failed and we fell through, this patch would trip.
    import sys
    import types
    fake_pywebpush = types.ModuleType("pywebpush")
    fake_pywebpush.webpush = _boom
    fake_pywebpush.WebPushException = Exception
    sys.modules["pywebpush"] = fake_pywebpush
    try:
        sub = {"id": 9, "endpoint": "https://127.0.0.1/x", "p256dh": "k", "auth": "a"}
        gone = notif_mod._send_one_push(sub, {"title": "t"})
    finally:
        sys.modules.pop("pywebpush", None)

    assert gone is False          # not pruned, just skipped
    assert called["webpush"] is False


def test_send_path_calls_webpush_for_safe_endpoint():
    """Sanity check the guard doesn't block legitimate sends: a safe endpoint
    reaches pywebpush.webpush."""
    called = {"webpush": False}

    def _ok(*a, **k):
        called["webpush"] = True

    import sys
    import types
    fake_pywebpush = types.ModuleType("pywebpush")
    fake_pywebpush.webpush = _ok
    fake_pywebpush.WebPushException = Exception
    sys.modules["pywebpush"] = fake_pywebpush
    try:
        sub = {
            "id": 9,
            "endpoint": "https://fcm.googleapis.com/fcm/send/abc",
            "p256dh": "k", "auth": "a",
        }
        gone = notif_mod._send_one_push(sub, {"title": "t"})
    finally:
        sys.modules.pop("pywebpush", None)

    assert gone is False
    assert called["webpush"] is True
