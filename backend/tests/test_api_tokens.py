"""
Personal read-only API tokens and GET /api/me/meter (feature F7).

DB-free part: the token helpers, the read-only-by-construction property (a
token is refused by get_current_user and only the meter route accepts one),
token auth (unknown, revoked, disabled, rate limit, last_used throttle), the
token endpoints and the meter's contract shape and plug scoping.

DB-gated part (CI's TEST_DATABASE_URL): a real token round-trip, revocation,
and that account closure revokes tokens while the export lists them without
their hash.
"""
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from fastapi import HTTPException
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient

import backend.services.api_tokens as api_tokens
from backend.database.models import GatewayStatus, PlugStatus, UserRole
from backend.routers.tokens import _meter_rows, meter_plug
from backend.services.api_tokens import (
    MAX_ACTIVE_TOKENS,
    authenticate_api_token,
    display_prefix,
    generate_token,
    get_meter_user,
    hash_token,
    is_api_token,
)
from backend.services.auth import get_current_user
from backend.services.rate_limit import SlidingWindowRateLimiter
from backend.services.telemetry import TelemetrySnapshot

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
requires_db = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="needs TEST_DATABASE_URL pointing at a throwaway Postgres (CI service)",
)
ENUM_TYPES = [
    "gateway_status", "plug_status", "session_status", "tx_type", "user_role",
    "payout_status", "dispute_status", "plug_report_status", "reservation_status",
    "queued_charge_status",
]


def _creds(token):
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def _sql(stmt) -> str:
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


def _token_row(token_id=3, last_used_at=None):
    return SimpleNamespace(id=token_id, last_used_at=last_used_at)


def _user(role=UserRole.DRIVER, tenant_id=None, disabled=False):
    return SimpleNamespace(id=7, role=role, tenant_id=tenant_id, is_disabled=disabled)


def _auth_db(row):
    """db whose first execute() is the token lookup returning `row`."""
    result = MagicMock()
    result.one_or_none.return_value = row
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


@pytest.fixture(autouse=True)
def _fresh_limiter(monkeypatch):
    monkeypatch.setattr(api_tokens, "api_token_rate_limiter", SlidingWindowRateLimiter(60, 60))


# ===========================================================================
# Helpers
# ===========================================================================

def test_token_format_prefix_and_hash():
    raw = generate_token()
    assert raw.startswith("amph_") and len(raw) >= 5 + 43
    assert generate_token() != raw
    assert display_prefix(raw) == raw[:11]
    digest = hash_token(raw)
    assert len(digest) == 64 and raw not in digest
    assert is_api_token(raw)
    assert not is_api_token("eyJhbGciOiJIUzI1NiJ9.x.y")
    assert not is_api_token(None)


# ===========================================================================
# Read-only by construction
# ===========================================================================

@pytest.mark.asyncio
async def test_get_current_user_refuses_an_api_token():
    """Every route except the meter authenticates here; a token is not a JWT."""
    db = AsyncMock()
    with pytest.raises(HTTPException) as exc:
        await get_current_user(_creds(generate_token()), db)
    assert exc.value.status_code == 401
    db.execute.assert_not_awaited()


def _uses(dependant, dep) -> bool:
    return dependant.call is dep or any(_uses(d, dep) for d in dependant.dependencies)


def _api_routes(routes):
    """Every APIRoute of the app, through FastAPI's included-router wrappers."""
    for r in routes:
        if isinstance(r, APIRoute):
            yield r
        elif hasattr(r, "original_router"):
            yield from _api_routes(r.original_router.routes)


def test_only_the_meter_route_accepts_a_token():
    import backend.main as main

    all_routes = list(_api_routes(main.app.other_asgi_app.routes))
    assert len(all_routes) > 50  # the walk really reached the routers
    routes = [
        (sorted(r.methods), r.path)
        for r in all_routes
        if _uses(r.dependant, get_meter_user)
    ]
    assert routes == [(["GET"], "/api/me/meter")]


# ===========================================================================
# Token auth
# ===========================================================================

