"""
Operator alerts: tell the owner (CPOs of the gateway's tenant, plus platform
admins for an offline gateway) when a gateway drops while idle or a plug
reports a safety fault, even when the console is closed.

Delivery is the existing notify() (bell + Web Push) and, when the
OPERATOR_ALERT_EMAIL env var is set, one plain-text email through
services/email.send_email (modelled on services/billing_emails.py: fire and
forget, never raises into the MQTT ingest path). Unset means no email.

Env (read at call time, so tests and a restart pick up changes):

- OPERATOR_ALERTS=off|0|false|disabled  kill switch, default on. Silences
  every alert here (bell, push and email); the passive gateway_events feed
  and the existing UNMETERED_CONSUMPTION / orphan_off bells are untouched.
- OPERATOR_ALERT_EMAIL                  optional address, default none.
- GATEWAY_OFFLINE_ALERT_COOLDOWN_SEC    per-gateway, default 10800 (3 h): the
  2026-10-20 forensic recorded 33 offline/online flaps, so an unstable link
  must not ring every time.
- OPERATOR_ALERT_COOLDOWN_SEC           per (gateway, plug, event) for safety
  faults, default 3600.

Cooldowns and the "offline notice was sent" memory are in process memory:
they reset on a backend restart (a restart can re-alert once). This cannot
alert on the app itself being down or evicted.

A CPO or admin account can also charge (POST /api/sessions/start has no role
gate). For the offline alert, users with an ACTIVE session on that gateway are
left out of the operator fan-out, because they already get the driver
"Charger connection lost" bell. Safety faults are not deduplicated this way:
the session is finalized before the fault alert runs, so the owner can still
see a driver cutoff notice plus the gateway_fault bell for one event.

Offline cooldown follow-up: the cooldown suppresses a second offline alert, so
a short outage followed by a long one would leave "Gateway back online" as the
last message. When a suppressed offline alert finds no unanswered offline
alert, one re-check is scheduled at the cooldown expiry; if the gateway is
still stored OFFLINE then, "Gateway still offline" is sent and the back-online
follow-up is armed again.
"""
import asyncio
import logging
import os
import time
from typing import Callable, Dict, List, Optional, Set, Tuple

from sqlalchemy import or_, select

from backend.services.email import send_email
from backend.services.notifications import notify

logger = logging.getLogger("amphive.operator_alerts")

# Safety faults that page the operator: event_type -> bell title.
FAULT_TITLES: Dict[str, str] = {
    "UNAUTHORIZED_ON": "Plug switched on with no session",
    "THERMAL_CUTOFF": "Plug overheated and was cut off",
    "OVERCURRENT_CUTOFF": "Plug over-current cutoff",
    "OTA_FAILED": "Firmware update failed",
}

_last_offline_alert: Dict[str, float] = {}
_offline_alerted: Set[str] = set()
_last_fault_alert: Dict[Tuple[str, Optional[int], str], float] = {}
_rechecks: Dict[str, "asyncio.Task[None]"] = {}  # one cooldown-expiry re-check per gateway
_pending: Set[asyncio.Task] = set()  # strong refs for in-flight email sends


def _now() -> float:
    return time.monotonic()


def _reset_state() -> None:
    """Clear the in-memory cooldowns (tests)."""
    _last_offline_alert.clear()
    _offline_alerted.clear()
    _last_fault_alert.clear()
    for task in _rechecks.values():
        task.cancel()
    _rechecks.clear()


def enabled() -> bool:
    return os.getenv("OPERATOR_ALERTS", "on").strip().lower() not in (
        "off", "0", "false", "disabled",
    )


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _cooled_down(last: Optional[float], cooldown: float) -> bool:
    return last is None or cooldown <= 0 or _now() - last >= cooldown


def _send_email_bg(subject: str, body: str) -> None:
    """Email OPERATOR_ALERT_EMAIL (when set) in the background; the blocking
    SMTP call runs in a thread so ingest never waits on it."""
    to_addr = os.getenv("OPERATOR_ALERT_EMAIL", "").strip()
    if not to_addr:
        return

    async def _send() -> None:
        try:
            await asyncio.to_thread(send_email, to_addr, subject, body)
        except Exception:
            logger.exception("Operator alert email failed", extra={"subject": subject})

    try:
        task = asyncio.get_running_loop().create_task(_send())
    except RuntimeError:
        return
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def _fan_out(
    user_ids: List[int], kind: str, title: str, body: str, severity: str,
    plug_id: Optional[int] = None,
) -> None:
    for uid in user_ids:
        await notify(uid, kind, title, body, severity=severity, plug_id=plug_id)
    _send_email_bg(f"AmpHive alert: {title}", f"{title}\n\n{body}\n")


async def _recipients(
    session_factory: Callable, gateway_id: str, include_admins: bool,
    exclude_active_drivers: bool = False,
) -> List[int]:
    """User ids to alert: the gateway tenant's CPOs, plus admins when asked.
    With exclude_active_drivers, users charging on this gateway right now are
    left out (they already got the driver bell for the same outage)."""
    from backend.database.models import (
        ChargingSession,
        Gateway,
        Plug,
        SessionStatus,
        User,
        UserRole,
    )

    async with session_factory() as session:
        tenant_id = (await session.execute(
            select(Gateway.tenant_id).where(Gateway.id == gateway_id)
        )).scalar_one_or_none()
        conds = []
        if tenant_id is not None:
            conds.append((User.role == UserRole.CPO) & (User.tenant_id == tenant_id))
        if include_admins:
            conds.append(User.role == UserRole.ADMIN)
        if not conds:
            return []
        query = select(User.id).where(or_(*conds))
        if exclude_active_drivers:
            charging = (
                select(ChargingSession.user_id)
                .join(Plug, ChargingSession.plug_id == Plug.id)
                .where(Plug.gateway_id == gateway_id,
                       ChargingSession.status == SessionStatus.ACTIVE)
            )
            query = query.where(User.id.not_in(charging))
        ids = (await session.execute(query)).scalars().all()
    return list(ids)


