"""
Self-service data export (DPDP Act §11 "right to access" shaped).

Builds one JSON document containing everything the platform holds that is
*about the caller*, assembled strictly from rows keyed on their own user id.
`GET /api/auth/me/export` serves it as a download.

Deliberate boundaries
---------------------
* **Only the caller's rows.** Every query below filters on `user_id` /
  `driver_user_id` == the authenticated user. Nothing is joined in that would
  disclose another driver, and operator-side fields that belong to the CPO
  rather than to this person (payout internals, other tenants' tariffs) are
  not included.
* **Charger identity, not operator identity.** Sessions and invoices name the
  charger and the operating organisation — that is the user's own transaction
  counterparty, which they are entitled to — but not the operator's staff
  accounts.
* **No credentials.** Password hashes, token digests, JWTs and push-subscription
  keys are never exported. Push subscriptions appear as an endpoint *host* and a
  creation date, which is enough for the user to recognise the device without
  handing back a live push credential.
* **Bounded.** Row counts are capped per collection with an explicit
  `truncated` flag rather than silently cut, so the export can never become an
  unbounded memory read of `telemetry_readings`-scale data. Per-session
  telemetry samples are deliberately excluded and the document says so.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.models import (
    ChargerGroup,
    ChargingSession,
    Gateway,
    GroupMembership,
    Invoice,
    LedgerTransaction,
    Notification,
    Plug,
    PlugReport,
    PlugWatch,
    PushSubscription,
    Reservation,
    SessionDispute,
    Tenant,
    User,
    UserFavorite,
)

logger = logging.getLogger("amphive.api")

# Per-collection ceiling. Generous for any real account, and the response says
# plainly when it bit rather than pretending the export is complete.
MAX_ROWS_PER_COLLECTION = 5000


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _host_of(endpoint: str) -> str:
    """`https://fcm.googleapis.com/fcm/send/AAAA...` -> `fcm.googleapis.com`.

    A push endpoint is a live credential: anyone holding it can push to that
    browser. The export identifies the device by its push *service* only.
    """
    try:
        from urllib.parse import urlparse

        return urlparse(endpoint).hostname or "unknown"
    except Exception:  # pragma: no cover — defensive
        return "unknown"


async def _collect(db: AsyncSession, stmt):
    """Run a statement with one extra row requested, so a truncated collection
    is detectable rather than silently short."""
    rows = list((await db.execute(stmt.limit(MAX_ROWS_PER_COLLECTION + 1))).all())
    truncated = len(rows) > MAX_ROWS_PER_COLLECTION
    return rows[:MAX_ROWS_PER_COLLECTION], truncated


async def build_export(db: AsyncSession, user: User) -> dict:
    """Assemble the caller's full data export document."""
    generated_at = datetime.now(timezone.utc)
    truncations: list[str] = []

    def note(name: str, truncated: bool):
        if truncated:
            truncations.append(name)

    # --- account ----------------------------------------------------------
    account = {
        "id": user.id,
        "email": user.email,
        "full_name": user.full_name,
        "role": user.role.value,
        "auth_provider": user.auth_provider,
        "google_account_linked": user.google_sub is not None,
        "email_verified": user.email_verified,
        "charging_credit_balance": float(user.coin_balance),
        "created_at": _iso(user.created_at),
        "organisation_id": user.tenant_id,
    }

    # --- charging sessions (with the charger they ran on) -----------------
    session_rows, truncated = await _collect(
        db,
        select(ChargingSession, Plug.name, Tenant.name)
        .outerjoin(Plug, Plug.id == ChargingSession.plug_id)
        .outerjoin(Gateway, Gateway.id == Plug.gateway_id)
        .outerjoin(Tenant, Tenant.id == Gateway.tenant_id)
        .where(ChargingSession.user_id == user.id)
        .order_by(ChargingSession.started_at.desc()),
    )
    note("charging_sessions", truncated)
    charging_sessions = [
        {
            "id": s.id,
            "charger": plug_name,
            "operator": tenant_name,
            "status": s.status.value,
            "started_at": _iso(s.started_at),
            "ended_at": _iso(s.ended_at),
            "energy_kwh": s.energy_kwh,
            "coins_spent": float(s.coins_spent) if s.coins_spent is not None else None,
        }
        for s, plug_name, tenant_name in session_rows
    ]

    # --- wallet ledger ----------------------------------------------------
    ledger_rows, truncated = await _collect(
        db,
        select(LedgerTransaction)
        .where(LedgerTransaction.user_id == user.id)
        .order_by(LedgerTransaction.created_at.desc()),
    )
    note("wallet_ledger", truncated)
    wallet_ledger = [
        {
            "id": t.id,
            "type": t.transaction_type.value,
            "amount_coins": float(t.amount),
            "balance_after_coins": float(t.balance_after),
            "description": t.description,
            "session_id": t.session_id,
            "payment_reference": t.razorpay_payment_id,
            "created_at": _iso(t.created_at),
        }
        for (t,) in ledger_rows
    ]

    # --- GST invoices -----------------------------------------------------
    invoice_rows, truncated = await _collect(
        db,
        select(Invoice)
        .where(Invoice.driver_user_id == user.id)
        .order_by(Invoice.issued_at.desc()),
    )
    note("invoices", truncated)
    invoices = [
        {
            "invoice_number": inv.invoice_number,
            "session_id": inv.session_id,
            "issued_at": _iso(inv.issued_at),
            "seller_legal_name": inv.seller_legal_name,
            "seller_gstin": inv.seller_gstin,
        }
        for (inv,) in invoice_rows
    ]

    # --- disputes + reports the user raised -------------------------------
    dispute_rows, truncated = await _collect(
        db,
        select(SessionDispute)
        .where(SessionDispute.driver_user_id == user.id)
        .order_by(SessionDispute.created_at.desc()),
    )
    note("disputes", truncated)
    disputes = [
        {
            "id": d.id,
            "session_id": d.session_id,
            "reason": d.reason,
            "status": d.status.value,
            "resolution_note": d.resolution_note,
            "refund_coins": float(d.refund_coins) if d.refund_coins is not None else None,
            "created_at": _iso(d.created_at),
            "resolved_at": _iso(d.resolved_at),
        }
        for (d,) in dispute_rows
    ]

    report_rows, truncated = await _collect(
        db,
        select(PlugReport)
        .where(PlugReport.driver_user_id == user.id)
        .order_by(PlugReport.created_at.desc()),
    )
    note("charger_reports", truncated)
    charger_reports = [
        {
            "id": r.id,
            "charger_id": r.plug_id,
            "category": r.category,
            "description": r.description,
            "status": r.status.value,
            "created_at": _iso(r.created_at),
        }
        for (r,) in report_rows
    ]

    # --- reservations -----------------------------------------------------
    reservation_rows, truncated = await _collect(
        db,
        select(Reservation)
        .where(Reservation.user_id == user.id)
        .order_by(Reservation.start_at.desc()),
    )
    note("reservations", truncated)
    reservations = [
        {
            "id": r.id,
            "charger_id": r.plug_id,
            "status": r.status.value,
            "start_at": _iso(r.start_at),
            "end_at": _iso(r.end_at),
            "created_at": _iso(r.created_at),
        }
        for (r,) in reservation_rows
    ]

    # --- group memberships / favourites / watches -------------------------
    membership_rows, truncated = await _collect(
        db,
        select(GroupMembership, ChargerGroup.name)
        .outerjoin(ChargerGroup, ChargerGroup.id == GroupMembership.group_id)
        .where(GroupMembership.user_id == user.id),
    )
    note("group_memberships", truncated)
    group_memberships = [
        {"group_id": m.group_id, "group_name": name, "joined_at": _iso(m.joined_at)}
        for m, name in membership_rows
    ]

    favorite_rows, truncated = await _collect(
        db, select(UserFavorite).where(UserFavorite.user_id == user.id)
    )
    note("favorites", truncated)
    favorites = [{"charger_id": f.plug_id, "created_at": _iso(f.created_at)} for (f,) in favorite_rows]

    watch_rows, truncated = await _collect(
        db, select(PlugWatch).where(PlugWatch.user_id == user.id)
    )
    note("charger_watches", truncated)
    charger_watches = [{"charger_id": w.plug_id, "created_at": _iso(w.created_at)} for (w,) in watch_rows]

    # --- notifications + push devices -------------------------------------
    notification_rows, truncated = await _collect(
        db,
        select(Notification)
        .where(Notification.user_id == user.id)
        .order_by(Notification.created_at.desc()),
    )
    note("notifications", truncated)
    notifications = [
        {
            "id": n.id,
            "type": n.type,
            "severity": n.severity,
            "title": n.title,
            "body": n.body,
            "created_at": _iso(n.created_at),
            "read": n.read,
        }
        for (n,) in notification_rows
    ]

    push_rows, truncated = await _collect(
        db, select(PushSubscription).where(PushSubscription.user_id == user.id)
    )
    note("push_devices", truncated)
    push_devices = [
        # Endpoint HOST only — the full endpoint is a live push credential.
        {"push_service": _host_of(p.endpoint), "created_at": _iso(p.created_at)}
        for (p,) in push_rows
    ]

    return {
        "export_format": "amphive.user-data-export.v1",
        "generated_at": generated_at.isoformat(),
        "about": (
            "Everything AmpHive holds that is about your account. Per-second "
            "telemetry samples recorded while charging are not included: they are "
            "retained for a limited period for billing and diagnostics and are "
            "summarised in each session's energy and cost figures above."
        ),
        "truncated_collections": truncations,
        "max_rows_per_collection": MAX_ROWS_PER_COLLECTION,
        "account": account,
        "charging_sessions": charging_sessions,
        "wallet_ledger": wallet_ledger,
        "invoices": invoices,
        "disputes": disputes,
        "charger_reports": charger_reports,
        "reservations": reservations,
        "group_memberships": group_memberships,
        "favorites": favorites,
        "charger_watches": charger_watches,
        "notifications": notifications,
        "push_devices": push_devices,
    }
