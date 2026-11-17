"""
Per-device logout: the `sid` claim and the revoked_sessions table.

Every sign-in mints a random session id (`sid`) that /me renewals carry
unchanged. POST /api/auth/logout revokes only the caller's sid; every
token-accepting path (get_current_user, get_current_user_optional, socket.io
connect and its re-auth) then rejects that session exactly like a
token_version mismatch. A sid-less token (minted before this feature) falls
back to the global token_version bump, and POST /api/auth/logout-everywhere
keeps that bump on purpose.

The first part is DB-free (mocked AsyncSession, statements compiled against
the postgresql dialect). The DB-gated part at the bottom needs
TEST_DATABASE_URL (CI service) and proves the real table behaviour.
"""
import asyncio
import importlib.util
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
import pytest_asyncio
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.dialects import postgresql

import backend.services.auth as auth_service
from backend.routers.auth import logout, logout_everywhere
from backend.services.auth import (
    JWT_ALGORITHM,
    JWT_SECRET_KEY,
    REVOKED_DETAIL,
    create_access_token,
    get_current_user,
    get_current_user_optional,
    is_sid_revoked,
    new_session_id,
    renewed_access_token,
    revoke_session,
    revoked_session_expiry,
    token_sid,
)
from backend.services.socketio_manager import _socket_user_still_valid, connect

SID = "s" * 22


def _user(user_id=1, token_version=0):
    u = MagicMock()
    u.id = user_id
    u.email = "driver@amphive.test"
    u.token_version = token_version
    u.is_disabled = False  # a bare MagicMock attr is truthy: spurious 403
    u.role.value = "driver"
    return u


def _db(user, revoked=None):
    """Mocked session: execute() loads `user`; scalar() answers the
    revoked_sessions probe (None leaves the AsyncMock default, a MagicMock)."""
    result = MagicMock()
    result.scalar_one_or_none.return_value = user
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    if revoked is not None:
        db.scalar = AsyncMock(return_value=revoked)
    return db


def _creds(token):
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def _decode(token):
    return jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])


def _pg(stmt):
    return str(stmt.compile(dialect=postgresql.dialect()))


# ===========================================================================
# The sid claim
# ===========================================================================

def test_sid_is_embedded_only_when_passed():
    assert _decode(create_access_token(1, "driver", "d@x.test", 0, sid=SID))["sid"] == SID
    assert "sid" not in _decode(create_access_token(1, "driver", "d@x.test", 0))


def test_new_session_ids_are_random_and_url_safe():
    a, b = new_session_id(), new_session_id()
    assert a != b
    assert len(a) >= 22
    assert all(c.isalnum() or c in "-_" for c in a)


@pytest.mark.parametrize("payload", [{}, {"sid": ""}, {"sid": 123}, {"sid": None}])
def test_token_sid_treats_absent_or_malformed_as_none(payload):
    assert token_sid(payload) is None


def test_renewal_keeps_sid_and_auth_time():
    signed_in = int(datetime.now(timezone.utc).timestamp()) - 3600
    payload = _decode(create_access_token(9, "driver", "d@x.test", 2, auth_time=signed_in, sid=SID))
    renewed = _decode(renewed_access_token(payload, _user(user_id=9, token_version=2)))
    assert renewed["sid"] == SID
    assert renewed["auth_time"] == signed_in


def test_renewal_of_a_sid_less_token_never_mints_one():
    """Upgrading a legacy token to a fresh sid would let the old sid-less copy
    outlive a later per-device logout, so renewal must not mint."""
    payload = _decode(create_access_token(9, "driver", "d@x.test", 2))
    renewed = _decode(renewed_access_token(payload, _user(user_id=9, token_version=2)))
    assert "sid" not in renewed


@pytest.mark.asyncio
async def test_login_mints_a_fresh_sid_per_sign_in(monkeypatch):
    """Two sign-ins of one account get two different sessions."""
    import backend.routers.auth as auth_router
    from backend.schemas import LoginRequest
    from backend.services.auth import hash_password

    monkeypatch.setattr(auth_router, "check_and_speed_up_active_session", AsyncMock())
    user = _user(user_id=7, token_version=0)
    user.hashed_password = hash_password("correct-horse")
    user.email_verified = True
    user.auth_provider = "password"
    user.full_name = "Driver"
    user.coin_balance = 0
    sids = []
    for _ in range(2):
        resp = await auth_router.login(
            LoginRequest(email=user.email, password="correct-horse"), _db(user)
        )
        sids.append(_decode(resp.token)["sid"])
    assert sids[0] != sids[1]


