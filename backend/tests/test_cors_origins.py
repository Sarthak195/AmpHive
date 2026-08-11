"""
Tests for the shared CORS allowlist (services/socketio_manager.cors_allowed_origins),
used by BOTH the FastAPI app (main.py) and the Socket.io server.

Security intent (FIX 4): a default production deploy must NOT trust localhost
dev origins with credentials — those are opt-in for dev via CORS_EXTRA_ORIGINS.
The real .app domains are always present.
"""
import pytest

from backend.services.socketio_manager import cors_allowed_origins

_LOCALHOST = ("http://localhost:5173", "http://localhost:3000")
_REAL = ("https://amphive.app", "https://cpo.amphive.app")


def test_default_allowlist_excludes_localhost(monkeypatch):
    """No CORS_EXTRA_ORIGINS (the prod default) → localhost is absent, so a
    plain prod deploy never echoes + credentials a localhost origin."""
    monkeypatch.delenv("CORS_EXTRA_ORIGINS", raising=False)
    origins = cors_allowed_origins()
    for host in _LOCALHOST:
        assert host not in origins
    for host in _REAL:
        assert host in origins  # real domains always present


def test_empty_extra_origins_is_treated_as_none(monkeypatch):
    """An explicitly-empty (or whitespace/comma-only) value must not sneak an
    empty-string origin into the list."""
    monkeypatch.setenv("CORS_EXTRA_ORIGINS", "  , ,")
    origins = cors_allowed_origins()
    assert "" not in origins
    assert list(origins) == list(_REAL)


def test_localhost_opt_in_via_env(monkeypatch):
    """Dev re-enables localhost by listing it in CORS_EXTRA_ORIGINS."""
    monkeypatch.setenv("CORS_EXTRA_ORIGINS", "http://localhost:5173, http://localhost:3000")
    origins = cors_allowed_origins()
    for host in _LOCALHOST + _REAL:
        assert host in origins


def test_extra_origins_are_deduped_and_order_preserving(monkeypatch):
    """A value already in the prod list can't be double-listed; new ones append
    in order."""
    monkeypatch.setenv(
        "CORS_EXTRA_ORIGINS", "https://amphive.app, https://staging.amphive.app"
    )
    origins = cors_allowed_origins()
    assert origins.count("https://amphive.app") == 1
    assert origins[-1] == "https://staging.amphive.app"


def test_main_app_uses_the_shared_helper(monkeypatch):
    """The FastAPI CORS middleware and the Socket.io server must draw from the
    same helper, so the two allowlists can't drift (localhost off in prod for
    both)."""
    monkeypatch.delenv("CORS_EXTRA_ORIGINS", raising=False)
    from fastapi.middleware.cors import CORSMiddleware

    from backend.main import app

    fastapi_app = app.other_asgi_app
    cors_mw = next(
        m for m in fastapi_app.user_middleware if m.cls is CORSMiddleware
    )
    kwargs = getattr(cors_mw, "kwargs", None) or getattr(cors_mw, "options", {})
    allow_origins = kwargs["allow_origins"]
    for host in _LOCALHOST:
        assert host not in allow_origins
    for host in _REAL:
        assert host in allow_origins


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
