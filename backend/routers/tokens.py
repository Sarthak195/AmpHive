"""
Personal read-only API tokens and the meter endpoint (feature F7).

  POST   /api/me/tokens        {name} -> {id, name, token, prefix, created_at}
                               (JWT only; the token is shown once; max 5 live)
  GET    /api/me/tokens        metadata only, never the token or its hash
  DELETE /api/me/tokens/{id}   revoke
  GET    /api/me/meter         JWT OR a personal token; the only route a
                               token is accepted on (services/api_tokens.py)

The meter is shaped for Home Assistant's REST sensor and evcc's custom http
meter (docs/HOME_ASSISTANT.md). It reads the in-memory telemetry store plus a
couple of indexed row lookups; it never scans telemetry_readings.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend import state
from backend.database.db import get_db
from backend.database.models import (
    ApiToken,
    ChargingSession,
    Gateway,
    Plug,
    SessionStatus,
    User,
    UserRole,
)
from backend.services.api_tokens import (
    MAX_ACTIVE_TOKENS,
    TOKEN_SCOPE_READ,
    display_prefix,
    generate_token,
    get_meter_user,
    hash_token,
)
from backend.services.auth import get_current_user
from backend.services.mqtt_manager import PLUG_POWER_STALE_SEC
from backend.services.session_lifecycle import gateway_is_live
from backend.services.telemetry import TelemetrySnapshot

logger = logging.getLogger("amphive.api")
router = APIRouter()

# Roles whose meter covers every plug of their tenant.
TENANT_METER_ROLES = frozenset({UserRole.CPO, UserRole.ADMIN, UserRole.CPO_VIEWER})


class ApiTokenCreateRequest(BaseModel):
    name: str = Field(..., max_length=64)

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Give the token a name.")
        return value


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _token_metadata(token: ApiToken) -> dict:
    """Everything the list shows. Never the token or its hash."""
    return {
        "id": token.id,
        "name": token.name,
        "prefix": token.token_prefix,
        "scope": token.scope,
        "created_at": _iso(token.created_at),
        "last_used_at": _iso(token.last_used_at),
    }


@router.post("/api/me/tokens", status_code=201)
async def create_api_token(
    req: ApiTokenCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Mint a read-only token. The raw value is in this response only."""
    user_id = user.id
    # Lock the caller's row so two concurrent creates cannot both pass the cap.
    await db.execute(select(User.id).where(User.id == user_id).with_for_update())
    active = await db.scalar(
        select(func.count())
        .select_from(ApiToken)
        .where(ApiToken.user_id == user_id, ApiToken.revoked_at.is_(None))
    )
    if isinstance(active, int) and active >= MAX_ACTIVE_TOKENS:
        raise HTTPException(
            status_code=409,
            detail=f"You already have {MAX_ACTIVE_TOKENS} active API tokens. Revoke one first.",
        )

    raw = generate_token()
    token = ApiToken(
        user_id=user_id,
        name=req.name,
        token_prefix=display_prefix(raw),
        token_hash=hash_token(raw),
        scope=TOKEN_SCOPE_READ,
    )
    db.add(token)
    await db.commit()
    await db.refresh(token)
    logger.info("API token created", extra={"user_id": user_id, "token_id": token.id})
    return {
        "id": token.id,
        "name": token.name,
        "token": raw,
        "prefix": token.token_prefix,
        "created_at": _iso(token.created_at),
    }