@pytest.mark.asyncio
async def test_unknown_or_revoked_token_is_401_and_lookup_is_by_hash():
    raw = generate_token()
    db = _auth_db(None)
    with pytest.raises(HTTPException) as exc:
        await authenticate_api_token(db, raw)
    assert exc.value.status_code == 401
    sql = _sql(db.execute.await_args_list[0].args[0])
    assert hash_token(raw) in sql
    assert raw not in sql
    assert "api_tokens.revoked_at IS NULL" in sql
    assert "api_tokens.scope = 'read'" in sql


@pytest.mark.asyncio
async def test_disabled_owner_is_401():
    db = _auth_db((_token_row(), _user(disabled=True)))
    with pytest.raises(HTTPException) as exc:
        await authenticate_api_token(db, generate_token())
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_valid_token_returns_owner_and_stamps_last_used_once_a_minute():
    user = _user()
    db = _auth_db((_token_row(last_used_at=None), user))
    assert await authenticate_api_token(db, generate_token()) is user
    stamp = _sql(db.execute.await_args_list[1].args[0])
    assert stamp.startswith("UPDATE api_tokens SET last_used_at")
    assert "last_used_at IS NULL OR" in stamp
    db.commit.assert_awaited_once()

    recent = datetime.now(timezone.utc) - timedelta(seconds=10)
    db = _auth_db((_token_row(last_used_at=recent), user))
    assert await authenticate_api_token(db, generate_token()) is user
    assert db.execute.await_count == 1  # lookup only, no stamp
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_per_token_rate_limit_is_429(monkeypatch):
    monkeypatch.setattr(api_tokens, "api_token_rate_limiter", SlidingWindowRateLimiter(2, 60))
    recent = datetime.now(timezone.utc)
    for _ in range(2):
        await authenticate_api_token(_auth_db((_token_row(last_used_at=recent), _user())), "amph_x")
    with pytest.raises(HTTPException) as exc:
        await authenticate_api_token(_auth_db((_token_row(last_used_at=recent), _user())), "amph_x")
    assert exc.value.status_code == 429
    assert "Retry-After" in exc.value.headers


@pytest.mark.asyncio
async def test_meter_user_needs_a_header_and_routes_jwts_to_get_current_user():
    with pytest.raises(HTTPException) as exc:
        await get_meter_user(MagicMock(), None, AsyncMock())
    assert exc.value.status_code == 401

    jwt_user = _user()
    with patch.object(api_tokens, "get_current_user", AsyncMock(return_value=jwt_user)) as gcu:
        assert await get_meter_user(MagicMock(), _creds("a.b.c"), AsyncMock()) is jwt_user
    gcu.assert_awaited_once()


# ===========================================================================
# Token endpoints
# ===========================================================================

def _recording_db(scalar=0, rowcount=1, tokens=()):
    statements = []
    result = MagicMock()
    result.rowcount = rowcount
    result.scalars.return_value.all.return_value = list(tokens)

    async def _execute(stmt, *a, **kw):
        statements.append(stmt)
        return result

    db = MagicMock()
    db.execute = AsyncMock(side_effect=_execute)
    db.scalar = AsyncMock(return_value=scalar)
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.add = MagicMock()

    async def _refresh(obj):
        obj.id = 11
        obj.created_at = datetime(2026, 11, 18, tzinfo=timezone.utc)

    db.refresh = AsyncMock(side_effect=_refresh)
    return db, statements


@pytest.fixture
def client_with():
    import backend.main as main
    from backend.database.db import get_db

    api = main.app.other_asgi_app

    def _make(db, user):
        async def _db():
            yield db

        api.dependency_overrides[get_db] = _db
        api.dependency_overrides[get_current_user] = lambda: user
        api.dependency_overrides[get_meter_user] = lambda: user
        return TestClient(main.app, raise_server_exceptions=False)

    yield _make
    for dep in (get_db, get_current_user, get_meter_user):
        api.dependency_overrides.pop(dep, None)


