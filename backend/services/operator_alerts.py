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

Durable state (gateways columns, migration 0051): offline_notified_at marks
an outstanding "Gateway offline" alert (cleared by the "back online"
follow-up) and last_operator_alert_at times the per-gateway offline cooldown.
The flag only counts while the gateway has not been seen since: telemetry
heals a gateway to ONLINE (stamping last_seen_at) without running the
back-online follow-up, so a flag older than last_seen_at is a stale answer
and the next real outage is still reported.
Both are decided and written under the gateway row lock, together with the
status check and the recipient query, so a backend restart neither repeats
nor loses the offline / back-online pair, and an `online` that committed
first means neither goes out. Delivery for one gateway is also serialized in
process (a per-gateway asyncio.Lock held across decide and fan-out, never the
DB row lock), so a "back online" is never delivered ahead of, or interleaved
with, the "offline" fan-out it answers. The per-(gateway, plug, event) fault
cooldown is kept in process memory and rebuilt from the gateway_events
audit rows after a restart: an earlier row for the same (gateway, plug,
event) inside the cooldown suppresses the alert, so a restart does not
re-page a fault.
This cannot alert on the app itself being down or evicted.

A CPO or admin account can also charge (POST /api/sessions/start has no role
gate). For the offline alert, users with an ACTIVE session on that gateway are
left out of the operator fan-out, because they already get the driver
"Charger connection lost" bell. For a safety fault, the driver whose session
the cutoff just finalized (and who got that cutoff notice) is left out of the
gateway_fault bell, so one fault gives each user one notice; the optional
email is unchanged.

Offline cooldown follow-up: the cooldown suppresses a second offline alert, so
a short outage followed by a long one would leave "Gateway back online" as the
last message. When a suppressed offline alert finds no unanswered offline
alert, one re-check is scheduled at the cooldown expiry; if the gateway is
still stored OFFLINE then, "Gateway still offline" is sent and the back-online
follow-up is armed again. A pending re-check is derived from the stored row
(OFFLINE, no outstanding alert, an earlier alert on record), so
rearm_offline_rechecks() re-arms it at backend startup.
"""
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

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

# Upper bound on the startup re-check scan (the gateways table is tiny).
_REARM_LIMIT = 500

_last_fault_alert: Dict[Tuple[str, Optional[int], str], datetime] = {}
_rechecks: Dict[str, "asyncio.Task[None]"] = {}  # one cooldown-expiry re-check per gateway
_gateway_locks: Dict[str, asyncio.Lock] = {}  # serializes offline/online decide + fan-out
_pending: Set[asyncio.Task] = set()  # strong refs for in-flight email sends


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _age_sec(then: datetime, now: datetime) -> float:
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (now - then).total_seconds()


def _reset_state() -> None:
    """Clear the in-memory state (tests)."""
    _last_fault_alert.clear()
    _gateway_locks.clear()
    for task in _rechecks.values():
        task.cancel()
    _rechecks.clear()


def _gateway_lock(gateway_id: str) -> asyncio.Lock:
    lock = _gateway_locks.get(gateway_id)
    if lock is None:
        lock = _gateway_locks[gateway_id] = asyncio.Lock()
    return lock


def enabled() -> bool:
    return os.getenv("OPERATOR_ALERTS", "on").strip().lower() not in (
        "off", "0", "false", "disabled",
    )


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _offline_alert_outstanding(gw) -> bool:
    """An offline alert is unanswered: flagged, and the gateway has not been
    seen since (a telemetry heal stamps last_seen_at but runs no follow-up)."""
    flagged = gw.offline_notified_at
    if flagged is None:
        return False
    seen = gw.last_seen_at
    return seen is None or _aware(seen) <= _aware(flagged)


def _cooled_down(last: Optional[datetime], cooldown: float) -> bool:
    return last is None or cooldown <= 0 or _age_sec(last, _now()) >= cooldown


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


async def _recipient_ids(
    session, tenant_id: Optional[int], gateway_id: str, include_admins: bool,
    exclude_active_drivers: bool = False,
) -> List[int]:
    """User ids to alert, on an open session: the tenant's CPOs, plus admins
    when asked. With exclude_active_drivers, users charging on this gateway
    right now are left out (they already got the driver bell for the same
    outage)."""
    from backend.database.models import ChargingSession, Plug, SessionStatus, User, UserRole

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
    return list((await session.execute(query)).scalars().all())


async def _recipients(
    session_factory: Callable, gateway_id: str, include_admins: bool,
    exclude_active_drivers: bool = False,
) -> List[int]:
    """_recipient_ids for the gateway's tenant, in a session of its own."""
    from backend.database.models import Gateway

    async with session_factory() as session:
        tenant_id = (await session.execute(
            select(Gateway.tenant_id).where(Gateway.id == gateway_id)
        )).scalar_one_or_none()
        return await _recipient_ids(
            session, tenant_id, gateway_id, include_admins, exclude_active_drivers
        )


