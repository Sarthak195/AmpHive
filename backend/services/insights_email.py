"""
[F8V] Monthly insights email: an opt-in once-a-month summary of the driver's
previous calendar month (sessions, energy, spend and the F8 ESTIMATES built from
the driver's own vehicle profile).

Not a start path and not a notification: it never touches a plug, and it is a
plain email, so no push preference or quiet hour applies to it.

Schedule. The session reaper calls send_monthly_insights() every few ticks. A
pass does nothing (no query) before 09:00 local on the 1st (pricing.DEFAULT_TZ,
the zone C7 uses for a driver, who has no tenant) or when SMTP is not
configured. Otherwise it sends each opted-in driver the PREVIOUS month once, on
the 1st after 09:00 or later when the backend was down. Months are UTC calendar
months, the same buckets as GET /api/me/stats, so the email agrees with the
dashboard bars.

Idempotency. The decision is a claim: UPDATE users SET insights_email_month =
<month> WHERE the stored month is NULL or older, in the same transaction that
reads the month's totals. Whoever wins the claim owns the month; a second pass
or a second worker updates no row and sends nothing. A month with no completed
sessions is claimed and skipped. A send failure puts the old month back
(insights_email_failures + 1) so the next pass retries; the third failure gives
the month up (it stays claimed) so a dead mail server cannot loop forever. A
crash between the claim and the send loses that month's mail, never duplicates
it.

Turning the toggle on (routers/notifications.py PUT /api/me/vehicle) claims the
month already finished, so the first email is the next month's, not a surprise
about last month.
"""
import asyncio
import calendar
import html
import logging
from datetime import datetime, timezone
from typing import Callable, Optional

from sqlalchemy import and_, func, or_, select, update

from backend.database.models import (
    ChargingSession,
    DisputeStatus,
    SessionDispute,
    SessionStatus,
    User,
)
from backend.services import email, insights, pricing

logger = logging.getLogger("amphive.insights_email")

SEND_HOUR_LOCAL = 9
MAX_SEND_ATTEMPTS = 3
BATCH = 25


def previous_month_key(now: datetime) -> str:
    """"YYYY-MM" of the calendar month before `now`'s month in the driver zone
    (pricing.DEFAULT_TZ). `now` must be timezone-aware."""
    local = now.astimezone(pricing._zone(pricing.DEFAULT_TZ))
    year, month = (local.year, local.month - 1) if local.month > 1 else (local.year - 1, 12)
    return f"{year:04d}-{month:02d}"


def due_month(now: datetime) -> Optional[str]:
    """The month to send now, or None before 09:00 local on the 1st."""
    local = now.astimezone(pricing._zone(pricing.DEFAULT_TZ))
    if local.day == 1 and local.hour < SEND_HOUR_LOCAL:
        return None
    return previous_month_key(now)


async def month_summary(db, user_id: int, key: str) -> dict:
    """Completed/paid sessions that started in UTC month `key`, spend net of
    APPROVED dispute refunds (the /api/me/stats series definition)."""
    year, month = (int(x) for x in key.split("-"))
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=timezone.utc)
    in_month = and_(
        ChargingSession.user_id == user_id,
        ChargingSession.started_at >= start,
        ChargingSession.started_at < end,
        ChargingSession.status.in_([SessionStatus.COMPLETED, SessionStatus.PAID]),
    )
    count, energy, coins = (await db.execute(
        select(
            func.count(ChargingSession.id),
            func.coalesce(func.sum(ChargingSession.energy_kwh), 0),
            func.coalesce(func.sum(ChargingSession.coins_spent), 0),
        ).where(in_month)
    )).one()
    refunds = await db.scalar(
        select(func.coalesce(func.sum(SessionDispute.refund_coins), 0))
        .select_from(SessionDispute)
        .join(ChargingSession, ChargingSession.id == SessionDispute.session_id)
        .where(in_month, SessionDispute.status == DisputeStatus.APPROVED)
    )
    return {
        "sessions": int(count or 0),
        "energy_kwh": float(energy or 0),
        "spend_inr": max(float(coins or 0) - float(refunds or 0), 0.0),
    }