# ===========================================================================
# is_sid_revoked
# ===========================================================================

@pytest.mark.asyncio
async def test_sid_less_token_issues_no_lookup():
    db = AsyncMock()
    assert await is_sid_revoked(db, None) is False
    db.scalar.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("found,expected", [(True, True), (False, False), (None, False)])
async def test_is_sid_revoked_reads_the_pk_probe(found, expected):
    db = AsyncMock()
    db.scalar = AsyncMock(return_value=found)
    assert await is_sid_revoked(db, SID) is expected
    sql = _pg(db.scalar.await_args.args[0])
    assert "revoked_sessions.sid" in sql and "EXISTS" in sql


@pytest.mark.asyncio
async def test_a_magicmock_probe_result_is_not_revoked():
    """The mocked-DB suites hand back a MagicMock for db.scalar; the
    `is True` comparison keeps those from reading as revoked."""
    db = AsyncMock()
    db.scalar = AsyncMock(return_value=MagicMock())
    assert await is_sid_revoked(db, SID) is False


# ===========================================================================
# get_current_user / get_current_user_optional
# ===========================================================================

@pytest.mark.asyncio
async def test_revoked_sid_gets_the_same_401_as_a_tv_mismatch():
    revoked_token = create_access_token(1, "driver", "d@x.test", 3, sid=SID)
    with pytest.raises(HTTPException) as sid_exc:
        await get_current_user(_creds(revoked_token), _db(_user(token_version=3), revoked=True))

    stale_token = create_access_token(1, "driver", "d@x.test", 2, sid=SID)
    with pytest.raises(HTTPException) as tv_exc:
        await get_current_user(_creds(stale_token), _db(_user(token_version=3), revoked=False))

    assert sid_exc.value.status_code == tv_exc.value.status_code == 401
    assert sid_exc.value.detail == tv_exc.value.detail == REVOKED_DETAIL
    assert sid_exc.value.headers == tv_exc.value.headers == {"WWW-Authenticate": "Bearer"}


@pytest.mark.asyncio
async def test_live_sid_is_accepted():
    token = create_access_token(1, "driver", "d@x.test", 3, sid=SID)
    user = _user(token_version=3)
    db = _db(user, revoked=False)
    assert await get_current_user(_creds(token), db) is user
    db.scalar.assert_awaited_once()


@pytest.mark.asyncio
async def test_sid_less_token_costs_no_extra_query():
    token = create_access_token(1, "driver", "d@x.test", 3)
    db = _db(_user(token_version=3), revoked=True)
    await get_current_user(_creds(token), db)
    db.scalar.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_tv_short_circuits_before_the_sid_lookup():
    token = create_access_token(1, "driver", "d@x.test", 2, sid=SID)
    db = _db(_user(token_version=3), revoked=False)
    with pytest.raises(HTTPException):
        await get_current_user(_creds(token), db)
    db.scalar.assert_not_awaited()


@pytest.mark.asyncio
async def test_optional_auth_degrades_a_revoked_sid_to_anonymous():
    """Public endpoints never 401 for a bad token: a revoked sid is treated
    like a tv mismatch there, i.e. anonymous (None)."""
    token = create_access_token(1, "driver", "d@x.test", 3, sid=SID)
    user = _user(token_version=3)
    assert await get_current_user_optional(_creds(token), _db(user, revoked=True)) is None
    assert await get_current_user_optional(_creds(token), _db(user, revoked=False)) is user


# ===========================================================================
# revoke_session and the expires_at formula
# ===========================================================================