async def _locked_gateway(session, gateway_id: str):
    from backend.database.models import Gateway

    return (await session.execute(
        select(Gateway).where(Gateway.id == gateway_id).with_for_update()
    )).scalar_one_or_none()


async def _decide_offline(
    session_factory: Callable, gateway_id: str, cooldown: float, recheck: bool,
) -> Tuple[Optional[List[int]], Optional[float]]:
    """Decide an offline alert under the gateway row lock. Returns
    (recipients, None) when the alert is claimed (both columns written in the
    same transaction), (None, seconds_left) when the cooldown suppressed it,
    and (None, None) when there is nothing to send: the gateway is unknown,
    no longer stored OFFLINE (an `online` committed first, so neither an
    "offline" nor its "back online" goes out), or already alerted. A re-check
    runs at the cooldown expiry, so it skips the cooldown test."""
    from backend.database.models import GatewayStatus

    async with session_factory() as session:
        gw = await _locked_gateway(session, gateway_id)
        if gw is None or gw.status != GatewayStatus.OFFLINE or _offline_alert_outstanding(gw):
            return None, None
        last = gw.last_operator_alert_at
        if not recheck and last is not None and not _cooled_down(last, cooldown):
            if gw.offline_notified_at is not None:
                # A stale flag (seen since): clear it so the startup scan
                # re-arms the re-check this suppression schedules.
                gw.offline_notified_at = None
                await session.commit()
            return None, cooldown - _age_sec(last, _now())
        now = _now()
        gw.offline_notified_at = now
        gw.last_operator_alert_at = now
        user_ids = await _recipient_ids(
            session, gw.tenant_id, gateway_id, include_admins=True, exclude_active_drivers=True,
        )
        await session.commit()
    return user_ids, None


async def _decide_online(
    session_factory: Callable, gateway_id: str, send: bool,
) -> Optional[List[int]]:
    """Answer an outstanding offline alert under the gateway row lock: clear
    offline_notified_at when the gateway is stored ONLINE, and return the
    follow-up's recipients. None when there is nothing to answer, or when
    `send` is False (the kill switch still clears the state)."""
    from backend.database.models import GatewayStatus

    async with session_factory() as session:
        gw = await _locked_gateway(session, gateway_id)
        if gw is None or gw.offline_notified_at is None or gw.status != GatewayStatus.ONLINE:
            return None
        gw.offline_notified_at = None
        user_ids = (
            await _recipient_ids(session, gw.tenant_id, gateway_id, include_admins=True)
            if send else None
        )
        await session.commit()
    return user_ids


