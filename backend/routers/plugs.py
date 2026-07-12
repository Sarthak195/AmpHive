"""
Plugs routes — moved verbatim from main.py (2026-07-07, TD#7 split).
"""
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import Date, and_, cast, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend import state
from backend.database.db import async_session_factory, get_db
from backend.database.models import (
    ChargerGroup, ChargingSession, Gateway, GatewayStatus, GroupMembership,
    LedgerTransaction, Plug, PlugStatus, PlugWatch, SessionStatus,
    TelemetryReading, Tenant, TransactionType, User, UserRole,
)
from backend.schemas import (
    AuthResponse, CpoGatewayCreateRequest, CpoGroupCreateRequest,
    CpoGroupUpdateRequest, CpoPlugCreateRequest, CpoPlugUpdateRequest,
    CpoSetupRequest, CreateOrderRequest, CreateOrderResponse,
    DirectPlugRequest, GatewayRegisterRequest, GroupResponse,
    JoinGroupRequest, LoginRequest, PlugRegisterRequest, PlugResponse,
    RegisterRequest, SessionStartRequest, SessionStopRequest, UserResponse,
    VerifyPaymentRequest,
)
from backend.services import payments as payment_service
from backend.services.auth import (
    create_access_token, decode_access_token, get_current_user,
    hash_password, verify_password,
)
from backend.services.money import ZERO_MONEY, to_money
from backend.services.pricing import resolve_rate_for_plug
from backend.services.rbac import require_role
from backend.services.session_lifecycle import (
    check_and_speed_up_active_session, finalize_charging_session,
    gateway_is_live, set_plug_telemetry_interval,
)
from backend.services.telemetry import COINS_PER_KWH

logger = logging.getLogger("amphive.api")
router = APIRouter()


async def _reservation_fields_for_plugs(db, plug_ids, user_id, now):
    """
    [Reservations] The PlugResponse reservation fields (reserved_now /
    reserved_now_by_me / reserved_until / next_reservation) for a batch of
    plugs in ONE grouped query — the list endpoint below is deliberately
    N+1-free and must stay that way. Lazily expires lapsed holds first
    (services/reservations.py; committed by the caller's read path), so a
    no-show never shows as "reserved". Returns {plug_id: field-dict};
    plugs absent from the map have no current/upcoming reservations.
    """
    from backend.database.models import Reservation, ReservationStatus
    from backend.schemas import ReservationWindow
    from backend.services.reservations import expire_lapsed_reservations

    if not plug_ids:
        return {}

    expired = await expire_lapsed_reservations(db, plug_ids=plug_ids, now=now)
    if expired:
        await db.commit()  # read path — persist the lazy flip

    rows = await db.execute(
        select(Reservation)
        .where(
            and_(
                Reservation.plug_id.in_(plug_ids),
                Reservation.status == ReservationStatus.BOOKED,
                Reservation.end_at > now,
            )
        )
        .order_by(Reservation.plug_id, Reservation.start_at)
    )

    fields = {}
    for res in rows.scalars():
        entry = fields.setdefault(res.plug_id, {
            "reserved_now": False,
            "reserved_now_by_me": False,
            "reserved_until": None,
            "next_reservation": None,
        })
        if res.start_at <= now:
            # The covering window (at most one — BOOKED windows never overlap).
            entry["reserved_now"] = True
            entry["reserved_now_by_me"] = res.user_id == user_id
            entry["reserved_until"] = res.end_at.isoformat()
        elif entry["next_reservation"] is None:
            # Rows are ordered by start_at, so the first future one wins.
            entry["next_reservation"] = ReservationWindow(
                start_at=res.start_at.isoformat(),
                end_at=res.end_at.isoformat(),
            )
    return fields

# ===========================================================================
# Plug Endpoints
# ===========================================================================

async def ensure_plug_group_access(
    db: AsyncSession, plug: Plug, user: User
) -> tuple[Optional[str], bool]:
    """
    The single-plug access rule, shared by GET /api/plugs/{id} and the
    watch endpoints below: a plug in a private (non-public) group is only
    visible to members of that group.

    Returns (group_name, is_private); raises 403 when the plug belongs to a
    private group the user hasn't joined.
    """
    group_name = None
    is_private = False
    if plug.group_id:
        group_result = await db.execute(
            select(ChargerGroup).where(ChargerGroup.id == plug.group_id)
        )
        group = group_result.scalar_one_or_none()
        group_name = group.name if group else None
        is_private = bool(group and not group.is_public)

        if group and not group.is_public:
            # Private group — check membership
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
                    detail="This plug belongs to a private group. Join the group first using an access code.",
                )
    return group_name, is_private