def test_expiry_is_auth_time_plus_max_session_plus_expiry(monkeypatch):
    monkeypatch.setattr(auth_service, "JWT_MAX_SESSION_DAYS", 90)
    monkeypatch.setattr(auth_service, "JWT_EXPIRY_DAYS", 7)
    now = datetime.now(timezone.utc)
    auth_time = int((now - timedelta(days=1)).timestamp())
    exp = int((now + timedelta(days=7)).timestamp())
    got = revoked_session_expiry({"auth_time": auth_time, "exp": exp})
    # The formula (auth_time + 97 days) is far past both floors here.
    assert got == datetime.fromtimestamp(auth_time, timezone.utc) + timedelta(days=97)


def test_expiry_falls_back_to_iat(monkeypatch):
    monkeypatch.setattr(auth_service, "JWT_MAX_SESSION_DAYS", 90)
    monkeypatch.setattr(auth_service, "JWT_EXPIRY_DAYS", 7)
    iat = int(datetime.now(timezone.utc).timestamp()) - 86400
    got = revoked_session_expiry({"iat": iat})
    assert got == datetime.fromtimestamp(iat, timezone.utc) + timedelta(days=97)


def test_expiry_is_floored_at_the_tokens_own_exp(monkeypatch):
    """A cut to JWT_MAX_SESSION_DAYS after sign-in must not let the row be
    pruned while the presented token is still valid."""
    monkeypatch.setattr(auth_service, "JWT_MAX_SESSION_DAYS", 1)
    monkeypatch.setattr(auth_service, "JWT_EXPIRY_DAYS", 1)
    now = datetime.now(timezone.utc)
    auth_time = int((now - timedelta(days=30)).timestamp())
    exp = int((now + timedelta(days=5)).timestamp())
    got = revoked_session_expiry({"auth_time": auth_time, "exp": exp})
    assert got == datetime.fromtimestamp(exp, timezone.utc)


def test_expiry_without_any_timestamp_is_floored_at_now_plus_expiry(monkeypatch):
    monkeypatch.setattr(auth_service, "JWT_EXPIRY_DAYS", 7)
    got = revoked_session_expiry({})
    expected = datetime.now(timezone.utc) + timedelta(days=7)
    assert abs((got - expected).total_seconds()) < 60


@pytest.mark.asyncio
async def test_revoke_session_needs_a_sid():
    with pytest.raises(ValueError):
        await revoke_session(AsyncMock(), 1, {"sub": "1"})


def _recording_db(cleanup_error=None):
    """A session that records execute/commit/rollback in call order."""
    calls = []
    db = AsyncMock()

    async def execute(stmt):
        sql = _pg(stmt)
        calls.append(("execute", sql))
        if cleanup_error is not None and sql.startswith("DELETE FROM revoked_sessions"):
            raise cleanup_error
        result = MagicMock()
        result.scalar_one.return_value = 5
        return result

    db.execute = AsyncMock(side_effect=execute)
    db.commit = AsyncMock(side_effect=lambda: calls.append(("commit", None)))
    db.rollback = AsyncMock(side_effect=lambda: calls.append(("rollback", None)))
    return db, calls


@pytest.mark.asyncio
async def test_logout_with_a_sid_revokes_only_this_device():
    """INSERT ... ON CONFLICT DO NOTHING, committed BEFORE the opportunistic
    cleanup; no users UPDATE (other devices keep working)."""
    token = create_access_token(1, "driver", "d@x.test", 4, sid=SID)
    db, calls = _recording_db()

    res = await logout(_user(token_version=4), db, _creds(token))

    assert res == {"status": "logged_out", "scope": "device"}
    kinds = [c[0] for c in calls]
    assert kinds == ["execute", "commit", "execute", "commit"]
    insert_sql, cleanup_sql = calls[0][1], calls[2][1]
    assert insert_sql.startswith("INSERT INTO revoked_sessions")
    assert "ON CONFLICT (sid) DO NOTHING" in insert_sql
    assert cleanup_sql.startswith("DELETE FROM revoked_sessions")
    assert "revoked_sessions.expires_at <" in cleanup_sql
    assert not any("UPDATE users" in (c[1] or "") for c in calls)


@pytest.mark.asyncio
async def test_insert_carries_the_sid_user_and_expiry():
    token = create_access_token(1, "driver", "d@x.test", 4, sid=SID)
    db, _ = _recording_db()
    await logout(_user(user_id=1, token_version=4), db, _creds(token))
    params = db.execute.await_args_list[0].args[0].compile(dialect=postgresql.dialect()).params
    assert params["sid"] == SID
    assert params["user_id"] == 1
    assert params["expires_at"] == revoked_session_expiry(_decode(token))


