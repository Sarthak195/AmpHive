"""
Recurring weekly charge routines (feature C5), owner only.

  GET    /api/me/routines        {routines: [...], max_routines}
  POST   /api/me/routines        create (max 5 per driver)
  PATCH  /api/me/routines/{id}   change days, time, limits, opt-in, on/off
  DELETE /api/me/routines/{id}   delete (withdraws a not-yet-started row)

Another driver's routine answers 404, never 403. A routine only ever queues
an ordinary scheduled charge (services/routines.py); nothing here can switch
a plug on. The start time is wall-clock in the plug's tenant timezone, which
every row returns as tenant_timezone. Writes are refused for a cpo_viewer by
the global read-only guard; a GET returns only the caller's own rows (plug
name and limits, no infrastructure fields).
"""
from datetime import datetime, time, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.db import get_db
from backend.database.models import ChargeRoutine, Gateway, Plug, Tenant, User
from backend.services import pricing
from backend.services.auth import get_current_user
from backend.services.routines import (
    MAX_ROUTINES_PER_USER,
    cancel_pending,
    has_group_access,
    skip_started_occurrence,
)
from backend.services.session_start import queued_charging_enabled

router = APIRouter()


def _clean_time(v: Optional[time]) -> Optional[time]:
    if v is None:
        return v
    if v.tzinfo is not None:
        raise ValueError("start_time is local wall-clock time; send it without an offset")
    return v.replace(second=0, microsecond=0)


class RoutineCreateRequest(BaseModel):
    plug_id: int
    weekdays: int = Field(..., ge=1, le=127)
    start_time: time
    # Same bounds as QueueChargeRequest / SessionStartRequest.
    max_duration_seconds: Optional[int] = Field(default=None, gt=0, le=86400)
    max_kwh: Optional[float] = Field(default=None, gt=0, le=100.0)
    auto_stop_when_full: bool = False
    enabled: bool = True

    @field_validator("start_time")
    @classmethod
    def _local_time(cls, v):
        return _clean_time(v)

    @model_validator(mode="after")
    def _needs_limit(self):
        if self.max_kwh is None and self.max_duration_seconds is None:
            raise ValueError("Set a time or energy limit for a routine.")
        return self


class RoutineUpdateRequest(BaseModel):
    weekdays: Optional[int] = Field(default=None, ge=1, le=127)
    start_time: Optional[time] = None
    max_duration_seconds: Optional[int] = Field(default=None, gt=0, le=86400)
    max_kwh: Optional[float] = Field(default=None, gt=0, le=100.0)
    auto_stop_when_full: Optional[bool] = None
    enabled: Optional[bool] = None

    @field_validator("start_time")
    @classmethod
    def _local_time(cls, v):
        return _clean_time(v)


