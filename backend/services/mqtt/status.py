"""
MQTTManager collaborator: gateway online/offline status + reconnect
reconciliation.

Extracted verbatim from services/mqtt_manager.py (god-object split): the
`.../status` topic handler, the online/offline transition persist (+ driver
offline notice, plug-connectivity broadcast), and the reconnect-time OFF
republish for orphaned plugs (a lost/failed OFF while the gateway was still
connected, or the NVS crash-recovery resume path). Mixed into MQTTManager;
see services/mqtt/__init__.py for why this is a mixin rather than a delegating
collaborator object.

`notify` is imported at module level (not late-imported inside a method like
most other mqtt/*.py handlers) specifically so tests can patch it at
`backend.services.mqtt.status.notify` — the operator (CPO) orphan-OFF alert
in `_republish_off_for_orphaned_plugs` is this module's only notify() call
site and there is no circular-import hazard to dodge (backend.services.
notifications imports only backend.database.* at module load).
"""
import asyncio
import logging
from typing import Any, Dict, Optional

from backend.services.notifications import notify

logger = logging.getLogger("amphive.mqtt")


class MQTTStatusMixin:
    """Gateway status transitions + reconnect-time orphan-OFF reconciliation."""

    # -----------------------------------------------------------------------
    # Inbound status handler — updates gateway online/offline state in DB
    # -----------------------------------------------------------------------

    def _handle_gateway_status(self, gateway_id: str, payload: Dict[str, Any]):
        """
        Process a gateway status update (online/offline).
        Updates the gateway's status and last_seen_at in the database.
        """
        status = payload.get("status", "offline")
        # Firmware version rides on the `online` status payload ({"fw": "..."}).
        fw = payload.get("fw")
        fw = str(fw)[:32] if fw else None
        logger.info(
            "Gateway status update",
            extra={"gateway_id": gateway_id, "status": status, "fw": fw},
        )

        if self.db_session_factory and self.event_loop:
            asyncio.run_coroutine_threadsafe(
                self._persist_gateway_status(gateway_id, status, fw),
                self.event_loop,
            )

    async def _persist_gateway_status(self, gateway_id: str, status: str, firmware_version: Optional[str] = None):
        """Persist gateway online/offline status (+ reported fw) to the database."""
        from backend.database.models import Gateway, GatewayStatus

        # Whether this message flipped the stored liveness state. Subscriptions
        # re-issue on every connect, so a backend/broker reconnect replays the
        # retained status message; keying the offline-notify and the socket push
        # off a *transition* (rather than the raw status) keeps a retained replay
        # from re-notifying drivers or re-broadcasting (REC-08).
        became_online = False
        became_offline = False
        try:
            async with self.db_session_factory() as session:
                from sqlalchemy import select

                result = await session.execute(
                    select(Gateway).where(Gateway.id == gateway_id)
                )
                gateway = result.scalar_one_or_none()
                if gateway:
                    is_online = status == "online"
                    was_offline = gateway.status == GatewayStatus.OFFLINE
                    became_online = is_online and was_offline
                    became_offline = not is_online and not was_offline
                    gateway.status = GatewayStatus.ONLINE if is_online else GatewayStatus.OFFLINE
                    # Do NOT stamp last_seen_at from a status message: a retained
                    # `online` replayed on reconnect would falsely refresh
                    # liveness for a possibly-wedged gateway. Telemetry — the real
                    # heartbeat — is what stamps last_seen_at (REC-09).
                    # Only overwrite the recorded fw when the payload carried one
                    # (the LWT/offline message has no fw — don't clobber it).
                    if firmware_version:
                        gateway.firmware_version = firmware_version
                    await session.commit()
                    logger.info(
                        "Gateway DB status updated",
                        extra={"gateway_id": gateway_id, "status": status},
                    )
                else:
                    logger.warning(
                        "Gateway not found in DB, ignoring status update",
                        extra={"gateway_id": gateway_id},
                    )
        except Exception as e:
            logger.error(
                "Failed to persist gateway status",
                extra={"gateway_id": gateway_id, "error": str(e)},
            )
            return

        if status == "online":
            # The OFF sweep runs on every `online` (idempotent safety net), but
            # the CPO bell only fires on a real OFFLINE->ONLINE transition: a
            # retained `online` replay arrives on every backend/broker
            # reconnect, and re-alerting operators about the same plugs on
            # every backend restart is noise (32 bells by the end of the
            # 2026-08-03 outage-recovery morning). Same replay-vs-transition
            # reasoning as the REC-08 gate on the driver offline notice.
            await self._republish_off_for_orphaned_plugs(
                gateway_id, alert_operators=became_online
            )
            # Push the current plug roster (retained) so a gateway that just
            # (re)connected builds/reconciles its slot table without waiting for
            # a command. Fires on every `online` incl. a retained replay — the
            # retained roster makes that idempotent (not gated on became_online).
            await self._publish_roster_for_gateway(gateway_id)
        elif became_offline:
            # Only on a real ONLINE->OFFLINE transition — not a retained replay.
            await self._notify_drivers_gateway_offline(gateway_id)

        # Push connectivity to clients on both transition directions so charger
        # lists flip reachable/unreachable immediately, ahead of the
        # telemetry-timeout path (Faster-offline Lever 1).
        if became_online or became_offline:
            await self._broadcast_plug_connectivity(gateway_id, became_online)

    async def _notify_drivers_gateway_offline(self, gateway_id: str):
        """
        A gateway going offline (LWT) mid-session means telemetry — and with it
        billing and remote stop — is gone; the reaper will finalize the session
        after SESSION_STALE_TIMEOUT_SEC. Tell each affected driver now rather
        than letting them discover a frozen monitor.
        """
        from sqlalchemy import select

        from backend.database.models import ChargingSession, Plug, SessionStatus

        try:
            async with self.db_session_factory() as session:
                rows = (await session.execute(
                    select(ChargingSession.id, ChargingSession.user_id, Plug.id, Plug.name)
                    .join(Plug, ChargingSession.plug_id == Plug.id)
                    .where(
                        Plug.gateway_id == gateway_id,
                        ChargingSession.status == SessionStatus.ACTIVE,
                    )
                )).all()
        except Exception:
            logger.exception(f"Offline-notification query failed for gateway {gateway_id}")
            return

        from backend.services.notifications import notify
        for session_id, user_id, plug_id, plug_name in rows:
            await notify(
                user_id,
                "charger_offline",
                "Charger connection lost",
                f"{plug_name} went offline during your session. The plug's own "
                f"safety limits still apply; if it doesn't reconnect, the "
                f"session will be closed and billed for the energy recorded "
                f"so far.",
                severity="warning",
                plug_id=plug_id,
                session_id=session_id,
            )

    async def _broadcast_plug_connectivity(self, gateway_id: str, gateway_online: bool):
        """
        On a gateway online<->offline transition, emit plug_connectivity for each
        of its plugs so the frontend flags/clears the plug as unreachable at once,
        without waiting on the telemetry-timeout path (Faster-offline Lever 1).
        """
        from sqlalchemy import select

        from backend.database.models import Plug

        try:
            async with self.db_session_factory() as session:
                plug_ids = (await session.execute(
                    select(Plug.id).where(Plug.gateway_id == gateway_id)
                )).scalars().all()
        except Exception:
            logger.exception(f"plug_connectivity query failed for gateway {gateway_id}")
            return

        from backend.services.socketio_manager import emit_plug_connectivity
        for plug_id in plug_ids:
            await emit_plug_connectivity(plug_id, gateway_online)

    async def _republish_off_for_orphaned_plugs(
        self, gateway_id: str, alert_operators: bool = True
    ):
        """
        On gateway reconnect, re-send OFF to each of its plugs that has no
        ACTIVE session. OFF commands aren't retained, so a gateway that was
        dead when its session got finalized (e.g. by the session reaper) never
        received one — and its NVS crash recovery resumes the session on
        reboot with the relay ON and nobody billing (observed 2026-07-07).
        Idempotent: an OFF to an already-off plug is a no-op on the firmware.

        [Queued charge] A WAITING queued charge is NOT an ACTIVE session, so
        Scenario B (gateway + plug both lost power, power returns, the Tapo
        auto-resumes its relay, the gateway reboots and reconnects) would see
        "no ACTIVE session" and OFF the very plug the debounced auto-start is
        pending on. Skip a plug that has a live WAITING queued charge which is
        still MID-DEBOUNCE (not powered yet, or powered for < auto_start_delay):
        it lost power so it's already off, and re-OFFing it here would fight the
        pending auto-start. Leave it OFF and let the queue sweep
        (services/session_reaper.py reap_queued_starts_once) own the actual
        energize — a proper start with a hold, caps, and a fresh session_id once
        the debounce elapses. Plugs with no queued charge (and eligible/past-
        debounce ones the sweep is about to start) keep the existing orphan-OFF.

        [Operator alert] Genuinely orphan-OFF'd plugs (an actual force-OFF, not
        a skipped mid-debounce one) are worth a CPO's attention — it means the
        plug was left OCCUPIED-looking with no session backing it. After every
        plug's OFF publishes, every CPO of the gateway's tenant gets one
        `orphan_off` bell notification per (plug, CPO) pair via notify() —
        but only when `alert_operators` is set: the caller passes
        became_online, so a retained `online` replay (re-delivered on every
        backend/broker reconnect) still runs the idempotent OFF sweep without
        re-alerting operators on every backend restart. This
        fires AFTER the `async with` session block closes (notify() opens its
        own transaction via async_session_factory — it must not nest inside
        this one), and the CPO-lookup query itself runs *inside* the session
        but only after the REC-06 publish loop below, never between the ACTIVE
        snapshot and the publishes (see the REC-06 note there for why).
        """
        from backend.database.models import (
            ChargingSession,
            Gateway,
            Plug,
            QueuedCharge,
            QueuedChargeStatus,
            SessionStatus,
            Tenant,
            User,
            UserRole,
        )
        from backend.services.session_lifecycle import plug_is_powered
        from backend.services.session_start import auto_start_delay

        off_plugs: list = []  # [(plug_id, plug_name), ...] actually OFF'd this call
        cpo_user_ids: list = []
        try:
            async with self.db_session_factory() as session:
                from datetime import datetime, timedelta, timezone

                from sqlalchemy import select

                plug_result = await session.execute(
                    select(Plug.id, Plug.local_ip, Plug.name).where(Plug.gateway_id == gateway_id)
                )
                plug_rows = plug_result.all()
                if not plug_rows:
                    return
                # {plug_id: local_ip} — the OFF carries local_ip so a rebooted
                # multi-plug gateway can learn the plug and actuate it (TD#20).
                plug_ips = {pid: ip for pid, ip, _name in plug_rows}
                plug_names = {pid: name for pid, _ip, name in plug_rows}

                active_result = await session.execute(
                    select(ChargingSession.plug_id).where(
                        ChargingSession.plug_id.in_(list(plug_ips.keys())),
                        ChargingSession.status == SessionStatus.ACTIVE,
                    )
                )
                active_plug_ids = set(active_result.scalars().all())

                # [Queued charge] Plugs with a live WAITING queued charge that is
                # still mid-debounce — leave these OFF for the queue sweep to
                # energize properly, don't orphan-OFF them here. Computed BEFORE
                # the synchronous publish loop so no await splits the snapshot.
                now = datetime.now(timezone.utc)
                queued_rows = await session.execute(
                    select(Plug, Tenant)
                    .join(QueuedCharge, QueuedCharge.plug_id == Plug.id)
                    .join(Tenant, Tenant.id == QueuedCharge.tenant_id)
                    .where(
                        QueuedCharge.plug_id.in_(list(plug_ips.keys())),
                        QueuedCharge.status == QueuedChargeStatus.WAITING,
                    )
                )
                queued_hold_plug_ids = set()
                for plug, tenant in queued_rows.all():
                    powered_since = plug.powered_since
                    if powered_since is not None and powered_since.tzinfo is None:
                        powered_since = powered_since.replace(tzinfo=timezone.utc)
                    mid_debounce = (
                        not plug_is_powered(plug, now)
                        or powered_since is None
                        or (now - powered_since)
                        < timedelta(minutes=auto_start_delay(tenant, plug))
                    )
                    if mid_debounce:
                        queued_hold_plug_ids.add(plug.id)

                # [REC-06] Publish each OFF INSIDE the same session, with no
                # await between the ACTIVE snapshot above and these synchronous
                # publishes: that leaves no yield point for a session started in
                # the gap to commit and then be wrongly killed. (Previously the
                # snapshot was taken, the session closed — an await boundary —
                # and only then were OFFs published, racing a fresh start.) The
                # [Operator alert] CPO lookup below runs AFTER this loop for the
                # exact same reason — it's an await, so it must not sit between
                # the snapshot and these synchronous publishes either.
                for plug_id, local_ip in plug_ips.items():
                    if plug_id not in active_plug_ids and plug_id not in queued_hold_plug_ids:
                        # wait=False: we're on the event loop — don't block it on
                        # the broker ack for a best-effort cleanup publish.
                        self.send_plug_command(gateway_id, plug_id, "OFF", local_ip=local_ip, wait=False)
                        off_plugs.append((plug_id, plug_names.get(plug_id)))
                        logger.info(
                            "Republished OFF on reconnect (no ACTIVE session)",
                            extra={"gateway_id": gateway_id, "plug_id": plug_id},
                        )

                # [Operator alert] Only look up CPOs to notify when something
                # was actually OFF'd on a real transition — the common cases
                # (a clean reconnect with no orphans, or a retained replay)
                # skip this query entirely.
                if off_plugs and alert_operators:
                    cpo_result = await session.execute(
                        select(User.id)
                        .join(Gateway, Gateway.tenant_id == User.tenant_id)
                        .where(Gateway.id == gateway_id, User.role == UserRole.CPO)
                    )
                    cpo_user_ids = list(cpo_result.scalars().all())
        except Exception as e:
            logger.error(
                "OFF republish on reconnect failed",
                extra={"gateway_id": gateway_id, "error": str(e)},
            )
            return

        # [Operator alert] Fired after the session block above closes — notify()
        # persists its own Notification row + Socket.io push per call and never
        # raises, so one CPO's failed delivery can't affect another's.
        for plug_id, plug_name in off_plugs:
            for cpo_id in cpo_user_ids:
                await notify(
                    cpo_id,
                    "orphan_off",
                    "Plug auto-recovered",
                    f"{plug_name or plug_id} on gateway {gateway_id} had no "
                    f"active session when it reconnected — force-OFF "
                    f"republished to prevent an orphaned charge.",
                    severity="warning",
                    plug_id=plug_id,
                )
