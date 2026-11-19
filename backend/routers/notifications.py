"""
Driver notification feed + Web Push subscription management.

The feed rows are written by services/notifications.py at the session
lifecycle / wallet / safety emit points; live delivery happens over the
Socket.io user room. This router is the pull side (list, mark read), the
push-subscription CRUD, and the caller's own push preferences
(GET/PUT /api/me/notification-prefs, services/notification_prefs.py).
"""
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.db import get_db
from backend.database.models import Notification, PushSubscription, Tenant, User
from backend.schemas import (
    NotificationListResponse,
    NotificationResponse,
    PushSubscribeRequest,
    PushUnsubscribeRequest,
)
from backend.services import insights
from backend.services import notifications as notification_service
from backend.services.auth import get_current_user
from backend.services.insights_email import previous_month_key
from backend.services.notification_prefs import NotificationPrefs, load_prefs

logger = logging.getLogger("amphive.api")
router = APIRouter()

# [L9] Per-user cap on stored Web-Push subscriptions. Each distinct browser /
# device is one row and a benign user has only a handful; the cap bounds a
# single authenticated user from flooding push_subscriptions with unique
# endpoints (storage-exhaustion DoS — and every stored endpoint is later a
# server-side POST target). Re-subscribing an already-stored endpoint updates
# it in place and is never capped, so a user's real devices keep working.
MAX_PUSH_SUBSCRIPTIONS_PER_USER = int(
    os.getenv("MAX_PUSH_SUBSCRIPTIONS_PER_USER") or "20"
)


@router.get("/api/notifications", response_model=NotificationListResponse)
async def list_notifications(
    unread_only: bool = False,
    limit: int = 50,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Newest-first notification feed for the current user, plus the total
    unread count (for the bell badge, independent of the page limit)."""
    limit = max(1, min(limit, 200))

    filters = [Notification.user_id == user.id]
    if unread_only:
        filters.append(Notification.read.is_(False))

    rows = (await db.execute(
        select(Notification)
        .where(*filters)
        .order_by(Notification.created_at.desc(), Notification.id.desc())
        .limit(limit)
    )).scalars().all()

    unread_count = (await db.execute(
        select(func.count()).select_from(Notification).where(
            Notification.user_id == user.id,
            Notification.read.is_(False),
        )
    )).scalar_one()

    return NotificationListResponse(
        notifications=[
            NotificationResponse(
                id=n.id, type=n.type, severity=n.severity, title=n.title,
                body=n.body, plug_id=n.plug_id, session_id=n.session_id,
                read=n.read,
                created_at=n.created_at.isoformat() if n.created_at else None,
            )
            for n in rows
        ],
        unread_count=unread_count,
    )


@router.post("/api/notifications/{notification_id}/read")
async def mark_notification_read(
    notification_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        update(Notification)
        .where(Notification.id == notification_id, Notification.user_id == user.id)
        .values(read=True)
    )
    await db.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Notification not found.")
    return {"status": "read"}


@router.post("/api/notifications/read-all")
async def mark_all_notifications_read(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        update(Notification)
        .where(Notification.user_id == user.id, Notification.read.is_(False))
        .values(read=True)
    )
    await db.commit()
    return {"status": "read", "count": result.rowcount}


# --- Web Push subscription management -------------------------------------

def _vapid_public_key() -> str:
    """Derive the browser-facing applicationServerKey (base64url, uncompressed
    EC point) from VAPID_PRIVATE_KEY — single source of truth, no separate
    public-key env to drift. Empty string when push is not configured."""
    if not notification_service.push_enabled():
        return ""
    try:
        from cryptography.hazmat.primitives import serialization
        from py_vapid import Vapid, b64urlencode

        v = Vapid.from_string(notification_service.VAPID_PRIVATE_KEY)
        raw = v.public_key.public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )
        return b64urlencode(raw)
    except Exception:
        logger.exception("Failed to derive the VAPID public key — push disabled.")
        return ""


@router.get("/api/notifications/push/public-key")
async def get_push_public_key(user: User = Depends(get_current_user)):
    """The VAPID applicationServerKey the browser needs for
    pushManager.subscribe(). enabled=false → the UI hides the push toggle."""
    key = _vapid_public_key()
    return {"enabled": bool(key), "vapid_public_key": key}


@router.post("/api/notifications/push/subscribe")
async def push_subscribe(
    req: PushSubscribeRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Store (or re-own) a browser push subscription. endpoint is UNIQUE: the
    same browser re-subscribing updates in place, and a subscription that
    changed hands (new login on a shared browser) moves to the current user."""
    existing = (await db.execute(
        select(PushSubscription).where(PushSubscription.endpoint == req.endpoint)
    )).scalar_one_or_none()

    if existing:
        existing.user_id = user.id
        existing.p256dh = req.keys.p256dh
        existing.auth = req.keys.auth
    else:
        # Only a NEW row grows the table, so the cap is checked here (a re-own /
        # key-refresh of an existing endpoint above is exempt). The count+insert
        # has a benign boundary race — a user might momentarily hold cap+1 rows
        # — which is harmless for a soft anti-DoS bound.
        sub_count = (await db.execute(
            select(func.count())
            .select_from(PushSubscription)
            .where(PushSubscription.user_id == user.id)
        )).scalar_one()
        if sub_count >= MAX_PUSH_SUBSCRIPTIONS_PER_USER:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Too many push subscriptions ({sub_count}); the limit is "
                    f"{MAX_PUSH_SUBSCRIPTIONS_PER_USER}. Remove an old device first."
                ),
            )
        db.add(PushSubscription(
            user_id=user.id,
            endpoint=req.endpoint,
            p256dh=req.keys.p256dh,
            auth=req.keys.auth,
        ))
    try:
        await db.commit()
    except IntegrityError:
        # Lost a concurrent-subscribe race on the same endpoint; the winner's
        # row stands (same browser, same keys — nothing to reconcile).
        await db.rollback()
    return {"status": "subscribed"}


