"""
"Request capacity" fan-out (caps + circuits feature).

One entry point — notify_capacity_available() — called from the two places a
circuit gains room:
  - services/session_lifecycle.py finalize_charging_session (a session on the
    circuit ended; the driver whose session it was is excluded — they get their
    own receipt and don't need to hear the circuit freed),
  - routers/cpo.py cpo_update_group (the operator raised the circuit cap).

One-shot contract, mirroring services/plug_watch.py: each requester whose plug
would now be admitted (current load + that plug's cap ≤ the circuit limit) is
notified once (type `capacity_available`, via services/notifications.py notify)
and exactly those request rows are deleted. Requests that still wouldn't fit
stay armed for the next time the circuit frees. Multiple fitting requesters are
all notified — first come, first served — and the real admission gate at start
arbitrates who actually gets the power (services/caps.py check_circuit_admission).

Best-effort by contract: this runs inside the billing path, so it must NEVER
raise — every failure is logged and swallowed, and the shared db session is
rolled back on error so a failed statement can't poison the caller.
"""
import logging
from typing import Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.models import CapacityRequest, ChargerGroup, Plug
from backend.services.caps import circuit_load_a, effective_plug_cap

logger = logging.getLogger("amphive.capacity")


async def notify_capacity_available(
    db: AsyncSession,
    group_id: Optional[int],
    exclude_user_id: Optional[int] = None,
) -> int:
    """Notify every pending requester on circuit `group_id` whose plug would now
    be admitted, then delete those request rows (one-shot). Returns the number
    notified (0 on any failure or when the group has no cap). Never raises."""
    if group_id is None:
        return 0
    try:
        group = (
            await db.execute(select(ChargerGroup).where(ChargerGroup.id == group_id))
        ).scalar_one_or_none()
        if group is None or group.max_current_a is None:
            return 0

        stmt = select(
            CapacityRequest.id, CapacityRequest.user_id, CapacityRequest.plug_id
        ).where(CapacityRequest.group_id == group_id)
        if exclude_user_id is not None:
            stmt = stmt.where(CapacityRequest.user_id != exclude_user_id)
        requests = (await db.execute(stmt)).all()
        if not requests:
            return 0

        load = await circuit_load_a(db, group_id)  # current committed load
        plug_ids = [plug_id for _, _, plug_id in requests]
        plugs = {
            p.id: p
            for p in (
                await db.execute(select(Plug).where(Plug.id.in_(plug_ids)))
            ).scalars().all()
        }

        from backend.services.notifications import notify

        fired_ids = []
        for req_id, user_id, plug_id in requests:
            plug = plugs.get(plug_id)
            if plug is None:
                fired_ids.append(req_id)  # plug gone — clear the stale request
                continue
            # Notifying doesn't energize anything, so test each request against
            # the SAME current load; the start-time admission gate arbitrates.
            if load + effective_plug_cap(plug) <= group.max_current_a + 1e-6:
                await notify(
                    user_id,
                    "capacity_available",
                    f"Capacity freed on {plug.name}",
                    f"There's room on {plug.name}'s circuit now — open the app and "
                    "tap Start (first come, first served).",
                    plug_id=plug.id,
                )
                fired_ids.append(req_id)

        if fired_ids:
            await db.execute(
                delete(CapacityRequest).where(CapacityRequest.id.in_(fired_ids))
            )
            await db.commit()
        return sum(1 for _ in fired_ids)
    except Exception:
        logger.exception(f"Capacity fan-out failed for group {group_id}")
        try:
            await db.rollback()
        except Exception:
            pass
        return 0