def test_create_returns_the_token_once_and_stores_only_its_hash(client_with):
    db, _ = _recording_db(scalar=0)
    resp = client_with(db, _user()).post("/api/me/tokens", json={"name": "  Home Assistant "})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert set(body) == {"id", "name", "token", "prefix", "created_at"}
    assert body["name"] == "Home Assistant"
    assert body["token"].startswith("amph_")
    assert body["prefix"] == body["token"][:11]
    stored = db.add.call_args.args[0]
    assert stored.token_hash == hash_token(body["token"])
    assert body["token"] not in vars(stored).values()
    assert stored.scope == "read"


def test_create_is_409_at_the_cap(client_with):
    db, _ = _recording_db(scalar=MAX_ACTIVE_TOKENS)
    resp = client_with(db, _user()).post("/api/me/tokens", json={"name": "evcc"})
    assert resp.status_code == 409
    db.add.assert_not_called()


def test_create_rejects_a_blank_name(client_with):
    db, _ = _recording_db()
    assert client_with(db, _user()).post("/api/me/tokens", json={"name": "   "}).status_code == 422


def test_list_is_metadata_only(client_with):
    tok = SimpleNamespace(
        id=1, name="HA", token_prefix="amph_abcdef", token_hash="f" * 64, scope="read",
        created_at=datetime(2026, 11, 18, tzinfo=timezone.utc), last_used_at=None,
    )
    db, statements = _recording_db(tokens=[tok])
    resp = client_with(db, _user()).get("/api/me/tokens")
    assert resp.status_code == 200
    assert resp.json()["tokens"] == [{
        "id": 1, "name": "HA", "prefix": "amph_abcdef", "scope": "read",
        "created_at": "2026-11-18T00:00:00+00:00", "last_used_at": None,
    }]
    assert "api_tokens.user_id = 7" in _sql(statements[0])


def test_revoke_is_scoped_to_the_caller_and_404_otherwise(client_with):
    db, statements = _recording_db(rowcount=1)
    assert client_with(db, _user()).delete("/api/me/tokens/4").status_code == 204
    sql = _sql(statements[0])
    assert "api_tokens.user_id = 7" in sql and "api_tokens.id = 4" in sql
    db.commit.assert_awaited_once()

    db, _ = _recording_db(rowcount=0)
    assert client_with(db, _user()).delete("/api/me/tokens/4").status_code == 404
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_revoke_all_targets_only_live_tokens_of_the_user():
    db, statements = _recording_db()
    await api_tokens.revoke_user_api_tokens(db, 7)
    sql = _sql(statements[0])
    assert sql.startswith("UPDATE api_tokens SET revoked_at")
    assert "api_tokens.user_id = 7" in sql and "api_tokens.revoked_at IS NULL" in sql


# ===========================================================================
# Meter
# ===========================================================================

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)


def _plug():
    return SimpleNamespace(id=2, name="Bay 1", status=PlugStatus.OCCUPIED)


def _gateway(seen_ago=5):
    return SimpleNamespace(status=GatewayStatus.ONLINE, last_seen_at=NOW - timedelta(seconds=seen_ago))


def _snap(age, status="charging"):
    return TelemetrySnapshot(
        plug_id=2, power_w=7100.5, current_a=31.2, voltage_v=229.8, status=status,
        updated_at=NOW.timestamp() - age,
    )


def test_meter_plug_matches_the_contract():
    session = SimpleNamespace(id=80, started_at=NOW - timedelta(hours=1), energy_kwh=1.838)
    entry = meter_plug(_plug(), _gateway(), session, _snap(3), NOW)
    assert entry == {
        "plug_id": 2, "name": "Bay 1", "status": "occupied", "online": True,
        "power_w": 7100.5, "current_a": 31.2, "voltage_v": 229.8,
        "reading_at": (NOW - timedelta(seconds=3)).isoformat(),
        "session": {"id": 80, "started_at": (NOW - timedelta(hours=1)).isoformat(), "energy_kwh": 1.838},
    }


def test_meter_plug_nulls_missing_placeholder_and_stale_readings():
    empty = meter_plug(_plug(), _gateway(seen_ago=9999), None, None, NOW)
    assert empty["online"] is False and empty["session"] is None
    assert [empty[k] for k in ("power_w", "current_a", "voltage_v", "reading_at")] == [None] * 4

    placeholder = meter_plug(_plug(), _gateway(), None, _snap(0, status="starting"), NOW)
    assert placeholder["power_w"] is None and placeholder["reading_at"] is None

    stale = meter_plug(_plug(), _gateway(), None, _snap(3600), NOW)
    assert stale["power_w"] is None and stale["voltage_v"] is None
    assert stale["reading_at"] == (NOW - timedelta(seconds=3600)).isoformat()


