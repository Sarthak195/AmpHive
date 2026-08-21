"""
Plugs routes — moved verbatim from main.py (2026-07-07, TD#7 split).
"""
import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.db import get_db
from backend.database.models import (
    CapacityRequest,
    ChargerGroup,
    ChargingSession,
    Gateway,
    GatewayEvent,
    GroupMembership,
    Plug,
    PlugPhoto,
    PlugPhotoStatus,
    PlugReport,
    PlugReportStatus,
    PlugReview,
    PlugStatus,
    PlugWatch,
    SessionStatus,
    Tenant,
    User,
    UserFavorite,
)
from backend.schemas import (
    PlugPhotoResponse,
    PlugReportCreateRequest,
    PlugReportResponse,
    PlugResponse,
    PlugReviewCreateRequest,
    PlugReviewResponse,
    PublicPlugResponse,
)
from backend.services.auth import (
    get_current_user,
    get_current_user_optional,
)
from backend.services.photo_publish import (
    PhotoPublishError,
    upload_object,
)
from backend.services.pricing import resolve_price_display, resolve_price_display_batch
from backend.services.rate_limit import public_map_rate_limiter, rate_limit_dependency
from backend.services.session_lifecycle import (
    gateway_is_live,
    plug_is_powered,
)

# [Queued charge] queue_available on PlugResponse = gateway online + plug
# unpowered + the CPO's queued-charging resolver says yes (Plug override ->
# Tenant default).
from backend.services.session_start import queued_charging_enabled

logger = logging.getLogger("amphive.api")
router = APIRouter()

# [Audit: DoS hardening] GET /api/plugs/available and GET /api/plugs/public
# (the latter UNAUTHENTICATED, only rate-limited) previously .all()'d every
# matching plug platform-wide with no bound — an uncapped payload/DoS
# surface that only gets worse as the fleet grows. Hard-capped at the query
# level (not a display-only truncation) so the response size stays bounded
# regardless of how many plugs exist; 500 is generous for any realistic
# single-user/single-map view while still being a real bound.
PLUG_LIST_HARD_CAP = 500


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


async def resolve_review_aggregates_batch(db, plug_ids):
    """
    [Reviews] avg_rating (mean PlugReview.rating, 1 dp) + review_count for a
    batch of plugs in ONE grouped query — same N+1-free discipline as
    _reservation_fields_for_plugs / resolve_price_display_batch. Returns
    {plug_id: (avg_rating|None, review_count)}; a plug with no reviews is simply
    absent from the map, and the caller defaults it to (None, 0) so the UI can
    tell "unrated" from "rated 0".
    """
    if not plug_ids:
        return {}

    rows = await db.execute(
        select(
            PlugReview.plug_id,
            func.avg(PlugReview.rating),
            func.count(PlugReview.id),
        )
        .where(PlugReview.plug_id.in_(plug_ids))
        .group_by(PlugReview.plug_id)
    )
    return {
        plug_id: (round(float(avg), 1) if avg is not None else None, int(count))
        for plug_id, avg, count in rows.all()
    }


async def resolve_photo_thumbnails_batch(db, plug_ids):
    """[Photos] One APPROVED photo URL per plug (the most recent) for the
    map/list thumbnail, in ONE query — same N+1-free discipline as the review
    aggregate. Returns {plug_id: url}; a plug with no approved photo is absent
    (the caller defaults thumbnail_url to None)."""
    if not plug_ids:
        return {}

    rows = await db.execute(
        select(PlugPhoto.plug_id, PlugPhoto.url)
        .where(
            PlugPhoto.plug_id.in_(plug_ids),
            PlugPhoto.status == PlugPhotoStatus.APPROVED,
        )
        .order_by(PlugPhoto.plug_id, PlugPhoto.created_at.desc())
    )
    out = {}
    for plug_id, url in rows.all():
        # Rows are ordered newest-first within each plug; keep the first seen.
        out.setdefault(plug_id, url)
    return out