@pytest.mark.asyncio
async def test_a_failed_cleanup_never_costs_the_revocation():
    token = create_access_token(1, "driver", "d@x.test", 4, sid=SID)
    db, calls = _recording_db(cleanup_error=RuntimeError("lock timeout"))

    res = await logout(_user(token_version=4), db, _creds(token))

    assert res["scope"] == "device"
    # The insert's commit landed before the cleanup was even tried.
    assert [c[0] for c in calls] == ["execute", "commit", "execute", "rollback"]


@pytest.mark.asyncio
async def test_logout_with_a_legacy_token_falls_back_to_the_global_bump():
    token = create_access_token(1, "driver", "d@x.test", 4)  # no sid
    db, calls = _recording_db()

    res = await logout(_user(token_version=4), db, _creds(token))

    assert res == {"status": "logged_out", "scope": "all"}
    assert [c[0] for c in calls] == ["execute", "commit"]
    assert calls[0][1].startswith("UPDATE users")
    assert "token_version + " in calls[0][1]
    assert not any("revoked_sessions" in (c[1] or "") for c in calls)


@pytest.mark.asyncio
async def test_logout_everywhere_bumps_token_version_even_with_a_sid():
    db, calls = _recording_db()

    res = await logout_everywhere(_user(token_version=4), db)

    assert res == {"status": "logged_out", "scope": "all"}
    assert [c[0] for c in calls] == ["execute", "execute", "commit"]
    assert calls[0][1].startswith("UPDATE users")
    assert "token_version + " in calls[0][1]
    assert "RETURNING users.token_version" in calls[0][1]
    # [F7] Personal API tokens die in the same commit.
    assert calls[1][1].startswith("UPDATE api_tokens")
    assert "revoked_at IS NULL" in calls[1][1]


def test_logout_everywhere_route_is_registered():
    from backend.routers.auth import router

    routes = {(r.path, tuple(sorted(r.methods))) for r in router.routes if hasattr(r, "methods")}
    assert ("/api/auth/logout", ("POST",)) in routes
    assert ("/api/auth/logout-everywhere", ("POST",)) in routes


# ===========================================================================
# Socket.io: connect and the per-event re-auth
# ===========================================================================

def _socket_db(mock_factory, user, revoked):
    mock_db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = user
    mock_db.execute = AsyncMock(return_value=result)
    mock_db.scalar = AsyncMock(return_value=revoked)
    mock_factory.return_value.__aenter__.return_value = mock_db
    return mock_db


@pytest.mark.asyncio
async def test_socket_connect_rejects_a_revoked_sid():
    with patch("backend.services.socketio_manager.decode_access_token") as mock_decode, \
         patch("backend.services.socketio_manager.async_session_factory") as mock_factory, \
         patch("backend.services.socketio_manager.sio") as mock_sio:
        mock_sio.save_session = AsyncMock()
        mock_sio.enter_room = AsyncMock()
        mock_decode.return_value = {"sub": "42", "tv": 0, "sid": SID}
        _socket_db(mock_factory, MagicMock(token_version=0), revoked=True)

        assert await connect("sock-1", {}, {"token": "jwt"}) is False
        mock_sio.save_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_socket_connect_saves_the_auth_sid_for_live_sessions():
    with patch("backend.services.socketio_manager.decode_access_token") as mock_decode, \
         patch("backend.services.socketio_manager.async_session_factory") as mock_factory, \
         patch("backend.services.socketio_manager.sio") as mock_sio:
        mock_sio.save_session = AsyncMock()
        mock_sio.enter_room = AsyncMock()
        mock_decode.return_value = {"sub": "42", "tv": 0, "sid": SID}
        _socket_db(mock_factory, MagicMock(token_version=0), revoked=False)

        assert await connect("sock-1", {}, {"token": "jwt"}) is True
        mock_sio.save_session.assert_awaited_once_with(
            "sock-1", {"user_id": 42, "tv": 0, "auth_sid": SID}
        )


