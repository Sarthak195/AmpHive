"""
MQTTManager collaborator: AmpHive Agent plug-discovery auto-populate.

Extracted verbatim from services/mqtt_manager.py (god-object split): the
`.../discovery` topic handler and the upsert that lets the backend stay
authoritative for `plugs.id` while accepting an agent's discovered devices.
Mixed into MQTTManager; see services/mqtt/__init__.py for why this is a mixin
rather than a delegating collaborator object.
"""
import asyncio
import json
import logging
import os
from typing import Any, Dict

logger = logging.getLogger("amphive.mqtt")

# Per-gateway cap on auto-discovered plugs (defense-in-depth). A single claimed
# gateway that announces an unbounded stream of distinct unique_ids would
# otherwise create one Plug row per id — storage / roster-bloat DoS. A real
# gateway fronts a handful of plugs, so the default leaves generous headroom;
# discovery fails SAFE when the cap is hit (the new-plug announcement is
# dropped, while updates to already-known plugs still apply). Env-tunable.
MAX_PLUGS_PER_GATEWAY = int(os.getenv("MAX_PLUGS_PER_GATEWAY") or "64")


class MQTTDiscoveryMixin:
    """Inbound plug-discovery handler — auto-populate agent-discovered plugs."""

    def _handle_gateway_plug_discovery(self, gateway_id: str, payload: Dict[str, Any]):
        """
        Process a plug-discovery announcement from an AmpHive Agent.

        Expected payload (docs/AMPHIVE_AGENT.md):
            {"unique_id": "kasa:AA:BB:..", "provider": "kasa",
             "model": "KP115", "alias": "Bay 3", "capabilities": ["switch","power","energy"]}

        The backend is authoritative for plug_id: it upserts a Plug keyed by the
        stable `unique_id` (the DB assigns `plugs.id`), then publishes the current
        {unique_id: plug_id} map back so the agent adopts the assigned ids. This
        is required because the MQTT plug_id IS the global `plugs.id` used by the
        telemetry handler — an agent must not invent its own.
        """
        unique_id = payload.get("unique_id")
        if not unique_id:
            logger.warning(
                "Discovery missing unique_id, ignoring",
                extra={"gateway_id": gateway_id},
            )
            return
        if self.db_session_factory and self.event_loop:
            asyncio.run_coroutine_threadsafe(
                self._persist_plug_discovery(gateway_id, payload),
                self.event_loop,
            )

    async def _persist_plug_discovery(self, gateway_id: str, payload: Dict[str, Any]):
        """Upsert the discovered plug by unique_id, then publish the assignment map."""
        from sqlalchemy import func, select

        from backend.database.models import Gateway, Plug

        # Truncate to match Plug.unique_id (String(128)) — like the siblings
        # below — so an over-long value can't raise a DataError on insert.
        unique_id = str(payload.get("unique_id"))[:128]
        alias = str(payload.get("alias") or unique_id)[:100]
        model = str(payload.get("model") or "agent")[:50]
        local_ip = str(payload.get("ip") or "agent")[:45]

        try:
            async with self.db_session_factory() as session:
                # Only auto-populate for a gateway that already exists (is claimed).
                gateway = (
                    await session.execute(select(Gateway).where(Gateway.id == gateway_id))
                ).scalar_one_or_none()
                if gateway is None:
                    logger.warning(
                        "Discovery for unknown gateway, ignoring (claim the gateway first)",
                        extra={"gateway_id": gateway_id},
                    )
                    return

                existing = (
                    await session.execute(
                        select(Plug).where(
                            Plug.gateway_id == gateway_id, Plug.unique_id == unique_id
                        )
                    )
                ).scalar_one_or_none()

                if existing is None:
                    # Per-gateway plug cap: bound how many rows a single
                    # (claimed) gateway can spawn via discovery. Fail safe —
                    # drop the announcement rather than grow the table without
                    # limit. Only NEW plugs are gated; updates below still run.
                    plug_count = (
                        await session.execute(
                            select(func.count())
                            .select_from(Plug)
                            .where(Plug.gateway_id == gateway_id)
                        )
                    ).scalar_one()
                    if plug_count >= MAX_PLUGS_PER_GATEWAY:
                        logger.warning(
                            "Discovery plug cap reached for gateway; ignoring new plug",
                            extra={
                                "gateway_id": gateway_id, "unique_id": unique_id,
                                "count": plug_count, "cap": MAX_PLUGS_PER_GATEWAY,
                            },
                        )
                        return
                    session.add(Plug(
                        gateway_id=gateway_id, name=alias, local_ip=local_ip,
                        plug_model=model, unique_id=unique_id,
                    ))
                    logger.info(
                        "Auto-populated new plug from discovery",
                        extra={"gateway_id": gateway_id, "unique_id": unique_id, "alias": alias},
                    )
                else:
                    existing.name = alias
                    existing.plug_model = model

                await session.commit()

                # Rebuild the full {unique_id: plug_id} map for this gateway.
                rows = (
                    await session.execute(
                        select(Plug.unique_id, Plug.id).where(
                            Plug.gateway_id == gateway_id, Plug.unique_id.is_not(None)
                        )
                    )
                ).all()
                assignments = {uid: pid for uid, pid in rows}
        except Exception as e:
            logger.error(
                "Failed to persist plug discovery",
                extra={"gateway_id": gateway_id, "error": str(e)},
            )
            return

        # Hand the authoritative ids back to the agent (retained, so it survives
        # an agent restart / late subscribe).
        self.client.publish(
            f"amphive/gateways/{gateway_id}/assign",
            json.dumps(assignments), qos=1, retain=True,
        )
        logger.info(
            "Published plug assignments",
            extra={"gateway_id": gateway_id, "assignments": assignments},
        )
        # Keep the ESP-consumed roster in sync with agent-discovered plugs.
        await self._publish_roster_for_gateway(gateway_id)
