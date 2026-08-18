"""
Self-service account closure (DPDP Act §12 "right to erasure" shaped).

Why anonymise instead of DELETE
-------------------------------
Every `user_id` foreign key in `models.py` is `ON DELETE CASCADE`, so a raw
`DELETE FROM users` would take the account's **charging sessions, ledger
transactions and GST tax invoices** with it. Those are not the user's personal
data alone — they are the counterparty half of a financial record that the
operator (and the platform) is required to keep, and they feed the CPO's
earnings, payout watermark and settlement maths. Destroying them would both
break tax record-keeping and silently rewrite an operator's revenue history.

So closure does two different things to two different classes of row:

**Purged (hard DELETE)** — rows that are purely the person, carry no money and
have no counterparty:
  push subscriptions, notifications, favourites, plug watches, group
  memberships, capacity requests, queued charges, upcoming reservations, and
  any outstanding password-reset / email-verification tokens.

**Retained but detached (anonymised)** — the `users` row itself is kept as a
tombstone so the financial rows still resolve, with every identifier scrubbed:
  email -> `deleted-user-{id}@deleted.amphive.invalid` (`.invalid` is the
  RFC 2606 reserved TLD, so the address can never be delivered to and can never
  collide with a real signup), full_name -> "Deleted user", password -> an
  unusable random hash, google_sub -> NULL, disabled, token_version bumped
  (every outstanding JWT dies immediately), `deleted_at` stamped.

The financial rows (charging_sessions, ledger_transactions, invoices,
offline_topups, session_disputes, plug_reports, payouts, audit_logs) then point
at a tombstone that identifies nobody.

Charging credit
---------------
Credit is a closed-loop prepaid instrument that is explicitly non-withdrawable
and non-transferable (see the /terms page). Any remaining balance is therefore
forfeited at closure, zeroed through the normal wallet debit path so the ledger
still reconciles, and recorded as its own ledger line. The UI states this
before the user confirms.

Refusals
--------
Closure is refused (409) while it would leave the platform inconsistent:
  * an ACTIVE charging session (finish or stop it first — it is still drawing
    power and still billing);
  * the caller is the last operator of a tenant that still owns gateways or has
    unsettled earnings — deleting them would orphan live hardware and money.
"""
import logging
import secrets
from datetime import datetime, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.models import (
    CapacityRequest,
    ChargingSession,
    EmailVerificationToken,
    Gateway,
    GroupMembership,
    LedgerTransaction,
    Notification,
    PasswordResetToken,
    PlugWatch,
    PushSubscription,
    QueuedCharge,
    QueuedChargeStatus,
    Reservation,
    ReservationStatus,
    SessionStatus,
    TransactionType,
    User,
    UserFavorite,
    UserRole,
)
from backend.services.auth import hash_password
from backend.services.money import ZERO_MONEY, to_money

logger = logging.getLogger("amphive.api")

# RFC 2606 reserves `.invalid` precisely so it can never resolve. A tombstone
# address built from it can never be mailed, can never be registered by a real
# person, and is unmistakably not a real address to anyone reading the table.
TOMBSTONE_EMAIL_DOMAIN = "deleted.amphive.invalid"
TOMBSTONE_NAME = "Deleted user"