async def _offline(session_factory: Callable, gateway_id: str, recheck: bool) -> None:
    cooldown = _float_env("GATEWAY_OFFLINE_ALERT_COOLDOWN_SEC", 10800.0)
    async with _gateway_lock(gateway_id):
        try:
            user_ids, wait = await _decide_offline(session_factory, gateway_id, cooldown, recheck)
            if wait is not None:
                logger.info(
                    "Gateway offline alert suppressed by cooldown",
                    extra={"gateway_id": gateway_id},
                )
                # The last message may be "back online"; re-check at cooldown
                # expiry so a real outage hiding behind a flap is still reported.
                _arm_recheck(session_factory, gateway_id, max(0.0, wait))
                return
            if user_ids is None:
                return
            if recheck:
                title = "Gateway still offline"
                body = (f"Gateway {gateway_id} went offline again and is still down. "
                        f"Charging and remote stop are unavailable until it reconnects.")
            else:
                title = "Gateway offline"
                body = (f"Gateway {gateway_id} has been offline for a while. Charging and "
                        f"remote stop are unavailable until it reconnects.")
            await _fan_out(user_ids, "gateway_offline", title, body, "warning")
        except Exception:
            logger.exception(f"Gateway offline alert failed for {gateway_id}")


async def alert_gateway_offline(session_factory: Callable, gateway_id: str) -> None:
    """The gateway stayed offline past the debounce: bell + push (+ email)
    the tenant's CPOs and platform admins, at most once per cooldown."""
    if not enabled():
        return
    await _offline(session_factory, gateway_id, recheck=False)


def _arm_recheck(session_factory: Callable, gateway_id: str, wait: float) -> None:
    if gateway_id in _rechecks:
        return
    try:
        task = asyncio.get_running_loop().create_task(
            _recheck_offline(session_factory, gateway_id, wait)
        )
    except RuntimeError:
        return
    _rechecks[gateway_id] = task


async def _recheck_offline(session_factory: Callable, gateway_id: str, wait: float) -> None:
    """Cooldown expired after a suppressed offline alert: if the gateway is
    still stored OFFLINE and no offline alert is outstanding, say so."""
    me = asyncio.current_task()
    try:
        await asyncio.sleep(wait)
        if _rechecks.get(gateway_id) is me:
            del _rechecks[gateway_id]
        if not enabled():
            return
        await _offline(session_factory, gateway_id, recheck=True)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(f"Gateway offline re-check failed for {gateway_id}")
    finally:
        if _rechecks.get(gateway_id) is me:
            del _rechecks[gateway_id]


async def rearm_offline_rechecks(session_factory: Callable) -> int:
    """Backend startup: re-arm the "still offline" re-checks a restart
    dropped. One is pending exactly when a gateway is stored OFFLINE with no
    outstanding offline alert (none flagged, or a flag the gateway was seen
    after) but an earlier alert on record (an alert
    suppressed after a flap). It fires at that alert's cooldown expiry, never
    sooner than the offline debounce, so the retained `online` a reconnected
    gateway replays after the restart lands first. Gateways never alerted are
    left alone. Never raises; returns how many were armed."""
    from backend.database.models import Gateway, GatewayStatus
    from backend.services.mqtt_manager import GATEWAY_OFFLINE_NOTIFY_DELAY_SEC

    if not enabled():
        return 0
    cooldown = _float_env("GATEWAY_OFFLINE_ALERT_COOLDOWN_SEC", 10800.0)
    try:
        async with session_factory() as session:
            rows = (await session.execute(
                select(Gateway.id, Gateway.last_operator_alert_at)
                .where(
                    Gateway.status == GatewayStatus.OFFLINE,
                    or_(Gateway.offline_notified_at.is_(None),
                        Gateway.last_seen_at > Gateway.offline_notified_at),
                    Gateway.last_operator_alert_at.is_not(None),
                )
                .limit(_REARM_LIMIT)
            )).all()
    except Exception:
        logger.exception("Could not re-arm gateway offline re-checks")
        return 0
    now = _now()
    floor = max(0.0, GATEWAY_OFFLINE_NOTIFY_DELAY_SEC)
    for gateway_id, last in rows:
        _arm_recheck(session_factory, gateway_id, max(floor, cooldown - _age_sec(last, now)))
    return len(rows)


