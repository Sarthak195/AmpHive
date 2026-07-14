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

# [Session limits] Duration backstop: an ACTIVE session that has outlived its
# own max_duration_seconds (snapshotted at start — see ChargingSession) is
# finalized with the same reason the telemetry-path mirror uses
# (MQTTManager._maybe_auto_stop_on_limits). Normally that mirror fires within
# ~1 s of the limit; this sweep only matters when telemetry still flows but
# the mirror was skipped (e.g. the backend restarted mid-frame) — a stale
# feed is already covered by REAP_REASON above. Governed by the same env
# toggle as the telemetry-path mirror (read independently here to keep this
# module import-light; same name, same default).
TIME_LIMIT_REAP_REASON = "auto-stopped: time limit reached"
AUTO_STOP_ON_LIMITS = os.getenv("AUTO_STOP_ON_LIMITS", "true").lower() in ("1", "true", "yes")

# [Reservations] When a booking's window opens, force-stop any session still
# running on that plug under a DIFFERENT user — the walk-up overrun the
# session-start booking gate can't catch (the gate only blocks NEW non-holder
# starts once the window covers now; a session already in progress started
# legally before it). Without this the holder arrives to a plug pinned
# OCCUPIED by a stranger. The reason string routes finalize's stop
# notification to the "plug reserved" title (services/session_lifecycle.py).
RESERVATION_REAP_REASON = "auto-stopped: plug reserved"
FORCE_STOP_WALKUP_ON_RESERVATION = os.getenv(
    "RESERVATION_FORCE_STOP_WALKUP", "true"
).lower() in ("1", "true", "yes")


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
            try:
                await self.reap_time_limited_once()
            except Exception:
                logger.exception("Session reaper time-limit sweep failed")
            try:
                await self.reap_reservation_starts_once()
            except Exception:
                logger.exception("Reservation-start sweep failed")
            try:
                await self.reap_reprice_once()
            except Exception:
                logger.exception("Pricing-v2 reprice sweep failed")

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

    def _time_limited_sessions_query(self):
        """ACTIVE sessions that carry a duration limit — the elapsed-time
        comparison happens in Python (see reap_time_limited_once) so this
        stays a plain portable SELECT, and the ACTIVE set is small by
        construction (MAX_ACTIVE_SESSIONS_PER_USER caps it per user)."""
        from backend.database.models import ChargingSession, SessionStatus

        return select(
            ChargingSession.id,
            ChargingSession.started_at,
            ChargingSession.max_duration_seconds,
        ).where(
            and_(
                ChargingSession.status == SessionStatus.ACTIVE,
                ChargingSession.max_duration_seconds.isnot(None),
            )
        )

    async def reap_time_limited_once(self) -> int:
        """[Session limits] Duration backstop sweep: finalize every ACTIVE
        session whose elapsed time has passed its own max_duration_seconds
        (NULL-limit legacy sessions are excluded by the query). Same
        one-txn-per-session / race-safe-finalize contract as reap_once —
        losing the row-lock re-check to a user stop or the telemetry-path
        limit mirror is not counted. Returns the count actually reaped."""
        if not AUTO_STOP_ON_LIMITS:
            return 0
        now = datetime.now(timezone.utc)

        async with self.db_session_factory() as db:
            result = await db.execute(self._time_limited_sessions_query())
            rows = list(result.all())

        overdue_ids = []
        for session_id, started_at, max_duration_seconds in rows:
            if started_at is None:
                continue
            if started_at.tzinfo is None:
                # Legacy rows written by the old naive-datetime hooks — same
                # treat-as-UTC convention as gateway_is_live/finalize.
                started_at = started_at.replace(tzinfo=timezone.utc)
            if (now - started_at).total_seconds() >= max_duration_seconds:
                overdue_ids.append(session_id)

        reaped = 0
        for session_id in overdue_ids:
            try:
                async with self.db_session_factory() as db:
                    outcome = await self.finalize(
                        db, session_id, reason=TIME_LIMIT_REAP_REASON
                    )
                if outcome is not None:
                    reaped += 1
                    logger.warning(
                        f"Reaped time-limited session {session_id}: "
                        f"{outcome['energy_kwh']} kWh, {outcome['coins_spent']} coins "
                        f"(outlived its own duration limit)"
                    )
            except Exception:
                logger.exception(f"Failed to reap time-limited session {session_id}")
        return reaped

    async def reap_reprice_once(self) -> int:
        """[Pricing v2] Backstop for the telemetry-path TOD reprice: close out
        the current rate segment of any ACTIVE session whose rate_valid_until
        has passed but no telemetry frame landed to trip it (a dropped/silent
        gateway). Bills the gap energy forward-only against the last PERSISTED
        energy_kwh — same helper (services/pricing.py reprice_session_if_due)
        the telemetry hook uses, so the math is identical.

        One txn per session, session row locked + ACTIVE re-checked (race-safe
        against a user stop or a telemetry frame doing the same reprice — one
        of them advances rate_valid_until, the other then no-ops). A boundary
        that landed on the same rate just advances the check point silently;
        only a real change emits one rate_changed notification. Returns the
        number of sessions actually repriced."""
        from backend.database.models import ChargingSession, Plug, SessionStatus
        from backend.services.billing import rate_changed_body
        from backend.services.notifications import notify
        from backend.services.pricing import reprice_session_if_due

        now = datetime.now(timezone.utc)

        # Candidates: ACTIVE, on a TOD tariff (rate_valid_until set), overdue.
        async with self.db_session_factory() as db:
            result = await db.execute(
                select(ChargingSession.id).where(
                    and_(
                        ChargingSession.status == SessionStatus.ACTIVE,
                        ChargingSession.rate_valid_until.isnot(None),
                        ChargingSession.rate_valid_until < now,
                    )
                )
            )
            due_ids = list(result.scalars().all())

        repriced = 0
        for session_id in due_ids:
            try:
                async with self.db_session_factory() as db:
                    locked = await db.execute(
                        select(ChargingSession)
                        .where(
                            and_(
                                ChargingSession.id == session_id,
                                ChargingSession.status == SessionStatus.ACTIVE,
                            )
                        )
                        .with_for_update()
                    )
                    session = locked.scalar_one_or_none()
                    if session is None:
                        continue
                    plug = (
                        await db.execute(select(Plug).where(Plug.id == session.plug_id))
                    ).scalar_one_or_none()
                    if plug is None:
                        await db.commit()
                        continue
                    change = await reprice_session_if_due(db, session, plug, at=now)
                    user_id = session.user_id
                    await db.commit()
                if change is not None:
                    new_rate, _boundary = change
                    repriced += 1
                    await notify(
                        user_id, "rate_changed", "Charging rate changed",
                        rate_changed_body(new_rate), severity="info",
                        session_id=session_id,
                    )
                    logger.info(
                        f"Repriced stale session {session_id} to {new_rate} coins/kWh "
                        f"(telemetry silent past its TOD boundary)"
                    )
            except Exception:
                logger.exception(f"Failed to reprice session {session_id}")
        return repriced

    async def reap_reservation_starts_once(self) -> int:
        """[Reservations] Process each booking whose window has just opened
        (start_at <= now < end_at, still BOOKED, not yet processed): nudge the
        holder that their reserved plug is ready, and force-stop any session
        still running on that plug under a DIFFERENT user. That walk-up started
        legally before the window (the session-start gate only blocks NEW
        non-holder starts once the window covers now), so it's the one overrun
        the gate can't catch — leaving it would pin the plug OCCUPIED by a
        stranger just as the holder arrives.

        Idempotent per reservation via started_notified_at (stamped under a row
        lock), so the nudge and the force-stop each fire exactly once across the
        60 s ticks and across a restart. The force-stop half is gated by
        RESERVATION_FORCE_STOP_WALKUP (default on); the holder nudge fires
        regardless. One reservation's failure never aborts the sweep. Returns
        the number of reservations activated this sweep."""
        from backend.database.models import (
            ChargingSession, Plug, Reservation, ReservationStatus, SessionStatus,
        )
        from backend.services.notifications import notify

        now = datetime.now(timezone.utc)

        # 1. Candidates: opened, still-BOOKED, not-yet-processed windows.
        #    Read-only, no locks.
        # ponytail (REC-12, accepted gap): a full-window backend outage means end_at > now never holds after restart, so the holder never gets the started-nudge — left as-is, since a post-window "plug ready until <past time>" nudge would mislead and force-stopping a now-legal walk-up on a lapsed window would be wrong; no-show EXPIRY stays restart-safe (end_at <= now flip).
        async with self.db_session_factory() as db:
            result = await db.execute(
                select(
                    Reservation.id, Reservation.plug_id, Reservation.user_id,
                    Reservation.end_at, Plug.name,
                )
                .join(Plug, Plug.id == Reservation.plug_id)
                .where(
                    and_(
                        Reservation.status == ReservationStatus.BOOKED,
                        Reservation.started_notified_at.is_(None),
                        Reservation.start_at <= now,
                        Reservation.end_at > now,
                    )
                )
            )
            candidates = list(result.all())

        activated = 0
        for res_id, plug_id, holder_id, end_at, plug_name in candidates:
            # 2. Claim the reservation atomically: win it only if still BOOKED
            #    and unprocessed. skip_locked so a racing holder-start that is
            #    fulfilling this very row (it locks the plug, then updates the
            #    reservation) never blocks the janitor — we skip and it drops
            #    out of next sweep's candidates as FULFILLED. The overrunning
            #    session is read here but finalized in its OWN txn below, so the
            #    session-row lock is never nested under this reservation lock.
            overrun = None
            async with self.db_session_factory() as db:
                locked = await db.execute(
                    select(Reservation)
                    .where(
                        and_(
                            Reservation.id == res_id,
                            Reservation.status == ReservationStatus.BOOKED,
                            Reservation.started_notified_at.is_(None),
                        )
                    )
                    .with_for_update(skip_locked=True)
                )
                reservation = locked.scalar_one_or_none()
                if reservation is None:
                    continue
                reservation.started_notified_at = now
                other = await db.execute(
                    select(ChargingSession.id, ChargingSession.user_id)
                    .where(
                        and_(
                            ChargingSession.plug_id == plug_id,
                            ChargingSession.status == SessionStatus.ACTIVE,
                            ChargingSession.user_id != holder_id,
                        )
                    )
                    .limit(1)
                )
                overrun = other.first()
                await db.commit()

            activated += 1

            # 3. Force-stop the overrun in its own txn: finalize locks the
            #    session row, bills persisted energy, frees the plug, and emits
            #    the walk-up driver's own "plug reserved" stop notification — so
            #    we don't notify them a second time here.
            freed = False
            if overrun is not None and FORCE_STOP_WALKUP_ON_RESERVATION:
                overrun_session_id, overrun_user_id = overrun
                try:
                    async with self.db_session_factory() as db:
                        outcome = await self.finalize(
                            db, overrun_session_id, reason=RESERVATION_REAP_REASON
                        )
                    if outcome is not None:
                        freed = True
                        logger.warning(
                            f"Force-stopped walk-up session {overrun_session_id} "
                            f"(user {overrun_user_id}) on plug {plug_id}: "
                            f"reservation {res_id} window opened"
                        )
                except Exception:
                    logger.exception(
                        f"Failed to force-stop session {overrun_session_id} for "
                        f"reservation {res_id}"
                    )

            # 4. Nudge the holder their plug is ready (best-effort — notify
            #    never raises). Mention the clear-out only when we freed it.
            body = (
                f"{plug_name} is ready — you can start charging now. "
                f"Reserved for you until {end_at.astimezone(timezone.utc):%H:%M} UTC."
            )
            if freed:
                body += " (the plug was just freed for you)"
            await notify(
                holder_id,
                "reservation_started",
                "Your reservation has started",
                body,
                severity="info",
                plug_id=plug_id,
            )

        if activated:
            logger.info(f"Reservation-start sweep activated {activated} booking(s)")
        return activated