def render(full_name: Optional[str], key: str, summary: dict, vehicle, origin: str):
    """(subject, plain text, html) for one driver's month. Pure."""
    year, month = (int(x) for x in key.split("-"))
    label = f"{calendar.month_name[month]} {year}"
    impact = insights.estimate_impact(summary["energy_kwh"], vehicle)
    a = insights.assumptions(vehicle)
    rows = [
        ("Sessions", f"{summary['sessions']}"),
        ("Energy", f"{summary['energy_kwh']:.3f} kWh"),
        ("Spent", f"₹{summary['spend_inr']:.2f} of charging credit"),
        ("Distance (estimate)", f"{impact['distance_km']:.1f} km"),
    ]
    if impact["co2_avoided_kg"] > 0:
        rows.append(("CO2 avoided (estimate)", f"{impact['co2_avoided_kg']:.2f} kg"))
    rows.append((
        "Petrol not burned (estimate)",
        f"{impact['petrol_litres']:.2f} L (₹{impact['petrol_equivalent_inr']:.0f})",
    ))
    note = (
        f"Estimates, not measurements. Assumes {a['ev_km_per_kwh']:g} km per kWh against a "
        f"petrol vehicle doing {a['ice_km_per_l']:g} km/L at ₹{a['petrol_price_per_l']:g}/L, "
        f"less grid CO2 at {a['grid_emission_factor_kg_per_kwh']:g} kg/kWh. Months are UTC. "
        f"Change the vehicle figures in Account."
    )
    off = (
        "You get this email once a month because you turned it on. To turn it off, open "
        f"{origin}/account and clear 'Email me a monthly charging summary'."
    )
    greeting = f"Hi {full_name or 'there'},"
    subject = f"Your AmpHive charging in {label}"

    width = max(len(k) for k, _ in rows) + 2
    text = "\n".join(
        [greeting, "", f"Here is your AmpHive charging summary for {label}.", ""]
        + [f"  {(k + ':').ljust(width)}{v}" for k, v in rows]
        + ["", note, "", f"Full history: {origin}/activity", "", off]
    )
    cells = "".join(
        f'<tr><td style="padding:4px 12px 4px 0;color:#555">{html.escape(k)}</td>'
        f'<td style="padding:4px 0"><strong>{html.escape(v)}</strong></td></tr>'
        for k, v in rows
    )
    html_body = (
        '<div style="font-family:system-ui,sans-serif;max-width:520px;color:#111">'
        f"<p>{html.escape(greeting)}</p>"
        f"<p>Here is your AmpHive charging summary for {html.escape(label)}.</p>"
        f'<table role="presentation" style="border-collapse:collapse">{cells}</table>'
        f'<p style="color:#555;font-size:13px">{html.escape(note)}</p>'
        f'<p><a href="{html.escape(origin)}/activity">Full history</a></p>'
        f'<p style="color:#555;font-size:13px">{html.escape(off)}</p>'
        "</div>"
    )
    return subject, text, html_body


async def _send_one(factory, user_id: int, key: str, old_month: Optional[str],
                    old_failures: int) -> int:
    """Claim `key` for one driver, then send. Returns 1 when a mail went out."""
    async with factory() as db:
        claimed = (await db.execute(
            update(User)
            .where(
                User.id == user_id,
                User.monthly_insights_email.is_(True),
                User.deleted_at.is_(None),
                User.is_disabled.is_(False),
                or_(User.insights_email_month.is_(None), User.insights_email_month < key),
            )
            .values(insights_email_month=key, insights_email_failures=0)
            .returning(User.email, User.full_name, User.vehicle_profile)
        )).first()
        if claimed is None:
            await db.rollback()
            return 0
        summary = await month_summary(db, user_id, key)
        await db.commit()
    if summary["sessions"] == 0:
        return 0

    to_addr, full_name, raw_vehicle = claimed
    try:
        subject, text, html_body = render(
            full_name, key, summary, insights.load_vehicle(raw_vehicle), email.frontend_origin()
        )
        await asyncio.to_thread(email.send_email_or_raise, to_addr, subject, text, html_body)
        return 1
    except Exception:
        logger.exception("Monthly insights email failed", extra={"user_id": user_id})
        failures = old_failures + 1
        if failures >= MAX_SEND_ATTEMPTS:
            values = {"insights_email_failures": 0}  # give the month up, stay claimed
        else:
            values = {"insights_email_month": old_month, "insights_email_failures": failures}
        async with factory() as db:
            await db.execute(
                update(User)
                .where(User.id == user_id, User.insights_email_month == key)
                .values(**values)
            )
            await db.commit()
        return 0


async def send_monthly_insights(factory: Callable, now: Optional[datetime] = None) -> int:
    """One reaper pass. Returns the number of emails sent."""
    now = now or datetime.now(timezone.utc)
    key = due_month(now)
    if key is None or not email.smtp_configured():
        return 0
    async with factory() as db:
        rows = (await db.execute(
            select(User.id, User.insights_email_month, User.insights_email_failures)
            .where(
                User.monthly_insights_email.is_(True),
                User.deleted_at.is_(None),
                User.is_disabled.is_(False),
                or_(User.insights_email_month.is_(None), User.insights_email_month < key),
            )
            .order_by(User.id)
            .limit(BATCH)
        )).all()
    sent = 0
    for user_id, old_month, old_failures in rows:
        try:
            sent += await _send_one(factory, user_id, key, old_month, old_failures)
        except Exception:
            logger.exception("Monthly insights email pass failed", extra={"user_id": user_id})
    return sent