def _routine_response(r: ChargeRoutine, plug_name: str, tz_name: Optional[str]) -> dict:
    return {
        "id": r.id,
        "plug_id": r.plug_id,
        "plug_name": plug_name,
        "weekdays": r.weekdays,
        "start_time": r.start_time.strftime("%H:%M"),
        "max_kwh": r.max_kwh,
        "max_duration_seconds": r.max_duration_seconds,
        "auto_stop_when_full": bool(r.auto_stop_when_full),
        "enabled": bool(r.enabled),
        "consecutive_failures": r.consecutive_failures or 0,
        "last_materialized_date": (
            r.last_materialized_date.isoformat() if r.last_materialized_date else None
        ),
        "tenant_timezone": pricing.zone_name(tz_name),
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


def _conflict(code: str, message: str, status_code: int = 409) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


async def _plug_context(db: AsyncSession, plug_id: int):
    """(plug, tenant) for a plug id, or None."""
    row = (await db.execute(
        select(Plug, Tenant)
        .join(Gateway, Gateway.id == Plug.gateway_id)
        .join(Tenant, Tenant.id == Gateway.tenant_id)
        .where(Plug.id == plug_id)
    )).one_or_none()
    return row


async def _owned(db: AsyncSession, routine_id: int, user: User) -> ChargeRoutine:
    routine = (await db.execute(
        select(ChargeRoutine)
        .where(and_(ChargeRoutine.id == routine_id, ChargeRoutine.user_id == user.id))
        .with_for_update()
    )).scalar_one_or_none()
    if routine is None:
        raise HTTPException(status_code=404, detail="Routine not found.")
    return routine


def _require_gate(tenant: Tenant, plug: Plug) -> None:
    if not queued_charging_enabled(tenant, plug):
        raise _conflict("queue_disabled", "Scheduled charging isn't enabled for this charger.")


@router.get("/api/me/routines")
async def list_routines(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = (await db.execute(
        select(ChargeRoutine, Plug.name, Tenant.timezone)
        .join(Plug, Plug.id == ChargeRoutine.plug_id)
        .join(Gateway, Gateway.id == Plug.gateway_id)
        .join(Tenant, Tenant.id == Gateway.tenant_id)
        .where(ChargeRoutine.user_id == user.id)
        .order_by(ChargeRoutine.created_at, ChargeRoutine.id)
    )).all()
    return {
        "routines": [_routine_response(r, name, tz) for r, name, tz in rows],
        "max_routines": MAX_ROUTINES_PER_USER,
    }


@router.post("/api/me/routines", status_code=201)
async def create_routine(
    req: RoutineCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Lock the user row so two creates can't both pass the cap check.
    await db.execute(select(User.id).where(User.id == user.id).with_for_update())
    count = (await db.execute(
        select(func.count()).select_from(ChargeRoutine).where(ChargeRoutine.user_id == user.id)
    )).scalar_one()
    if count >= MAX_ROUTINES_PER_USER:
        raise _conflict("routine_limit", f"You can have up to {MAX_ROUTINES_PER_USER} routines.")
    ctx = await _plug_context(db, req.plug_id)
    if ctx is None:
        raise HTTPException(status_code=404, detail=f"Plug {req.plug_id} not found.")
    plug, tenant = ctx
    if not await has_group_access(db, user.id, plug):
        raise HTTPException(
            status_code=403,
            detail="You don't have access to this plug. Join the group first.",
        )
    if req.enabled:
        _require_gate(tenant, plug)
    routine = ChargeRoutine(
        user_id=user.id,
        plug_id=plug.id,
        tenant_id=tenant.id,
        weekdays=req.weekdays,
        start_time=req.start_time,
        max_kwh=req.max_kwh,
        max_duration_seconds=req.max_duration_seconds,
        auto_stop_when_full=req.auto_stop_when_full,
        enabled=req.enabled,
        consecutive_failures=0,
    )
    skip_started_occurrence(routine, pricing._zone(tenant.timezone), datetime.now(timezone.utc))
    db.add(routine)
    await db.commit()
    await db.refresh(routine)
    return _routine_response(routine, plug.name, tenant.timezone)


@router.patch("/api/me/routines/{routine_id}")
async def update_routine(
    routine_id: int,
    req: RoutineUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    routine = await _owned(db, routine_id, user)
    ctx = await _plug_context(db, routine.plug_id)
    if ctx is None:
        raise HTTPException(status_code=404, detail="Routine not found.")
    plug, tenant = ctx
    fields = req.model_fields_set
    for name in ("weekdays", "start_time", "auto_stop_when_full"):
        if name in fields:
            value = getattr(req, name)
            if value is None:
                raise _conflict("invalid_routine", f"{name} can't be empty.", status_code=422)
            setattr(routine, name, value)
    # A limit may be cleared with an explicit null, as long as one remains.
    for name in ("max_kwh", "max_duration_seconds"):
        if name in fields:
            setattr(routine, name, getattr(req, name))
    if routine.max_kwh is None and routine.max_duration_seconds is None:
        raise _conflict("routine_needs_limit", "Set a time or energy limit for a routine.", status_code=422)
    reenabled = False
    if "enabled" in fields and req.enabled is not None and req.enabled != routine.enabled:
        reenabled = req.enabled
        if req.enabled:
            _require_gate(tenant, plug)
            # A fresh start: earlier failures no longer count.
            routine.consecutive_failures = 0
        else:
            await cancel_pending(db, routine)
        routine.enabled = req.enabled
    if reenabled or {"weekdays", "start_time"} & fields:
        # Never fire for an occurrence that began before this save.
        skip_started_occurrence(routine, pricing._zone(tenant.timezone), datetime.now(timezone.utc))
    await db.commit()
    await db.refresh(routine)
    return _routine_response(routine, plug.name, tenant.timezone)


@router.delete("/api/me/routines/{routine_id}")
async def delete_routine(
    routine_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    routine = await _owned(db, routine_id, user)
    await cancel_pending(db, routine)
    await db.delete(routine)
    await db.commit()
    return {"id": routine_id, "status": "deleted"}
