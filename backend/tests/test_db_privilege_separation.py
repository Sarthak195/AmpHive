"""DB privilege separation (least-privilege runtime role) — unit tests for the
pure URL-derivation + identifier-validation logic in backend/database/db.py.

The role-provisioning SQL itself is DB-gated (needs a live Postgres) and is
verified at activation time on the server; here we lock down the parts that run
with no database: how RUNTIME_DATABASE_URL is derived from APP_DB_PASSWORD /
APP_DB_USER, that separation is safe-inert by default, and that a hostile role
name is rejected rather than string-built into DDL.

Each case loads db.py as a FRESH throwaway module (not importlib.reload) so it
can never mutate the real backend.database.db that the rest of the suite uses.
"""
import asyncio
import importlib.util
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

_DB_PATH = Path(__file__).resolve().parents[1] / "database" / "db.py"
_OWNER_URL = "postgresql+asyncpg://postgres:sup3r-secret@db:5432/amphive"

_counter = 0


def _load_db(monkeypatch, **env):
    """Execute db.py fresh under a unique module name with a controlled env."""
    global _counter
    _counter += 1
    monkeypatch.setenv("DATABASE_URL", _OWNER_URL)
    for key in ("APP_DB_PASSWORD", "APP_DB_USER"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    spec = importlib.util.spec_from_file_location(f"_db_probe_{_counter}", _DB_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_separation_inert_by_default(monkeypatch):
    """With APP_DB_PASSWORD unset, runtime == owner URL: byte-identical to before."""
    db = _load_db(monkeypatch)
    assert db.SEPARATION_ACTIVE is False
    assert db.RUNTIME_DATABASE_URL == db.DATABASE_URL == _OWNER_URL


def test_password_activates_least_priv_role(monkeypatch):
    db = _load_db(monkeypatch, APP_DB_PASSWORD="runtime-pw-123")
    assert db.SEPARATION_ACTIVE is True
    assert db.RUNTIME_DATABASE_URL != db.DATABASE_URL
    runtime = make_url(db.RUNTIME_DATABASE_URL)
    owner = make_url(_OWNER_URL)
    # Runtime swaps ONLY the credentials; host/port/db/driver are preserved.
    assert runtime.username == "amphive_app"
    assert runtime.password == "runtime-pw-123"
    assert runtime.host == owner.host
    assert runtime.port == owner.port
    assert runtime.database == owner.database
    assert runtime.drivername == owner.drivername
    # The migration/owner URL keeps its superuser role untouched.
    assert make_url(db.DATABASE_URL).username == "postgres"


def test_custom_app_db_user(monkeypatch):
    db = _load_db(monkeypatch, APP_DB_PASSWORD="pw", APP_DB_USER="amphive_rw")
    assert make_url(db.RUNTIME_DATABASE_URL).username == "amphive_rw"


def test_hostile_app_db_user_is_rejected_at_import(monkeypatch):
    """A role name that isn't a plain SQL identifier must fail loudly, never be
    string-built into a runtime URL / CREATE ROLE."""
    with pytest.raises(RuntimeError):
        _load_db(monkeypatch, APP_DB_PASSWORD="pw", APP_DB_USER="app; DROP TABLE users;--")


def test_provision_rejects_bad_identifiers(monkeypatch):
    """_provision_runtime_role validates role + dbname BEFORE touching the
    connection, so a bad identifier raises without executing any SQL."""
    db = _load_db(monkeypatch, APP_DB_PASSWORD="pw")

    class _Boom:
        async def execute(self, *a, **k):  # must never be reached
            raise AssertionError("SQL executed despite an invalid identifier")

    with pytest.raises(RuntimeError):
        asyncio.run(db._provision_runtime_role(_Boom(), 'bad"role', "pw", "amphive"))
    with pytest.raises(RuntimeError):
        asyncio.run(db._provision_runtime_role(_Boom(), "amphive_app", "pw", "bad-db"))
