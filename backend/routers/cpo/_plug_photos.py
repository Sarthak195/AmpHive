"""
CPO Plug Photo moderation routes (database/models.py PlugPhoto — driver-side
upload lives in backend/routers/plugs.py: POST /api/plugs/{plug_id}/photos).

Driver photos are HELD FOR APPROVAL: a new upload is PENDING and invisible on
the public map until a CPO/admin approves it here. Mirrors _plug_reports.py's
tenant-scoped list + lifecycle shape. `tenant_id` is denormalized onto PlugPhoto
at upload (plug -> gateway -> tenant), so the queue is a single indexed equality
filter, no join. Rejecting also deletes the stored bytes from the public bucket
(services/photo_publish.delete_object) — a rejected photo must actually leave
storage, not just be hidden.
"""
import asyncio
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.db import get_db
from backend.database.models import PlugPhoto, PlugPhotoStatus, User
from backend.schemas import PlugPhotoResponse
from backend.services.photo_publish import PhotoPublishError, delete_object
from backend.services.rbac import require_role

from ._common import logger

router = APIRouter()


def _photo_response(photo: PlugPhoto) -> PlugPhotoResponse:
    return PlugPhotoResponse(
        id=photo.id,
        plug_id=photo.plug_id,
        url=photo.url,
        status=photo.status.value,
        created_at=photo.created_at.isoformat() if photo.created_at else None,
    )


@router.get("/api/cpo/plug-photos", response_model=List[PlugPhotoResponse])
async def cpo_list_plug_photos(
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
    status_filter: Optional[str] = "pending",
    limit: int = 100,
):
    """
    Driver photos submitted for the CPO's own plugs, for moderation. Defaults to
    the PENDING queue; pass status_filter=approved|rejected (or empty for all).
    Tenant-scoped via the denormalized tenant_id, newest first.
    """
    limit = max(1, min(limit, 500))
    conditions = [PlugPhoto.tenant_id == user.tenant_id]
    if status_filter:
        try:
            conditions.append(PlugPhoto.status == PlugPhotoStatus(status_filter))
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid status_filter '{status_filter}'. Valid: {[s.value for s in PlugPhotoStatus]}",
            )

    result = await db.execute(
        select(PlugPhoto)
        .where(and_(*conditions))
        .order_by(PlugPhoto.created_at.desc(), PlugPhoto.id.desc())
        .limit(limit)
    )
    return [_photo_response(p) for p in result.scalars().all()]


async def _load_pending_photo(db, photo_id: int, tenant_id: Optional[int]) -> PlugPhoto:
    """Load a tenant-owned photo locked for update, or 404. 409 if it has
    already been moderated (approved/rejected) — moderation is one-shot."""
    result = await db.execute(
        select(PlugPhoto)
        .where(and_(PlugPhoto.id == photo_id, PlugPhoto.tenant_id == tenant_id))
        .with_for_update()
    )
    photo = result.scalar_one_or_none()
    if not photo:
        raise HTTPException(status_code=404, detail="Plug photo not found or access denied.")
    if photo.status != PlugPhotoStatus.PENDING:
        raise HTTPException(
            status_code=409,
            detail=f"This photo is already {photo.status.value}.",
        )
    return photo


@router.post("/api/cpo/plug-photos/{photo_id}/approve", response_model=PlugPhotoResponse)
async def cpo_approve_plug_photo(
    photo_id: int,
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """Approve a pending photo — it becomes publicly visible on the map/panel.
    Tenant-scoped; one-shot (409 if already moderated)."""
    photo = await _load_pending_photo(db, photo_id, user.tenant_id)
    photo.status = PlugPhotoStatus.APPROVED
    photo.reviewed_at = datetime.now(timezone.utc)
    photo.reviewed_by_user_id = user.id
    await db.commit()
    await db.refresh(photo)
    logger.info(f"Plug photo {photo.id} approved by {user.email}")
    return _photo_response(photo)


@router.post("/api/cpo/plug-photos/{photo_id}/reject", response_model=PlugPhotoResponse)
async def cpo_reject_plug_photo(
    photo_id: int,
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """Reject a pending photo — mark it REJECTED and delete the stored bytes from
    the public bucket. The GCS delete is best-effort (idempotent on 404); a
    storage error is logged but does not block the rejection, so a bad photo is
    always taken out of the queue even if cleanup needs a retry."""
    photo = await _load_pending_photo(db, photo_id, user.tenant_id)
    photo.status = PlugPhotoStatus.REJECTED
    photo.reviewed_at = datetime.now(timezone.utc)
    photo.reviewed_by_user_id = user.id
    object_name = photo.object_name
    await db.commit()
    await db.refresh(photo)

    try:
        await asyncio.to_thread(delete_object, object_name)
    except PhotoPublishError:
        logger.exception(
            "Rejected plug photo %s but failed to delete its object %s from storage",
            photo.id, object_name,
        )

    logger.info(f"Plug photo {photo.id} rejected by {user.email}")
    return _photo_response(photo)
