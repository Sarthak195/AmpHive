"""
AmpHive Telemetry Persistence Service
=====================================
Buffered background batch-flush of raw telemetry samples into the
`telemetry_readings` table. Decoupled from the live SSE `TelemetryStore`
(services/telemetry.py) so the real-time path is never blocked by DB writes.

Design:
- The MQTT paho callback thread calls `enqueue()` — thread-safe, non-blocking,
  bounded. It touches only a `deque` and integer counters (GIL-atomic), and
  never schedules anything on the asyncio loop.
- A periodic asyncio task (owned by `lifespan` in main.py) drains the buffer and
  does ONE bulk INSERT per interval.
- Telemetry cadence is ~15s/plug, so volume is low. A per-message insert would
  also work, but batching bounds connection churn and lets us amortize the
  plug->tenant / active-session lookups. Tradeoff: on a crash, at most one
  flush-interval of samples is lost — acceptable for analytical charts. The
  authoritative per-session totals live on `charging_sessions` and are updated
  separately (MQTTManager._persist_telemetry), so they are unaffected.

Enrichment (tenant_id, active session_id) happens at flush time, not enqueue
time, because the paho thread has no DB access and must not touch the loop. So
`enqueue()` carries only the device-known fields; `_flush_once()` resolves the
rest with two small IN(...) queries over the batch's distinct plug ids.
"""

import asyncio
import logging
import os
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from sqlalchemy import delete, insert, select

logger = logging.getLogger("amphive.telemetry_persistence")

# How often the flush task drains the buffer (seconds).
FLUSH_INTERVAL_SEC = float(os.getenv("TELEMETRY_FLUSH_INTERVAL_SEC", "10.0"))
# Max buffered readings; oldest are dropped if the DB is down/slow.
BUFFER_MAX = int(os.getenv("TELEMETRY_BUFFER_MAX", "10000"))
# Delete readings older than this many days. 0 = retention disabled (keep all).
RETENTION_DAYS = int(os.getenv("TELEMETRY_RETENTION_DAYS", "0"))
# Run the retention prune every N flushes (default ~hourly at a 10s interval).
PRUNE_EVERY_N_FLUSHES = int(os.getenv("TELEMETRY_PRUNE_EVERY_N_FLUSHES", "360"))


class TelemetryPersistenceService:
    """Buffers raw telemetry samples and batch-flushes them to the database."""

    def __init__(self, db_session_factory: Callable):
        self.db_session_factory = db_session_factory
        # deque append/popleft are GIL-atomic; maxlen bounds memory.
        self._buffer: "deque[dict]" = deque(maxlen=BUFFER_MAX)
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._dropped = 0
        self._flush_count = 0

    # ------------------------------------------------------------------
    # Producer side — called from the paho MQTT thread. Cheap, non-blocking.
    # ------------------------------------------------------------------
    def enqueue(self, reading: dict) -> None:
        """
        Buffer one raw telemetry sample. `reading` carries only device-known
        fields: plug_id, recorded_at, power_w, energy_kwh, voltage_v, current_a,
        status. tenant_id / session_id are resolved later at flush time.
        """
        if len(self._buffer) >= BUFFER_MAX:
            # deque(maxlen) will drop the oldest on append; log periodically.
            self._dropped += 1
            if self._dropped % 1000 == 1:
                logger.warning(
                    f"Telemetry buffer full ({BUFFER_MAX}); dropping oldest. "
                    f"total_dropped={self._dropped}"
                )
        self._buffer.append(reading)

    # ------------------------------------------------------------------
    # Lifecycle — driven by lifespan() in main.py on the asyncio loop.
    # ------------------------------------------------------------------
    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._task = loop.create_task(self._run())
        logger.info(
            f"Telemetry flush loop started (interval={FLUSH_INTERVAL_SEC}s, "
            f"buffer_max={BUFFER_MAX}, retention_days={RETENTION_DAYS})"
        )

    async def stop(self) -> None:
        """Drain the tail, then cancel the flush task."""
        self._stop.set()
        if self._task:
            await self._flush_once()  # persist whatever is still buffered
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # ------------------------------------------------------------------
    # Consumer side — runs on the asyncio loop.
    # ------------------------------------------------------------------
    async def _run(self) -> None:
        while not self._stop.is_set():
            # Wake early if stop() is called; otherwise flush every interval.
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=FLUSH_INTERVAL_SEC)
            except asyncio.TimeoutError:
                pass
            await self._flush_once()
            self._flush_count += 1
            if RETENTION_DAYS > 0 and self._flush_count % PRUNE_EVERY_N_FLUSHES == 0:
                await self._prune_old()

    async def _flush_once(self) -> None:
        if not self._buffer:
            return

        # Drain the buffer into a batch.
        batch = []
        while self._buffer:
            try:
                batch.append(self._buffer.popleft())
            except IndexError:
                break
        if not batch:
            return

        # Imported here to avoid a circular import at module load.
        from backend.database.models import (
            ChargingSession,
            Gateway,
            Plug,
            SessionStatus,
            TelemetryReading,
        )

        try:
            async with self.db_session_factory() as session:
                plug_ids = {r["plug_id"] for r in batch}

                # Resolve tenant_id (plug -> gateway -> tenant) for the batch.
                tenant_rows = await session.execute(
                    select(Plug.id, Gateway.tenant_id)
                    .join(Gateway, Plug.gateway_id == Gateway.id)
                    .where(Plug.id.in_(plug_ids))
                )
                tenant_by_plug = {pid: tid for pid, tid in tenant_rows.all()}

                # Resolve the active session id per plug (if any).
                sess_rows = await session.execute(
                    select(ChargingSession.plug_id, ChargingSession.id).where(
                        ChargingSession.plug_id.in_(plug_ids),
                        ChargingSession.status == SessionStatus.ACTIVE,
                    )
                )
                session_by_plug = {pid: sid for pid, sid in sess_rows.all()}

                rows = []
                for r in batch:
                    tid = tenant_by_plug.get(r["plug_id"])
                    if tid is None:
                        # Unknown/deleted plug — skip rather than fail the batch.
                        continue
                    rows.append({
                        **r,
                        "tenant_id": tid,
                        "session_id": session_by_plug.get(r["plug_id"]),
                    })

                if rows:
                    await session.execute(insert(TelemetryReading), rows)
                    await session.commit()
            logger.debug(f"Flushed {len(batch)} telemetry readings")
        except Exception as e:
            # Transient failure (DB down, etc.): re-buffer at the front and retry
            # next cycle. Still bounded by the deque maxlen.
            logger.error(f"Telemetry flush failed ({len(batch)} rows): {e}")
            for r in reversed(batch):
                self._buffer.appendleft(r)

    async def _prune_old(self) -> None:
        from backend.database.models import TelemetryReading

        cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
        try:
            async with self.db_session_factory() as session:
                result = await session.execute(
                    delete(TelemetryReading).where(TelemetryReading.recorded_at < cutoff)
                )
                await session.commit()
                logger.info(
                    f"Pruned {result.rowcount} telemetry rows older than {RETENTION_DAYS}d"
                )
        except Exception as e:
            logger.error(f"Telemetry prune failed: {e}")
