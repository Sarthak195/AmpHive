"""
Sessions routes — moved verbatim from main.py (2026-07-07, TD#7 split).
"""
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend import state
from backend.database.db import get_db
from backend.database.models import (
    ChargerGroup,
    ChargingSession,
    DisputeStatus,
    Gateway,
    GroupMembership,
    Plug,
    PlugStatus,
    SessionDispute,
    SessionStatus,
    Tenant,
    User,
    UserRole,
)
from backend.schemas import (
    DisputeCreateRequest,
    DisputeResponse,
    QueueChargeRequest,
    SessionLimitsUpdateRequest,
    SessionStartRequest,
    SessionStopRequest,
)
from backend.services.auth import (
    get_current_user,
)
from backend.services.billing import session_cost
from backend.services.money import energy_cost, to_money
from backend.services.pricing import max_rate_over_window
from backend.services.session_lifecycle import (
    finalize_charging_session,
    gateway_is_live,
    plug_is_powered,
    # Not called here, but tests patch it on this module (the start flow's
    # telemetry-interval stub); keep it importable as that patch surface.
    set_plug_telemetry_interval,  # noqa: F401
)

# [Session start refactor] The ACTIVE-session start body (caps -> rate -> hold
# -> create -> claim -> publish -> rollback) lives in one shared helper so the
# reaper's queued-charge auto-start bills identically to a walk-up start
# (docs/proposals/queued-charge-offline-plug.md §4). MIN_START_BALANCE_COINS is
# re-exported here — it moved to session_start so the queue endpoint and the
# auto-start share one floor — so main.py's GET /api/config import still works.
from backend.services.session_start import (
    MIN_START_BALANCE_COINS,
    begin_active_session,
)
from backend.services.wallet import available_balance

logger = logging.getLogger("amphive.api")
router = APIRouter()

# How many charging sessions a user may run concurrently (e.g. two vehicles
# on two plugs). The API and UI list every active session, so raising this
# needs no code change.
MAX_ACTIVE_SESSIONS_PER_USER = int(os.getenv("MAX_ACTIVE_SESSIONS_PER_USER", "2"))

# [Queued charge] How many WAITING queued charges a driver may hold at once —
# small, matching the active-session cap; the concurrent-session cap itself is
# enforced at auto-start, not here, so a driver can queue while at their active
# cap (docs/proposals/queued-charge-offline-plug.md).
MAX_QUEUED_CHARGES_PER_USER = int(os.getenv("MAX_QUEUED_CHARGES_PER_USER", "2"))

# ===========================================================================
# Charging Session Endpoints
# ===========================================================================

