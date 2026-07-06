"""
AmpHive Session Reaper
======================
Periodic janitor for ACTIVE charging sessions whose telemetry has gone
silent — a gateway that died mid-session (or never received the ON) leaves
the session ACTIVE forever otherwise: the only other timeout lives in the
firmware, which is useless when the gateway itself is gone. The plug stays
pinned OCCUPIED and the user keeps an unstoppable session.

Staleness = no telemetry attributed to the session for SESSION_STALE_TIMEOUT_SEC
(COALESCE(last_telemetry_at, started_at), so a session that never produced a
reading gets the same grace period from its start). Reaped sessions go through
the same finalize path as a user-initiated stop — billed for persisted energy,
wallet debited, ledger row written, plug freed — with the reason recorded in
the ledger description.

The finalize callable is injected (main.finalize_charging_session) to avoid a
circular import; it locks the session row and re-checks ACTIVE, so racing a
concurrent user stop settles exactly once.
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional

from sqlalchemy import and_, func, select

logger = logging.getLogger("amphive.session_reaper")

# How often the reaper scans for stale sessions.
REAPER_INTERVAL_SEC = float(os.getenv("SESSION_REAPER_INTERVAL_SEC", "60"))
# How long a session may go without telemetry before it is force-finalized.
# Healthy sessions report every ~1-10 s; an observed broker reconnect took
# ~100 s, so 300 s tolerates outage blips without letting sessions rot.
STALE_TIMEOUT_SEC = float(os.getenv("SESSION_STALE_TIMEOUT_SEC", "300"))

REAP_REASON = "auto-stopped: telemetry lost"


class SessionReaperService:
    """Owns a background task (started/stopped by the app lifespan) that
    finalizes ACTIVE sessions with stale telemetry."""

    def __init__(
        self,
        db_session_factory: Callable,
        finalize: Callable[..., Awaitable[Optional[dict]]],
        interval_sec: float = REAPER_INTERVAL_SEC,
        stale_timeout_sec: float = STALE_TIMEOUT_SEC,
    ):
        self.db_session_factory = db_session_factory
        self.finalize = finalize
        self.interval_sec = interval_sec
        self.stale_timeout_sec = stale_timeout_sec
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._task = loop.create_task(self._run())
        logger.info(
            f"Session reaper started (interval={self.interval_sec}s, "
            f"stale_timeout={self.stale_timeout_sec}s)"
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_sec)
            except asyncio.TimeoutError:
                pass
            if self._stop.is_set():
                return
            try:
                await self.reap_once()
            except Exception:
                # The janitor must survive transient DB errors.
                logger.exception("Session reaper sweep failed")

    def _stale_session_ids_query(self, cutoff: datetime):
        from backend.database.models import ChargingSession, SessionStatus

        return select(ChargingSession.id).where(
            and_(
                ChargingSession.status == SessionStatus.ACTIVE,
                func.coalesce(
                    ChargingSession.last_telemetry_at, ChargingSession.started_at
                )
                < cutoff,
            )
        )

    async def reap_once(self) -> int:
        """One sweep: finalize every stale ACTIVE session. Returns the count
        actually reaped (a session that a user stops mid-sweep is skipped by
        the finalize row-lock re-check and not counted)."""
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=self.stale_timeout_sec)

        async with self.db_session_factory() as db:
            result = await db.execute(self._stale_session_ids_query(cutoff))
            stale_ids = list(result.scalars().all())

        reaped = 0
        for session_id in stale_ids:
            # One DB session/transaction per reap so a failure on one session
            # doesn't poison the rest of the sweep.
            try:
                async with self.db_session_factory() as db:
                    outcome = await self.finalize(db, session_id, reason=REAP_REASON)
                if outcome is not None:
                    reaped += 1
                    logger.warning(
                        f"Reaped stale session {session_id}: "
                        f"{outcome['energy_kwh']} kWh, {outcome['coins_spent']} coins "
                        f"(no telemetry for >{self.stale_timeout_sec:.0f}s)"
                    )
            except Exception:
                logger.exception(f"Failed to reap session {session_id}")
        return reaped