@router.get("/api/plugs/available", response_model=List[PlugResponse])
async def get_available_plugs(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Get all plugs the current user can access:
    - Plugs in public groups
    - Plugs in private groups the user has joined
    - Ungrouped plugs (group_id = NULL, treated as public/legacy)
    """
    # One round-trip: accessibility filter via subqueries, group name and
    # gateway coords (the fallback location) via joins. Previously this was
    # 3 + N queries — one ChargerGroup.name lookup per plug.
    joined_group_ids = (
        select(GroupMembership.group_id)
        .where(GroupMembership.user_id == user.id)
        .scalar_subquery()
    )
    public_group_ids = (
        select(ChargerGroup.id)
        .where(ChargerGroup.is_public == True)
        .scalar_subquery()
    )

    rows = await db.execute(
        select(Plug, ChargerGroup.name, ChargerGroup.is_public, Gateway)
        .join(Gateway, Gateway.id == Plug.gateway_id)
        .outerjoin(ChargerGroup, ChargerGroup.id == Plug.group_id)
        .where(
            or_(
                Plug.group_id.is_(None),  # ungrouped/legacy: public to all
                Plug.group_id.in_(joined_group_ids),
                Plug.group_id.in_(public_group_ids),
            )
        )
    )

    # [Plug watches] The current user's armed "notify me when free" watches —
    # ONE extra query for the whole list (keeps this endpoint's deliberate
    # N+1-free shape; served by the UNIQUE(user_id, plug_id)'s leading column).
    watched_plug_ids = set(
        (await db.execute(
            select(PlugWatch.plug_id).where(PlugWatch.user_id == user.id)
        )).scalars().all()
    )

    now = datetime.now(timezone.utc)
    all_rows = rows.all()

    # [Reservations] reserved_now / next_reservation etc. for every listed
    # plug in one grouped query — NOT per-plug (this endpoint stays N+1-free).
    reservation_fields = await _reservation_fields_for_plugs(
        db, [plug.id for plug, _, _, _ in all_rows], user.id, now
    )

    responses = []
    for plug, group_name, group_is_public, gateway in all_rows:
        # Resolved effective rate (plug tariff -> group tariff -> tenant
        # default -> env fallback; services/pricing.py). One lookup per plug
        # — tolerable N+1 for typical per-site plug counts; batch this if the
        # cross-tenant available-plugs list ever grows large enough to matter.
        price_per_kwh = await resolve_rate_for_plug(db, plug)
        responses.append(
            PlugResponse(
                id=plug.id,
                name=plug.name,
                status=plug.status.value,
                current_power_w=plug.current_power_w,
                plug_model=plug.plug_model,
                group_name=group_name,
                latitude=plug.latitude if plug.latitude is not None else gateway.latitude,
                longitude=plug.longitude if plug.longitude is not None else gateway.longitude,
                gateway_online=gateway_is_live(gateway, now),
                price_per_kwh=float(price_per_kwh),
                # group_is_public is None for ungrouped plugs (outer join) —
                # only an explicit False marks a private group.
                is_private=group_is_public is False,
                watching=plug.id in watched_plug_ids,
                **reservation_fields.get(plug.id, {}),
            )
        )
    return responses


@router.get("/api/plugs/{plug_id}", response_model=PlugResponse)
async def get_plug(
    plug_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Look up a single plug by ID. Verifies the user has access to it.
    This is called when a driver manually enters a Plug ID in the app.
    """
    result = await db.execute(select(Plug).where(Plug.id == plug_id))
    plug = result.scalar_one_or_none()

    if not plug:
        raise HTTPException(status_code=404, detail=f"Plug with ID {plug_id} not found.")

    # Access check: verify user can see this plug (403 for a private group
    # the user hasn't joined — shared with the watch endpoints below).
    group_name, is_private = await ensure_plug_group_access(db, plug, user)

    # Effective coords: the plug's own, else its gateway's site location.
    # Also load the gateway to report live reachability to the driver.
    gw_row = await db.execute(
        select(Gateway).where(Gateway.id == plug.gateway_id)
    )
    gateway = gw_row.scalar_one_or_none()
    gw_lat = gateway.latitude if gateway else None
    gw_lng = gateway.longitude if gateway else None

    # Resolved effective rate: plug tariff -> group tariff -> tenant default
    # -> env fallback (services/pricing.py resolve_rate_for_plug).
    price_per_kwh = await resolve_rate_for_plug(db, plug)

    # [Reservations] Same grouped helper the list uses, over this one plug.
    now = datetime.now(timezone.utc)
    reservation_fields = await _reservation_fields_for_plugs(
        db, [plug.id], user.id, now
    )

    # [Plug watches] Whether THIS user has an armed "notify me when free"
    # watch on the plug (drives the Home card's bell toggle).
    watching = (await db.execute(
        select(PlugWatch.id).where(
            and_(PlugWatch.user_id == user.id, PlugWatch.plug_id == plug.id)
        )
    )).scalar_one_or_none() is not None

    return PlugResponse(
        id=plug.id,
        name=plug.name,
        status=plug.status.value,
        current_power_w=plug.current_power_w,
        plug_model=plug.plug_model,
        group_name=group_name,
        latitude=plug.latitude if plug.latitude is not None else gw_lat,
        longitude=plug.longitude if plug.longitude is not None else gw_lng,
        gateway_online=gateway_is_live(gateway) if gateway else False,
        price_per_kwh=float(price_per_kwh),
        is_private=is_private,
        watching=watching,
        **reservation_fields.get(plug.id, {}),
    )


# ===========================================================================
# Plug Watches — "notify me when free"
# ===========================================================================

@router.post("/api/plugs/{plug_id}/watch")
async def watch_plug(
    plug_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Arm a one-shot "notify me when free" watch on a plug: when it next flips
    back to AVAILABLE (session end / maintenance clear), the driver gets a
    `plug_available` notification through the existing pipeline (feed +
    Socket.io + Web Push) and the watch clears itself
    (services/plug_watch.py).

    Idempotent — watching an already-watched plug returns the same 200.
    Access follows GET /api/plugs/{id}: 403 for a private-group plug the
    user hasn't joined. Deliberately permissive about plug state (occupied,
    offline, and maintenance are all legitimately watchable); the single
    rejected case is a plug that is startable RIGHT NOW (AVAILABLE with a
    live gateway) — there is nothing to wait for.
    """
    result = await db.execute(select(Plug).where(Plug.id == plug_id))
    plug = result.scalar_one_or_none()
    if not plug:
        raise HTTPException(status_code=404, detail=f"Plug with ID {plug_id} not found.")

    await ensure_plug_group_access(db, plug, user)

    if plug.status == PlugStatus.AVAILABLE:
        gw_row = await db.execute(
            select(Gateway).where(Gateway.id == plug.gateway_id)
        )
        gateway = gw_row.scalar_one_or_none()
        if gateway and gateway_is_live(gateway):
            raise HTTPException(
                status_code=409,
                detail="This plug is available right now — no need to watch it.",
            )

    existing = (await db.execute(
        select(PlugWatch).where(
            and_(PlugWatch.user_id == user.id, PlugWatch.plug_id == plug_id)
        )
    )).scalar_one_or_none()
    if not existing:
        db.add(PlugWatch(user_id=user.id, plug_id=plug_id))
        try:
            await db.commit()
        except IntegrityError:
            # Double-tap race: the UNIQUE(user_id, plug_id) row landed between
            # the check and this insert — already watching, same outcome.
            await db.rollback()

    return {"status": "watching", "plug_id": plug_id, "watching": True}


@router.delete("/api/plugs/{plug_id}/watch")
async def unwatch_plug(
    plug_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Disarm a "notify me when free" watch. Idempotent — deleting a watch that
    doesn't exist (never armed, already fired, or the plug is gone) is a
    no-op 200. No access check on purpose: the row is the user's own, and a
    driver who has since left the plug's private group must still be able to
    clear their watch.
    """
    await db.execute(
        delete(PlugWatch).where(
            and_(PlugWatch.user_id == user.id, PlugWatch.plug_id == plug_id)
        )
    )
    await db.commit()
    return {"status": "not_watching", "plug_id": plug_id, "watching": False}