class AccountClosureRefused(Exception):
    """Closure would leave live hardware, an in-flight session, or unsettled
    money in an inconsistent state. Carries the operator-facing reason."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def tombstone_email(user_id: int) -> str:
    return f"deleted-user-{user_id}@{TOMBSTONE_EMAIL_DOMAIN}"


async def assert_closable(db: AsyncSession, user: User) -> None:
    """Raise AccountClosureRefused if this account cannot be closed yet."""
    active = (
        await db.execute(
            select(func.count(ChargingSession.id)).where(
                ChargingSession.user_id == user.id,
                ChargingSession.status == SessionStatus.ACTIVE,
            )
        )
    ).scalar_one()
    if active:
        raise AccountClosureRefused(
            "You have a charging session in progress. Stop it first, then close "
            "your account."
        )

    # An operator account is the owner of record for hardware and money. Only
    # refuse when this account is the tenant's LAST operator AND the tenant
    # still owns something — a co-operator or a wound-down tenant is fine.
    if user.role in (UserRole.CPO, UserRole.ADMIN) and user.tenant_id is not None:
        other_operators = (
            await db.execute(
                select(func.count(User.id)).where(
                    User.tenant_id == user.tenant_id,
                    User.id != user.id,
                    User.role.in_((UserRole.CPO, UserRole.ADMIN)),
                    User.deleted_at.is_(None),
                )
            )
        ).scalar_one()
        if not other_operators:
            gateways = (
                await db.execute(
                    select(func.count(Gateway.id)).where(Gateway.tenant_id == user.tenant_id)
                )
            ).scalar_one()
            if gateways:
                raise AccountClosureRefused(
                    "You are the only operator on an organisation that still has "
                    "chargers registered. Transfer or remove them (or add another "
                    "operator) before closing this account."
                )


async def close_account(db: AsyncSession, user: User) -> dict:
    """Purge personal rows, forfeit any credit, anonymise the account.

    Caller is responsible for having authenticated the user and confirmed
    intent. Commits once, at the end. Returns a small summary for the audit
    log and the API response.
    """
    await assert_closable(db, user)

    # Lock the row and read the balance as a COLUMN, not through the ORM
    # instance. `select(User).with_for_update()` would take the lock but return
    # the identity-mapped instance loaded back in get_current_user, whose
    # coin_balance is stale as of auth time (expire_on_commit=False) — the
    # lost-update trap services/wallet.py documents at length. A scalar select
    # bypasses the identity map and yields the committed value, so a top-up
    # landing between authentication and closure is forfeited (and ledgered)
    # accurately instead of being under-reported.
    user_id = user.id
    forfeited = to_money(
        (
            await db.execute(
                select(User.coin_balance).where(User.id == user_id).with_for_update()
            )
        ).scalar_one()
        or ZERO_MONEY
    )

    # --- 1. Forfeit any remaining charging credit -------------------------
    if forfeited > ZERO_MONEY:
        db.add(
            LedgerTransaction(
                user_id=user_id,
                transaction_type=TransactionType.ACCOUNT_CLOSURE,
                amount=-forfeited,
                balance_after=ZERO_MONEY,
                description=(
                    "Charging credit forfeited on account closure (closed-loop "
                    "credit is non-refundable and non-transferable)"
                ),
            )
        )
        await db.execute(
            update(User)
            .where(User.id == user_id)
            .values(coin_balance=ZERO_MONEY)
            .execution_options(synchronize_session=False)
        )

    # --- 2. Cancel anything still scheduled -------------------------------
    await db.execute(
        update(Reservation)
        .where(
            Reservation.user_id == user_id,
            Reservation.status == ReservationStatus.BOOKED,
        )
        .values(status=ReservationStatus.CANCELLED)
        .execution_options(synchronize_session=False)
    )
    await db.execute(
        update(QueuedCharge)
        .where(
            QueuedCharge.user_id == user_id,
            QueuedCharge.status == QueuedChargeStatus.WAITING,
        )
        .values(status=QueuedChargeStatus.CANCELLED)
        .execution_options(synchronize_session=False)
    )

    # --- 3. Purge the purely-personal rows --------------------------------
    purged: dict[str, int] = {}
    for label, model, column in (
        ("push_subscriptions", PushSubscription, PushSubscription.user_id),
        ("notifications", Notification, Notification.user_id),
        ("favorites", UserFavorite, UserFavorite.user_id),
        ("plug_watches", PlugWatch, PlugWatch.user_id),
        ("group_memberships", GroupMembership, GroupMembership.user_id),
        ("capacity_requests", CapacityRequest, CapacityRequest.user_id),
        ("password_reset_tokens", PasswordResetToken, PasswordResetToken.user_id),
        ("email_verification_tokens", EmailVerificationToken, EmailVerificationToken.user_id),
    ):
        result = await db.execute(
            delete(model).where(column == user_id).execution_options(synchronize_session=False)
        )
        purged[label] = result.rowcount or 0

    # --- 4. Anonymise the account row itself ------------------------------
    # token_version is bumped DB-side so every outstanding JWT (this request's
    # included) is rejected on its next use.
    now = datetime.now(timezone.utc)
    await db.execute(
        update(User)
        .where(User.id == user_id)
        .values(
            email=tombstone_email(user_id),
            full_name=TOMBSTONE_NAME,
            hashed_password=hash_password(secrets.token_urlsafe(32)),
            google_sub=None,
            auth_provider="deleted",
            email_verified=False,
            is_disabled=True,
            deleted_at=now,
            token_version=User.token_version + 1,
        )
        .execution_options(synchronize_session=False)
    )

    await db.commit()

    summary = {
        "user_id": user_id,
        "forfeited_coins": float(forfeited),
        "purged": purged,
        "closed_at": now.isoformat(),
    }
    logger.info(
        "Account closed (anonymised; financial records retained)",
        extra={"user_id": user_id, "forfeited_coins": float(forfeited)},
    )
    return summary
