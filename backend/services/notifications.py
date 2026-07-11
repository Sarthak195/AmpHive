"""
Driver notifications (TODO.md "Driver notifications"; pairs with the CPO
gateway_events feed).

One entry point — notify() — called from the session-lifecycle / wallet /
safety emit points. Each call:
  1. persists a Notification row (its own transaction via
     async_session_factory, so callers' transactions stay untouched),
  2. emits a Socket.io "notification" event to the user's room (live UI),
  3. fans out Web Push to the user's stored browser subscriptions
     (best-effort, in a thread — pywebpush is blocking).

Web Push is enabled by setting VAPID_PRIVATE_KEY (+ optional VAPID_SUBJECT)
in the environment; without it, steps 1–2 still work and push is skipped.
Subscriptions the push service reports gone (404/410) are pruned.
"""
import asyncio
import logging
import json
import os
from typing import Optional

from sqlalchemy import delete, select

from backend.database.db import async_session_factory
from backend.database.models import Notification, PushSubscription

logger = logging.getLogger("amphive.notifications")

VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "")
# Contact URI the push service may use to reach the operator (spec-required).
VAPID_SUBJECT = os.getenv("VAPID_SUBJECT", "mailto:admin@amphive.example")


def _serialize(n: Notification) -> dict:
    return {
        "id": n.id,
        "type": n.type,
        "severity": n.severity,
        "title": n.title,
        "body": n.body,
        "plug_id": n.plug_id,
        "session_id": n.session_id,
        "read": bool(n.read),
        "created_at": n.created_at.isoformat() if n.created_at else None,
    }


async def notify(
    user_id: int,
    type: str,
    title: str,
    body: str,
    *,
    severity: str = "info",
    plug_id: Optional[int] = None,
    session_id: Optional[int] = None,
) -> Optional[dict]:
    """
    Create + deliver a notification. Never raises — a notification failure
    must not break the billing/telemetry path that triggered it. Returns the
    serialized notification (or None on persist failure).
    """
    try:
        async with async_session_factory() as db:
            row = Notification(
                user_id=user_id,
                type=type[:32],
                severity=severity[:16],
                title=title[:120],
                body=body[:500],
                plug_id=plug_id,
                session_id=session_id,
            )
            db.add(row)
            await db.commit()
            # created_at is a server default and expire_on_commit=False, so
            # without this refresh the live Socket.io payload would carry a
            # null timestamp (the DB row itself is fine).
            await db.refresh(row)
            payload = _serialize(row)
    except Exception:
        logger.exception(f"Failed to persist notification for user {user_id} ({type})")
        return None

    # Live delivery to connected clients (late import: circular at module load).
    try:
        from backend.services.socketio_manager import emit_notification
        await emit_notification(user_id, payload)
    except Exception:
        logger.exception(f"Socket.io delivery failed for notification {payload.get('id')}")

    # Web Push fan-out (skipped when VAPID keys are not configured).
    try:
        await _push_to_user(user_id, payload)
    except Exception:
        logger.exception(f"Web push fan-out failed for notification {payload.get('id')}")

    return payload


def push_enabled() -> bool:
    return bool(VAPID_PRIVATE_KEY)


async def _push_to_user(user_id: int, payload: dict) -> None:
    if not push_enabled():
        return

    async with async_session_factory() as db:
        subs = (await db.execute(
            select(PushSubscription).where(PushSubscription.user_id == user_id)
        )).scalars().all()
        sub_data = [
            {"id": s.id, "endpoint": s.endpoint, "p256dh": s.p256dh, "auth": s.auth}
            for s in subs
        ]
    if not sub_data:
        return

    loop = asyncio.get_running_loop()
    dead_ids = []
    for sub in sub_data:
        # pywebpush is blocking (requests) — keep it off the event loop.
        gone = await loop.run_in_executor(None, _send_one_push, sub, payload)
        if gone:
            dead_ids.append(sub["id"])

    if dead_ids:
        async with async_session_factory() as db:
            await db.execute(
                delete(PushSubscription).where(PushSubscription.id.in_(dead_ids))
            )
            await db.commit()
        logger.info(f"Pruned {len(dead_ids)} dead push subscription(s) for user {user_id}")


def _send_one_push(sub: dict, payload: dict) -> bool:
    """Send one push message. Returns True when the subscription is gone
    (404/410) and should be pruned; False otherwise (success or transient
    failure — transient failures are logged and dropped, not retried)."""
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        logger.warning("pywebpush not installed — web push disabled.")
        return False

    try:
        webpush(
            subscription_info={
                "endpoint": sub["endpoint"],
                "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
            },
            data=json.dumps(payload),
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims={"sub": VAPID_SUBJECT},
        )
        return False
    except WebPushException as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status in (404, 410):
            return True
        logger.warning(f"Web push to subscription {sub['id']} failed (status={status}): {e}")
        return False
    except Exception as e:
        logger.warning(f"Web push to subscription {sub['id']} errored: {e}")
        return False