@router.delete("/api/notifications/push/subscribe")
async def push_unsubscribe(
    req: PushUnsubscribeRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import delete
    await db.execute(
        delete(PushSubscription).where(
            PushSubscription.endpoint == req.endpoint,
            PushSubscription.user_id == user.id,
        )
    )
    await db.commit()
    return {"status": "unsubscribed"}


# --- Push preferences (C7) -------------------------------------------------

async def _prefs_response(db: AsyncSession, user: User, prefs: NotificationPrefs) -> dict:
    """The caller's own prefs plus the zone quiet hours are read in (the
    tenant's, else the pricing default). Nothing here is personal or infra
    data, so a cpo_viewer reading their own prefs needs no masking."""
    from backend.services.pricing import zone_name

    tz_name = None
    if user.tenant_id is not None:
        tz_name = await db.scalar(select(Tenant.timezone).where(Tenant.id == user.tenant_id))
    return {**prefs.model_dump(), "timezone": zone_name(tz_name)}


@router.get("/api/me/notification-prefs")
async def get_notification_prefs(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Per-type push toggles and quiet hours (defaults when never saved)."""
    return await _prefs_response(db, user, load_prefs(user.notification_prefs))


@router.put("/api/me/notification-prefs")
async def put_notification_prefs(
    req: NotificationPrefs,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Replace the caller's prefs. Only suppressible types have toggles; the
    safety and billing allowlist always pushes and cannot be set here."""
    await db.execute(
        update(User).where(User.id == user.id).values(notification_prefs=req.model_dump())
    )
    await db.commit()
    return await _prefs_response(db, user, req)


# --- Vehicle profile and monthly insights email (F8V) -----------------------

class VehicleSettings(insights.VehicleProfile):
    """PUT body: the three vehicle figures (null = default) and the email
    opt-in. Omitting monthly_insights_email leaves it unchanged."""
    monthly_insights_email: Optional[bool] = None


def _vehicle_response(user: User, vehicle: Optional[insights.VehicleProfile],
                      monthly: bool) -> dict:
    """The saved figures, the defaults they replace and the net CO2 factor in
    force (the receipt multiplies energy by it). Own data only."""
    return {
        "km_per_kwh": vehicle.km_per_kwh if vehicle else None,
        "petrol_price_per_l": vehicle.petrol_price_per_l if vehicle else None,
        "petrol_km_per_l": vehicle.petrol_km_per_l if vehicle else None,
        "monthly_insights_email": monthly,
        "defaults": {
            "km_per_kwh": insights.EV_KM_PER_KWH,
            "petrol_price_per_l": insights.PETROL_PRICE_PER_L,
            "petrol_km_per_l": insights.ICE_KM_PER_L,
        },
        "co2_avoided_kg_per_kwh": round(insights.co2_avoided_kg_per_kwh(vehicle), 4),
    }


@router.get("/api/me/vehicle")
async def get_vehicle(user: User = Depends(get_current_user)):
    """The caller's vehicle figures for the F8 insights, and the monthly
    insights email opt-in (off by default)."""
    return _vehicle_response(
        user, insights.load_vehicle(user.vehicle_profile), bool(user.monthly_insights_email)
    )


@router.put("/api/me/vehicle")
async def put_vehicle(
    req: VehicleSettings,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Replace the caller's vehicle figures (all null = the defaults). Only the
    caller's own estimates change; CPO-side series keep the defaults."""
    vehicle = insights.VehicleProfile(
        km_per_kwh=req.km_per_kwh,
        petrol_price_per_l=req.petrol_price_per_l,
        petrol_km_per_l=req.petrol_km_per_l,
    )
    stored = vehicle.model_dump()
    values: dict = {"vehicle_profile": stored if any(v is not None for v in stored.values()) else None}
    monthly = bool(user.monthly_insights_email)
    if req.monthly_insights_email is not None:
        monthly = req.monthly_insights_email
        values["monthly_insights_email"] = monthly
        if monthly and not user.monthly_insights_email:
            # Turning it on claims the month already finished: the first email
            # is next month's, not a surprise about last month.
            finished = previous_month_key(datetime.now(timezone.utc))
            if not user.insights_email_month or user.insights_email_month < finished:
                values["insights_email_month"] = finished
            values["insights_email_failures"] = 0
    await db.execute(update(User).where(User.id == user.id).values(**values))
    await db.commit()
    return _vehicle_response(user, vehicle, monthly)
