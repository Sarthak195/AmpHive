"""
Plugs routes — moved verbatim from main.py (2026-07-07, TD#7 split).
"""
import logging
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_, delete, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.db import get_db
from backend.database.models import (
    CapacityRequest, ChargerGroup, Gateway, GroupMembership, Plug, PlugStatus, PlugWatch,
    Tenant, User,
)
from backend.schemas import (
    PlugResponse,
    PublicPlugResponse,
)
from backend.services.rate_limit import public_map_rate_limiter, rate_limit_dependency
from backend.services.auth import (
    get_current_user,
)
from backend.services.pricing import resolve_price_display
from backend.services.session_lifecycle import (
    gateway_is_live, plug_is_powered,
)
# [Queued charge] queue_available on PlugResponse = gateway online + plug
# unpowered + the CPO's queued-charging resolver says yes (Plug override ->
# Tenant default).
from backend.services.session_start import queued_charging_enabled

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
        select(Plug, ChargerGroup.name, ChargerGroup.is_public, Gateway, Tenant)
        .join(Gateway, Gateway.id == Plug.gateway_id)
        .join(Tenant, Tenant.id == Gateway.tenant_id)
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
        db, [plug.id for plug, _, _, _, _ in all_rows], user.id, now
    )

    responses = []
    for plug, group_name, group_is_public, gateway, tenant in all_rows:
        gw_online = gateway_is_live(gateway, now)
        powered = plug_is_powered(plug, now)
        # Resolved effective rate + [Pricing v2] the next TOD price and when it
        # changes (plug tariff -> group tariff -> tenant default -> env fallback;
        # services/pricing.py). resolve_price_display loads the tariff+slots ONCE
        # — same per-plug cost as the old resolve_rate_for_plug, so this adds no
        # query. Still one lookup per plug (tolerable N+1 for per-site plug
        # counts; batch this if the cross-tenant list ever grows large).
        rate, changes_at, next_rate = await resolve_price_display(db, plug)
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
                gateway_online=gw_online,
                plug_powered=powered,
                queue_available=(
                    gw_online and not powered
                    and queued_charging_enabled(tenant, plug)
                ),
                price_per_kwh=float(rate),
                price_next_per_kwh=float(next_rate) if next_rate is not None else None,
                price_changes_at=changes_at.isoformat() if changes_at is not None else None,
                # group_is_public is None for ungrouped plugs (outer join) —
                # only an explicit False marks a private group.
                is_private=group_is_public is False,
                watching=plug.id in watched_plug_ids,
                **reservation_fields.get(plug.id, {}),
            )
        )
    return responses


