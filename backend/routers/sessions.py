"""
Sessions routes — moved verbatim from main.py (2026-07-07, TD#7 split).
"""
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import Date, and_, cast, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend import state
from backend.database.db import async_session_factory, get_db
from backend.database.models import (
    ChargerGroup, ChargingSession, DisputeStatus, Gateway, GatewayStatus,
    GroupMembership, LedgerTransaction, Plug, PlugStatus, SessionDispute,
    SessionStatus, TelemetryReading, Tenant, TransactionType, User, UserRole,
)
from backend.schemas import (
    AuthResponse, CpoGatewayCreateRequest, CpoGroupCreateRequest,
    CpoGroupUpdateRequest, CpoPlugCreateRequest, CpoPlugUpdateRequest,
    CpoSetupRequest, CreateOrderRequest, CreateOrderResponse,
    DirectPlugRequest, DisputeCreateRequest, DisputeResponse,
    GatewayRegisterRequest, GroupResponse, JoinGroupRequest, LoginRequest,
    PlugRegisterRequest, PlugResponse, RegisterRequest,
    SessionLimitsUpdateRequest, SessionStartRequest,
    SessionStopRequest, UserResponse, VerifyPaymentRequest,
)
from backend.services import payments as payment_service
from backend.services.auth import (
    create_access_token, decode_access_token, get_current_user,
    hash_password, verify_password,
)
from backend.services.money import ZERO_MONEY, energy_cost, to_money
from backend.services.pricing import resolve_rate_for_plug
from backend.services.rbac import require_role
from backend.services.session_lifecycle import (
    check_and_speed_up_active_session, finalize_charging_session,
    gateway_is_live, set_plug_telemetry_interval,
)
from backend.services.telemetry import COINS_PER_KWH
from backend.services.wallet import available_balance

logger = logging.getLogger("amphive.api")
router = APIRouter()

# How many charging sessions a user may run concurrently (e.g. two vehicles
# on two plugs). The API and UI list every active session, so raising this
# needs no code change.
MAX_ACTIVE_SESSIONS_PER_USER = int(os.getenv("MAX_ACTIVE_SESSIONS_PER_USER", "2"))

