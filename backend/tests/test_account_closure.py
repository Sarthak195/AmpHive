"""
Self-service data rights: DELETE /api/auth/me and GET /api/auth/me/export.

Pure tests (no DB) cover the guards a mis-click or a replayed request must not
get past. The DB-gated tests — which need CI's postgres:15 service, exported as
TEST_DATABASE_URL — prove the thing that actually matters about closure: that
the PERSONAL rows are gone and the FINANCIAL rows survive against an anonymised
tombstone. A closure that cascaded away a driver's charging sessions would take
the operator's tax records and earnings history with it, which is precisely why
this is an anonymise and not a DELETE.
"""
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")

requires_db = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="needs TEST_DATABASE_URL pointing at a throwaway Postgres (CI service)",
)

ENUM_TYPES = [
    "gateway_status", "plug_status", "session_status", "tx_type", "user_role",
    "payout_status", "dispute_status", "plug_report_status", "reservation_status",
    "queued_charge_status", "plug_photo_status",
]


# ===========================================================================
# Confirmation + re-auth guards (no DB needed)
# ===========================================================================

@pytest.mark.asyncio
async def test_closure_requires_the_exact_confirm_phrase():
    """Closure is irreversible and forfeits credit, so it must be impossible to
    trigger by a mis-click or a replayed request — a typed phrase, not a flag."""
    from backend.routers.auth import DELETE_ACCOUNT_CONFIRM_PHRASE, delete_my_account
    from backend.schemas import DeleteAccountRequest

    user = SimpleNamespace(id=1, auth_provider="password", hashed_password="x", tenant_id=None)
    for wrong in ("", "yes", "delete", "DELETE MY ACCOUNT!"):
        with pytest.raises(HTTPException) as exc:
            await delete_my_account(DeleteAccountRequest(confirm=wrong), user=user, db=None)
        assert exc.value.status_code == 400
        assert DELETE_ACCOUNT_CONFIRM_PHRASE in exc.value.detail


@pytest.mark.asyncio
async def test_closure_requires_the_password_on_a_password_account():
    from backend.routers.auth import DELETE_ACCOUNT_CONFIRM_PHRASE, delete_my_account
    from backend.schemas import DeleteAccountRequest
    from backend.services.auth import hash_password

    user = SimpleNamespace(
        id=1, auth_provider="password", hashed_password=hash_password("correct-horse"),
        tenant_id=None,
    )

    for bad in (None, "wrong-password"):
        with pytest.raises(HTTPException) as exc:
            await delete_my_account(
                DeleteAccountRequest(confirm=DELETE_ACCOUNT_CONFIRM_PHRASE, password=bad),
                user=user, db=None,
            )
        assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_google_only_account_needs_no_password(monkeypatch):
    """A Google-created account's stored hash is a deliberately unusable random
    value, so NO password could ever verify. The bearer token is the proof of
    identity there, exactly as on every other authenticated route — requiring a
    password would make closure impossible for those users."""
    import backend.routers.auth as auth_router
    from backend.routers.auth import DELETE_ACCOUNT_CONFIRM_PHRASE, delete_my_account
    from backend.schemas import DeleteAccountRequest

    called = {}

    async def fake_close(db, user):
        called["user_id"] = user.id
        return {"user_id": user.id, "forfeited_coins": 0.0, "purged": {}, "closed_at": "now"}

    async def fake_audit(*args, **kwargs):
        return None

    monkeypatch.setattr(auth_router, "close_account", fake_close)
    monkeypatch.setattr(auth_router, "try_record_audit", fake_audit)

    user = SimpleNamespace(id=9, auth_provider="google", hashed_password="unusable", tenant_id=None)
    res = await delete_my_account(
        DeleteAccountRequest(confirm=DELETE_ACCOUNT_CONFIRM_PHRASE), user=user, db=None
    )
    assert res["status"] == "closed"
    assert called["user_id"] == 9


def test_tombstone_address_is_undeliverable_and_unique_per_account():
    """`.invalid` is RFC 2606 reserved: the tombstone can never be mailed and
    can never collide with a real signup, while still satisfying the unique
    index on users.email."""
    from backend.services.account_closure import tombstone_email

    assert tombstone_email(7).endswith("@deleted.amphive.invalid")
    assert tombstone_email(7) != tombstone_email(8)


# ===========================================================================
# DB-gated: what survives closure and what does not
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


