"""
"Notify me when free" plug watches.

One entry point — notify_watchers_plug_available() — called from the two
places a plug flips back to AVAILABLE:
  - services/session_lifecycle.py finalize_charging_session (a charging
    session ended; the driver whose session it was is excluded — they get
    their own receipt notification and don't need to hear their plug freed),
  - routers/cpo.py cpo_plug_maintenance's `clear` action (an operator
    returned the plug to service).

One-shot contract: each watcher is notified once (type `plug_available`,
delivered by services/notifications.py notify() — persistent feed +
Socket.io user room + Web Push) and exactly the notified watch rows are
deleted, so a driver is never re-pinged on every later flip unless they
re-arm the bell. An excluded user's row is neither notified nor deleted —
it stays armed for the next flip.

Best-effort by contract: this runs inside the billing path
(finalize_charging_session), so it must NEVER raise — every failure is
logged and swallowed, and the shared db session is rolled back on error so
a failed statement here can't poison the caller's subsequent use of it
(mirrors notify()'s own never-raises contract).
"""
import logging
from typing import Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.models import Plug, PlugWatch

logger = logging.getLogger("amphive.plug_watch")


async def notify_watchers_plug_available(
    db: AsyncSession,
    plug: Plug,
    exclude_user_id: Optional[int] = None,
) -> int:
    """
    Notify everyone watching `plug` that it is AVAILABLE again, then delete
    those watch rows (one-shot). Returns the number of watchers notified
    (0 on any failure). Never raises — see the module docstring.
    """
    try:
        stmt = select(PlugWatch.id, PlugWatch.user_id).where(
            PlugWatch.plug_id == plug.id
        )
        if exclude_user_id is not None:
            stmt = stmt.where(PlugWatch.user_id != exclude_user_id)
        watches = (await db.execute(stmt)).all()
        if not watches:
            return 0

        # notify() is itself best-effort (never raises into callers); late
        # import mirrors the other emit points (import-cycle hygiene).
        from backend.services.notifications import notify

        for _watch_id, user_id in watches:
            await notify(
                user_id,
                "plug_available",
                f"{plug.name} is now free",
                f"The charger you were watching ({plug.name}) just became "
                f"available — first come, first served.",
                plug_id=plug.id,
            )

        # One-shot: clear exactly the rows fanned out above — not a blanket
        # per-plug delete, so a watch armed concurrently mid-fan-out (or the
        # excluded driver's own) survives for the next flip.
        await db.execute(
            delete(PlugWatch).where(
                PlugWatch.id.in_([watch_id for watch_id, _ in watches])
            )
        )
        await db.commit()
        return len(watches)
    except Exception:
        logger.exception(f"Plug-watch fan-out failed for plug {plug.id}")
        try:
            # Restore the shared session: a failed statement above would
            # otherwise poison the caller's transaction (the billing path).
            await db.rollback()
        except Exception:
            pass
        return 0
