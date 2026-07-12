"""
CPO admin action audit trail (TD#26).

There was previously no record of admin actions (gateway/plug/group create-
delete, status changes, access-code regeneration) in this multi-tenant
billing system — no accountability if a CPO operator's account is compromised
or misused. `AuditLog` (backend/database/models.py) closes that gap; this
module is the one place that constructs and persists a row for it.

Mirrors the append-only-log pattern already used for `GatewayEvent`
(operational alarms): plain-String `action`/`target_type` (the taxonomy is
expected to grow without a schema migration) and a `(tenant_id, created_at)`
index for the CPO-scoped read path (`GET /api/cpo/audit`).
"""
import logging
from typing import Optional, Union

from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.models import AuditLog

logger = logging.getLogger("amphive.api")


async def record_audit(
    db: AsyncSession,
    *,
    tenant_id: int,
    actor_user_id: Optional[int],
    action: str,
    target_type: str,
    target_id: Optional[Union[int, str]] = None,
    detail: Optional[str] = None,
) -> AuditLog:
    """
    Stage an AuditLog row on `db`. Does NOT flush or commit — this only calls
    `db.add()`, matching the add()-then-commit() shape already used
    throughout routers/cpo.py, so the caller decides exactly when (and
    whether) the write is persisted relative to the action it documents.

    Most callers should use `try_record_audit` below instead of calling this
    directly, unless they need to control the commit themselves.
    """
    entry = AuditLog(
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action=action,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        detail=detail,
    )
    db.add(entry)
    return entry


async def try_record_audit(
    db: AsyncSession,
    *,
    tenant_id: int,
    actor_user_id: Optional[int],
    action: str,
    target_type: str,
    target_id: Optional[Union[int, str]] = None,
    detail: Optional[str] = None,
) -> None:
    """
    Best-effort audit write for routers/cpo.py: call this AFTER the primary
    admin action's own commit has already landed. It stages the AuditLog row
    (record_audit) and commits it in its own transaction on the same
    session — an audit failure (bad data, a transient DB error) must never
    undo or fail the admin action that already succeeded, so any exception
    here is caught and the session rolled back rather than propagated (the
    rollback itself is guarded too — nothing on this path may raise).

    Not swallowed silently: a failure is logged at ERROR (with traceback) so
    a broken audit path is visible in the server logs instead of just quietly
    losing accountability records.

    Session-state caveat for callers: the failure-path rollback EXPIRES every
    ORM instance in the session (SQLAlchemy expires all non-expunged objects
    on rollback — expire_on_commit=False doesn't apply to rollback), and
    touching an expired attribute on an AsyncSession raises MissingGreenlet
    instead of lazily refreshing. Build/snapshot any response data taken from
    ORM objects BEFORE calling this function (see the payout endpoints in
    routers/cpo.py), or re-fetch it afterwards.
    """
    try:
        await record_audit(
            db,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            detail=detail,
        )
        await db.commit()
    except Exception:
        # Roll back FIRST: the failed commit leaves the transaction aborted,
        # and any later use of this session by the caller would error until
        # it's rolled back. Guarded so even a rollback failure (e.g. the
        # connection died) can't propagate into — and fail — the admin action
        # that already committed.
        try:
            await db.rollback()
        except Exception:
            logger.exception("Audit rollback failed; session may be unusable")
        logger.exception(
            f"Audit log write failed (non-fatal, action not affected): "
            f"action={action} target={target_type}:{target_id} tenant={tenant_id}"
        )
