"""
MQTTManager collaborator: inbound gateway alarm/event handling.

Extracted verbatim from services/mqtt_manager.py (god-object split): parses
the `.../alarms` topic (safety cutoffs + OTA lifecycle notices), persists a
GatewayEvent + broadcasts it, finalizes the session a hardware/agent cutoff
already stopped locally, and — for genuine hardware faults only — force-enters
the plug into MAINTENANCE. Mixed into MQTTManager; see services/mqtt/__init__.py
for why this is a mixin rather than a delegating collaborator object.

AUTO_MAINTENANCE_ON_CRITICAL_ALARM lives in services/mqtt_manager.py (the
facade module), not here — tests monkeypatch it there — so it's read via a
fresh `from backend.services.mqtt_manager import ...` at call time (this
codebase's existing "import here to avoid circular imports" convention)
instead of a module-level import, so it always sees the live value.
"""
import asyncio
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("amphive.mqtt")


class MQTTAlarmMixin:
    """Inbound alarm/event handling: persist + broadcast + finalize/maintenance."""

    # Map a firmware alarm/event string to a UI severity. Safety cutoffs and an
    # unauthorized energize are operator-critical; OTA lifecycle notices are
    # informational. Unknown strings default to "warning" so nothing is silently
    # swallowed.
    _EVENT_SEVERITY = {
        "THERMAL_CUTOFF": "critical",
        "OVERCURRENT_CUTOFF": "critical",
        # OVERCURRENT_CAP is a soft/policy cap trip (the car drew more than the
        # operator-set per-plug cap, below the P110's own hardware cutoff) — a
        # warning, not a hardware fault.
        "OVERCURRENT_CAP": "warning",
        # LOCAL_LIMIT_CUTOFF is the software agent's local watchdog hitting the
        # session's own kWh/duration limit — an expected end-of-session (the
        # agent cut the plug OFF locally), not a fault.
        "LOCAL_LIMIT_CUTOFF": "info",
        "UNAUTHORIZED_ON": "critical",
        "OTA_STARTED": "info",
        "OTA_OK_REBOOTING": "info",
        "OTA_FAILED": "warning",
        "OTA_REFUSED_SESSION_ACTIVE": "info",
        "OTA_START_FAILED": "warning",
    }

    _EVENT_DETAIL = {
        "UNAUTHORIZED_ON": "Plug switched ON with no active session (physical button / app / stale resume) — forced OFF locally.",
        "THERMAL_CUTOFF": "Plug reported overheat — session cut off locally.",
        "OVERCURRENT_CUTOFF": "Plug reported over-current — session cut off locally.",
        "OVERCURRENT_CAP": "Plug drew more than its configured current cap — session stopped locally.",
        "LOCAL_LIMIT_CUTOFF": "Session hit its energy/duration limit — the gateway agent cut the plug off locally.",
    }

    def _handle_gateway_alarm(self, gateway_id: str, payload: Dict[str, Any]):
        """
        Process an alarm/event from a gateway (topic .../alarms). Two shapes:
          {"error": "UNAUTHORIZED_ON", "plug_id": 1}   # safety alarm/fault
          {"event": "OTA_STARTED"}                       # OTA lifecycle notice
        Persists a GatewayEvent (audit + CPO feed) and broadcasts it so a live
        client can react (e.g. warn the driver, flash the operator's alert feed).
        """
        event_type = payload.get("error") or payload.get("event")
        if not event_type:
            logger.warning(
                "Alarm missing error/event field",
                extra={"gateway_id": gateway_id, "payload": payload},
            )
            return

        event_type = str(event_type)[:48]
        severity = self._EVENT_SEVERITY.get(event_type, "warning")
        detail = self._EVENT_DETAIL.get(event_type)

        plug_id = payload.get("plug_id")
        try:
            plug_id = int(plug_id) if plug_id is not None else None
        except (ValueError, TypeError):
            plug_id = None

        logger.warning(
            "Gateway alarm",
            extra={
                "gateway_id": gateway_id,
                "plug_id": plug_id,
                "event_type": event_type,
                "severity": severity,
            },
        )

        if self.db_session_factory and self.event_loop:
            asyncio.run_coroutine_threadsafe(
                self._persist_gateway_event(gateway_id, plug_id, event_type, severity, detail),
                self.event_loop,
            )

    async def _persist_gateway_event(self, gateway_id: str, plug_id: Optional[int],
                                     event_type: str, severity: str, detail: Optional[str]):
        """Store the event (tenant resolved from the gateway) and broadcast it."""
        from sqlalchemy import select

        from backend.database.models import Gateway, GatewayEvent
        from backend.services.mqtt_manager import AUTO_MAINTENANCE_ON_CRITICAL_ALARM

        try:
            async with self.db_session_factory() as session:
                gw = (await session.execute(
                    select(Gateway).where(Gateway.id == gateway_id)
                )).scalar_one_or_none()
                if not gw:
                    logger.warning(
                        "Alarm for unknown gateway, dropping event",
                        extra={"gateway_id": gateway_id},
                    )
                    return

                event = GatewayEvent(
                    tenant_id=gw.tenant_id,
                    gateway_id=gateway_id,
                    plug_id=plug_id,
                    event_type=event_type,
                    severity=severity,
                    detail=detail,
                )
                session.add(event)
                await session.commit()
                event_id = event.id
                created_at = event.created_at
        except Exception as e:
            logger.error(
                "Failed to persist gateway event",
                extra={"gateway_id": gateway_id, "plug_id": plug_id, "error": str(e)},
            )
            return

        # Broadcast to connected clients (best-effort; import late to avoid a
        # circular import at module load).
        try:
            from backend.services.socketio_manager import emit_gateway_alarm
            await emit_gateway_alarm({
                "id": event_id,
                "gateway_id": gateway_id,
                "plug_id": plug_id,
                "event_type": event_type,
                "severity": severity,
                "detail": detail,
                "created_at": created_at.isoformat() if created_at else None,
            })
        except Exception as e:
            logger.error(
                "Failed to broadcast gateway alarm",
                extra={"gateway_id": gateway_id, "plug_id": plug_id, "error": str(e)},
            )

        # A finalize-worthy alarm means the firmware already forced the relay OFF
        # and cleared its local session — finalize the backend session to match
        # (bills the recorded energy, frees the plug, notifies the driver).
        # Previously the session sat ACTIVE until the reaper noticed, and the
        # driver never learned why charging stopped. This includes OVERCURRENT_CAP
        # (a soft cap trip): the firmware stopped charging, so the backend must
        # finalize too — otherwise the plug is orphaned OCCUPIED until the reaper.
        if plug_id is not None and event_type in self._FINALIZE_ALARM_REASONS:
            await self._finalize_session_after_cutoff(plug_id, event_type)

        # Fault console (additive): hardware safety cutoffs ALSO auto-enter the
        # plug into MAINTENANCE so a NEW session can't start until an operator
        # clears it (session-start already 409s any non-AVAILABLE plug).
        # Deliberately sequenced AFTER the finalize call above, in this same
        # coroutine — not an independently scheduled task — so the MAINTENANCE
        # flip can't be raced/overwritten by finalize_charging_session's own
        # `plug.status = AVAILABLE`. Runs even when no session was found above
        # (a cutoff with no active session still needs the plug taken out of
        # service). Env-gated via AUTO_MAINTENANCE_ON_CRITICAL_ALARM.
        # OVERCURRENT_CAP is deliberately EXCLUDED (not in the maintenance set):
        # it's a policy limit on a HEALTHY plug, so the plug stays AVAILABLE after
        # finalize rather than forcing an operator to clear maintenance each trip.
        if (
            AUTO_MAINTENANCE_ON_CRITICAL_ALARM
            and plug_id is not None
            and event_type in self._MAINTENANCE_ALARM_REASONS
        ):
            await self._auto_enter_maintenance(plug_id, event_type)

    # Alarms after which the firmware has already stopped charging locally, so the
    # backend must finalize the session to match. The value is the finalize reason,
    # which routes the driver notification (see session_lifecycle.finalize_*).
    _FINALIZE_ALARM_REASONS = {
        "THERMAL_CUTOFF": "safety cutoff: plug reported overheat",
        "OVERCURRENT_CUTOFF": "safety cutoff: plug reported over-current",
        "OVERCURRENT_CAP": "current cap exceeded: plug drew over its configured limit",
        # The software agent's local watchdog (agent/amphive_agent/core.py)
        # already cut the plug OFF and cleared its local session — finalize so
        # the session bills and the driver is notified instead of the session
        # orphaning ACTIVE until the reaper. A healthy plug: no maintenance.
        "LOCAL_LIMIT_CUTOFF": "limit reached: session hit its energy/duration limit",
    }

    # The subset that also takes the plug OUT OF SERVICE — genuine hardware faults
    # only. A cap trip (OVERCURRENT_CAP) is intentionally NOT here.
    _MAINTENANCE_ALARM_REASONS = {"THERMAL_CUTOFF", "OVERCURRENT_CUTOFF"}

    async def _finalize_session_after_cutoff(self, plug_id: int, event_type: str):
        """Finalize the ACTIVE session (if any) on a plug the firmware just cut
        off. Race-safe: finalize row-locks and re-checks ACTIVE, so a
        concurrent user stop / reaper settles exactly once."""
        from sqlalchemy import select

        from backend.database.models import ChargingSession, SessionStatus
        from backend.services.session_lifecycle import finalize_charging_session

        try:
            async with self.db_session_factory() as db:
                session_id = (await db.execute(
                    select(ChargingSession.id).where(
                        ChargingSession.plug_id == plug_id,
                        ChargingSession.status == SessionStatus.ACTIVE,
                    )
                )).scalar_one_or_none()
            if session_id is None:
                return
            async with self.db_session_factory() as db:
                outcome = await finalize_charging_session(
                    db, session_id, reason=self._FINALIZE_ALARM_REASONS[event_type]
                )
            if outcome is not None:
                logger.warning(
                    f"Finalized session {session_id} after {event_type} on plug {plug_id}"
                )
        except Exception:
            logger.exception(f"Post-cutoff finalize failed for plug {plug_id} ({event_type})")

    async def _auto_enter_maintenance(self, plug_id: int, event_type: str):
        """
        Force a plug into MAINTENANCE after a hardware safety cutoff (called
        from _persist_gateway_event, right after _finalize_session_after_cutoff
        — see the ordering note there). The firmware has already force-OFF'd
        the relay locally — this only stops a *new* session from being started
        on the plug (session-start 409s any non-AVAILABLE plug) until an
        operator clears it via POST /api/cpo/plugs/{id}/maintenance. No-op if
        the plug is unknown or already MAINTENANCE.
        """
        from sqlalchemy import select

        from backend.database.models import Plug, PlugStatus

        try:
            async with self.db_session_factory() as session:
                plug = (await session.execute(
                    select(Plug).where(Plug.id == plug_id)
                )).scalar_one_or_none()
                if plug is None:
                    logger.warning(
                        "Auto-maintenance: plug not found, skipping",
                        extra={"plug_id": plug_id},
                    )
                    return
                if plug.status == PlugStatus.MAINTENANCE:
                    return
                plug.status = PlugStatus.MAINTENANCE
                await session.commit()
        except Exception as e:
            logger.error(
                "Auto-maintenance failed",
                extra={"plug_id": plug_id, "error": str(e)},
            )
            return

        logger.warning(
            "Plug auto-set to MAINTENANCE after safety alarm (operator must clear it)",
            extra={"plug_id": plug_id, "event_type": event_type},
        )
        try:
            from backend.services.socketio_manager import emit_plug_status
            await emit_plug_status(plug_id, PlugStatus.MAINTENANCE.value)
        except Exception as e:
            logger.error(
                "Failed to broadcast auto-maintenance plug_status",
                extra={"plug_id": plug_id, "error": str(e)},
            )