@router.get("/api/me/tokens")
async def list_api_tokens(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The caller's live tokens, newest first (metadata only)."""
    rows = (
        await db.execute(
            select(ApiToken)
            .where(ApiToken.user_id == user.id, ApiToken.revoked_at.is_(None))
            .order_by(ApiToken.created_at.desc(), ApiToken.id.desc())
        )
    ).scalars().all()
    return {"tokens": [_token_metadata(t) for t in rows], "max_active": MAX_ACTIVE_TOKENS}


@router.delete("/api/me/tokens/{token_id}", status_code=204)
async def revoke_api_token(
    token_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Revoke one of the caller's live tokens; 404 for anyone else's, an
    unknown id or one already revoked."""
    user_id = user.id
    result = await db.execute(
        update(ApiToken)
        .where(
            ApiToken.id == token_id,
            ApiToken.user_id == user_id,
            ApiToken.revoked_at.is_(None),
        )
        .values(revoked_at=datetime.now(timezone.utc))
        .execution_options(synchronize_session=False)
    )
    if not getattr(result, "rowcount", 0):
        await db.rollback()
        raise HTTPException(status_code=404, detail="API token not found.")
    await db.commit()
    logger.info("API token revoked", extra={"user_id": user_id, "token_id": token_id})
    return Response(status_code=204)


# --- Meter ------------------------------------------------------------------

def _snapshot_fields(snapshot: Optional[TelemetrySnapshot], now_ts: float) -> dict:
    """power/current/voltage plus reading_at from the in-memory store.

    No snapshot, or start_session's placeholder ("starting": zeros stamped at
    start, not a reading), gives all-null. A snapshot older than
    PLUG_POWER_STALE_SEC keeps its reading_at but nulls the values, so a
    dead gateway never reports its last power draw as current."""
    if snapshot is None or snapshot.status == "starting":
        return {"power_w": None, "current_a": None, "voltage_v": None, "reading_at": None}
    fresh = now_ts - snapshot.updated_at <= PLUG_POWER_STALE_SEC
    return {
        "power_w": float(snapshot.power_w) if fresh else None,
        "current_a": float(snapshot.current_a) if fresh else None,
        "voltage_v": float(snapshot.voltage_v) if fresh else None,
        "reading_at": datetime.fromtimestamp(snapshot.updated_at, timezone.utc).isoformat(),
    }


def meter_plug(
    plug: Plug,
    gateway: Gateway,
    session: Optional[ChargingSession],
    snapshot: Optional[TelemetrySnapshot],
    now: datetime,
) -> dict:
    """One entry of the meter's "plugs" list (the F7 contract)."""
    return {
        "plug_id": plug.id,
        "name": plug.name,
        "status": plug.status.value,
        "online": gateway_is_live(gateway, now),
        **_snapshot_fields(snapshot, now.timestamp()),
        "session": None
        if session is None
        else {
            "id": session.id,
            "started_at": session.started_at.isoformat(),
            # The row's billed energy (monotonic max() in _persist_telemetry),
            # not the raw wire counter.
            "energy_kwh": float(session.energy_kwh or 0.0),
        },
    }


async def _meter_rows(db: AsyncSession, user: User) -> list:
    """(plug, gateway, active session or None) triples the caller may read.

    A cpo/admin/cpo_viewer with a tenant reads every plug of that tenant.
    Everyone else (a driver, or an operator not attached to a tenant, whose
    `tenant_id == None` filter would match unclaimed inventory) reads only
    the plugs of their own ACTIVE sessions."""
    if user.role in TENANT_METER_ROLES and user.tenant_id is not None:
        plug_rows = (
            await db.execute(
                select(Plug, Gateway)
                .join(Gateway, Plug.gateway_id == Gateway.id)
                .where(Gateway.tenant_id == user.tenant_id)
                .order_by(Plug.id)
            )
        ).all()
        plug_ids = [plug.id for plug, _ in plug_rows]
        sessions: dict[int, ChargingSession] = {}
        if plug_ids:
            active = (
                await db.execute(
                    select(ChargingSession)
                    .where(
                        ChargingSession.plug_id.in_(plug_ids),
                        ChargingSession.status == SessionStatus.ACTIVE,
                    )
                    .order_by(ChargingSession.started_at)
                )
            ).scalars().all()
            sessions = {s.plug_id: s for s in active}
        return [(plug, gateway, sessions.get(plug.id)) for plug, gateway in plug_rows]

    rows = (
        await db.execute(
            select(ChargingSession, Plug, Gateway)
            .join(Plug, ChargingSession.plug_id == Plug.id)
            .join(Gateway, Plug.gateway_id == Gateway.id)
            .where(
                ChargingSession.user_id == user.id,
                ChargingSession.status == SessionStatus.ACTIVE,
            )
            .order_by(ChargingSession.started_at)
        )
    ).all()
    return [(plug, gateway, session) for session, plug, gateway in rows]


@router.get("/api/me/meter")
async def get_meter(
    user: User = Depends(get_meter_user),
    db: AsyncSession = Depends(get_db),
):
    """Live power, current, voltage and session energy for the plugs the
    caller can read. Accepts the normal JWT or a personal read-only token."""
    now = datetime.now(timezone.utc)
    store = state.telemetry_store
    plugs = [
        meter_plug(plug, gateway, session, store.get_latest(plug.id), now)
        for plug, gateway, session in await _meter_rows(db, user)
    ]
    return {"generated_at": now.isoformat(), "plugs": plugs}