async def _seed_driver_with_history(factory):
    """A driver with credit, one completed billed session, a ledger row, an
    invoice, a notification, a favourite and a push subscription."""
    from backend.database.models import (
        ChargingSession,
        Gateway,
        GatewayStatus,
        Invoice,
        LedgerTransaction,
        Notification,
        Plug,
        PlugStatus,
        PushSubscription,
        SessionStatus,
        Tenant,
        TransactionType,
        User,
        UserFavorite,
        UserRole,
    )

    async with factory() as db:
        tenant = Tenant(name="Volt Co")
        db.add(tenant)
        await db.flush()

        driver = User(
            email="driver@example.com", hashed_password="x", full_name="Real Name",
            role=UserRole.DRIVER, coin_balance=Decimal("42.50"), email_verified=True,
        )
        db.add(driver)
        await db.flush()

        gw = Gateway(id="gw-1", tenant_id=tenant.id, name="GW", vpn_ip="gw-1",
                     status=GatewayStatus.ONLINE)
        db.add(gw)
        await db.flush()
        plug = Plug(gateway_id=gw.id, name="Plug A", local_ip="192.168.1.5",
                    status=PlugStatus.AVAILABLE)
        db.add(plug)
        await db.flush()

        started = datetime.now(timezone.utc) - timedelta(hours=2)
        session = ChargingSession(
            user_id=driver.id, plug_id=plug.id, tenant_id=tenant.id,
            status=SessionStatus.COMPLETED, started_at=started,
            ended_at=started + timedelta(hours=1), energy_kwh=3.5,
            coins_spent=Decimal("17.50"),
        )
        db.add(session)
        await db.flush()

        db.add(LedgerTransaction(
            user_id=driver.id, session_id=session.id,
            transaction_type=TransactionType.SESSION_DEBIT,
            amount=Decimal("-17.50"), balance_after=Decimal("42.50"),
            description="Charging session",
        ))
        db.add(Invoice(
            tenant_id=tenant.id, session_id=session.id, driver_user_id=driver.id,
            invoice_number="INV-0001", amount_coins=Decimal("17.50"),
            taxable_value_inr=Decimal("14.83"), gst_rate_pct=Decimal("18.00"),
            gst_amount_inr=Decimal("2.67"), total_inr=Decimal("17.50"),
            energy_kwh=3.5, rate_coins_per_kwh=Decimal("5.00"),
        ))
        db.add(Notification(
            user_id=driver.id, type="session_stopped", severity="info",
            title="Session finished", body="Your charge is done.",
        ))
        db.add(UserFavorite(user_id=driver.id, plug_id=plug.id))
        db.add(PushSubscription(
            user_id=driver.id, endpoint="https://fcm.googleapis.com/fcm/send/AAA",
            p256dh="k", auth="a",
        ))
        await db.commit()
        return {"driver_id": driver.id, "session_id": session.id, "plug_id": plug.id}


@requires_db
@pytest.mark.asyncio
async def test_closure_keeps_financial_records_and_erases_personal_ones(factory):
    from sqlalchemy import func, select

    from backend.database.models import (
        ChargingSession,
        Invoice,
        LedgerTransaction,
        Notification,
        PushSubscription,
        TransactionType,
        User,
        UserFavorite,
    )
    from backend.services.account_closure import TOMBSTONE_NAME, close_account

    seeded = await _seed_driver_with_history(factory)

    async with factory() as db:
        user = (await db.execute(select(User).where(User.id == seeded["driver_id"]))).scalar_one()
        old_epoch = user.token_version
        summary = await close_account(db, user)

    assert summary["forfeited_coins"] == 42.50

    async with factory() as db:
        closed = (await db.execute(select(User).where(User.id == seeded["driver_id"]))).scalar_one()

        # Identity is gone; the row survives so the financial FKs still resolve.
        assert closed.email == f"deleted-user-{closed.id}@deleted.amphive.invalid"
        assert closed.full_name == TOMBSTONE_NAME
        assert closed.google_sub is None
        assert closed.is_disabled is True
        assert closed.email_verified is False
        assert closed.deleted_at is not None
        # Every outstanding JWT dies on its next request.
        assert closed.token_version == old_epoch + 1
        # Credit forfeited, and the ledger still reconciles.
        assert closed.coin_balance == Decimal("0.00")

        forfeit = (
            await db.execute(
                select(LedgerTransaction).where(
                    LedgerTransaction.user_id == closed.id,
                    LedgerTransaction.transaction_type == TransactionType.ACCOUNT_CLOSURE,
                )
            )
        ).scalar_one()
        assert forfeit.amount == Decimal("-42.50")
        assert forfeit.balance_after == Decimal("0.00")

        # RETAINED: the operator's tax records and earnings history.
        assert (await db.execute(
            select(func.count(ChargingSession.id)).where(ChargingSession.user_id == closed.id)
        )).scalar_one() == 1
        assert (await db.execute(
            select(func.count(Invoice.id)).where(Invoice.driver_user_id == closed.id)
        )).scalar_one() == 1

        # PURGED: rows that are purely the person.
        for model, column in (
            (Notification, Notification.user_id),
            (UserFavorite, UserFavorite.user_id),
            (PushSubscription, PushSubscription.user_id),
        ):
            assert (await db.execute(
                select(func.count()).select_from(model).where(column == closed.id)
            )).scalar_one() == 0


