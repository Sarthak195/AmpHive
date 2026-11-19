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
from sqlalchemy.exc import DataError, DBAPIError, IntegrityError, StatementError

logger = logging.getLogger("amphive.telemetry_persistence")

# How often the flush task drains the buffer (seconds).
FLUSH_INTERVAL_SEC = float(os.getenv("TELEMETRY_FLUSH_INTERVAL_SEC", "10.0"))
# Max buffered readings; oldest are dropped if the DB is down/slow.
BUFFER_MAX = int(os.getenv("TELEMETRY_BUFFER_MAX", "10000"))
# Delete readings older than this many days. 0 = retention disabled (keep all).
RETENTION_DAYS = int(os.getenv("TELEMETRY_RETENTION_DAYS", "0"))
# Run the retention prune every N flushes (default ~hourly at a 10s interval).
PRUNE_EVERY_N_FLUSHES = int(os.getenv("TELEMETRY_PRUNE_EVERY_N_FLUSHES", "360"))


def _is_data_error(exc: BaseException) -> bool:
    """True for a bad-row error (constraint/data/bind failure) that retrying the
    same row can never fix, as opposed to a transient connection/DB-down error."""
    if isinstance(exc, (IntegrityError, DataError)):
        return True
    # Under asyncpg, SQLAlchemy maps most Postgres class-22 data errors (e.g.
    # string truncation, numeric out of range) to a generic DBAPIError, so
    # classify by SQLSTATE: 22 = data exception, 23 = integrity violation.
    orig = getattr(exc, "orig", None)
    code = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    if isinstance(code, str) and code[:2] in ("22", "23"):
        return True
    # asyncpg's client-side "invalid input for query argument" DataError has no
    # SQLSTATE and is mapped to InterfaceError; a closed connection is also an
    # InterfaceError (transient), so match the original class by name.
    cause = getattr(orig, "__cause__", None)
    if cause is not None and any(c.__name__ == "DataError" for c in type(cause).__mro__):
        return True
    # Bind-processing failures (e.g. a wrongly-typed value) are StatementErrors
    # that are not DBAPIErrors; DBAPIErrors outside the classes above (operational,
    # interface, ...) stay transient.
    return isinstance(exc, StatementError) and not isinstance(exc, DBAPIError)


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
        self._bad_rows_dropped = 0
        # Wall-clock time of the last flush tick (admin health page).
        self.last_flush_at: Optional[datetime] = None

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
            self.last_flush_at = datetime.now(timezone.utc)
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
                    try:
                        await session.execute(insert(TelemetryReading), rows)
                        await session.commit()
                    except Exception as e:
                        if not _is_data_error(e):
                            raise
                        # A bad row must not block history forever: retry the
                        # batch row by row (each in a savepoint), drop the bad.
                        await session.rollback()
                        await self._insert_isolating(session, insert(TelemetryReading), rows, e)
                        await session.commit()
            logger.debug(f"Flushed {len(batch)} telemetry readings")
        except Exception as e:
            # Transient failure (DB down, etc.): re-buffer at the front and retry
            # next cycle. Still bounded by the deque maxlen. Data errors on the
            # insert are isolated per row above and never reach here.
            logger.error(f"Telemetry flush failed ({len(batch)} rows): {e}")
            for r in reversed(batch):
                self._buffer.appendleft(r)

    async def _insert_isolating(self, session, stmt, rows: list, batch_error) -> None:
        """Insert rows one at a time, each in a savepoint; drop and count the rows
        that fail with a data error. Any other error propagates (batch re-buffered)."""
        dropped = 0
        for row in rows:
            try:
                async with session.begin_nested():
                    await session.execute(stmt, [row])
            except Exception as e:
                if not _is_data_error(e):
                    raise
                dropped += 1
        self._bad_rows_dropped += dropped
        logger.error(
            f"Telemetry batch insert hit a data error ({batch_error}); "
            f"dropped {dropped} bad row(s), persisted {len(rows) - dropped}. "
            f"total_bad_dropped={self._bad_rows_dropped}"
        )

    async def _prune_old(self) -> None:
        from backend.database.models import Plug, TelemetryReading

        cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
        try:
            async with self.db_session_factory() as session:
                # recorded_at alone has no index, so filtering on it only made
                # this hourly DELETE a full scan of the whole table (288 MB in
                # prod, 2026-11-16), which pushed the app out of the e2-micro's
                # memory every hour. Adding plug_id lets Postgres range-scan
                # idx_telemetry_plug_recorded instead. plug_id is NOT NULL with
                # an ON DELETE CASCADE FK to plugs, so every row is still covered.
                plug_ids = (await session.execute(select(Plug.id))).scalars().all()
                result = await session.execute(
                    delete(TelemetryReading).where(
                        TelemetryReading.plug_id.in_(plug_ids),
                        TelemetryReading.recorded_at < cutoff,
                    )
                )
                await session.commit()
                logger.info(
                    f"Pruned {result.rowcount} telemetry rows older than {RETENTION_DAYS}d"
                )
        except Exception as e:
            logger.error(f"Telemetry prune failed: {e}")
