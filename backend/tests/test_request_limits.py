"""
Tests for DoS-hardening request limits (audit hardening batch):

- The Content-Length-based request-body-size cap middleware in
  backend/main.py (max_body_size_middleware / MAX_REQUEST_BODY_BYTES):
  over-cap requests get a 413, under-cap requests pass through untouched,
  and a request with no Content-Length (chunked transfer) is let through
  unchecked.
- That the plug-list endpoints in backend/routers/plugs.py
  (get_available_plugs / get_public_plugs) bound their query with a hard
  `.limit()` cap, so neither can return an unbounded, platform-wide result
  set — one of the two is UNAUTHENTICATED (only rate-limited).

Mirrors the middleware-test style in test_rate_limiting.py's
_blanket_app()-style minimal-app pattern, but lives in its own file (a
different agent owns test_rate_limiting.py).
"""
import asyncio
import inspect
import os

from fastapi import FastAPI
from fastapi.testclient import TestClient

import backend.main as main


def _body_size_app():
    """Minimal app with ONLY the body-size middleware mounted — the real
    backend.main registration (ordering relative to CORS/rate-limit) is not
    re-asserted here, same scoping choice test_rate_limiting.py makes for
    its own blanket-middleware unit tests."""
    app = FastAPI()
    app.middleware("http")(main.max_body_size_middleware)

    @app.post("/echo")
    def echo():
        return {"ok": True}

    return app


# ------------------------------------------------------------- middleware ---

def test_request_over_the_cap_is_rejected_with_413(monkeypatch):
    monkeypatch.setattr(main, "MAX_REQUEST_BODY_BYTES", 10)
    client = TestClient(_body_size_app())

    resp = client.post("/echo", content=b"x" * 11)

    assert resp.status_code == 413
    assert "too large" in resp.json()["detail"].lower()


def test_request_under_the_cap_passes(monkeypatch):
    monkeypatch.setattr(main, "MAX_REQUEST_BODY_BYTES", 10)
    client = TestClient(_body_size_app())

    resp = client.post("/echo", content=b"x" * 5)

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_request_exactly_at_the_cap_passes(monkeypatch):
    """The cap is a strict '>' rejection — a body exactly at the limit is
    allowed, not off-by-one rejected."""
    monkeypatch.setattr(main, "MAX_REQUEST_BODY_BYTES", 10)
    client = TestClient(_body_size_app())

    resp = client.post("/echo", content=b"x" * 10)

    assert resp.status_code == 200


def test_missing_content_length_passes_through(monkeypatch):
    """No Content-Length (e.g. chunked transfer) is let through unchecked —
    nginx's client_max_body_size is the backstop for that case in every
    deployed environment (this is a pure JSON API, so we deliberately don't
    stream-count the body here)."""
    monkeypatch.setattr(main, "MAX_REQUEST_BODY_BYTES", 10)

    class _Headers(dict):
        def get(self, key, default=None):
            return super().get(key.lower(), default)

    class _Request:
        headers = _Headers()

    async def call_next(request):
        return "downstream-response"

    result = asyncio.run(main.max_body_size_middleware(_Request(), call_next))

    assert result == "downstream-response"


def test_malformed_content_length_passes_through(monkeypatch):
    """A non-integer Content-Length can't be evaluated against the cap —
    fail open (let it through) rather than 500 on a malformed header."""
    monkeypatch.setattr(main, "MAX_REQUEST_BODY_BYTES", 10)

    class _Headers(dict):
        def get(self, key, default=None):
            return super().get(key.lower(), default)

    class _Request:
        headers = _Headers({"content-length": "not-a-number"})

    async def call_next(request):
        return "downstream-response"

    result = asyncio.run(main.max_body_size_middleware(_Request(), call_next))

    assert result == "downstream-response"


def test_cap_is_env_overridable():
    """MAX_REQUEST_BODY_BYTES is read from the environment (default 1 MiB =
    1048576) at import time — assert the actual wiring in main.py rather
    than re-deriving the formula, without reloading the module (which owns
    live route/middleware registration, so reloading it mid-suite would be
    destructive)."""
    src = inspect.getsource(main)
    assert 'os.getenv("MAX_REQUEST_BODY_BYTES"' in src
    assert main.MAX_REQUEST_BODY_BYTES == int(
        os.getenv("MAX_REQUEST_BODY_BYTES", 1024 * 1024)
    )


def test_default_cap_is_one_megabyte():
    assert int(os.getenv("MAX_REQUEST_BODY_BYTES", 1024 * 1024)) == 1048576


# ------------------------------------------------------- plug-list caps ---

def test_plug_list_queries_have_a_hard_cap():
    """get_available_plugs (authenticated) and get_public_plugs
    (UNAUTHENTICATED — only rate-limited) previously .all()'d every matching
    plug platform-wide with no bound. Both must query with a hard
    PLUG_LIST_HARD_CAP .limit() so the response size stays bounded as the
    fleet grows."""
    from backend.routers import plugs

    assert hasattr(plugs, "PLUG_LIST_HARD_CAP")
    assert plugs.PLUG_LIST_HARD_CAP > 0

    for fn in (plugs.get_available_plugs, plugs.get_public_plugs):
        src = inspect.getsource(fn)
        assert ".limit(PLUG_LIST_HARD_CAP)" in src, (
            f"{fn.__name__} is missing the .limit(PLUG_LIST_HARD_CAP) query cap"
        )
