"""
Driver notification feed + Web Push subscription management.

The feed rows are written by services/notifications.py at the session
lifecycle / wallet / safety emit points; live delivery happens over the
Socket.io user room. This router is the pull side (list, mark read) plus the
push-subscription CRUD.
"""
import logging
import os

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.db import get_db
from backend.database.models import Notification, PushSubscription, User
from backend.schemas import (
    NotificationListResponse,
    NotificationResponse,
    PushSubscribeRequest,
    PushUnsubscribeRequest,
)
from backend.services import notifications as notification_service
from backend.services.auth import get_current_user

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