@router.post("/api/sessions/start")
async def start_charging_session(
    req: SessionStartRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Start a charging session on a specific plug.
    0. Lock the user row and enforce the concurrent-session cap.
    1. Verify user has access to the plug (group check).
    2. Lock the plug row and claim it (avoids two concurrent starts on one
       plug); confirm its gateway is live; resolve the billing rate.
    3. Size and check the session's authorization hold — min(available
       balance, max_kwh * rate) must clear MIN_START_BALANCE_COINS (402
       otherwise). Replaces the old flat wallet-balance floor: available
       balance nets out coins already held by this user's OTHER active
       sessions (MARKET_GAP_ANALYSIS.md §3 "Authorization hold"; the hold
       itself closes the old finalize-time forgiven-overage leak — see
       SECURITY.md §5). Must come AFTER the rate is resolved above, since
       sizing the hold needs it.
    4. Commit the session + OCCUPIED status FIRST, then publish MQTT ON.
       (Publishing first could leave the plug live with no session billing it
       if the DB write then fails. If the publish fails we roll the claim back.)
    5. Start the telemetry stream.
    """
    # 0. Lock the user row, then count ACTIVE sessions under that lock: two
    #    simultaneous starts by the same user serialize here, so the loser
    #    counts the winner's committed session (a plain count would let both
    #    pass and exceed the cap). Lock order is user -> plug everywhere the
    #    two are taken together (finalize goes session -> user -> plug), so
    #    no lock cycle is possible.
    locked_user = await db.execute(
        select(User).where(User.id == user.id).with_for_update()
    )
    user = locked_user.scalar_one()

    count_result = await db.execute(
        select(func.count())
        .select_from(ChargingSession)
        .where(
            and_(
                ChargingSession.user_id == user.id,
                ChargingSession.status == SessionStatus.ACTIVE,
            )
        )
    )
    active_count = count_result.scalar_one()
    if active_count >= MAX_ACTIVE_SESSIONS_PER_USER:
        raise HTTPException(
            status_code=409,
            detail=(
                f"You already have {active_count} active charging "
                f"session{'s' if active_count != 1 else ''} "
                f"(limit {MAX_ACTIVE_SESSIONS_PER_USER}). "
                "Stop one before starting another."
            ),
        )

    # 1. Verify plug exists and lock the row for the duration of this txn, so a
    #    concurrent start blocks here and then sees OCCUPIED (closes the TOCTOU
    #    between the availability check and the claim below).
    result = await db.execute(
        select(Plug).where(Plug.id == req.plug_id).with_for_update()
    )
    plug = result.scalar_one_or_none()
    if not plug:
        raise HTTPException(status_code=404, detail=f"Plug {req.plug_id} not found.")

    # Access check for private groups
    if plug.group_id:
        group_result = await db.execute(
            select(ChargerGroup).where(ChargerGroup.id == plug.group_id)
        )
        group = group_result.scalar_one_or_none()
        if group and not group.is_public:
            membership_result = await db.execute(
                select(GroupMembership).where(
                    and_(
                        GroupMembership.user_id == user.id,
                        GroupMembership.group_id == group.id,
                    )
                )
            )
            if not membership_result.scalar_one_or_none():
                raise HTTPException(
                    status_code=403,
                    detail="You don't have access to this plug. Join the group first.",
                )

    # 3. Claim the plug (still holding the row lock). Anything but AVAILABLE
    #    blocks the start (TD#22): OCCUPIED means in use, and OFFLINE /
    #    MAINTENANCE mean the CPO took the plug out of service (or it was
    #    never commissioned — new plugs default to OFFLINE and must be set
    #    AVAILABLE in the CPO portal). Previously only OCCUPIED was rejected,
    #    so an out-of-service plug was still startable — it pinned OCCUPIED
    #    and billed nothing.
    if plug.status == PlugStatus.OCCUPIED:
        raise HTTPException(status_code=409, detail="This plug is currently in use.")
    if plug.status != PlugStatus.AVAILABLE:
        raise HTTPException(
            status_code=409,
            detail=(
                f"This plug is out of service ({plug.status.value}). "
                "Ask the operator to re-enable it."
            ),
        )

    # [Reservations] Booking gate — still under the plug row lock, so a
    # concurrent booking/start on this plug serializes with us. Lazy no-show
    # expiry runs FIRST (services/reservations.py, shared with the booking
    # overlap check and every list endpoint), so a hold whose grace lapsed
    # never blocks a walk-up start even with no background sweep. If a
    # BOOKED window covers now: someone else's -> 409; the caller's own ->
    # remembered here and marked FULFILLED (with the session linked) in the
    # same commit as the plug claim below. Local imports on purpose — this
    # router's shared header import block is a merge hot-spot across the
    # parallel feature branches (same rationale as the GST section below).
    from backend.database.models import Reservation, ReservationStatus
    from backend.services.reservations import expire_lapsed_reservations

    now_utc = datetime.now(timezone.utc)
    await expire_lapsed_reservations(db, plug_id=plug.id, now=now_utc)
    reservation_result = await db.execute(
        select(Reservation)
        .where(
            and_(
                Reservation.plug_id == plug.id,
                Reservation.status == ReservationStatus.BOOKED,
                Reservation.start_at <= now_utc,
                Reservation.end_at > now_utc,
            )
        )
        .order_by(Reservation.start_at)
        .limit(1)
    )
    covering_reservation = reservation_result.scalar_one_or_none()
    if covering_reservation is not None and covering_reservation.user_id != user.id:
        raise HTTPException(
            status_code=409,
            detail=(
                "Plug is reserved until "
                f"{covering_reservation.end_at.astimezone(timezone.utc):%H:%M} UTC."
            ),
        )

    gw_result = await db.execute(select(Gateway).where(Gateway.id == plug.gateway_id))
    gateway = gw_result.scalar_one()

    # Refuse to start against a gateway that isn't demonstrably alive: the MQTT
    # publish below only confirms the *broker* accepted the command, so without
    # this check a session on a dead gateway starts "successfully" and then
    # sits ACTIVE forever with the plug pinned OCCUPIED and no telemetry.
    if not gateway_is_live(gateway):
        raise HTTPException(
            status_code=409,
            detail="This charger's gateway is offline. Try again once it reconnects.",
        )

    # [Plug power] A live gateway can still front a plug that LOST power (mains
    # or relay power lost, or its plug agent down) — it stops reporting telemetry
    # while the gateway itself stays online, leaving a STALE last_telemetry_at.
    # Refuse the start on that positive evidence of power loss (same shape as the
    # gateway gate above); starting there would pin the plug OCCUPIED with no draw
    # and bill nothing. A plug that has simply never reported (last_telemetry_at
    # NULL — freshly provisioned, or the brief post-migration backfill window) is
    # NOT blocked: absence of a heartbeat isn't proof of no power, and the reaper
    # closes out a session that never draws.
    if plug.last_telemetry_at is not None and not plug_is_powered(plug):
        raise HTTPException(
            status_code=409,
            detail="This charger has no power right now. Try again once power is restored.",
        )

    # [Session start refactor] Steps 3-5 — circuit admission, rate resolve +
    # snapshot, authorization-hold sizing (incl. the MIN_START_BALANCE_COINS
    # 402 floor), create the ACTIVE session, claim the plug OCCUPIED (fulfilling
    # any covering reservation), commit, publish the MQTT ON (rolling the claim
    # back on failure), and start the telemetry stream — all run in the shared
    # begin_active_session helper so the reaper's queued-charge auto-start bills
    # identically to this walk-up start (docs/proposals/queued-charge-offline-plug.md
    # §4). It raises the same typed failures (402 balance / 409 caps / 500
    # publish) this path always did.
    session = await begin_active_session(
        db, user, plug, gateway,
        max_kwh=req.max_kwh,
        max_duration_seconds=req.max_duration_seconds,
        covering_reservation=covering_reservation,
    )

    logger.info(
        "Session started",
        extra={
            "session_id": session.id, "user_id": user.id, "email": user.email,
            "plug_id": plug.id, "gateway_id": plug.gateway_id,
        },
    )

    # [Reservations] Advance warning: if another member has this plug booked
    # SOON, this walk-up session will be force-stopped when that window opens
    # (services/session_reaper.py reap_reservation_starts_once). Tell the driver
    # up front so the stop isn't a surprise. A capped session only runs into
    # bookings before its own end; an uncapped one uses the policy lookahead.
    # Best-effort and fully non-blocking — a successful start must never fail on
    # this warning path (local imports: this router's shared header is a merge
    # hot-spot across the parallel feature branches, same as the gate above).
    try:
        from backend.services import reservations as reservation_policy
        from backend.services.reservations import next_conflicting_reservation

        if req.max_duration_seconds:
            # A duration-capped session can't run past its own cap, so it can
            # only collide with a booking starting before then — use the cap as
            # the horizon (it's the default 4 h since 2026-07-12 unless the
            # driver set a shorter limit).
            horizon_min = req.max_duration_seconds / 60
        else:
            # No cap (legacy / explicit null) — no known end, so fall back to
            # the policy heuristic horizon.
            horizon_min = reservation_policy.RESERVATION_WALKUP_WARN_LOOKAHEAD_MIN
        upcoming = await next_conflicting_reservation(
            db, plug.id, user.id,
            horizon=now_utc + timedelta(minutes=horizon_min),
            now=now_utc,
        )
        if upcoming is not None:
            from backend.services.notifications import notify
            await notify(
                user.id,
                "reservation_ahead",
                "Heads up — this plug is reserved soon",
                (
                    f"{plug.name} is reserved from "
                    f"{upcoming.start_at.astimezone(timezone.utc):%H:%M} UTC by "
                    "another member — your session will be stopped then."
                ),
                severity="warning",
                plug_id=plug.id,
                session_id=session.id,
            )
    except Exception:
        logger.warning("Walk-up reservation warning failed", exc_info=True)

    return {
        "status": "started",
        "session_id": session.id,
        "plug_id": plug.id,
        "plug_name": plug.name,
        # [Session limits] Echo the effective stop conditions so the UI can
        # show progress toward them without a second fetch.
        "max_kwh": session.max_kwh,
        "max_duration_seconds": session.max_duration_seconds,
        "message": f"Charging started on {plug.name}.",
    }


@router.post("/api/sessions/stop")
async def stop_charging_session(
    req: SessionStopRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Stop an active charging session (billing + cleanup happen in
    finalize_charging_session, shared with the session reaper).
    """
    result = await finalize_charging_session(
        db, req.session_id, expected_user_id=user.id
    )
    if result is not None:
        return result

    # Nothing was finalized — distinguish "not yours/doesn't exist" from
    # "already finished" for the API contract. Race-safe: finalization
    # definitively did not happen in this request.
    check = await db.execute(
        select(ChargingSession).where(
            and_(
                ChargingSession.id == req.session_id,
                ChargingSession.user_id == user.id,
            )
        )
    )
    if check.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    raise HTTPException(status_code=400, detail="This session is not active.")