@requires_db
@pytest.mark.asyncio
async def test_closure_refused_while_a_session_is_active(factory):
    from sqlalchemy import select, update

    from backend.database.models import ChargingSession, SessionStatus, User
    from backend.services.account_closure import AccountClosureRefused, close_account

    seeded = await _seed_driver_with_history(factory)
    async with factory() as db:
        await db.execute(
            update(ChargingSession)
            .where(ChargingSession.id == seeded["session_id"])
            .values(status=SessionStatus.ACTIVE, ended_at=None)
        )
        await db.commit()

    async with factory() as db:
        user = (await db.execute(select(User).where(User.id == seeded["driver_id"]))).scalar_one()
        with pytest.raises(AccountClosureRefused) as exc:
            await close_account(db, user)
        assert "in progress" in exc.value.reason

    # Nothing was scrubbed by the refused attempt.
    async with factory() as db:
        still_there = (await db.execute(select(User).where(User.id == seeded["driver_id"]))).scalar_one()
        assert still_there.email == "driver@example.com"
        assert still_there.deleted_at is None


@requires_db
@pytest.mark.asyncio
async def test_export_returns_the_callers_own_data_and_no_credentials(factory):
    from sqlalchemy import select

    from backend.database.models import User
    from backend.services.data_export import build_export

    seeded = await _seed_driver_with_history(factory)

    async with factory() as db:
        user = (await db.execute(select(User).where(User.id == seeded["driver_id"]))).scalar_one()
        doc = await build_export(db, user)

    assert doc["export_format"] == "amphive.user-data-export.v1"
    assert doc["account"]["email"] == "driver@example.com"
    assert len(doc["charging_sessions"]) == 1
    assert doc["charging_sessions"][0]["operator"] == "Volt Co"
    assert len(doc["wallet_ledger"]) == 1
    assert len(doc["invoices"]) == 1
    assert doc["truncated_collections"] == []

    # A push endpoint is a live credential — only its service host is exported.
    assert doc["push_devices"] == [
        {"push_service": "fcm.googleapis.com", "created_at": doc["push_devices"][0]["created_at"]}
    ]

    # Nothing credential-shaped anywhere in the document.
    import json

    blob = json.dumps(doc)
    for forbidden in ("hashed_password", "token_hash", "p256dh", '"auth"', "fcm/send/AAA"):
        assert forbidden not in blob, f"{forbidden} must never appear in an export"


@requires_db
@pytest.mark.asyncio
async def test_export_never_leaks_another_users_rows(factory):
    """The whole point of a self-service export is that it is self-scoped."""

    from backend.database.models import User, UserRole
    from backend.services.data_export import build_export

    seeded = await _seed_driver_with_history(factory)

    async with factory() as db:
        stranger = User(
            email="stranger@example.com", hashed_password="x", full_name="Stranger",
            role=UserRole.DRIVER, coin_balance=Decimal("0.00"), email_verified=True,
        )
        db.add(stranger)
        await db.commit()
        await db.refresh(stranger)
        doc = await build_export(db, stranger)

    assert doc["account"]["email"] == "stranger@example.com"
    assert doc["charging_sessions"] == []
    assert doc["wallet_ledger"] == []
    assert doc["invoices"] == []
    assert doc["notifications"] == []
    import json

    assert "driver@example.com" not in json.dumps(doc)
    assert str(seeded["session_id"]) not in json.dumps(doc["charging_sessions"])


# ===========================================================================
# Wave 1-3 per-user tables: export + closure coverage
# ===========================================================================