@pytest.mark.asyncio
async def test_socket_connect_stale_tv_skips_the_sid_lookup():
    with patch("backend.services.socketio_manager.decode_access_token") as mock_decode, \
         patch("backend.services.socketio_manager.async_session_factory") as mock_factory, \
         patch("backend.services.socketio_manager.sio") as mock_sio:
        mock_sio.save_session = AsyncMock()
        mock_decode.return_value = {"sub": "42", "tv": 1, "sid": SID}
        mock_db = _socket_db(mock_factory, MagicMock(token_version=2), revoked=False)

        assert await connect("sock-1", {}, {"token": "jwt"}) is False
        mock_db.scalar.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("revoked,expected", [(True, False), (False, True)])
async def test_socket_reauth_checks_the_saved_auth_sid(revoked, expected):
    with patch("backend.services.socketio_manager.async_session_factory") as mock_factory, \
         patch("backend.services.socketio_manager.sio") as mock_sio:
        mock_sio.get_session = AsyncMock(return_value={"user_id": 42, "tv": 0, "auth_sid": SID})
        _socket_db(mock_factory, MagicMock(token_version=0, is_disabled=False), revoked=revoked)

        assert await _socket_user_still_valid("sock-1") is expected


@pytest.mark.asyncio
async def test_socket_reauth_without_auth_sid_issues_no_lookup():
    with patch("backend.services.socketio_manager.async_session_factory") as mock_factory, \
         patch("backend.services.socketio_manager.sio") as mock_sio:
        mock_sio.get_session = AsyncMock(return_value={"user_id": 42, "tv": 0})
        mock_db = _socket_db(mock_factory, MagicMock(token_version=0, is_disabled=False), revoked=True)

        assert await _socket_user_still_valid("sock-1") is True
        mock_db.scalar.assert_not_awaited()


# ===========================================================================
# Model and migration (structure only; the DB diff runs in test_migrations.py)
# ===========================================================================

