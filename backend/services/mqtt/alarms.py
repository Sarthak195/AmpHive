"""
MQTTManager collaborator: inbound gateway alarm/event handling.

Extracted verbatim from services/mqtt_manager.py (god-object split): parses
the `.../alarms` topic (safety cutoffs + OTA lifecycle notices + the fw 2.8.0
boot / plug-reachability diagnostics), persists a
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
import math
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger("amphive.mqtt")

# GatewayEvent.detail is String(255) (database/models.py). Postgres rejects an
# over-long value outright — and the except in _persist_gateway_event would
# then drop the whole event — so every detail is capped to the column size.
_DETAIL_MAX_LEN = 255

# Characters allowed through _safe_token (fw version strings, reset-reason
# names); every other run of characters collapses to "_".
_TOKEN_UNSAFE = re.compile(r"[^A-Za-z0-9._+-]+")


def _safe_float(v: Any) -> Optional[float]:
    """Best-effort float() for an optional alarm-payload field; None on
    missing/malformed/non-finite so a bad frame degrades to the static
    fallback detail text instead of crashing the handler."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        return None
    return f if math.isfinite(f) else None


def _safe_int(v: Any, lo: int, hi: int) -> Optional[int]:
    """Best-effort int for an optional, untrusted alarm-payload field: rounded,
    None when missing/malformed/non-finite/outside [lo, hi]. A JSON bool is
    not a number here (Python would happily treat true as 1)."""
    if isinstance(v, bool):
        return None
    f = _safe_float(v)
    if f is None:
        return None
    i = int(round(f))
    return i if lo <= i <= hi else None