async def alert_gateway_online(session_factory: Callable, gateway_id: str) -> None:
    """Follow-up, only when an offline alert for this gateway was sent."""
    async with _gateway_lock(gateway_id):
        try:
            user_ids = await _decide_online(session_factory, gateway_id, send=enabled())
            if user_ids is None:
                return
            await _fan_out(
                user_ids, "gateway_online", "Gateway back online",
                f"Gateway {gateway_id} is reachable again.", "info",
            )
        except Exception:
            logger.exception(f"Gateway online follow-up failed for {gateway_id}")


async def _earlier_fault_at(
    session_factory: Callable, gateway_id: str, plug_id: Optional[int],
    event_type: str, before_id: int, since: datetime,
) -> Optional[datetime]:
    """created_at of the newest gateway_events row for the same (gateway,
    plug, event) written before row `before_id` and since `since`, or None.
    One indexed (gateway_id, created_at) lookup, LIMIT 1."""
    from backend.database.models import GatewayEvent

    plug_match = (GatewayEvent.plug_id.is_(None) if plug_id is None
                  else GatewayEvent.plug_id == plug_id)
    async with session_factory() as session:
        return (await session.execute(
            select(GatewayEvent.created_at)
            .where(GatewayEvent.gateway_id == gateway_id,
                   GatewayEvent.created_at >= since,
                   GatewayEvent.event_type == event_type,
                   plug_match,
                   GatewayEvent.id < before_id)
            .order_by(GatewayEvent.created_at.desc())
            .limit(1)
        )).scalar_one_or_none()


async def alert_safety_fault(
    session_factory: Callable, gateway_id: str, plug_id: Optional[int],
    event_type: str, severity: str, detail: Optional[str],
    exclude_user_ids: Iterable[int] = (),
    event_id: Optional[int] = None,
) -> None:
    """Bell + push (+ email) the tenant's CPOs about a safety fault or failed
    OTA, deduplicated per (gateway, plug, event) within the cooldown.
    exclude_user_ids: users who already got a notice for this same fault
    (the driver of the session the cutoff finalized). event_id: the
    gateway_events row just stored for this fault; when the in-memory
    cooldown has no entry (first fault, or after a restart), an earlier row
    for the same fault inside the cooldown suppresses the alert."""
    if event_type not in FAULT_TITLES or not enabled():
        return
    key = (gateway_id, plug_id, event_type)
    cooldown = _float_env("OPERATOR_ALERT_COOLDOWN_SEC", 3600.0)
    known = key in _last_fault_alert
    if not _cooled_down(_last_fault_alert.get(key), cooldown):
        logger.info("Operator fault alert suppressed by cooldown", extra={"event_type": event_type})
        return
    now = _last_fault_alert[key] = _now()
    if not known and event_id is not None and cooldown > 0:
        try:
            earlier = await _earlier_fault_at(
                session_factory, gateway_id, plug_id, event_type, event_id,
                now - timedelta(seconds=cooldown),
            )
        except Exception:
            logger.exception(f"Fault cooldown lookup failed for {gateway_id} ({event_type})")
            earlier = None
        if earlier is not None:
            # Restart inside the cooldown: this fault was already reported.
            _last_fault_alert[key] = _aware(earlier)
            logger.info("Operator fault alert suppressed by cooldown", extra={"event_type": event_type})
            return
    try:
        skip = set(exclude_user_ids)
        user_ids = [
            uid for uid in await _recipients(session_factory, gateway_id, include_admins=False)
            if uid not in skip
        ]
        where = f"plug {plug_id} on gateway {gateway_id}" if plug_id is not None else f"gateway {gateway_id}"
        body = f"{where}: {detail or event_type}"
        await _fan_out(
            user_ids, "gateway_fault", FAULT_TITLES[event_type], body, severity,
            plug_id=plug_id,
        )
    except Exception:
        logger.exception(f"Operator fault alert failed for {gateway_id} ({event_type})")