@pytest.mark.asyncio
async def test_operator_meter_is_tenant_scoped():
    result = MagicMock()
    result.all.return_value = []
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    await _meter_rows(db, _user(role=UserRole.CPO_VIEWER, tenant_id=5))
    assert "gateways.tenant_id = 5" in _sql(db.execute.await_args_list[0].args[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("role,tenant", [(UserRole.DRIVER, 5), (UserRole.ADMIN, None)])
async def test_driver_or_tenantless_meter_is_own_active_sessions_only(role, tenant):
    result = MagicMock()
    result.all.return_value = []
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    await _meter_rows(db, _user(role=role, tenant_id=tenant))
    sql = _sql(db.execute.await_args_list[0].args[0])
    assert "charging_sessions.user_id = 7" in sql
    assert "charging_sessions.status = 'active'" in sql
    assert "gateways.tenant_id =" not in sql


def test_meter_endpoint_shape(client_with):
    db, _ = _recording_db()
    result = MagicMock()
    result.all.return_value = []
    db.execute = AsyncMock(return_value=result)
    resp = client_with(db, _user()).get("/api/me/meter")
    assert resp.status_code == 200
    body = resp.json()
    assert body["plugs"] == []
    assert datetime.fromisoformat(body["generated_at"]).tzinfo is not None


def test_meter_endpoint_without_auth_is_401():
    import backend.main as main

    client = TestClient(main.app, raise_server_exceptions=False)
    assert client.get("/api/me/meter").status_code == 401
    assert client.get("/api/me/meter", headers={"Authorization": "Basic eDp5"}).status_code == 401


# ===========================================================================
# DB-gated
# ===========================================================================

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


async def _seed_user_with_token(factory):
    from backend.database.models import ApiToken, User

    raw = generate_token()
    async with factory() as db:
        user = User(email="owner@example.com", hashed_password="x", full_name="Owner",
                    role=UserRole.DRIVER, email_verified=True)
        db.add(user)
        await db.flush()
        db.add(ApiToken(user_id=user.id, name="HA", token_prefix=display_prefix(raw),
                        token_hash=hash_token(raw), scope="read"))
        await db.commit()
        return user.id, raw


@requires_db
@pytest.mark.asyncio
async def test_db_token_round_trip_and_revocation(factory):
    from sqlalchemy import select

    from backend.database.models import ApiToken

    user_id, raw = await _seed_user_with_token(factory)
    async with factory() as db:
        assert (await authenticate_api_token(db, raw)).id == user_id
        stamped = (await db.execute(select(ApiToken.last_used_at))).scalar_one()
        assert stamped is not None

    async with factory() as db:
        assert await api_tokens.revoke_user_api_tokens(db, user_id) == 1
        await db.commit()

    async with factory() as db:
        with pytest.raises(HTTPException) as exc:
            await authenticate_api_token(db, raw)
        assert exc.value.status_code == 401


@requires_db
@pytest.mark.asyncio
async def test_db_closure_revokes_tokens_and_export_lists_them_without_hash(factory):
    import json

    from sqlalchemy import select

    from backend.database.models import ApiToken, User
    from backend.services.account_closure import close_account
    from backend.services.data_export import build_export

    user_id, raw = await _seed_user_with_token(factory)
    async with factory() as db:
        user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
        doc = await build_export(db, user)
    assert [t["name"] for t in doc["api_tokens"]] == ["HA"]
    blob = json.dumps(doc)
    assert raw not in blob and hash_token(raw) not in blob and "token_hash" not in blob

    async with factory() as db:
        user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
        await close_account(db, user)

    async with factory() as db:
        assert (await db.execute(select(ApiToken.revoked_at))).scalar_one() is not None
        with pytest.raises(HTTPException):
            await authenticate_api_token(db, raw)