async def _is_offline(session_factory: Callable, gateway_id: str) -> bool:
    from backend.database.models import Gateway, GatewayStatus

    async with session_factory() as session:
        status = (await session.execute(
            select(Gateway.status).where(Gateway.id == gateway_id)
        )).scalar_one_or_none()
    return status == GatewayStatus.OFFLINE


async def _send_offline(session_factory: Callable, gateway_id: str, title: str, body: str) -> None:
    # Claimed before any await: one task per gateway at a time, but a replaced
    # notice must not double-send.
    _last_offline_alert[gateway_id] = _now()
    _offline_alerted.add(gateway_id)
    try:
        user_ids = await _recipients(
            session_factory, gateway_id, include_admins=True, exclude_active_drivers=True
        )
        await _fan_out(user_ids, "gateway_offline", title, body, "warning")
    except Exception:
        logger.exception(f"Gateway offline alert failed for {gateway_id}")


async def alert_gateway_offline(session_factory: Callable, gateway_id: str) -> None:
    """The gateway stayed offline past the debounce: bell + push (+ email)
    the tenant's CPOs and platform admins, at most once per cooldown."""
    if not enabled():
        return
    cooldown = _float_env("GATEWAY_OFFLINE_ALERT_COOLDOWN_SEC", 10800.0)
    last = _last_offline_alert.get(gateway_id)
    if not _cooled_down(last, cooldown):
        logger.info("Gateway offline alert suppressed by cooldown", extra={"gateway_id": gateway_id})
        # The last message may be "back online"; re-check at cooldown expiry so
        # a real outage hiding behind a flap is still reported.
        if gateway_id not in _offline_alerted and gateway_id not in _rechecks and last is not None:
            wait = max(0.0, cooldown - (_now() - last))
            try:
                task = asyncio.get_running_loop().create_task(
                    _recheck_offline(session_factory, gateway_id, wait)
                )
            except RuntimeError:
                return
            _rechecks[gateway_id] = task
        return
    await _send_offline(
        session_factory, gateway_id, "Gateway offline",
        f"Gateway {gateway_id} has been offline for a while. Charging and "
        f"remote stop are unavailable until it reconnects.",
    )


async def _recheck_offline(session_factory: Callable, gateway_id: str, wait: float) -> None:
    """Cooldown expired after a suppressed offline alert: if the gateway is
    still stored OFFLINE and no offline alert is outstanding, say so."""
    me = asyncio.current_task()
    try:
        await asyncio.sleep(wait)
        if _rechecks.get(gateway_id) is me:
            del _rechecks[gateway_id]
        if not enabled() or gateway_id in _offline_alerted:
            return
        if not await _is_offline(session_factory, gateway_id):
            return
        await _send_offline(
            session_factory, gateway_id, "Gateway still offline",
            f"Gateway {gateway_id} went offline again and is still down. "
            f"Charging and remote stop are unavailable until it reconnects.",
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(f"Gateway offline re-check failed for {gateway_id}")
    finally:
        if _rechecks.get(gateway_id) is me:
            del _rechecks[gateway_id]


async def alert_gateway_online(session_factory: Callable, gateway_id: str) -> None:
    """Follow-up, only when an offline alert for this gateway was sent."""
    if gateway_id not in _offline_alerted:
        return
    _offline_alerted.discard(gateway_id)
    if not enabled():
        return
    try:
        user_ids = await _recipients(session_factory, gateway_id, include_admins=True)
        await _fan_out(
            user_ids, "gateway_online", "Gateway back online",
            f"Gateway {gateway_id} is reachable again.", "info",
        )
    except Exception:
        logger.exception(f"Gateway online follow-up failed for {gateway_id}")


async def alert_safety_fault(
    session_factory: Callable, gateway_id: str, plug_id: Optional[int],
    event_type: str, severity: str, detail: Optional[str],
) -> None:
    """Bell + push (+ email) the tenant's CPOs about a safety fault or failed
    OTA, deduplicated per (gateway, plug, event) within the cooldown."""
    if event_type not in FAULT_TITLES or not enabled():
        return
    key = (gateway_id, plug_id, event_type)
    cooldown = _float_env("OPERATOR_ALERT_COOLDOWN_SEC", 3600.0)
    if not _cooled_down(_last_fault_alert.get(key), cooldown):
        logger.info("Operator fault alert suppressed by cooldown", extra={"event_type": event_type})
        return
    _last_fault_alert[key] = _now()
    try:
        user_ids = await _recipients(session_factory, gateway_id, include_admins=False)
        where = f"plug {plug_id} on gateway {gateway_id}" if plug_id is not None else f"gateway {gateway_id}"
        body = f"{where}: {detail or event_type}"
        await _fan_out(
            user_ids, "gateway_fault", FAULT_TITLES[event_type], body, severity,
            plug_id=plug_id,
        )
    except Exception:
        logger.exception(f"Operator fault alert failed for {gateway_id} ({event_type})")