# Minimum wallet balance (coins) required to START a session — a float so a
# session can't begin with too little credit to cover meaningful charging. Also
# exposed via GET /api/config so the UI shows the same number it enforces.
# [Auth holds] Checked against the session's AVAILABLE balance (coin_balance
# minus what other ACTIVE sessions already hold — services/wallet.py
# available_balance()), not the raw wallet balance: a driver with plenty of
# coins but most of it held by another running session must not be allowed
# to start a third session past what's actually left to reserve.
# `or "50"` (not a getenv default): compose passes the var through with plain
# ${VAR} interpolation, so an unset .env entry arrives as an EMPTY string —
# float("") would crash-loop the container (the GST_RATE_PCT lesson).
MIN_START_BALANCE_COINS = float(os.getenv("MIN_START_BALANCE_COINS") or "50")

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

    # Resolve the coins-per-kWh rate for this plug (plug's own tariff -> its
    # group's tariff -> the tenant's default tariff -> the global
    # COINS_PER_KWH env fallback) and SNAPSHOT it onto the session now, so a
    # tariff edit or reassignment made mid-session never retroactively
    # changes what this session bills (see services/pricing.py).
    rate_coins_per_kwh = await resolve_rate_for_plug(db, plug)

    # Size the session's authorization hold (MARKET_GAP_ANALYSIS.md §3): the
    # old flat MIN_START_BALANCE_COINS floor let a session start with just
    # enough to *begin*, then finalize forgave any overage past the wallet
    # (min(final_cost, balance) — the revenue leak SECURITY.md §5 flags).
    # Instead, reserve this session's worst-case cost up front: the smaller
    # of (a) what's actually left to spend right now — coin_balance net of
    # whatever this user's OTHER active sessions already hold, so two
    # concurrent starts (serialized on the user-row lock taken in step 0)
    # can never double-reserve the same coins — and (b) the most this
    # session could possibly bill, its own max_kwh cap at the resolved rate.
    # The 402 floor now gates on this AVAILABLE figure, not the raw balance:
    # a driver with plenty of coins but most of it held by another running
    # session must not be allowed to reserve past what's actually left.
    available = await available_balance(db, user.id)
    # The floor gates the driver's AVAILABLE balance, not the hold: the hold is
    # capped by the session's own max_kwh, so a small session (e.g. 5 kWh at
    # 5/kWh = 25 coins) legitimately reserves less than the floor — rejecting
    # on the hold would block modest sessions for fully-funded wallets (found
    # live in prod 2026-07-12). The floor's job is only to keep dust wallets
    # from starting; the hold's job is to bound what this session can bill.
    if available < MIN_START_BALANCE_COINS:
        raise HTTPException(
            status_code=402,
            detail=(
                f"Insufficient available balance. You have {available} coins "
                f"available for this session (wallet balance "
                f"{user.coin_balance} coins; the difference, if any, is held "
                f"by another active session). Minimum {MIN_START_BALANCE_COINS:g} "
                "required to start."
            ),
        )
    hold = to_money(min(available, energy_cost(req.max_kwh, rate_coins_per_kwh)))

    session = ChargingSession(
        tenant_id=gateway.tenant_id,
        user_id=user.id,
        plug_id=plug.id,
        status=SessionStatus.ACTIVE,
        rate_coins_per_kwh=rate_coins_per_kwh,
        hold_coins=hold,
        # [Session limits] Snapshot the request's stop conditions (always,
        # including the schema defaults) — the firmware enforces these
        # locally from the MQTT ON payload below but publishes no alarm on
        # the cutoff, so the backend mirrors the enforcement from these
        # persisted values (MQTTManager._maybe_auto_stop_on_limits + the
        # reaper's duration backstop). Note the hold sizing above already
        # uses req.max_kwh, so a smaller user limit automatically shrinks
        # the auth hold too.
        max_kwh=req.max_kwh,
        max_duration_seconds=req.max_duration_seconds,
    )
    db.add(session)
    if covering_reservation is not None:
        # [Reservations] The holder is starting inside their own window:
        # fulfil the booking and link the session (flush first so session.id
        # exists). Same transaction as the plug claim, so a crash can never
        # leave a FULFILLED reservation pointing at no session.
        await db.flush()
        covering_reservation.status = ReservationStatus.FULFILLED
        covering_reservation.session_id = session.id
    plug.status = PlugStatus.OCCUPIED
    # Commit the claim + session BEFORE touching hardware, releasing the lock.
    await db.commit()
    await db.refresh(session)

    # Broadcast the claim so other clients' plug lists flip to OCCUPIED live.
    from backend.services.socketio_manager import emit_plug_status
    await emit_plug_status(plug.id, PlugStatus.OCCUPIED.value)

    # 4. Now command the gateway. If this fails, undo the claim so the plug
    #    doesn't stay OCCUPIED with a live ACTIVE session nobody can drive.
    success = state.mqtt_manager.send_plug_command(
        gateway_id=plug.gateway_id,
        plug_id=plug.id,
        action="ON",
        max_duration=req.max_duration_seconds,
        max_kwh=req.max_kwh,
        session_id=session.id,
        local_ip=plug.local_ip,
    )
    if not success:
        session.status = SessionStatus.CANCELLED
        session.ended_at = datetime.now(timezone.utc)
        plug.status = PlugStatus.AVAILABLE
        if covering_reservation is not None:
            # [Reservations] The start never actually happened — give the
            # holder their window back instead of burning it as FULFILLED.
            covering_reservation.status = ReservationStatus.BOOKED
            covering_reservation.session_id = None
        await db.commit()
        await emit_plug_status(plug.id, PlugStatus.AVAILABLE.value)
        raise HTTPException(
            status_code=500,
            detail="Failed to publish start command to the gateway. The gateway may be offline.",
        )

    # 5. Initialize the telemetry stream for this plug, seeded with the same
    #    snapshotted rate so the live cost_coins preview matches what
    #    finalize_charging_session will actually bill.
    state.telemetry_store.start_session(plug.id, rate_coins_per_kwh=rate_coins_per_kwh)
    await set_plug_telemetry_interval(db, plug.id, 1000)

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

    if "max_duration_seconds" in updates:
        session.max_duration_seconds = updates["max_duration_seconds"]
    if "max_kwh" in updates:
        session.max_kwh = updates["max_kwh"]
        # Re-size the hold for a held session (skip legacy NULL-hold ones).
        # available_balance() already nets out THIS session's own hold (it's
        # ACTIVE), so add it back to get what this session may reserve, then cap
        # by the new max_kwh * rate — the exact start-path sizing.
        if session.hold_coins is not None:
            rate = (
                session.rate_coins_per_kwh
                if session.rate_coins_per_kwh is not None
                else COINS_PER_KWH
            )
            headroom = await available_balance(db, user.id) + session.hold_coins
            session.hold_coins = to_money(
                min(headroom, energy_cost(session.max_kwh, rate))
            )

    await db.commit()
    await db.refresh(session)

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
):
    """Return past charging sessions for the current user, most recent first."""
    result = await db.execute(
        select(ChargingSession)
        .where(ChargingSession.user_id == user.id)
        .order_by(ChargingSession.started_at.desc())
        .limit(50)
    )
    sessions = list(result.scalars().all())

    return [
        {
            "id": s.id,
            "plug_id": s.plug_id,
            "started_at": s.started_at.isoformat() if s.started_at else None,
            "ended_at": s.ended_at.isoformat() if s.ended_at else None,
            "energy_kwh": round(s.energy_kwh, 3),
            "coins_spent": round(s.coins_spent, 2),
            "status": s.status.value,
        }
        for s in sessions
    ]


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
    SessionNotInvoiceableError, invoice_to_dict, issue_invoice_for_session,
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