# [Photos] Accepted image types, by leading magic bytes. Kept tiny and explicit
# (no Pillow/imghdr dependency) — we only need to reject non-images before
# spending a network round-trip storing them.
_PHOTO_MAX_BYTES = 8 * 1024 * 1024  # 8 MiB — generous for a phone photo.
_PHOTO_MAGIC = (
    (b"\xff\xd8\xff", "image/jpeg", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
)


def _sniff_image(data: bytes):
    """Return (content_type, ext) if ``data`` begins with a supported image
    magic, else None. WebP is RIFF....WEBP (magic split across two ranges)."""
    for magic, content_type, ext in _PHOTO_MAGIC:
        if data.startswith(magic):
            return content_type, ext
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    return None


def _author_display(full_name: Optional[str], email: Optional[str]) -> str:
    """Privacy-preserving label for a public review: the driver's first name if
    they have a real full_name, else their email local-part with the tail
    masked (``sat***``). NEVER the full email or the user id — the reviews read
    is unauthenticated."""
    name = (full_name or "").strip()
    if name:
        return name.split()[0]
    local = (email or "").split("@")[0] or "driver"
    if len(local) <= 3:
        return local[0] + "***"
    return local[:3] + "***"


def _plug_review_response(review: PlugReview, *, author_display: str, is_mine: bool = False) -> PlugReviewResponse:
    return PlugReviewResponse(
        id=review.id,
        plug_id=review.plug_id,
        rating=review.rating,
        body=review.body,
        author_display=author_display,
        created_at=review.created_at.isoformat() if review.created_at else None,
        is_mine=is_mine,
    )


async def _has_completed_charge(db, plug_id: int, user_id: int) -> bool:
    """[Reviews] The verified-charger gate: True iff this user has at least one
    finished ChargingSession (COMPLETED or PAID) on this plug. A driver can't
    rate a charger they never used. Served by ix_charging_sessions_plug_status /
    ix_charging_sessions_user_status."""
    row = await db.execute(
        select(ChargingSession.id)
        .where(
            ChargingSession.plug_id == plug_id,
            ChargingSession.user_id == user_id,
            ChargingSession.status.in_([SessionStatus.COMPLETED, SessionStatus.PAID]),
        )
        .limit(1)
    )
    return row.scalar_one_or_none() is not None

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
        .where(ChargerGroup.is_public == True)  # noqa: E712 (SQL boolean, not Python)
        .scalar_subquery()
    )

    rows = await db.execute(
        select(Plug, ChargerGroup.name, ChargerGroup.is_public, ChargerGroup.tariff_id, Gateway, Tenant)
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
        .limit(PLUG_LIST_HARD_CAP)
    )

    # [Plug watches] The current user's armed "notify me when free" watches —
    # ONE extra query for the whole list (keeps this endpoint's deliberate
    # N+1-free shape; served by the UNIQUE(user_id, plug_id)'s leading column).
    watched_plug_ids = set(
        (await db.execute(
            select(PlugWatch.plug_id).where(PlugWatch.user_id == user.id)
        )).scalars().all()
    )

    # [Favorites] Same shape as watched_plug_ids above — ONE extra query for
    # the whole list, keeping this endpoint N+1-free.
    favorited_plug_ids = set(
        (await db.execute(
            select(UserFavorite.plug_id).where(UserFavorite.user_id == user.id)
        )).scalars().all()
    )

    now = datetime.now(timezone.utc)
    all_rows = rows.all()
    if len(all_rows) >= PLUG_LIST_HARD_CAP:
        logger.warning(
            "GET /api/plugs/available truncated to the %d-row hard cap",
            PLUG_LIST_HARD_CAP,
            extra={"user_id": user.id, "cap": PLUG_LIST_HARD_CAP},
        )

    # [Reservations] reserved_now / next_reservation etc. for every listed
    # plug in one grouped query — NOT per-plug (this endpoint stays N+1-free).
    reservation_fields = await _reservation_fields_for_plugs(
        db, [plug.id for plug, *_ in all_rows], user.id, now
    )

    # Resolved effective rate + [Pricing v2] next TOD price for EVERY listed
    # plug (chain: plug tariff -> group tariff -> tenant default -> env
    # fallback) in two fixed IN-queries — the chain inputs (plug/group tariff
    # ids, tenant) already ride the joined rows, so the old per-plug
    # resolve_price_display chain (up to 5 queries per plug) is gone. Same
    # per-plug result; one consistent "now" for the whole list.
    price_display = await resolve_price_display_batch(
        db,
        [(plug, group_tariff_id, tenant)
         for plug, _, _, group_tariff_id, _, tenant in all_rows],
        at=now,
    )

    # [Reviews] avg_rating + review_count for the whole list in one grouped
    # query — keeps this endpoint N+1-free (same shape as price_display above).
    review_aggs = await resolve_review_aggregates_batch(
        db, [plug.id for plug, *_ in all_rows]
    )
    photo_thumbs = await resolve_photo_thumbnails_batch(
        db, [plug.id for plug, *_ in all_rows]
    )

    responses = []
    for plug, group_name, group_is_public, _group_tariff_id, gateway, tenant in all_rows:
        avg_rating, review_count = review_aggs.get(plug.id, (None, 0))
        gw_online = gateway_is_live(gateway, now)
        powered = plug_is_powered(plug, now)
        rate, changes_at, next_rate = price_display[plug.id]
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
                rated_power_w=plug.rated_power_w,
                connector_type=plug.connector_type,
                is_favorite=plug.id in favorited_plug_ids,
                avg_rating=avg_rating,
                review_count=review_count,
                thumbnail_url=photo_thumbs.get(plug.id),
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
        select(Plug, ChargerGroup.tariff_id, Gateway, Tenant)
        .join(Gateway, Gateway.id == Plug.gateway_id)
        .join(Tenant, Tenant.id == Gateway.tenant_id)
        .outerjoin(ChargerGroup, ChargerGroup.id == Plug.group_id)
        .where(
            or_(
                Plug.group_id.is_(None),  # ungrouped/legacy: public to all
                Plug.group_id.in_(public_group_ids),
            )
        )
        .limit(PLUG_LIST_HARD_CAP)
    )

    now = datetime.now(timezone.utc)
    all_rows = rows.all()
    if len(all_rows) >= PLUG_LIST_HARD_CAP:
        logger.warning(
            "GET /api/plugs/public truncated to the %d-row hard cap",
            PLUG_LIST_HARD_CAP,
            extra={"cap": PLUG_LIST_HARD_CAP},
        )

    # Effective coords: the plug's own, else its gateway's site (same
    # fallback the authenticated list uses). No location → not mappable,
    # and only mappable plugs get priced.
    mappable = []
    for plug, group_tariff_id, gateway, tenant in all_rows:
        lat = plug.latitude if plug.latitude is not None else gateway.latitude
        lon = plug.longitude if plug.longitude is not None else gateway.longitude
        if lat is None or lon is None:
            continue
        mappable.append((plug, group_tariff_id, gateway, tenant, lat, lon))

    # [Audit: N+1] Two fixed IN-queries price the whole map. This endpoint is
    # UNAUTHENTICATED (rate-limited), so its per-plug query cost matters the
    # most of all the list endpoints.
    price_display = await resolve_price_display_batch(
        db,
        [(plug, group_tariff_id, tenant)
         for plug, group_tariff_id, _, tenant, _, _ in mappable],
        at=now,
    )

    # [Reviews] Public aggregate rating for every mapped plug in one grouped
    # query (unauthenticated — no per-user data leaves here, just a mean+count).
    review_aggs = await resolve_review_aggregates_batch(
        db, [plug.id for plug, _, _, _, _, _ in mappable]
    )
    photo_thumbs = await resolve_photo_thumbnails_batch(
        db, [plug.id for plug, _, _, _, _, _ in mappable]
    )

    out = []
    for plug, _group_tariff_id, gateway, _tenant, lat, lon in mappable:
        rate, _changes_at, _next_rate = price_display[plug.id]
        avg_rating, review_count = review_aggs.get(plug.id, (None, 0))
        out.append(
            PublicPlugResponse(
                id=plug.id,
                name=plug.name,
                status=plug.status.value,
                latitude=lat,
                longitude=lon,
                price_per_kwh=float(rate),
                gateway_online=gateway_is_live(gateway, now),
                rated_power_w=plug.rated_power_w,
                connector_type=plug.connector_type,
                avg_rating=avg_rating,
                review_count=review_count,
                thumbnail_url=photo_thumbs.get(plug.id),
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

    # [Favorites] Whether THIS user has starred the plug — same shape as the
    # watching lookup just above.
    is_favorite = (await db.execute(
        select(UserFavorite.id).where(
            and_(UserFavorite.user_id == user.id, UserFavorite.plug_id == plug.id)
        )
    )).scalar_one_or_none() is not None

    # [Reviews] avg_rating + review_count for this one plug (same grouped helper
    # the lists use, over a single id).
    avg_rating, review_count = (
        await resolve_review_aggregates_batch(db, [plug.id])
    ).get(plug.id, (None, 0))
    thumbnail_url = (
        await resolve_photo_thumbnails_batch(db, [plug.id])
    ).get(plug.id)

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
        rated_power_w=plug.rated_power_w,
        connector_type=plug.connector_type,
        is_favorite=is_favorite,
        avg_rating=avg_rating,
        review_count=review_count,
        thumbnail_url=thumbnail_url,
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
# Favorites — a standing "star this charger" preference
# ===========================================================================

@router.post("/api/plugs/{plug_id}/favorite")
async def favorite_plug(
    plug_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Star a plug as a favorite (a STANDING preference — unlike watch_plug's
    one-shot arm, nothing clears this automatically). Idempotent — starring
    an already-favorited plug returns the same 200. Access follows
    GET /api/plugs/{id}: 403 for a private-group plug the user hasn't
    joined. Unlike watch_plug, there is no state-based rejection — starring
    an available-right-now plug is perfectly normal (it's a bookmark, not a
    "notify me" arm).
    """
    result = await db.execute(select(Plug).where(Plug.id == plug_id))
    plug = result.scalar_one_or_none()
    if not plug:
        raise HTTPException(status_code=404, detail=f"Plug with ID {plug_id} not found.")

    await ensure_plug_group_access(db, plug, user)

    existing = (await db.execute(
        select(UserFavorite).where(
            and_(UserFavorite.user_id == user.id, UserFavorite.plug_id == plug_id)
        )
    )).scalar_one_or_none()
    if not existing:
        db.add(UserFavorite(user_id=user.id, plug_id=plug_id))
        try:
            await db.commit()
        except IntegrityError:
            # Double-tap race: the UNIQUE(user_id, plug_id) row landed between
            # the check and this insert — already favorited, same outcome.
            await db.rollback()

    return {"status": "favorited", "plug_id": plug_id, "is_favorite": True}


@router.delete("/api/plugs/{plug_id}/favorite")
async def unfavorite_plug(
    plug_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Un-star a favorite. Idempotent — deleting a favorite that doesn't exist
    (never starred, or the plug is gone) is a no-op 200. No access check on
    purpose, same rationale as unwatch_plug: the row is the user's own.
    """
    await db.execute(
        delete(UserFavorite).where(
            and_(UserFavorite.user_id == user.id, UserFavorite.plug_id == plug_id)
        )
    )
    await db.commit()
    return {"status": "unfavorited", "plug_id": plug_id, "is_favorite": False}


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


# ===========================================================================
# Tariff preview — the plug's full effective price schedule
# (redesign/ui-v3 — plans/redesign-v3-contract.md §4 "Driver gaps")
# ===========================================================================

@router.get("/api/plugs/{plug_id}/tariff-preview")
async def get_plug_tariff_preview(
    plug_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    The plug's EFFECTIVE tariff, previewed for the driver before they start:
    ``{base_price_per_kwh, price_now, slots: [{days, start_minute,
    end_minute, price_per_kwh}]}``.

    Any authenticated role; visibility follows GET /api/plugs/{id} exactly
    (ensure_plug_group_access — 403 for a private-group plug the caller
    hasn't joined). The tariff resolves through the SAME chain
    services/pricing.py resolve_rate_for_plug uses (plug -> group -> tenant
    default -> global env fallback) — this endpoint reuses that module's
    resolution + slot helpers rather than re-implementing them, so preview
    and billing can never disagree. ``days`` expands the slot's days_mask
    into weekday indices (0=Mon .. 6=Sun, the mask's own bit order).
    ``price_now`` re-resolves at "now" in the tenant's local zone: the
    covering slot's price, else the flat base. No tariff anywhere in the
    chain -> the env default rate with no slots.
    """
    from backend.database.models import TariffSlot
    from backend.services.money import to_money
    from backend.services.pricing import (
        _resolve_tariff_and_tz,
        _slot_rate_and_bound,
        _to_local,
        _zone,
        default_rate,
    )

    result = await db.execute(select(Plug).where(Plug.id == plug_id))
    plug = result.scalar_one_or_none()
    if not plug:
        raise HTTPException(status_code=404, detail=f"Plug with ID {plug_id} not found.")

    await ensure_plug_group_access(db, plug, user)

    tariff, tz_name = await _resolve_tariff_and_tz(db, plug)
    if tariff is None:
        # Legacy env fallback — flat rate, no time-of-day structure.
        rate = float(default_rate())
        return {"base_price_per_kwh": rate, "price_now": rate, "slots": []}

    slots = (
        await db.execute(select(TariffSlot).where(TariffSlot.tariff_id == tariff.id))
    ).scalars().all()

    base = to_money(tariff.price_per_kwh)
    at_local = _to_local(datetime.now(timezone.utc), _zone(tz_name))
    slot_rate, _boundary = _slot_rate_and_bound(slots, at_local)
    price_now = slot_rate if slot_rate is not None else base

    return {
        "base_price_per_kwh": float(base),
        "price_now": float(price_now),
        "slots": [
            {
                "days": [d for d in range(7) if (int(s.days_mask) >> d) & 1],
                "start_minute": int(s.start_min),
                "end_minute": int(s.end_min),
                "price_per_kwh": float(to_money(s.price_per_kwh)),
            }
            for s in sorted(slots, key=lambda s: (int(s.start_min), int(s.end_min)))
        ],
    }


# ===========================================================================
# "Report a problem with this charger" (database/models.py PlugReport)
# ===========================================================================

def _plug_report_response(report: PlugReport) -> PlugReportResponse:
    return PlugReportResponse(
        id=report.id,
        plug_id=report.plug_id,
        tenant_id=report.tenant_id,
        driver_user_id=report.driver_user_id,
        category=report.category,
        description=report.description,
        status=report.status.value,
        resolution_note=report.resolution_note,
        created_at=report.created_at.isoformat() if report.created_at else None,
        resolved_at=report.resolved_at.isoformat() if report.resolved_at else None,
        resolved_by_user_id=report.resolved_by_user_id,
    )


@router.post("/api/plugs/{plug_id}/report", response_model=PlugReportResponse)
async def report_plug_problem(
    plug_id: int,
    req: PlugReportCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    "Report a problem with this charger" — any authenticated driver who can
    SEE the plug (ensure_plug_group_access — same 403 rule as watch_plug)
    may flag it, whether or not they've ever charged there. No session, no
    money: a hardware/data-quality signal for the CPO, distinct from
    SessionDispute (POST /api/sessions/{id}/dispute — session-FK'd, coins-
    only refund remedy). Reviewed via GET /api/cpo/plug-reports + POST
    /api/cpo/plug-reports/{id}/resolve.

    Deliberately permissive about plug state — unlike watch_plug (which
    rejects a plug with nothing to wait for), there is no state gate here:
    broken/offline/maintenance plugs are exactly the ones most worth
    reporting.

    Also writes a GatewayEvent (event_type="DRIVER_PROBLEM_REPORT",
    severity="warning") so the EXISTING CPO alert strip
    (components/CpoAlerts.jsx) and Health nav badge pick this up on their
    normal poll/broadcast path — no new alert pipeline. Acknowledging that
    GatewayEvent (the existing per-event ack endpoint) is independent of
    resolving this PlugReport.
    """
    result = await db.execute(select(Plug).where(Plug.id == plug_id))
    plug = result.scalar_one_or_none()
    if not plug:
        raise HTTPException(status_code=404, detail=f"Plug with ID {plug_id} not found.")

    await ensure_plug_group_access(db, plug, user)

    gw_row = await db.execute(select(Gateway).where(Gateway.id == plug.gateway_id))
    gateway = gw_row.scalar_one_or_none()
    if not gateway:
        # plug.gateway_id is a NOT NULL FK — this should never happen in
        # practice, but fail closed rather than write a tenant-less report.
        raise HTTPException(status_code=404, detail="This charger's gateway no longer exists.")

    report = PlugReport(
        plug_id=plug.id,
        tenant_id=gateway.tenant_id,
        driver_user_id=user.id,
        category=req.category,
        description=req.description,
        status=PlugReportStatus.OPEN,
    )
    db.add(report)

    event = GatewayEvent(
        tenant_id=gateway.tenant_id,
        gateway_id=gateway.id,
        plug_id=plug.id,
        event_type="DRIVER_PROBLEM_REPORT",
        severity="warning",
        detail=f"{req.category}: {req.description[:200]}",
    )
    db.add(event)

    await db.commit()
    await db.refresh(report)
    await db.refresh(event)

    logger.info(
        "Plug problem reported",
        extra={
            "plug_id": plug.id, "report_id": report.id,
            "user_id": user.id, "email": user.email,
        },
    )

    # Best-effort live broadcast (same shape MQTTAlarmMixin._persist_gateway_event
    # uses for hardware alarms) so the alert strip reacts immediately instead
    # of waiting on its 60s poll. Never blocks or fails the report itself.
    try:
        from backend.services.socketio_manager import emit_gateway_alarm
        await emit_gateway_alarm({
            "id": event.id,
            "gateway_id": gateway.id,
            "plug_id": plug.id,
            "event_type": event.event_type,
            "severity": event.severity,
            "detail": event.detail,
            "created_at": event.created_at.isoformat() if event.created_at else None,
        })
    except Exception:
        logger.exception(
            "Failed to broadcast plug report alarm", extra={"plug_id": plug.id},
        )

    return _plug_report_response(report)


# ===========================================================================
# Plug reviews — verified-charger star ratings (database/models.py PlugReview)
# ===========================================================================

async def _plug_is_public(db, plug: Plug) -> bool:
    """A plug is publicly visible when it is ungrouped/legacy (group_id NULL) or
    sits in a public ChargerGroup — the same rule get_public_plugs filters on.
    Used to decide whether an ANONYMOUS caller may read a plug's reviews."""
    if plug.group_id is None:
        return True
    is_public = (await db.execute(
        select(ChargerGroup.is_public).where(ChargerGroup.id == plug.group_id)
    )).scalar_one_or_none()
    return is_public is True


@router.get(
    "/api/plugs/{plug_id}/reviews",
    response_model=List[PlugReviewResponse],
    dependencies=[Depends(rate_limit_dependency(public_map_rate_limiter, "map"))],
)
async def list_plug_reviews(
    plug_id: int,
    db: AsyncSession = Depends(get_db),
    user: Optional[User] = Depends(get_current_user_optional),
):
    """
    PUBLIC — the star reviews for one plug, newest first, for the discovery
    "Photos & reviews" panel. No auth required (part of pre-signup discovery),
    rate-limited on the same public-map limiter as GET /api/plugs/public.

    Visibility mirrors the map: an ANONYMOUS caller may read reviews only for a
    publicly-visible plug (ungrouped or public group) — a private/society plug
    404s for them, exactly as it is absent from the public map. A SIGNED-IN
    caller is held to the normal per-plug access rule (ensure_plug_group_access,
    403 for a private group they haven't joined). `author_display` is a
    privacy-preserving label; no email/user-id ever leaves here.
    """
    plug = (await db.execute(select(Plug).where(Plug.id == plug_id))).scalar_one_or_none()
    if not plug:
        raise HTTPException(status_code=404, detail=f"Plug with ID {plug_id} not found.")

    if user is None:
        if not await _plug_is_public(db, plug):
            # Don't reveal a private plug's existence to an anonymous caller.
            raise HTTPException(status_code=404, detail=f"Plug with ID {plug_id} not found.")
    else:
        await ensure_plug_group_access(db, plug, user)

    rows = await db.execute(
        select(PlugReview, User.full_name, User.email)
        .join(User, User.id == PlugReview.driver_user_id)
        .where(PlugReview.plug_id == plug_id)
        .order_by(PlugReview.created_at.desc())
        .limit(200)
    )
    out = []
    for review, full_name, email in rows.all():
        display = _author_display(full_name, email)
        out.append(_plug_review_response(
            review,
            author_display=display,
            is_mine=(user is not None and review.driver_user_id == user.id),
        ))
    return out


@router.post("/api/plugs/{plug_id}/reviews", response_model=PlugReviewResponse)
async def create_or_update_plug_review(
    plug_id: int,
    req: PlugReviewCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Leave (or edit) a star review for a plug — VERIFIED CHARGERS ONLY. The
    caller must be able to see the plug (ensure_plug_group_access, 403 for a
    private group) AND have at least one finished ChargingSession
    (completed|paid) on it (_has_completed_charge, 403 otherwise) — a driver
    can't rate a charger they never used.

    One standing review per driver per plug: a second POST is an EDIT, not a
    duplicate. We insert optimistically and, on the UNIQUE(driver_user_id,
    plug_id) IntegrityError, update the existing row instead (same idempotency
    trick as favorite/watch) — so a double-tap race can't create two rows.
    """
    plug = (await db.execute(select(Plug).where(Plug.id == plug_id))).scalar_one_or_none()
    if not plug:
        raise HTTPException(status_code=404, detail=f"Plug with ID {plug_id} not found.")

    await ensure_plug_group_access(db, plug, user)

    if not await _has_completed_charge(db, plug.id, user.id):
        raise HTTPException(
            status_code=403,
            detail="You can only review a charger you've completed a session at.",
        )

    gateway = (await db.execute(
        select(Gateway).where(Gateway.id == plug.gateway_id)
    )).scalar_one_or_none()
    if not gateway:
        raise HTTPException(status_code=404, detail="This charger's gateway no longer exists.")

    now = datetime.now(timezone.utc)
    review = PlugReview(
        plug_id=plug.id,
        tenant_id=gateway.tenant_id,
        driver_user_id=user.id,
        rating=req.rating,
        body=req.body,
    )
    db.add(review)
    try:
        await db.commit()
    except IntegrityError:
        # Already reviewed this plug — treat the second submit as an edit.
        await db.rollback()
        existing = (await db.execute(
            select(PlugReview).where(
                and_(
                    PlugReview.driver_user_id == user.id,
                    PlugReview.plug_id == plug.id,
                )
            )
        )).scalar_one()
        existing.rating = req.rating
        existing.body = req.body
        existing.updated_at = now
        await db.commit()
        await db.refresh(existing)
        review = existing
    else:
        await db.refresh(review)

    logger.info(
        "Plug review saved",
        extra={"plug_id": plug.id, "review_id": review.id, "user_id": user.id},
    )
    return _plug_review_response(
        review, author_display=_author_display(user.full_name, user.email), is_mine=True
    )


@router.delete("/api/plugs/{plug_id}/reviews/mine", status_code=204)
async def delete_my_plug_review(
    plug_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Remove the caller's OWN review of this plug (idempotent — a 204 whether
    or not a row existed, same shape as un-favoriting)."""
    await db.execute(
        delete(PlugReview).where(
            and_(
                PlugReview.driver_user_id == user.id,
                PlugReview.plug_id == plug_id,
            )
        )
    )
    await db.commit()
    return None


# ===========================================================================
# Plug photos — driver-submitted, held for approval (models.py PlugPhoto)
# ===========================================================================

def _plug_photo_response(photo: PlugPhoto, *, is_mine: bool = False) -> PlugPhotoResponse:
    return PlugPhotoResponse(
        id=photo.id,
        plug_id=photo.plug_id,
        url=photo.url,
        status=photo.status.value,
        created_at=photo.created_at.isoformat() if photo.created_at else None,
        is_mine=is_mine,
    )


@router.get(
    "/api/plugs/{plug_id}/photos",
    response_model=List[PlugPhotoResponse],
    dependencies=[Depends(rate_limit_dependency(public_map_rate_limiter, "map"))],
)
async def list_plug_photos(
    plug_id: int,
    db: AsyncSession = Depends(get_db),
    user: Optional[User] = Depends(get_current_user_optional),
):
    """
    PUBLIC — a plug's photos for the "Photos & reviews" panel. Anonymous callers
    see only APPROVED photos (and only for a publicly-visible plug — a private
    plug 404s, same rule as list_plug_reviews). A SIGNED-IN caller additionally
    sees their OWN pending photos (so an uploader gets "waiting for review"
    feedback) and is held to the normal per-plug access rule.
    """
    plug = (await db.execute(select(Plug).where(Plug.id == plug_id))).scalar_one_or_none()
    if not plug:
        raise HTTPException(status_code=404, detail=f"Plug with ID {plug_id} not found.")

    if user is None:
        if not await _plug_is_public(db, plug):
            raise HTTPException(status_code=404, detail=f"Plug with ID {plug_id} not found.")
        visibility = PlugPhoto.status == PlugPhotoStatus.APPROVED
    else:
        await ensure_plug_group_access(db, plug, user)
        # Approved (public) OR the caller's own pending/rejected? Show approved
        # to everyone and the caller's own PENDING (not their rejected ones).
        visibility = or_(
            PlugPhoto.status == PlugPhotoStatus.APPROVED,
            and_(
                PlugPhoto.uploaded_by_user_id == user.id,
                PlugPhoto.status == PlugPhotoStatus.PENDING,
            ),
        )

    rows = await db.execute(
        select(PlugPhoto)
        .where(and_(PlugPhoto.plug_id == plug_id, visibility))
        .order_by(PlugPhoto.created_at.desc())
        .limit(60)
    )
    return [
        _plug_photo_response(p, is_mine=(user is not None and p.uploaded_by_user_id == user.id))
        for p in rows.scalars()
    ]


@router.post("/api/plugs/{plug_id}/photos", response_model=PlugPhotoResponse, status_code=201)
async def upload_plug_photo(
    plug_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Upload a photo of a charger — VERIFIED CHARGERS ONLY (same gate as reviews:
    ensure_plug_group_access + a finished ChargingSession here). HELD FOR
    APPROVAL: the row lands PENDING and is invisible on the public map until a
    CPO/admin approves it (routers/cpo/_plug_photos.py).

    RAW body, NOT multipart (no python-multipart dependency — same convention as
    the admin firmware upload). The bytes are size-guarded (413) and magic-byte
    sniffed to a supported image type (JPEG/PNG/WebP, 400 otherwise) BEFORE being
    published to the public bucket gs://amphive-plug-photos (keyless, off the
    event loop via asyncio.to_thread — services/photo_publish.py).
    """
    plug = (await db.execute(select(Plug).where(Plug.id == plug_id))).scalar_one_or_none()
    if not plug:
        raise HTTPException(status_code=404, detail=f"Plug with ID {plug_id} not found.")

    await ensure_plug_group_access(db, plug, user)

    if not await _has_completed_charge(db, plug.id, user.id):
        raise HTTPException(
            status_code=403,
            detail="You can only add photos to a charger you've completed a session at.",
        )

    gateway = (await db.execute(
        select(Gateway).where(Gateway.id == plug.gateway_id)
    )).scalar_one_or_none()
    if not gateway:
        raise HTTPException(status_code=404, detail="This charger's gateway no longer exists.")

    body = await request.body()
    if len(body) > _PHOTO_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Photo exceeds the {_PHOTO_MAX_BYTES // (1024 * 1024)} MiB limit.",
        )
    sniffed = _sniff_image(body)
    if sniffed is None:
        raise HTTPException(
            status_code=400,
            detail="Uploaded file is not a supported image (JPEG, PNG or WebP).",
        )
    content_type, ext = sniffed

    object_name = f"plug-{plug_id}/{uuid.uuid4().hex}.{ext}"
    try:
        url = await asyncio.to_thread(upload_object, object_name, body, content_type)
    except PhotoPublishError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to store the photo: {exc}",
        )

    photo = PlugPhoto(
        plug_id=plug.id,
        tenant_id=gateway.tenant_id,
        uploaded_by_user_id=user.id,
        url=url,
        object_name=object_name,
        status=PlugPhotoStatus.PENDING,
    )
    db.add(photo)
    await db.commit()
    await db.refresh(photo)

    logger.info(
        "Plug photo uploaded (pending review)",
        extra={"plug_id": plug.id, "photo_id": photo.id, "user_id": user.id},
    )
    return _plug_photo_response(photo, is_mine=True)


# ===========================================================================
# Reliability / uptime — services/reliability.py plug_uptime_7d
# ===========================================================================

@router.get("/api/plugs/{plug_id}/reliability")
async def get_plug_reliability(
    plug_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    The plug's 7-day REACHABILITY — gateway+mains power, NOT
    availability-to-charge (services/reliability.py plug_uptime_7d has the
    full definition). ``{uptime_pct, sample_window_days, last_seen_at}``;
    uptime_pct is null for a plug too young to have a meaningful reading.

    Any authenticated role; visibility follows GET /api/plugs/{id} exactly
    (ensure_plug_group_access), same lazy single-plug pattern as
    GET /api/plugs/{id}/tariff-preview. Deliberately NOT folded into the
    polled bulk list endpoints (GET /api/plugs/available, /public) — each
    call is a grouped-but-real telemetry_readings scan, too costly to run
    for every plug on every 30s poll.
    """
    from backend.services.reliability import plug_uptime_7d

    result = await db.execute(select(Plug).where(Plug.id == plug_id))
    plug = result.scalar_one_or_none()
    if not plug:
        raise HTTPException(status_code=404, detail=f"Plug with ID {plug_id} not found.")

    await ensure_plug_group_access(db, plug, user)

    data = await plug_uptime_7d(db, plug)
    return {
        "uptime_pct": data["uptime_pct"],
        "sample_window_days": data["sample_window_days"],
        "last_seen_at": data["last_seen_at"].isoformat() if data["last_seen_at"] else None,
    }



