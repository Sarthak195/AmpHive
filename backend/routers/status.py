"""
Public network status (GET /api/status), no auth.

One small aggregate of data that is already public via GET /api/plugs/public
and /map: overall health, gateway online/total, plug availability counts,
7-day reachability per public plug and today's tariff strip. It deliberately
carries no firmware version, backup age, IP, credential or per-gateway id, and
the newest gateway heartbeat is truncated to the minute.

Cost control for the 1 GB VM: the whole payload is cached in-process for
STATUS_CACHE_TTL_SEC (60 s) behind a lock, per-plug reachability (a grouped
telemetry scan) for REACHABILITY_TTL_SEC (1 h), and the route sets its own
``Cache-Control: public, max-age=60`` (main.py defaults API responses to
no-store). Cloudflare does not cache JSON by default, so edge caching would
need a Cache Rule; without one every request still reaches the in-process
cache, which is cheap.
"""
import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Response
from fastapi.responses import JSONResponse
from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.db import get_db
from backend.database.models import (
    ChargerGroup,
    Gateway,
    Plug,
    Tariff,
    TariffSlot,
    Tenant,
)
from backend.routers.plugs import get_public_plugs
from backend.services.money import to_money
from backend.services.pricing import (
    DEFAULT_TZ,
    _pick_tariff,
    _price_display_from_slots,
    _to_local,
    _zone,
    default_rate,
    zone_name,
)
from backend.services.rate_limit import rate_limit_dependency, status_rate_limiter
from backend.services.reliability import plug_uptime_7d
from backend.services.session_lifecycle import gateway_is_live

logger = logging.getLogger("amphive.status")
router = APIRouter()

STATUS_CACHE_TTL_SEC = 60.0
REACHABILITY_TTL_SEC = 3600.0

_clock = time.monotonic  # patched in tests
_lock = asyncio.Lock()
_payload_cache: dict = {"at": None, "body": None}
# plug_id -> (monotonic time, uptime_pct or None)
_reach_cache: dict = {}


def _minute(dt: Optional[datetime]) -> Optional[str]:
    """ISO UTC timestamp truncated to the minute (coarsened on purpose)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(second=0, microsecond=0).isoformat()


def _availability_counts(plugs) -> dict:
    """Plug counts by what a driver could do right now. Offline wins over the
    DB status (a plug behind an unreachable gateway cannot be started)."""
    counts = {"total": len(plugs), "available": 0, "occupied": 0, "offline": 0, "maintenance": 0}
    for p in plugs:
        if not p.gateway_online or p.status == "offline":
            counts["offline"] += 1
        elif p.status in counts:
            counts[p.status] += 1
    return counts


def _overall(gw_online: int, gw_total: int) -> str:
    return "operational" if gw_online == gw_total else "degraded"


async def _reachability(db: AsyncSession, plug_id: int, now: float) -> Optional[float]:
    hit = _reach_cache.get(plug_id)
    if hit is not None and now - hit[0] < REACHABILITY_TTL_SEC:
        return hit[1]
    plug = await db.get(Plug, plug_id)
    pct = (await plug_uptime_7d(db, plug))["uptime_pct"] if plug is not None else None
    _reach_cache[plug_id] = (now, pct)
    return pct


async def _tariff_today(db: AsyncSession, plug_id: int) -> dict:
    """24 hourly rates (the rate in force at the top of each local hour today)
    for one plug's effective tariff, in its tenant's timezone. Two queries,
    then pure evaluation, so it stays cheap behind the 60 s cache."""
    row = (await db.execute(
        select(Plug, ChargerGroup.tariff_id, Tenant)
        .join(Gateway, Gateway.id == Plug.gateway_id)
        .join(Tenant, Tenant.id == Gateway.tenant_id)
        .outerjoin(ChargerGroup, ChargerGroup.id == Plug.group_id)
        .where(Plug.id == plug_id)
    )).first()
    if row is None:
        return {"timezone": zone_name(DEFAULT_TZ), "hourly": []}
    plug, group_tariff_id, tenant = row
    tz_name = tenant.timezone or DEFAULT_TZ
    chain = (plug.tariff_id, group_tariff_id, tenant.default_tariff_id)
    ids = {t for t in chain if t is not None}
    tariffs = {}
    if ids:
        tariffs = {
            t.id: t for t in (
                await db.execute(select(Tariff).where(Tariff.id.in_(ids)))
            ).scalars()
        }
    tariff = _pick_tariff(chain, tariffs)
    slots = []
    flat = default_rate()
    if tariff is not None:
        flat = to_money(tariff.price_per_kwh)
        slots = list((await db.execute(
            select(TariffSlot).where(TariffSlot.tariff_id == tariff.id)
        )).scalars())
    midnight = _to_local(datetime.now(timezone.utc), _zone(tz_name)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    hourly = [
        float(_price_display_from_slots(slots, midnight.replace(hour=h), flat)[0])
        for h in range(24)
    ]
    return {"timezone": zone_name(tz_name), "hourly": hourly}


async def _build(db: AsyncSession, now: float) -> dict:
    await db.execute(text("SELECT 1"))
    plugs = await get_public_plugs(db=db)

    public_group_ids = (
        select(ChargerGroup.id).where(ChargerGroup.is_public == True)  # noqa: E712
        .scalar_subquery()
    )
    gateways = (await db.execute(
        select(Gateway).where(Gateway.id.in_(
            select(Plug.gateway_id).where(
                or_(Plug.group_id.is_(None), Plug.group_id.in_(public_group_ids))
            )
        ))
    )).scalars().all()
    utc_now = datetime.now(timezone.utc)
    live = [g for g in gateways if gateway_is_live(g, utc_now)]
    seen = [
        g.last_seen_at if g.last_seen_at.tzinfo else g.last_seen_at.replace(tzinfo=timezone.utc)
        for g in gateways if g.last_seen_at is not None
    ]

    reach = []
    for p in plugs:
        reach.append({
            "plug_id": p.id,
            "name": p.name,
            "status": p.status,
            "online": p.gateway_online,
            "uptime_7d_pct": await _reachability(db, p.id, now),
        })

    return {
        "status": _overall(len(live), len(gateways)),
        "db": "ok",
        "generated_at": _minute(utc_now),
        "gateways": {
            "online": len(live),
            "total": len(gateways),
            "last_seen_at": _minute(max(seen, default=None)),
        },
        "plugs": _availability_counts(plugs),
        "reachability": reach,
        "tariff_today": await _tariff_today(db, plugs[0].id) if plugs else None,
    }


@router.get(
    "/api/status",
    dependencies=[Depends(rate_limit_dependency(status_rate_limiter, "status"))],
)
async def get_status(response: Response, db: AsyncSession = Depends(get_db)):
    """PUBLIC (no auth) network status. See the module docstring."""
    now = _clock()
    async with _lock:
        cached_at = _payload_cache["at"]
        if cached_at is None or now - cached_at >= STATUS_CACHE_TTL_SEC:
            try:
                body = await _build(db, now)
            except Exception:
                logger.exception("status build failed")
                return JSONResponse(
                    {"status": "down", "db": "error"},
                    status_code=503,
                    headers={"Cache-Control": "no-store"},
                )
            _payload_cache["at"], _payload_cache["body"] = now, body
        body = _payload_cache["body"]
    response.headers["Cache-Control"] = f"public, max-age={int(STATUS_CACHE_TTL_SEC)}"
    return body