# Every table with a foreign key to users, and what export and closure do with
# it. A new such table fails the drift guard below until it is classified here
# (and handled in services/data_export.py / services/account_closure.py).
# Export: "exported" = build_export reads it; anything else is a reason it is
# not (credential, operator-side record, or already covered by the ledger).
# Closure: "purged" = close_account DELETEs it; "retained" = financial or audit
# record kept against the tombstone; "revoked" = API tokens are revoked.
_USER_TABLES = {
    "api_tokens": ("exported", "revoked"),
    "audit_logs": ("operator-side", "retained"),
    "email_verification_tokens": ("credential", "purged"),
    "password_reset_tokens": ("credential", "purged"),
    "payouts": ("operator-side", "retained"),
    "promo_codes": ("operator-side", "retained"),
    "push_subscriptions": ("exported", "purged"),
    "revoked_sessions": ("credential", "retained"),
    "group_memberships": ("exported", "purged"),
    "offline_topups": ("ledger", "retained"),
    "capacity_requests": ("exported", "purged"),
    "charging_sessions": ("exported", "retained"),
    "plug_photos": ("exported", "purged"),
    "plug_reports": ("exported", "retained"),
    "plug_reviews": ("exported", "purged"),
    "plug_watches": ("exported", "purged"),
    "promo_redemptions": ("exported", "retained"),
    "user_favorites": ("exported", "purged"),
    "invoices": ("exported", "retained"),
    "ledger_transactions": ("exported", "retained"),
    "notifications": ("exported", "purged"),
    "queued_charges": ("exported", "purged"),
    "reservations": ("exported", "retained"),
    "session_disputes": ("exported", "retained"),
    "charge_routines": ("exported", "purged"),
}


def _model_for(table_name):
    from backend.database.models import Base

    for mapper in Base.registry.mappers:
        if mapper.local_table.name == table_name:
            return mapper.class_
    raise AssertionError(table_name)


def test_every_user_keyed_table_is_classified_for_export_and_closure():
    import inspect

    from backend.database.models import Base
    from backend.services import account_closure, data_export

    user_tables = {
        t.name
        for t in Base.metadata.sorted_tables
        if any(fk.column.table.name == "users" for c in t.columns for fk in c.foreign_keys)
    }
    assert user_tables == set(_USER_TABLES), (
        "classify the new per-user table in _USER_TABLES and handle it in "
        "services/data_export.py and services/account_closure.py"
    )

    export_src = inspect.getsource(data_export)
    closure_src = inspect.getsource(account_closure)
    for table, (export, closure) in _USER_TABLES.items():
        name = _model_for(table).__name__
        if export == "exported":
            assert name in export_src, f"{table} missing from the data export"
        if closure == "purged":
            assert name in closure_src, f"{table} missing from account closure"


async def _seed_wave_rows(factory, seeded):
    from datetime import time

    from sqlalchemy import select, update

    from backend.database.models import (
        ApiToken,
        ChargeRoutine,
        Gateway,
        OfflineTopup,
        Plug,
        PlugPhoto,
        PlugReview,
        PromoCode,
        PromoRedemption,
        User,
    )

    uid = seeded["driver_id"]
    async with factory() as db:
        plug = (await db.execute(select(Plug).where(Plug.id == seeded["plug_id"]))).scalar_one()
        tenant_id = (
            await db.execute(select(Gateway.tenant_id).where(Gateway.id == plug.gateway_id))
        ).scalar_one()
        await db.execute(
            update(User).where(User.id == uid).values(
                notification_prefs={"quiet_hours": {"start": "22:00", "end": "07:00"}}
            )
        )
        db.add(ChargeRoutine(
            user_id=uid, plug_id=plug.id, tenant_id=tenant_id, weekdays=0b0100011,
            start_time=time(6, 30), max_kwh=5.0, auto_stop_when_full=True,
        ))
        db.add(PlugReview(plug_id=plug.id, tenant_id=tenant_id, driver_user_id=uid,
                          rating=4, body="Fast and tidy"))
        db.add(PlugPhoto(plug_id=plug.id, tenant_id=tenant_id, uploaded_by_user_id=uid,
                         url="gs://secret-bucket/p1.jpg", object_name="plug-1/p1.jpg"))
        db.add(ApiToken(user_id=uid, name="Home Assistant", token_prefix="amph_ab12",
                        token_hash="f" * 64))
        code = PromoCode(tenant_id=tenant_id, code_hash="e" * 64, code_hint="WXYZ",
                         amount_coins=Decimal("25.00"), max_redemptions=5, per_user_limit=1)
        db.add(code)
        await db.flush()
        topup = OfflineTopup(tenant_id=tenant_id, driver_user_id=uid,
                             amount_coins=Decimal("25.00"), promo_code_id=code.id)
        db.add(topup)
        await db.flush()
        db.add(PromoRedemption(code_id=code.id, user_id=uid, offline_topup_id=topup.id))
        await db.commit()