_MIGRATION = (
    Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0042_revoked_sessions.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("mig_0042_revoked_sessions", _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_chains_onto_0041_queued_not_before():
    mig = _load_migration()
    assert mig.revision == "0042_revoked_sessions"
    assert mig.down_revision == "0041_queued_not_before"
    assert "PROVISIONAL NUMBERING" not in _MIGRATION.read_text(encoding="utf-8")


def test_migration_matches_the_model():
    from backend.database.models import RevokedSession

    table = RevokedSession.__table__
    assert [c.name for c in table.primary_key.columns] == ["sid"]
    fk = next(iter(table.c.user_id.foreign_keys))
    assert fk.target_fullname == "users.id" and fk.ondelete == "CASCADE"
    index_names = {ix.name for ix in table.indexes}
    assert index_names == {"ix_revoked_sessions_user_id", "ix_revoked_sessions_expires_at"}
    source = _MIGRATION.read_text(encoding="utf-8")
    for name in index_names:
        assert name in source
    assert "sid         TEXT PRIMARY KEY" in source
    assert "ON DELETE CASCADE" in source


def test_downgrade_promotes_live_revocations_before_dropping():
    """A downgrade (or an image rollback) must not un-revoke logged-out
    devices: the live rows become a token_version bump first."""
    mig = _load_migration()
    executed = []
    fake_op = MagicMock()
    fake_op.execute = executed.append
    mig.op = fake_op

    mig.downgrade()

    assert executed[0] == mig.PROMOTE_LIVE_REVOCATIONS_SQL
    assert executed[0].startswith("UPDATE users SET token_version = token_version + 1")
    assert "expires_at > now()" in executed[0]
    assert executed[1] == "DROP TABLE IF EXISTS revoked_sessions"


# ===========================================================================
# DB-gated: the real table (CI with TEST_DATABASE_URL)
# ===========================================================================

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")

requires_db = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="needs TEST_DATABASE_URL pointing at a throwaway Postgres (CI service)",
)

# Same reset as test_account_closure.py: drop_all leaves enum types behind.
ENUM_TYPES = [
    "gateway_status", "plug_status", "session_status", "tx_type", "user_role",
    "payout_status", "dispute_status", "plug_report_status", "reservation_status",
    "queued_charge_status",
]


@pytest_asyncio.fixture()
async def factory():
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from backend.database.models import Base

    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        for enum_name in ENUM_TYPES:
            await conn.execute(text(f"DROP TYPE IF EXISTS {enum_name} CASCADE"))
        await conn.run_sync(Base.metadata.create_all)

    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def _seed_user(factory, email="driver@example.com"):
    from backend.database.models import User, UserRole

    async with factory() as db:
        user = User(
            email=email, hashed_password="x", full_name="Driver",
            role=UserRole.DRIVER, email_verified=True,
        )
        db.add(user)
        await db.commit()
        return user.id


def _signed_in(user_id, tv=0):
    return create_access_token(user_id, "driver", "driver@example.com", tv, sid=new_session_id())


async def _auth(factory, token):
    async with factory() as db:
        return await get_current_user(_creds(token), db)


async def _row_count(factory):
    from sqlalchemy import func, select

    from backend.database.models import RevokedSession

    async with factory() as db:
        return await db.scalar(select(func.count()).select_from(RevokedSession))


@requires_db
@pytest.mark.asyncio
async def test_db_logout_signs_out_only_this_device(factory):
    user_id = await _seed_user(factory)
    phone, laptop = _signed_in(user_id), _signed_in(user_id)

    async with factory() as db:
        user = await get_current_user(_creds(phone), db)
        assert (await logout(user, db, _creds(phone)))["scope"] == "device"

    with pytest.raises(HTTPException) as exc:
        await _auth(factory, phone)
    assert exc.value.status_code == 401 and exc.value.detail == REVOKED_DETAIL
    assert (await _auth(factory, laptop)).id == user_id

    # A renewal of the revoked session carries the same sid, so it is dead too.
    renewed = renewed_access_token(_decode(phone), await _auth(factory, laptop))
    with pytest.raises(HTTPException):
        await _auth(factory, renewed)

    # Public endpoints see the revoked device as anonymous.
    async with factory() as db:
        assert await get_current_user_optional(_creds(phone), db) is None


@requires_db
@pytest.mark.asyncio
async def test_db_concurrent_double_logout_is_one_row_and_no_error(factory):
    user_id = await _seed_user(factory)
    token = _signed_in(user_id)
    payload = _decode(token)

    async def revoke():
        async with factory() as db:
            await revoke_session(db, user_id, payload)

    await asyncio.gather(revoke(), revoke())
    assert await _row_count(factory) == 1


@requires_db
@pytest.mark.asyncio
async def test_db_next_revoke_prunes_only_expired_rows(factory):
    from sqlalchemy import select

    from backend.database.models import RevokedSession

    user_id = await _seed_user(factory)
    now = datetime.now(timezone.utc)
    async with factory() as db:
        db.add(RevokedSession(sid="old", user_id=user_id, expires_at=now - timedelta(days=1)))
        db.add(RevokedSession(sid="live", user_id=user_id, expires_at=now + timedelta(days=1)))
        await db.commit()

    async with factory() as db:
        await revoke_session(db, user_id, _decode(_signed_in(user_id)))

    async with factory() as db:
        sids = set((await db.execute(select(RevokedSession.sid))).scalars())
    assert "old" not in sids and "live" in sids and len(sids) == 2


@requires_db
@pytest.mark.asyncio
async def test_db_logout_everywhere_kills_every_device(factory):
    user_id = await _seed_user(factory)
    phone, laptop = _signed_in(user_id), _signed_in(user_id)

    async with factory() as db:
        user = await get_current_user(_creds(laptop), db)
        assert (await logout_everywhere(user, db))["scope"] == "all"

    for token in (phone, laptop):
        with pytest.raises(HTTPException) as exc:
            await _auth(factory, token)
        assert exc.value.detail == REVOKED_DETAIL
    assert await _row_count(factory) == 0  # the bump alone does it


@requires_db
@pytest.mark.asyncio
async def test_db_rows_cascade_with_the_user(factory):
    from sqlalchemy import delete

    from backend.database.models import User

    user_id = await _seed_user(factory)
    async with factory() as db:
        await revoke_session(db, user_id, _decode(_signed_in(user_id)))
    assert await _row_count(factory) == 1

    async with factory() as db:
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()
    assert await _row_count(factory) == 0