def _safe_token(v: Any, max_len: int) -> Optional[str]:
    """A short identifier-like string (fw version, reset-reason name) from an
    untrusted field: letters/digits/`._+-` only, capped at max_len; None when
    missing or nothing usable is left."""
    if v is None or isinstance(v, (dict, list, bool)):
        return None
    s = _TOKEN_UNSAFE.sub("_", str(v)[: max_len * 4]).strip("_")[:max_len]
    return s or None


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _gateway_boot_detail(payload: Dict[str, Any]) -> str:
    """Human-readable detail for the fw >= 2.8.0 boot report, e.g. "Gateway
    booted (fw 2.8.0-direct): reset reason brownout [15], boot #42, Wi-Fi RSSI
    -79 dBm after 3 attempts, last Wi-Fi failure reason 2, min free heap
    123456 B." Every field is optional and untrusted: a missing/malformed one
    just drops its clause. `uptime_s` is accepted but not shown (it is always
    a few seconds at boot)."""
    fw = _safe_token(payload.get("fw"), 32)
    reason = _safe_token(payload.get("reset_reason"), 32)
    code = _safe_int(payload.get("reset_code"), 0, 1000)
    boot_count = _safe_int(payload.get("boot_count"), 0, 10**9)
    rssi = _safe_int(payload.get("wifi_rssi"), -127, 0)
    attempts = _safe_int(payload.get("wifi_attempts"), 1, 10**6)
    # ESP-IDF Wi-Fi disconnect reason codes start at 1, so 0 means "none
    # since boot" and is left out rather than printed.
    wifi_reason = _safe_int(payload.get("last_wifi_reason"), 1, 1000)
    min_heap = _safe_int(payload.get("min_free_heap"), 0, 10**9)

    parts: List[str] = []
    if reason and code is not None:
        parts.append(f"reset reason {reason} [{code}]")
    elif reason:
        parts.append(f"reset reason {reason}")
    elif code is not None:
        parts.append(f"reset code {code}")
    if boot_count is not None:
        parts.append(f"boot #{boot_count}")
    if rssi is not None:
        wifi = f"Wi-Fi RSSI {rssi} dBm"
        if attempts is not None:
            wifi += f" after {_plural(attempts, 'attempt')}"
        parts.append(wifi)
    elif attempts is not None:
        parts.append(f"Wi-Fi up after {_plural(attempts, 'attempt')}")
    if wifi_reason is not None:
        parts.append(f"last Wi-Fi failure reason {wifi_reason}")
    if min_heap is not None:
        parts.append(f"min free heap {min_heap} B")

    head = f"Gateway booted (fw {fw})" if fw else "Gateway booted"
    return f"{head}: {', '.join(parts)}." if parts else f"{head}."


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
        # [Unmetered consumption] Firmware's own one-shot offline-consumption
        # report (tapo_plug_reconcile_idle_baseline, fw >= 2.4.0) — an
        # accountability signal on an otherwise-healthy plug, same severity
        # class as UNAUTHORIZED_ON (below), not a hardware fault.
        "UNMETERED_CONSUMPTION": "warning",
        # [fw 2.8.0 diagnostics] Field-forensics reports, never faults: the
        # boot report (why the chip reset) and a plug-side LAN outage/recovery.
        # None of them finalizes, enters maintenance or rings a bell — they
        # only land in the passive gateway_events feed like every alarm.
        "GATEWAY_BOOT": "info",
        "PLUG_UNREACHABLE": "warning",
        "PLUG_RECOVERED": "info",
    }

    _EVENT_DETAIL = {
        "UNAUTHORIZED_ON": "Plug switched ON with no active session (physical button / app / stale resume) — forced OFF locally.",
        "THERMAL_CUTOFF": "Plug reported overheat — session cut off locally.",
        "OVERCURRENT_CUTOFF": "Plug reported over-current — session cut off locally.",
        "OVERCURRENT_CAP": "Plug drew more than its configured current cap — session stopped locally.",
        "LOCAL_LIMIT_CUTOFF": "Session hit its energy/duration limit — the gateway agent cut the plug off locally.",
        # Overridden below with the actual estimate when the payload carries
        # `kwh` (it always should, on fw >= 2.4.0) — kept as a fallback for a
        # malformed/future payload that omits it.
        "UNMETERED_CONSUMPTION": "Plug's energy counters advanced with no active session covering it — possible unmetered use while the gateway was unreachable.",
        # Fallbacks for the fw 2.8.0 diagnostics when their numbers are
        # missing/malformed; _handle_gateway_alarm builds the full text.
        "GATEWAY_BOOT": "Gateway booted.",
        "PLUG_UNREACHABLE": "Gateway can't reach the plug over the LAN - readings paused until it answers.",
        "PLUG_RECOVERED": "Plug reachable again.",
    }

    # [fw 2.8.0 diagnostics] Forensic records, not alerts: stored already
    # acknowledged so they stay out of the unacknowledged set the console
    # fetches. That set is windowed server-side (CpoAlerts asks for the newest
    # 50, newest first, and drops info rows only in the browser), so a boot
    # report on every chip reset — or a brownout boot loop — would otherwise
    # crowd an older unacknowledged critical (UNAUTHORIZED_ON, ...) out of the
    # alert strip, and tick the Health nav badge each time. The rows are still
    # persisted, broadcast and logged; the Health page shows them with
    # "Unacknowledged only" unticked. PLUG_UNREACHABLE is a real warning and
    # stays unacknowledged.
    _PRE_ACKED_EVENT_TYPES = frozenset({"GATEWAY_BOOT", "PLUG_RECOVERED"})

    def _handle_gateway_alarm(self, gateway_id: str, payload: Dict[str, Any]):
        """
        Process an alarm/event from a gateway (topic .../alarms). Two shapes:
          {"error": "UNAUTHORIZED_ON", "plug_id": 1}   # safety alarm/fault
          {"event": "OTA_STARTED"}                       # OTA lifecycle notice
        Persists a GatewayEvent (audit + CPO feed) and broadcasts it so a live
        client can react (e.g. warn the driver, flash the operator's alert feed).

        [Unmetered consumption] `{"error":"UNMETERED_CONSUMPTION","plug_id":N,
        "kwh":<estimate>,"today_kwh":<f>,"month_kwh":<f>}` is the firmware's
        own one-shot offline-consumption report (tapo_plug_reconcile_idle_
        baseline, fw >= 2.4.0, docs/MQTT_CONTRACT.md) — the `kwh` estimate is
        folded into a human-readable `detail` string here (not a schema
        change to _persist_gateway_event, which every other alarm type also
        shares).

        [fw 2.8.0 diagnostics] Three more `detail`-folded shapes, all
        informational (no finalize / maintenance / bell):
          {"event":"GATEWAY_BOOT","fw":..,"reset_reason":..,"reset_code":..,
           "boot_count":..,"wifi_rssi":..,"last_wifi_reason":..,
           "wifi_attempts":..,"min_free_heap":..,"uptime_s":..}
          {"error":"PLUG_UNREACHABLE","plug_id":N,"failures":N}
          {"event":"PLUG_RECOVERED","plug_id":N,"down_s":N}
        Values are untrusted: coerced/sanitized field by field, a bad one just
        drops its clause, and every detail is capped at _DETAIL_MAX_LEN.
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

        if event_type == "UNMETERED_CONSUMPTION":
            estimated_kwh = _safe_float(payload.get("kwh"))
            if estimated_kwh is not None:
                detail = (
                    f"Plug consumed an estimated {estimated_kwh:.3f} kWh with no "
                    f"billed session covering it — counters advanced while the "
                    f"gateway couldn't report (possible unauthorized use)."
                )
        elif event_type == "GATEWAY_BOOT":
            detail = _gateway_boot_detail(payload)
        elif event_type == "PLUG_UNREACHABLE":
            failures = _safe_int(payload.get("failures"), 1, 10**6)
            if failures is not None:
                detail = (
                    f"Gateway can't reach the plug over the LAN "
                    f"({_plural(failures, 'failed poll')} in a row) - readings "
                    f"paused until it answers."
                )
        elif event_type == "PLUG_RECOVERED":
            down_s = _safe_int(payload.get("down_s"), 0, 10**9)
            if down_s is not None:
                detail = f"Plug reachable again after ~{down_s} s."

        if detail is not None:
            detail = detail[:_DETAIL_MAX_LEN]

        plug_id = payload.get("plug_id")
        try:
            plug_id = int(plug_id) if plug_id is not None else None
        except (ValueError, TypeError, OverflowError):  # OverflowError: JSON Infinity
            plug_id = None

        logger.warning(
            "Gateway alarm",
            extra={
                "gateway_id": gateway_id,
                "plug_id": plug_id,
                "event_type": event_type,
                "severity": severity,
                # Carries e.g. the GATEWAY_BOOT reset reason, so the backend
                # log alone answers "why did the gateway restart?".
                "detail": detail,
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

        from backend.database.models import Gateway, GatewayEvent, Plug
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

                # Captured now (not read off `gw` after the block closes) —
                # an AsyncSession expires instance attributes on commit by
                # default, so a post-commit `gw.tenant_id` read could need a
                # lazy-load with no active session/greenlet to run it in.
                tenant_id = gw.tenant_id

                # Ownership check (mirrors services/mqtt/telemetry.py's
                # "the payload's plug_id must name a plug of this gateway"):
                # broker ACLs scope *topics*, not payload claims, so a gateway
                # with valid creds for its OWN alarms topic could otherwise put
                # ANOTHER tenant's plug_id in the payload and force-finalize /
                # maintenance / unmetered-notify a victim plug (plug ids are
                # small sequential ints). Resolve the plug in the same session
                # and require it to belong to THIS gateway; a foreign or unknown
                # plug_id is spoofed, so drop it to None. That degrades the alarm
                # to a gateway-level event — persisted + broadcast on the
                # attacker's OWN tenant exactly as a genuine plug_id=None event
                # already is — and every plug-scoped action below is gated on
                # `plug_id is not None`, so none of them touch the victim.
                if plug_id is not None:
                    plug = (await session.execute(
                        select(Plug).where(Plug.id == plug_id)
                    )).scalar_one_or_none()
                    if plug is None or plug.gateway_id != gateway_id:
                        logger.warning(
                            "Alarm plug ownership mismatch — dropping foreign plug_id",
                            extra={
                                "gateway_id": gateway_id,
                                "plug_id": plug_id,
                                "actual_owner_gateway_id": plug.gateway_id if plug else None,
                                "event_type": event_type,
                            },
                        )
                        plug_id = None

                event = GatewayEvent(
                    tenant_id=tenant_id,
                    gateway_id=gateway_id,
                    plug_id=plug_id,
                    event_type=event_type,
                    severity=severity,
                    detail=detail,
                    acknowledged=event_type in self._PRE_ACKED_EVENT_TYPES,
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

        # [Unmetered consumption] Bell-notify every CPO of the tenant — an
        # accountability signal an operator should see promptly, not just in
        # the passive gateway_events feed. Mirrors the orphan_off CPO-notify
        # pattern in services/mqtt/status.py._republish_off_for_orphaned_plugs.
        # Fires for BOTH sources of this event type: the firmware's own
        # one-shot report (via _handle_gateway_alarm above) and the backend's
        # continuous cross-check (services/mqtt/telemetry.py._persist_telemetry)
        # — both funnel through this one function.
        if event_type == "UNMETERED_CONSUMPTION" and plug_id is not None:
            await self._notify_cpos_unmetered_consumption(tenant_id, gateway_id, plug_id, detail)

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

    async def _notify_cpos_unmetered_consumption(self, tenant_id: Optional[int], gateway_id: str,
                                                  plug_id: int, detail: Optional[str]):
        """Bell-notify every CPO of the tenant about an UNMETERED_CONSUMPTION
        event (called from _persist_gateway_event; see the call site there).
        Best-effort: notify() itself never raises (backend/services/
        notifications.py), and a gateway with no resolved tenant (unclaimed
        inventory — shouldn't happen for a live/telemetry-producing gateway,
        but guarded anyway) just skips."""
        if tenant_id is None:
            return

        from sqlalchemy import select

        from backend.database.models import Plug, User, UserRole

        try:
            async with self.db_session_factory() as session:
                plug_name = (await session.execute(
                    select(Plug.name).where(Plug.id == plug_id)
                )).scalar_one_or_none()
                cpo_ids = (await session.execute(
                    select(User.id).where(User.tenant_id == tenant_id, User.role == UserRole.CPO)
                )).scalars().all()
        except Exception:
            logger.exception(
                f"Unmetered-consumption CPO lookup failed for plug {plug_id} (gateway {gateway_id})"
            )
            return

        body = detail or (
            f"{plug_name or plug_id} shows energy use with no billed session covering it."
        )
        from backend.services.notifications import notify
        for cpo_id in cpo_ids:
            await notify(
                cpo_id, "unmetered_consumption", "Unmetered consumption detected",
                body, severity="warning", plug_id=plug_id,
            )