@router.patch("/api/sessions/{session_id}/limits")
async def update_session_limits(
    session_id: int,
    req: SessionLimitsUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Update a RUNNING session's stop conditions (max_kwh / max_duration_seconds)
    after it started — "start now, set the target later". Send whichever you're
    changing (400 if neither).

    Enforcement is backend-side and near-immediate: _persist_telemetry reads the
    session's limits fresh from the DB on every telemetry frame (~1 s), so the
    shared finalize auto-stop honors the new value within ~1 s. The firmware's
    local relay watchdog still holds the limit sent in the ON payload at start,
    so RAISING a limit above that on-device value is bounded by the charger
    until a firmware SET_LIMITS command ships (tracked follow-up); lowering /
    adjusting within it is exact. Billing is always metered energy, so neither
    case can misbill.

    Auth hold: changing max_kwh re-sizes this session's hold (hold_coins) with
    the same min(available, max_kwh * rate) rule the start path uses — grown so
    a raised ceiling stays covered, shrunk to free the difference when lowered.
    Recomputed under the user-row lock (taken first; user -> session order here
    has no cycle with finalize's session -> user). Legacy NULL-hold sessions
    keep a NULL hold.

    [Security] The resized hold is FLOORED at this session's already-accrued
    cost (billing.session_cost at the session's current energy_kwh — the same
    segment-aware function finalize bills with), and a max_kwh below energy
    already delivered is rejected outright (409). Without both: finalize caps
    the debit at min(final_cost, hold_coins), so lowering max_kwh below energy
    already consumed could shrink the hold under what's owed and forgive the
    difference (e.g. charge 29 kWh, PATCH max_kwh to 0.1, pay ~0.1 kWh worth).
    """
    updates = {}
    if req.max_kwh is not None:
        updates["max_kwh"] = req.max_kwh
    if req.max_duration_seconds is not None:
        updates["max_duration_seconds"] = req.max_duration_seconds
    if not updates:
        raise HTTPException(
            status_code=400,
            detail="Provide max_kwh and/or max_duration_seconds to update.",
        )

    # Lock the user row first so the hold recompute below can't race a
    # concurrent start/patch for this user (the same lock the start path takes).
    locked_user = await db.execute(
        select(User).where(User.id == user.id).with_for_update()
    )
    if locked_user.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="User not found.")

    result = await db.execute(
        select(ChargingSession)
        .where(
            and_(
                ChargingSession.id == session_id,
                ChargingSession.user_id == user.id,
            )
        )
        .with_for_update()
    )
    session = result.scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    if session.status != SessionStatus.ACTIVE:
        raise HTTPException(
            status_code=409,
            detail="This session is not active — its limits can no longer change.",
        )

    # [Security] Reject a target below energy already delivered — checked
    # against the persisted energy_kwh BEFORE any mutation below. Without
    # this, a 200 here would be followed within ~1 s by the telemetry-path
    # auto-stop mirror finalizing the session (energy >= max_kwh) — a target
    # a driver could never actually charge down to, only exploit.
    if "max_kwh" in updates and updates["max_kwh"] < (session.energy_kwh or 0.0):
        raise HTTPException(
            status_code=409,
            detail="target is below energy already delivered",
        )

    # Load the plug once — needed for the TOD hold re-sizing below and to push
    # the updated watchdogs to the firmware after the commit.
    plug = (
        await db.execute(select(Plug).where(Plug.id == session.plug_id))
    ).scalar_one()

    if "max_duration_seconds" in updates:
        session.max_duration_seconds = updates["max_duration_seconds"]
    if "max_kwh" in updates:
        session.max_kwh = updates["max_kwh"]
    # Re-size the hold whenever max_kwh OR max_duration_seconds changes (skip
    # legacy NULL-hold ones). available_balance() already nets out THIS session's
    # own hold (it's ACTIVE), so add it back to get what this session may reserve,
    # then cap by max_kwh * rate — the exact start-path sizing. A longer window
    # can cross into a higher-rate TOD slot, so max_rate_over_window (and thus the
    # hold) can change even when only max_duration_seconds is edited.
    if (
        ("max_kwh" in updates or "max_duration_seconds" in updates)
        and session.hold_coins is not None
        and session.max_kwh is not None  # no kWh limit → no energy_cost to size against
    ):
        # [Pricing v2] Size at the WORST-CASE rate over the session's window
        # (not the current segment rate), so raising max_kwh on a TOD plug
        # whose price rises later can't leave the hold under-covering and
        # forgive overage — matches the start path. Flat tariff => the flat
        # rate => unchanged from the old single-rate sizing.
        max_rate = await max_rate_over_window(
            db, plug, datetime.now(timezone.utc),
            session.max_duration_seconds or 24 * 3600,
        )
        headroom = await available_balance(db, user.id) + session.hold_coins
        # [Security] Floor the resize at what this session has ALREADY
        # accrued — the same segment-aware session_cost finalize bills with —
        # so a lowered max_kwh can never shrink the hold below cost already
        # owed. finalize caps the debit at min(final_cost, hold_coins), so an
        # under-floored hold here would silently forgive the difference (the
        # "charge 29 kWh, PATCH max_kwh to 0.1" exploit).
        accrued_floor = to_money(session_cost(session, session.energy_kwh or 0.0))
        resized = to_money(min(headroom, energy_cost(session.max_kwh, max_rate)))
        session.hold_coins = to_money(max(accrued_floor, resized))

    await db.commit()
    await db.refresh(session)

    # [Firmware SET_LIMITS] Push the updated watchdog thresholds to the gateway
    # so RAISING a limit above the value baked into the original ON payload
    # actually takes effect on-device. The firmware's SET_LIMITS handler updates
    # max_kwh/max_duration in place WITHOUT re-baselining energy (PR #35), so
    # billing is unaffected and re-pushing ON (which would re-baseline) is
    # avoided. Best-effort: the telemetry-path backend mirror already enforces
    # the new limit within ~1 s regardless, so a failed publish never fails this
    # request; a gateway that lacks SET_LIMITS (pre-1.x fw) simply ignores it.
    # Legacy NULL-limit sessions never carried firmware limits — skip them.
    if (
        state.mqtt_manager is not None
        and session.max_kwh is not None
        and session.max_duration_seconds is not None
    ):
        from backend.services.caps import effective_plug_cap
        state.mqtt_manager.send_plug_limits(
            gateway_id=plug.gateway_id,
            plug_id=plug.id,
            max_kwh=session.max_kwh,
            max_duration_seconds=session.max_duration_seconds,
            local_ip=plug.local_ip,
            # Re-arm the on-device OVERCURRENT_CAP at the plug's effective cap
            # (its own max_current_a, or DEFAULT_PLUG_CAP_A) — same resolution
            # as the ON path, so a cap the operator changed mid-session lands.
            max_current_a=effective_plug_cap(plug),
        )

    logger.info(
        "Session limits updated",
        extra={
            "session_id": session.id, "user_id": user.id,
            "max_kwh": session.max_kwh,
            "max_duration_seconds": session.max_duration_seconds,
        },
    )

    return {
        "status": "updated",
        "session_id": session.id,
        "max_kwh": session.max_kwh,
        "max_duration_seconds": session.max_duration_seconds,
    }


@router.get("/api/sessions/active")
async def get_active_session(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    All ACTIVE charging sessions for the logged-in user, newest first (a user
    may run up to MAX_ACTIVE_SESSIONS_PER_USER concurrently). The top-level
    single-session fields mirror the newest entry for older clients.
    """
    result = await db.execute(
        select(ChargingSession, Plug.name)
        .join(Plug, Plug.id == ChargingSession.plug_id)
        .where(
            and_(
                ChargingSession.user_id == user.id,
                ChargingSession.status == SessionStatus.ACTIVE,
            )
        )
        .order_by(ChargingSession.started_at.desc())
    )
    sessions = [
        {
            "session_id": session.id,
            "plug_id": session.plug_id,
            "plug_name": plug_name,
            "started_at": session.started_at.isoformat() if session.started_at else None,
            # [Session limits] The stop conditions this session was started
            # with (NULL for legacy pre-limit sessions) — the monitor shows
            # progress toward them ("0.42 / 1.00 kWh · stops automatically").
            "max_kwh": session.max_kwh,
            "max_duration_seconds": session.max_duration_seconds,
        }
        for session, plug_name in result.all()
    ]
    if not sessions:
        return {"active": False, "sessions": []}

    return {"active": True, "sessions": sessions, **sessions[0]}


@router.get("/api/sessions/history")
async def get_session_history(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = 50,
    offset: int = 0,
):
    """
    Past charging sessions for the current user, most recent first.

    [redesign/ui-v3 contract §4] Paginated: returns ``{"total": int,
    "items": [...]}`` with the house limit/offset params (limit capped at
    200, default 50), and each row now carries ``plug_name`` (single join —
    this list stays N+1-free like /api/sessions/active).
    """
    limit, offset = max(1, min(limit, 200)), max(0, offset)

    total = (
        await db.execute(
            select(func.count(ChargingSession.id)).where(
                ChargingSession.user_id == user.id
            )
        )
    ).scalar() or 0

    result = await db.execute(
        select(ChargingSession, Plug.name)
        .join(Plug, Plug.id == ChargingSession.plug_id)
        .where(ChargingSession.user_id == user.id)
        .order_by(ChargingSession.started_at.desc(), ChargingSession.id.desc())
        .limit(limit)
        .offset(offset)
    )

    return {
        "total": int(total),
        "items": [
            {
                "id": s.id,
                "plug_id": s.plug_id,
                "plug_name": plug_name,
                "started_at": s.started_at.isoformat() if s.started_at else None,
                "ended_at": s.ended_at.isoformat() if s.ended_at else None,
                "energy_kwh": round(s.energy_kwh, 3),
                "coins_spent": round(float(s.coins_spent), 2),
                "status": s.status.value,
            }
            for s, plug_name in result.all()
        ],
    }


# ===========================================================================
# Queued Charge Endpoints (queue a charge during an outage → the reaper
# auto-starts it when line power returns — services/session_reaper.py
# reap_queued_starts_once. See docs/proposals/queued-charge-offline-plug.md.)
# ===========================================================================

def _queued_charge_response(qc) -> dict:
    """The 201 body for a WAITING queued charge (POST /queue) — the exact shape
    a frontend agent depends on."""
    return {
        "id": qc.id,
        "plug_id": qc.plug_id,
        "status": qc.status.value,
        "created_at": qc.created_at.isoformat() if qc.created_at else None,
        "expires_at": qc.expires_at.isoformat() if qc.expires_at else None,
        "max_kwh": qc.max_kwh,
        "max_duration_seconds": qc.max_duration_seconds,
    }


@router.post("/api/sessions/queue", status_code=201)
async def queue_charge(
    req: QueueChargeRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    [Queued charge] Queue an auto-start on a plug whose gateway is ONLINE but
    whose line power is out (the plug stopped reporting telemetry while its
    gateway stays up). When power returns and holds for the CPO's debounce, the
    session reaper starts the session with the snapshotted stop conditions.

    Validates (structured 409 `detail.code` on each reject, like the caps
    `circuit_full` block, so the UI can branch): the plug exists and is
    accessible; its gateway is live (`gateway_offline`); it is currently
    UNpowered (`plug_powered` — a powered plug should just be started); the CPO
    has queued charging enabled for it (`queue_disabled`); the driver's
    AVAILABLE balance clears the start floor (402 `insufficient_balance` — funds
    are NOT locked, only re-checked at auto-start); the driver is under the
    per-user queue cap (`queue_limit`); and no WAITING queue already exists for
    this (driver, plug) (`already_queued`, also a DB partial-unique backstop).
    """
    from backend.database.models import QueuedCharge, QueuedChargeStatus
    from backend.services.session_start import queue_ttl, queued_charging_enabled

    result = await db.execute(select(Plug).where(Plug.id == req.plug_id))
    plug = result.scalar_one_or_none()
    if not plug:
        raise HTTPException(status_code=404, detail=f"Plug {req.plug_id} not found.")

    # Access check for private groups — a driver must not queue a plug they
    # can't see (mirrors the start path's membership gate).
    if plug.group_id:
        group_result = await db.execute(
            select(ChargerGroup).where(ChargerGroup.id == plug.group_id)
        )
        group = group_result.scalar_one_or_none()
        if group and not group.is_public:
            membership_result = await db.execute(
                select(GroupMembership).where(
                    and_(
                        GroupMembership.user_id == user.id,
                        GroupMembership.group_id == group.id,
                    )
                )
            )
            if not membership_result.scalar_one_or_none():
                raise HTTPException(
                    status_code=403,
                    detail="You don't have access to this plug. Join the group first.",
                )

    gw_result = await db.execute(select(Gateway).where(Gateway.id == plug.gateway_id))
    gateway = gw_result.scalar_one()
    tenant = (
        await db.execute(select(Tenant).where(Tenant.id == gateway.tenant_id))
    ).scalar_one()

    # Queueing only makes sense while the gateway can still relay the eventual
    # ON — a dead gateway can't be commanded when power returns.
    if not gateway_is_live(gateway):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "gateway_offline",
                "message": "This charger's gateway is offline — nothing can start it later. Try again once it reconnects.",
            },
        )
    # The whole point is an UNpowered plug. A powered one should just be started.
    if plug_is_powered(plug):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "plug_powered",
                "message": "This charger has power right now — just start charging instead of queueing.",
            },
        )
    if not queued_charging_enabled(tenant, plug):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "queue_disabled",
                "message": "Queued charging isn't enabled for this charger.",
            },
        )

    # Balance floor (funds are NOT locked — re-checked at auto-start), gated on
    # the AVAILABLE balance like the start path.
    available = await available_balance(db, user.id)
    if available < MIN_START_BALANCE_COINS:
        raise HTTPException(
            status_code=402,
            detail={
                "code": "insufficient_balance",
                "message": (
                    f"Insufficient available balance. You have {available} coins available; "
                    f"minimum {MIN_START_BALANCE_COINS:g} required to queue a charge."
                ),
            },
        )

    waiting_count = (await db.execute(
        select(func.count())
        .select_from(QueuedCharge)
        .where(
            and_(
                QueuedCharge.user_id == user.id,
                QueuedCharge.status == QueuedChargeStatus.WAITING,
            )
        )
    )).scalar_one()
    if waiting_count >= MAX_QUEUED_CHARGES_PER_USER:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "queue_limit",
                "message": (
                    f"You already have {waiting_count} queued charge(s) "
                    f"(limit {MAX_QUEUED_CHARGES_PER_USER})."
                ),
            },
        )

    existing = (await db.execute(
        select(QueuedCharge.id).where(
            and_(
                QueuedCharge.plug_id == plug.id,
                QueuedCharge.user_id == user.id,
                QueuedCharge.status == QueuedChargeStatus.WAITING,
            )
        )
    )).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "already_queued",
                "message": "You've already queued a charge on this plug.",
            },
        )

    now = datetime.now(timezone.utc)
    qc = QueuedCharge(
        tenant_id=gateway.tenant_id,
        user_id=user.id,
        plug_id=plug.id,
        max_kwh=req.max_kwh,
        max_duration_seconds=req.max_duration_seconds,
        status=QueuedChargeStatus.WAITING,
        expires_at=now + timedelta(minutes=queue_ttl(tenant, plug)),
    )
    db.add(qc)
    try:
        await db.commit()
    except IntegrityError:
        # Lost a double-submit race to the partial unique index — already queued.
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "already_queued",
                "message": "You've already queued a charge on this plug.",
            },
        )
    await db.refresh(qc)

    logger.info(
        "Charge queued",
        extra={
            "queued_charge_id": qc.id, "user_id": user.id,
            "plug_id": plug.id, "gateway_id": plug.gateway_id,
        },
    )

    # Notify the driver their charge is queued (best-effort — never raises).
    from backend.services.notifications import notify
    await notify(
        user.id,
        "queued",
        "Charge queued",
        (
            f"{plug.name} has no power right now — your charge is queued and will "
            "start automatically when power returns."
        ),
        severity="info",
        plug_id=plug.id,
    )

    return _queued_charge_response(qc)


@router.get("/api/sessions/queued")
async def get_queued_charges(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """[Queued charge] The driver's WAITING queued charges (soonest-created
    first), with the plug name and the expiry the UI counts down to."""
    from backend.database.models import QueuedCharge, QueuedChargeStatus

    result = await db.execute(
        select(QueuedCharge, Plug.name)
        .join(Plug, Plug.id == QueuedCharge.plug_id)
        .where(
            and_(
                QueuedCharge.user_id == user.id,
                QueuedCharge.status == QueuedChargeStatus.WAITING,
            )
        )
        .order_by(QueuedCharge.created_at)
    )
    return [
        {
            "id": qc.id,
            "plug_id": qc.plug_id,
            "plug_name": plug_name,
            "status": qc.status.value,
            "created_at": qc.created_at.isoformat() if qc.created_at else None,
            "expires_at": qc.expires_at.isoformat() if qc.expires_at else None,
            "max_kwh": qc.max_kwh,
            "max_duration_seconds": qc.max_duration_seconds,
        }
        for qc, plug_name in result.all()
    ]


@router.delete("/api/sessions/queue/{queued_charge_id}")
async def cancel_queued_charge(
    queued_charge_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """[Queued charge] Cancel a WAITING queued charge (owner only). Row-locked +
    re-checked WAITING so a cancel racing the reaper's auto-start settles once:
    if the reaper already started/expired it, this is a 409, not a spurious
    cancel. Idempotent-ish — a missing/foreign row is a 404."""
    from backend.database.models import QueuedCharge, QueuedChargeStatus

    result = await db.execute(
        select(QueuedCharge)
        .where(
            and_(
                QueuedCharge.id == queued_charge_id,
                QueuedCharge.user_id == user.id,
            )
        )
        .with_for_update()
    )
    qc = result.scalar_one_or_none()
    if qc is None:
        raise HTTPException(status_code=404, detail="Queued charge not found.")
    if qc.status != QueuedChargeStatus.WAITING:
        raise HTTPException(
            status_code=409,
            detail=f"This queued charge is no longer waiting ({qc.status.value}).",
        )

    qc.status = QueuedChargeStatus.CANCELLED
    await db.commit()

    logger.info(
        "Queued charge cancelled",
        extra={"queued_charge_id": qc.id, "user_id": user.id, "plug_id": qc.plug_id},
    )
    return {"status": "cancelled"}


# ===========================================================================
# Session Dispute Endpoint (coins-only refund flow — see SessionDispute /
# MARKET_GAP_ANALYSIS.md §3 "Refunds". CPO-side review lives in
# backend/routers/cpo.py: GET /api/cpo/disputes, POST
# /api/cpo/disputes/{id}/resolve.)
# ===========================================================================

def _dispute_response(dispute: SessionDispute) -> DisputeResponse:
    return DisputeResponse(
        id=dispute.id,
        session_id=dispute.session_id,
        tenant_id=dispute.tenant_id,
        driver_user_id=dispute.driver_user_id,
        reason=dispute.reason,
        status=dispute.status.value,
        resolution_note=dispute.resolution_note,
        refund_coins=float(dispute.refund_coins) if dispute.refund_coins is not None else None,
        created_at=dispute.created_at.isoformat() if dispute.created_at else None,
        resolved_at=dispute.resolved_at.isoformat() if dispute.resolved_at else None,
        resolved_by_user_id=dispute.resolved_by_user_id,
    )


@router.post("/api/sessions/{session_id}/dispute", response_model=DisputeResponse)
async def dispute_session(
    session_id: int,
    req: DisputeCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    File a dispute against one of the caller's own finished charging
    sessions. Coins-only remedy: an approved dispute credits the driver's
    coin wallet — there is no Razorpay money-out path (see
    MARKET_GAP_ANALYSIS.md §3 "Refunds"). The CPO who owns the session's plug
    reviews it via GET /api/cpo/disputes and
    POST /api/cpo/disputes/{id}/resolve.

    Rules:
    - The session must belong to the caller (404 otherwise — same
      not-yours-or-doesn't-exist ambiguity /api/sessions/stop uses).
    - The session must be finished, i.e. not ACTIVE (409): a live session
      hasn't billed a final amount yet, so there's nothing to dispute.
    - At most one OPEN dispute per session (409) — also enforced by a
      partial unique DB index (SessionDispute.__table_args__), so a
      double-submit race can't slip two past this check-then-insert.
    """
    result = await db.execute(
        select(ChargingSession).where(
            and_(ChargingSession.id == session_id, ChargingSession.user_id == user.id)
        )
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found.")

    if session.status == SessionStatus.ACTIVE:
        raise HTTPException(
            status_code=409,
            detail="This session is still active. Stop it before filing a dispute.",
        )

    existing = await db.execute(
        select(SessionDispute.id).where(
            and_(
                SessionDispute.session_id == session_id,
                SessionDispute.status == DisputeStatus.OPEN,
            )
        )
    )
    if existing.first() is not None:
        raise HTTPException(
            status_code=409,
            detail="An open dispute already exists for this session.",
        )

    dispute = SessionDispute(
        session_id=session.id,
        tenant_id=session.tenant_id,
        driver_user_id=user.id,
        reason=req.reason,
        status=DisputeStatus.OPEN,
    )
    db.add(dispute)
    try:
        await db.commit()
    except IntegrityError:
        # Lost a double-submit race to the partial unique index — the
        # pre-check above missed a dispute committed concurrently.
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="An open dispute already exists for this session.",
        )
    await db.refresh(dispute)

    logger.info(
        "Dispute filed",
        extra={
            "dispute_id": dispute.id, "session_id": session.id,
            "user_id": user.id, "email": user.email,
        },
    )

    return _dispute_response(dispute)


# ===========================================================================
# GST Tax Invoice (appended — feat/gst-invoices; local imports here are
# intentional, not an oversight, to avoid touching the shared header import
# block above, which a concurrent wallet/hold change also edits.)
# ===========================================================================
from fastapi.responses import HTMLResponse  # noqa: E402

from backend.services.invoices import (  # noqa: E402
    SessionNotInvoiceableError,
    invoice_to_dict,
    issue_invoice_for_session,
    render_invoice_html,
)


@router.get("/api/sessions/{session_id}/invoice")
async def get_session_invoice(
    session_id: int,
    format: Optional[str] = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Fetch the GST tax invoice for a finished, billed session — issuing it on
    the first call (services/invoices.py issue_invoice_for_session is
    idempotent, so every later call just returns the same invoice_number).

    Access: the driver who owns the session, or a cpo/admin of the owning
    tenant. Anyone else gets 404 — existence isn't leaked by a 403, same
    convention as stop_charging_session's ownership check and
    cpo_cancel_payout's cross-tenant guard. `?format=html` returns a
    minimal printable invoice (inline CSS, no PDF dependency — meant to be
    saved/printed via the browser's own print dialog) instead of JSON.
    """
    session_result = await db.execute(
        select(ChargingSession).where(ChargingSession.id == session_id)
    )
    session = session_result.scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")

    if user.role == UserRole.DRIVER:
        if session.user_id != user.id:
            raise HTTPException(status_code=404, detail="Session not found.")
    elif user.role == UserRole.CPO:
        if session.tenant_id != user.tenant_id:
            raise HTTPException(status_code=404, detail="Session not found.")
    elif user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Access denied.")

    try:
        invoice = await issue_invoice_for_session(db, session_id)
    except SessionNotInvoiceableError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if format == "html":
        return HTMLResponse(content=await render_invoice_html(db, invoice))

    return invoice_to_dict(invoice)


# ===========================================================================
# Driver-side gap endpoints (redesign/ui-v3 — plans/redesign-v3-contract.md
# §4 "Driver gaps"). Appended with local imports, same merge-hot-spot
# rationale as the GST section above.
#
# ROUTE ORDER MATTERS: GET /api/sessions/{session_id} below is a catch-all
# for the /api/sessions/* GET namespace — a non-integer segment 422s rather
# than falling through — so every STATIC sibling (/active, /history,
# /queued above; /disputes/my and /me/stats here) must be registered
# BEFORE it. Keep it the LAST GET route in this file.
# ===========================================================================
import re  # noqa: E402

from backend.database.models import LedgerTransaction, TransactionType  # noqa: E402
from backend.services.pricing import default_rate  # noqa: E402


@router.get("/api/me/stats")
async def get_my_stats(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Current-calendar-month + lifetime charging aggregates for the caller,
    for the driver dashboard/account page:
    ``{month: {energy_kwh, spend_coins, sessions}, lifetime: {...}}``.

    Only FINISHED sessions count (status != ACTIVE — completed/paid/
    cancelled): ``coins_spent`` is only written at finalize, so an ACTIVE
    session has no billed spend yet and would skew energy-vs-spend. Same
    stored-column semantics /api/sessions/history rows expose. "Month" is
    the current UTC calendar month (started_at >= the 1st, 00:00 UTC).
    """
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    finished = and_(
        ChargingSession.user_id == user.id,
        ChargingSession.status != SessionStatus.ACTIVE,
    )

    async def _bucket(*extra_conditions):
        row = (
            await db.execute(
                select(
                    func.count(ChargingSession.id),
                    func.coalesce(func.sum(ChargingSession.energy_kwh), 0),
                    func.coalesce(func.sum(ChargingSession.coins_spent), 0),
                ).where(finished, *extra_conditions)
            )
        ).first()
        count, energy, coins = row if row else (0, 0, 0)
        return {
            "energy_kwh": round(float(energy or 0), 3),
            "spend_coins": round(float(coins or 0), 2),
            "sessions": int(count or 0),
        }

    return {
        "month": await _bucket(ChargingSession.started_at >= month_start),
        "lifetime": await _bucket(),
    }


@router.get("/api/sessions/disputes/my")
async def get_my_disputes(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The caller's disputes, newest first — the driver-side mirror of the
    CPO's GET /api/cpo/disputes (which owns resolution)."""
    result = await db.execute(
        select(SessionDispute)
        .where(SessionDispute.driver_user_id == user.id)
        .order_by(SessionDispute.created_at.desc(), SessionDispute.id.desc())
    )
    return [
        {
            "id": d.id,
            "session_id": d.session_id,
            "status": d.status.value,
            "reason": d.reason,
            "resolution_note": d.resolution_note,
            "refund_coins": float(d.refund_coins) if d.refund_coins is not None else None,
            "created_at": d.created_at.isoformat() if d.created_at else None,
            "resolved_at": d.resolved_at.isoformat() if d.resolved_at else None,
        }
        for d in result.scalars().all()
    ]


@router.get("/api/sessions/{session_id}")
async def get_session_detail(
    session_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Full receipt/detail for ONE session — the same field shape
    finalize_charging_session's stop response returns (so the frontend's
    receipt component renders either interchangeably), with ``status``
    carrying the session's real status (a live session is viewable too) and
    ``plug_name`` joined in.

    Access mirrors GET /api/sessions/{id}/invoice: the driver who owns the
    session, or a cpo/admin of the owning tenant — anyone else gets 404, not
    403, so existence isn't leaked.

    Derivation notes for a STORED session (vs. the live stop path):
    - ``coins_spent`` is the finalized debit; ``shortfall_coins`` is
      re-derived as billed-cost-minus-collected via services/billing
      session_cost (0 while ACTIVE — nothing billed yet).
    - ``balance_before``/``balance_remaining`` come from the session's
      SESSION_DEBIT ledger row (None while ACTIVE / for a pre-ledger row).
    - ``reason`` is recovered from that ledger row's "[reason]" suffix
      (finalize embeds it in the description); None when there wasn't one.
    """
    row = (
        await db.execute(
            select(ChargingSession, Plug.name)
            .outerjoin(Plug, Plug.id == ChargingSession.plug_id)
            .where(ChargingSession.id == session_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    session, plug_name = row

    if user.role == UserRole.DRIVER:
        if session.user_id != user.id:
            raise HTTPException(status_code=404, detail="Session not found.")
    elif user.role == UserRole.CPO:
        if session.tenant_id != user.tenant_id:
            raise HTTPException(status_code=404, detail="Session not found.")
    elif user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Access denied.")

    energy = session.energy_kwh or 0.0
    rate = (
        session.rate_coins_per_kwh
        if session.rate_coins_per_kwh is not None
        else default_rate()
    )
    coins_spent = float(session.coins_spent or 0)

    is_active = session.status == SessionStatus.ACTIVE
    shortfall = 0.0
    if not is_active:
        # Billed cost re-derived exactly as finalize computed it (segment-aware).
        shortfall = max(float(session_cost(session, energy)) - coins_spent, 0.0)

    # The finalize-time SESSION_DEBIT ledger row carries the wallet balances
    # (admin adjustments use session_id NULL, refunds use REFUND — no clash).
    ledger = (
        await db.execute(
            select(LedgerTransaction)
            .where(
                and_(
                    LedgerTransaction.session_id == session.id,
                    LedgerTransaction.transaction_type == TransactionType.SESSION_DEBIT,
                )
            )
            .order_by(LedgerTransaction.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    balance_before = balance_remaining = None
    reason = None
    if ledger is not None:
        if ledger.balance_after is not None:
            balance_remaining = round(float(ledger.balance_after), 2)
            # amount is the signed delta (negative debit): before = after - amount.
            balance_before = round(float(ledger.balance_after - ledger.amount), 2)
        if ledger.description:
            m = re.search(r"\[([^\]]+)\]", ledger.description)
            if m:
                reason = m.group(1)

    duration_sec = None
    if session.started_at and session.ended_at:
        started = session.started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        ended = session.ended_at
        if ended.tzinfo is None:
            ended = ended.replace(tzinfo=timezone.utc)
        duration_sec = int((ended - started).total_seconds())

    return {
        "status": session.status.value,
        "session_id": session.id,
        "plug_id": session.plug_id,
        "plug_name": plug_name if plug_name is not None else f"Plug #{session.plug_id}",
        "energy_kwh": round(energy, 3),
        "peak_power_w": round(session.peak_power_w or 0.0, 1),
        "price_per_kwh": float(rate),
        "settled_cost_coins": (
            float(session.settled_cost_coins)
            if session.settled_cost_coins is not None else None
        ),
        "coins_spent": round(coins_spent, 2),
        "shortfall_coins": round(shortfall, 2),
        "balance_before": balance_before,
        "balance_remaining": balance_remaining,
        "duration_sec": duration_sec,
        "started_at": session.started_at.isoformat() if session.started_at else None,
        "ended_at": session.ended_at.isoformat() if session.ended_at else None,
        "max_kwh": session.max_kwh,
        "max_duration_seconds": session.max_duration_seconds,
        "reason": reason,
    }