@router.get(
    "/api/plugs/public",
    response_model=List[PublicPlugResponse],
    dependencies=[Depends(rate_limit_dependency(public_map_rate_limiter, "map"))],
)
async def get_public_plugs(db: AsyncSession = Depends(get_db)):
    """
    PUBLIC (no auth) — nearby PUBLIC chargers for the pre-signup discovery map.

    Returns ONLY plugs that are ungrouped/legacy or in a **public** charger group
    AND have a known location, with a deliberately minimal projection
    (`PublicPlugResponse`). Private/society plugs (in a non-public group) are
    never included — publicly geolocating them would be a privacy leak. Starting
    a charge still requires an account (this endpoint is read-only discovery).

    Declared before `/api/plugs/{plug_id}` so the static path wins the match.
    """
    public_group_ids = (
        select(ChargerGroup.id)
        .where(ChargerGroup.is_public == True)  # noqa: E712 (SQL boolean, not Python)
        .scalar_subquery()
    )
    rows = await db.execute(
        select(Plug, Gateway)
        .join(Gateway, Gateway.id == Plug.gateway_id)
        .where(
            or_(
                Plug.group_id.is_(None),  # ungrouped/legacy: public to all
                Plug.group_id.in_(public_group_ids),
            )
        )
    )

    now = datetime.now(timezone.utc)
    out = []
    for plug, gateway in rows.all():
        # Effective coords: the plug's own, else its gateway's site (same
        # fallback the authenticated list uses). No location → not mappable.
        lat = plug.latitude if plug.latitude is not None else gateway.latitude
        lon = plug.longitude if plug.longitude is not None else gateway.longitude
        if lat is None or lon is None:
            continue
        rate, _changes_at, _next_rate = await resolve_price_display(db, plug)
        out.append(
            PublicPlugResponse(
                id=plug.id,
                name=plug.name,
                status=plug.status.value,
                latitude=lat,
                longitude=lon,
                price_per_kwh=float(rate),
                gateway_online=gateway_is_live(gateway, now),
            )
        )
    return out


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
    # [Queued charge] The tenant carries the queued-charging default the plug
    # override falls back to.
    tenant = None
    if gateway is not None:
        tenant = (
            await db.execute(select(Tenant).where(Tenant.id == gateway.tenant_id))
        ).scalar_one_or_none()

    # Resolved effective rate + [Pricing v2] next TOD price and its change time
    # (plug tariff -> group tariff -> tenant default -> env fallback;
    # services/pricing.py resolve_price_display).
    rate, changes_at, next_rate = await resolve_price_display(db, plug)

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

    gw_online = gateway_is_live(gateway) if gateway else False
    powered = plug_is_powered(plug, now)
    return PlugResponse(
        id=plug.id,
        name=plug.name,
        status=plug.status.value,
        current_power_w=plug.current_power_w,
        plug_model=plug.plug_model,
        group_name=group_name,
        latitude=plug.latitude if plug.latitude is not None else gw_lat,
        longitude=plug.longitude if plug.longitude is not None else gw_lng,
        gateway_online=gw_online,
        plug_powered=powered,
        queue_available=(
            gw_online and not powered and tenant is not None
            and queued_charging_enabled(tenant, plug)
        ),
        price_per_kwh=float(rate),
        price_next_per_kwh=float(next_rate) if next_rate is not None else None,
        price_changes_at=changes_at.isoformat() if changes_at is not None else None,
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


# ===========================================================================
# Request capacity — a start blocked by a full circuit (caps + circuits)
# ===========================================================================

@router.post("/api/plugs/{plug_id}/request-capacity")
async def request_capacity(
    plug_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Arm a one-shot "request capacity" when a plug's shared circuit is at its
    current cap (the `circuit_full` 409 from a start). When the circuit next
    has room — a session on it ends or the operator raises the cap — the driver
    gets a `capacity_available` notification and this row clears itself
    (services/capacity.py).

    Idempotent (UNIQUE user+plug). Rejects (400) a plug that isn't on a capped
    circuit, or a circuit that actually has room right now (nothing to wait for
    — just start). Access follows GET /api/plugs/{id} (403 for a private group
    the user hasn't joined).
    """
    plug = (await db.execute(select(Plug).where(Plug.id == plug_id))).scalar_one_or_none()
    if not plug:
        raise HTTPException(status_code=404, detail=f"Plug with ID {plug_id} not found.")

    await ensure_plug_group_access(db, plug, user)

    if plug.group_id is None:
        raise HTTPException(
            status_code=400,
            detail="This charger isn't on a shared circuit — there's nothing to wait for.",
        )
    group = (
        await db.execute(select(ChargerGroup).where(ChargerGroup.id == plug.group_id))
    ).scalar_one_or_none()
    if group is None or group.max_current_a is None:
        raise HTTPException(
            status_code=400,
            detail="This charger's circuit has no capacity limit — just start charging.",
        )

    from backend.services.caps import circuit_load_a, effective_plug_cap
    load = await circuit_load_a(db, plug.group_id)
    if load + effective_plug_cap(plug) <= group.max_current_a + 1e-6:
        raise HTTPException(
            status_code=400,
            detail="There's capacity available right now — just tap Start.",
        )

    existing = (await db.execute(
        select(CapacityRequest).where(
            and_(CapacityRequest.user_id == user.id, CapacityRequest.plug_id == plug_id)
        )
    )).scalar_one_or_none()
    if not existing:
        db.add(CapacityRequest(user_id=user.id, plug_id=plug_id, group_id=plug.group_id))
        try:
            await db.commit()
        except IntegrityError:
            # Double-tap race on UNIQUE(user_id, plug_id) — already requested.
            await db.rollback()

    return {"status": "requested", "plug_id": plug_id}



