"""
MQTTManager collaborator: inbound firmware log-line diagnostics.

Firmware >= 2.1.0-direct forwards its own WARN/ERROR ESP-IDF log lines as
PLAIN TEXT (not JSON) to `amphive/gateways/{gw}/logs`, QoS 0 (see
firmware/main/main.c log_forward_task/log_line_level and
docs/MQTT_CONTRACT.md). This is a diagnostics feed — persist-only, no
notify()/socket fan-out (unlike alarms.py, which drives the operator alert
feed + driver session finalization). Mixed into MQTTManager; see
services/mqtt/__init__.py for why this is a mixin rather than a delegating
collaborator object.
"""
import asyncio
import logging

logger = logging.getLogger("amphive.mqtt")

# Mirrors firmware's LOG_FWD_LINE_MAX (main.c) — the firmware itself already
# truncates to this before publishing, so this is a defensive re-truncation
# (a stray non-firmware publisher on this topic must not overflow the
# gateway_logs.message column, capped at 220 in the model/migration).
LOG_LINE_MAX = 200


class MQTTLogsMixin:
    """Inbound firmware log-line handling: parse + persist only."""

    def _handle_gateway_log(self, gateway_id: str, raw: str):
        """
        Process one raw log line from `.../logs` (plain text, not JSON).
        Firmware's ESP-IDF log format, optionally colorized:
          "\\033[0;31mE (12345) TAG: message\\033[0m"  (color escape + level letter
                                                          + "(timestamp) TAG: message")
        or, uncolored:
          "E (12345) TAG: message"
        Mirrors firmware's log_line_level(): skip a leading ANSI color escape
        (`\\033[...m`), then the first character is the level letter.
        """
        line = raw.rstrip()
        if not line:
            return
        line = line[:LOG_LINE_MAX]

        level = self._log_line_level(line)
        if level == "E":
            severity = "error"
        elif level == "W":
            severity = "warning"
        else:
            # I/D/V (or an unrecognized/malformed line) — informational.
            severity = "info"

        if self.db_session_factory and self.event_loop:
            asyncio.run_coroutine_threadsafe(
                self._persist_gateway_log(gateway_id, severity, line),
                self.event_loop,
            )

    @staticmethod
    def _log_line_level(s: str) -> str:
        """Port of firmware's log_line_level() (main.c): skip a leading ANSI
        colour escape (`\\033[...m`), then return the first character — the
        ESP-IDF level letter (E/W/I/D/V) on a well-formed line."""
        if s.startswith("\033["):
            m = s.find("m")
            if m != -1:
                s = s[m + 1:]
        return s[0] if s else ""

    async def _persist_gateway_log(self, gateway_id: str, level: str, message: str):
        """Store the log line (tenant resolved from the gateway). No
        notify()/socket fan-out — this is a diagnostics feed, not an alert."""
        from sqlalchemy import select

        from backend.database.models import Gateway, GatewayLog

        try:
            async with self.db_session_factory() as session:
                gw = (await session.execute(
                    select(Gateway).where(Gateway.id == gateway_id)
                )).scalar_one_or_none()
                if not gw:
                    logger.warning(
                        "Log line for unknown gateway, dropping",
                        extra={"gateway_id": gateway_id},
                    )
                    return

                session.add(GatewayLog(
                    tenant_id=gw.tenant_id,
                    gateway_id=gateway_id,
                    level=level,
                    message=message[:220],
                ))
                await session.commit()
        except Exception as e:
            logger.error(
                "Failed to persist gateway log line",
                extra={"gateway_id": gateway_id, "error": str(e)},
            )