@requires_db
@pytest.mark.asyncio
async def test_export_lists_routines_reviews_photos_promos_and_tokens_without_secrets(factory):
    import json

    from sqlalchemy import select

    from backend.database.models import User
    from backend.services.data_export import build_export

    seeded = await _seed_driver_with_history(factory)
    await _seed_wave_rows(factory, seeded)

    async with factory() as db:
        user = (await db.execute(select(User).where(User.id == seeded["driver_id"]))).scalar_one()
        doc = await build_export(db, user)

    routine = doc["charge_routines"][0]
    assert routine["charger"] == "Plug A"
    assert routine["weekdays"] == ["monday", "tuesday", "saturday"]
    assert routine["start_time_local"] == "06:30"
    assert routine["max_kwh"] == 5.0
    assert routine["auto_stop_when_full"] is True
    assert routine["enabled"] is True
    assert routine["consecutive_failures"] == 0
    assert doc["charger_reviews"][0]["rating"] == 4
    assert doc["charger_reviews"][0]["body"] == "Fast and tidy"
    assert doc["charger_photos"][0]["status"] == "pending"
    assert doc["promo_redemptions"][0]["amount_coins"] == 25.0
    assert doc["promo_redemptions"][0]["operator"] == "Volt Co"
    assert doc["api_tokens"][0]["name"] == "Home Assistant"
    assert doc["account"]["notification_prefs"]["quiet_hours"]["start"] == "22:00"

    blob = json.dumps(doc)
    for forbidden in ("f" * 64, "e" * 64, "WXYZ", "secret-bucket", "plug-1/p1.jpg"):
        assert forbidden not in blob, f"{forbidden} must never appear in an export"


@requires_db
@pytest.mark.asyncio
async def test_closure_purges_routines_reviews_photos_and_keeps_promo_redemptions(factory, monkeypatch):
    from sqlalchemy import func, select

    import backend.services.account_closure as closure
    from backend.database.models import (
        ChargeRoutine,
        PlugPhoto,
        PlugReview,
        PromoRedemption,
        User,
    )

    deleted = []
    monkeypatch.setattr(closure, "delete_object", lambda name: deleted.append(name))

    seeded = await _seed_driver_with_history(factory)
    await _seed_wave_rows(factory, seeded)
    uid = seeded["driver_id"]

    async with factory() as db:
        user = (await db.execute(select(User).where(User.id == uid))).scalar_one()
        summary = await closure.close_account(db, user)

    assert summary["purged"]["charge_routines"] == 1
    assert summary["purged"]["plug_reviews"] == 1
    assert summary["purged"]["plug_photos"] == 1
    assert deleted == ["plug-1/p1.jpg"]

    async with factory() as db:
        for model, column in (
            (ChargeRoutine, ChargeRoutine.user_id),
            (PlugReview, PlugReview.driver_user_id),
            (PlugPhoto, PlugPhoto.uploaded_by_user_id),
        ):
            assert (await db.execute(
                select(func.count()).select_from(model).where(column == uid)
            )).scalar_one() == 0
        # The redemption is the promo's usage count: it stays against the tombstone.
        assert (await db.execute(
            select(func.count(PromoRedemption.id)).where(PromoRedemption.user_id == uid)
        )).scalar_one() == 1
        closed = (await db.execute(select(User).where(User.id == uid))).scalar_one()
        assert closed.notification_prefs is None


@requires_db
@pytest.mark.asyncio
async def test_closure_survives_a_failing_photo_object_delete(factory, monkeypatch):
    from sqlalchemy import select

    import backend.services.account_closure as closure
    from backend.database.models import User

    def boom(name):
        raise RuntimeError("bucket unreachable")

    monkeypatch.setattr(closure, "delete_object", boom)
    seeded = await _seed_driver_with_history(factory)
    await _seed_wave_rows(factory, seeded)

    async with factory() as db:
        user = (await db.execute(select(User).where(User.id == seeded["driver_id"]))).scalar_one()
        summary = await closure.close_account(db, user)
    assert summary["purged"]["plug_photos"] == 1
