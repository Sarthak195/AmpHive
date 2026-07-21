"""
MQTTManager collaborator: outbound command publishing.

Extracted verbatim from services/mqtt_manager.py (god-object split): the
retained plug-roster publish (`.../config`) and the per-plug command
publishers (ON/OFF, OTA trigger, SET_INTERVAL, SET_LIMITS). Mixed into
MQTTManager; see services/mqtt/__init__.py for why this is a mixin rather than
a delegating collaborator object.
"""
import json
import logging
from typing import Optional

import paho.mqtt.client as mqtt

logger = logging.getLogger("amphive.mqtt")


class MQTTCommandMixin:
    """Outbound plug-roster + per-plug command publishing."""

    # -----------------------------------------------------------------------
    # Plug roster (amphive/gateways/{gw}/config) — backend-pushed, retained
    # -----------------------------------------------------------------------

    def publish_plug_roster(self, gateway_id: str, plugs: list) -> None:
        """
        Publish the retained per-gateway plug roster on
        `amphive/gateways/{gw}/config` so the firmware builds its slot table
        proactively — add / re-IP / remove — instead of learning each plug
        lazily from the `local_ip` on an ON/OFF command (and instead of a single
        plug IP baked in at captive-portal setup).

        `plugs` is a list of `{plug_id, local_ip, max_current_a}` dicts
        (`max_current_a` may be None → firmware uses its default cap; the plug's
        display name is deliberately omitted — the firmware slot has no name and
        4 long names could overflow the device's 512-byte inbound buffer). Empty
        list is a valid roster (a gateway with no plugs → free all non-active
        slots on-device). Retained, so a rebooting gateway gets the current
        roster the instant it subscribes. Mirrors the retained `assign` publish;
        fire-and-forget (no wait_for_publish). The topic sits under the gateway's
        own subtree, so the existing `pattern readwrite amphive/gateways/%u/#`
        ACL already permits the gateway to subscribe it — no ACL change (SEC §8.5).
        """
        if self.client is None:
            return
        payload = {"v": 1, "plugs": plugs}
        self.client.publish(
            f"amphive/gateways/{gateway_id}/config",
            json.dumps(payload), qos=1, retain=True,
        )
        logger.info(
            "Published plug roster",
            extra={"gateway_id": gateway_id, "plug_count": len(plugs)},
        )

    async def _publish_roster_for_gateway(self, gateway_id: str) -> None:
        """Load a gateway's plugs and publish the retained roster (see
        `publish_plug_roster`). Used on gateway (re)connect and after a discovery
        upsert; the CPO plug-CRUD path builds the roster from its own request
        session instead (routers/cpo.py `_publish_gateway_roster`)."""
        from sqlalchemy import select

        from backend.database.models import Plug

        try:
            async with self.db_session_factory() as session:
                rows = (await session.execute(
                    select(Plug.id, Plug.local_ip, Plug.max_current_a)
                    .where(Plug.gateway_id == gateway_id)
                )).all()
            roster = [
                {"plug_id": pid, "local_ip": ip, "max_current_a": cap}
                for pid, ip, cap in rows
            ]
            self.publish_plug_roster(gateway_id, roster)
        except Exception as e:
            logger.error(
                "Failed to publish plug roster",
                extra={"gateway_id": gateway_id, "error": str(e)},
            )

    # -----------------------------------------------------------------------
    # Outbound command publisher
    # -----------------------------------------------------------------------

    def send_plug_command(self, gateway_id: str, plug_id: int, action: str, max_duration: int = 14400, max_kwh: float = 30.0, session_id: Optional[int] = None, local_ip: Optional[str] = None, max_current_a: Optional[float] = None, wait: bool = True) -> bool:
        """
        Sends an ON/OFF command to a specific plug registered under a gateway.
        Topic: amphive/gateways/{gateway_id}/plugs/{plug_id}/commands
        Payload: {"action": "ON"/"OFF", "max_duration_seconds": X, "max_kwh": Y}

        When `session_id` is given (session start), it is included as a string.
        The firmware persists it for crash recovery and echoes it back in
        telemetry so the backend can attribute a reading to the exact session
        rather than just "the active session on this plug".

        When `max_current_a` is given (session start ON), it is included as the
        plug's effective current cap (amps) for on-device enforcement — the plug
        measures real current and can trip its relay if it exceeds this. The
        caller resolves it via services/caps.py effective_plug_cap (the plug's
        own cap, or DEFAULT_PLUG_CAP_A). Older firmware ignores the extra field,
        so this is backward-safe; OFF/cleanup publishes omit it.

        When `local_ip` is given, it is included so a multi-plug gateway
        (TD#20) knows which physical plug to actuate — and can learn a plug it
        hasn't seen before (e.g. after a reboot) without a static on-device
        roster. The DB is the source of truth for `plugs.local_ip`; pass it on
        every ON/OFF. Older single-plug firmware ignores the extra field and
        falls back to its provisioned target plug, so this is backward-safe.

        `wait=False` skips the blocking wait for the broker ack — for
        best-effort publishes issued from the event loop (blocking it up to
        3 s per publish would stall every other coroutine).
        """
        topic = f"amphive/gateways/{gateway_id}/plugs/{plug_id}/commands"
        payload = {
            "action": action.upper(),
            "max_duration_seconds": max_duration,
            "max_kwh": max_kwh
        }
        if session_id is not None:
            payload["session_id"] = str(session_id)
        if local_ip:
            payload["local_ip"] = local_ip
        if max_current_a is not None:
            payload["max_current_a"] = max_current_a

        try:
            payload_str = json.dumps(payload)
            info = self.client.publish(topic, payload_str, qos=1)
            if not wait:
                logger.info(
                    "Published plug command (no-wait)",
                    extra={
                        "gateway_id": gateway_id, "plug_id": plug_id,
                        "action": payload["action"], "topic": topic,
                    },
                )
                return info.rc == mqtt.MQTT_ERR_SUCCESS
            info.wait_for_publish(timeout=3.0)
            logger.info(
                "Published plug command",
                extra={
                    "gateway_id": gateway_id, "plug_id": plug_id,
                    "action": payload["action"], "topic": topic, "session_id": session_id,
                },
            )
            return info.is_published()
        except Exception as e:
            logger.error(
                "Failed to publish plug command",
                extra={"gateway_id": gateway_id, "plug_id": plug_id, "topic": topic, "error": str(e)},
            )
            return False

    def send_gateway_ota(self, gateway_id: str, plug_id: int, firmware_url: str) -> bool:
        """
        Trigger an OTA firmware update on a gateway.

        The firmware subscribes only to the per-plug command topic
        (amphive/gateways/{gw}/plugs/+/commands), so the OTA command rides one
        of the gateway's plug topics; the firmware ignores the plug_id for
        OTA (it's a gateway-scoped action) and does not touch active_plug_id.
        Payload: {"action": "OTA", "url": "<http(s) url>"}. The gateway
        refuses the update while a session is active and reboots into the
        passive slot on success (rollback-protected).
        """
        topic = f"amphive/gateways/{gateway_id}/plugs/{plug_id}/commands"
        payload = {"action": "OTA", "url": firmware_url}
        try:
            payload_str = json.dumps(payload)
            info = self.client.publish(topic, payload_str, qos=1)
            info.wait_for_publish(timeout=3.0)
            logger.info(
                "Published OTA command",
                extra={
                    "gateway_id": gateway_id, "plug_id": plug_id,
                    "firmware_url": firmware_url, "topic": topic,
                },
            )
            return info.is_published()
        except Exception as e:
            logger.error(
                "Failed to publish OTA command",
                extra={"gateway_id": gateway_id, "plug_id": plug_id, "topic": topic, "error": str(e)},
            )
            return False

    def send_plug_interval(self, gateway_id: str, plug_id: int, interval_ms: int, wait: bool = False) -> bool:
        """
        Sends a SET_INTERVAL command to a specific plug registered under a gateway.
        Topic: amphive/gateways/{gateway_id}/plugs/{plug_id}/commands
        Payload: {"action": "SET_INTERVAL", "interval_ms": X}
        """
        topic = f"amphive/gateways/{gateway_id}/plugs/{plug_id}/commands"
        payload = {
            "action": "SET_INTERVAL",
            "interval_ms": interval_ms
        }

        try:
            payload_str = json.dumps(payload)
            info = self.client.publish(topic, payload_str, qos=1)
            if wait:
                info.wait_for_publish(timeout=3.0)
            logger.info(
                "Published interval command",
                extra={
                    "gateway_id": gateway_id, "plug_id": plug_id,
                    "interval_ms": interval_ms, "topic": topic,
                },
            )
            # wait=True actually confirms the PUBACK; wait=False only enqueued it,
            # so report the publish rc (mirrors send_plug_command).
            return info.is_published() if wait else info.rc == mqtt.MQTT_ERR_SUCCESS
        except Exception as e:
            logger.error(
                "Failed to publish interval command",
                extra={"gateway_id": gateway_id, "plug_id": plug_id, "topic": topic, "error": str(e)},
            )
            return False

    def send_plug_limits(self, gateway_id: str, plug_id: int, max_kwh: float, max_duration_seconds: int, local_ip: Optional[str] = None, max_current_a: Optional[float] = None, wait: bool = False) -> bool:
        """
        Sends a SET_LIMITS command to a specific plug registered under a gateway.
        Topic: amphive/gateways/{gateway_id}/plugs/{plug_id}/commands
        Payload: {"action": "SET_LIMITS", "max_kwh": X, "max_duration_seconds": Y, "local_ip": "<str>", "max_current_a": Z}

        Updates a RUNNING session's watchdog thresholds (energy + duration caps)
        in place. Unlike ON, the firmware does **not** re-read the meter baseline
        or touch start_energy_kwh/start_time_s/session_active/session_id — so
        billing is unaffected. The firmware ignores it when no session is active.
        `local_ip` targets the physical plug on a multi-plug gateway (TD#20),
        mirroring ON/OFF; the empty-string default is harmless for older
        single-plug firmware, which falls back to its provisioned target.

        When `max_current_a` is given, the firmware re-arms the running
        session's OVERCURRENT_CAP watchdog at it (and resets the debounce);
        omitted (None), the key is left out of the payload and the on-device
        cap is untouched — mirroring the ON path's optional cap. The caller
        resolves it via services/caps.py effective_plug_cap. Older firmware
        ignores the extra field, so this is backward-safe.
        """
        topic = f"amphive/gateways/{gateway_id}/plugs/{plug_id}/commands"
        payload = {
            "action": "SET_LIMITS",
            "max_kwh": max_kwh,
            "max_duration_seconds": max_duration_seconds,
            "local_ip": local_ip or "",
        }
        if max_current_a is not None:
            payload["max_current_a"] = max_current_a

        try:
            payload_str = json.dumps(payload)
            info = self.client.publish(topic, payload_str, qos=1)
            if wait:
                info.wait_for_publish(timeout=3.0)
            logger.info(
                "Published limits command",
                extra={
                    "gateway_id": gateway_id, "plug_id": plug_id,
                    "max_kwh": max_kwh, "max_duration_seconds": max_duration_seconds,
                    "topic": topic,
                },
            )
            # wait=True actually confirms the PUBACK; wait=False only enqueued it,
            # so report the publish rc (mirrors send_plug_command).
            return info.is_published() if wait else info.rc == mqtt.MQTT_ERR_SUCCESS
        except Exception as e:
            logger.error(
                "Failed to publish limits command",
                extra={"gateway_id": gateway_id, "plug_id": plug_id, "topic": topic, "error": str(e)},
            )
            return False
