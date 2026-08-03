"""
Platform-admin routes (redesign/ui-v3 — plans/redesign-v3-contract.md §4).

The admin console's API surface: cross-tenant visibility (tenants, users,
payouts, gateways, disputes, audit) plus the two platform-level user
mutations (role/disable, wallet adjustment). Every endpoint requires the
'admin' role via require_role — and deliberately does NOT require a
tenant_id on the caller: platform admins have tenant_id NULL by design,
which is exactly what distinguishes them from a tenant-scoped CPO.

Conventions mirror routers/cpo.py: async SQLAlchemy 2.0 selects, money via
services/money.to_money, audit rows via services/audit.try_record_audit
(written AFTER the primary commit; response snapshots taken first — see
that function's session-state caveat), paginated lists return
{"total": int, "items": [...]} with limit (cap 200, default 50) + offset.
"""
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.db import get_db
from backend.database.models import (
    AuditLog,
    ChargingSession,
    DisputeStatus,
    FirmwareRelease,
    Gateway,
    GatewayLog,
    GatewayStatus,
    LedgerTransaction,
    Payout,
    PayoutStatus,
    Plug,
    SessionDispute,
    SessionStatus,
    Tenant,
    TransactionType,
    User,
    UserRole,
)
from backend.schemas import (
    AdminAdjustBalanceRequest,
    AdminFirmwareReleaseCreateRequest,
    AdminGatewayMintRequest,
    AdminUserUpdateRequest,
)
from backend.services.audit import try_record_audit
from backend.services.money import ZERO_MONEY, to_money
from backend.services.rbac import require_role
from backend.services.session_lifecycle import (
    GATEWAY_LIVENESS_WINDOW_SEC,
    gateway_is_live,
)
from backend.services.versioning import version_sort_key

logger = logging.getLogger("amphive.api")
router = APIRouter()

# Statuses that count as realized revenue — same set the CPO analytics use.
_REVENUE_STATUSES = (SessionStatus.COMPLETED, SessionStatus.PAID)


def _clamp_page(limit: int, offset: int) -> Tuple[int, int]:
    """House pagination bounds (see GET /api/cpo/audit): limit capped at 200
    per page, offset non-negative."""
    return max(1, min(limit, 200)), max(0, offset)


# [Claim-code onboarding] Unambiguous alphabet for admin-minted gateway claim
# codes — no 0/O, 1/I/L (hand-copied off a printed label, same rationale as
# the firmware's captive-portal setup-code alphabet, firmware/main/main.c).
# Uppercase-only: routers/cpo/_gateways.py normalizes a submitted code to
# uppercase before lookup, so this is the canonical stored form.
_CLAIM_CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
_CLAIM_CODE_LEN = 10


async def _generate_unique_claim_code(db: AsyncSession) -> str:
    """A fresh claim_code not already in use. claim_code is UNIQUE (partial
    index, WHERE NOT NULL — see Gateway.__table_args__), so a collision on
    32**10 possibilities is astronomically unlikely; the retry loop just
    turns one into a re-roll instead of an IntegrityError, mirroring
    routers/cpo/_groups.py's generate_unique_access_code."""
    for _ in range(20):
        code = "".join(secrets.choice(_CLAIM_CODE_ALPHABET) for _ in range(_CLAIM_CODE_LEN))
        existing = await db.execute(select(Gateway.id).where(Gateway.claim_code == code))
        if existing.scalar_one_or_none() is None:
            return code
    raise HTTPException(status_code=500, detail="Could not generate a unique claim code; try again.")


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


# ===========================================================================
# Platform overview
# ===========================================================================


@router.get("/api/admin/stats/overview")
async def admin_stats_overview(
    user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Cross-tenant platform KPIs for the admin dashboard. Counting/summing
    queries only — mirrors cpo_analytics_overview, minus the tenant scope.
    """
    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    tenants_total = (await db.execute(select(func.count(Tenant.id)))).scalar() or 0

    role_rows = (
        await db.execute(select(User.role, func.count(User.id)).group_by(User.role))
    ).all()
    role_counts = {role: int(count) for role, count in role_rows}

    gateways_total = (await db.execute(select(func.count(Gateway.id)))).scalar() or 0
    gateways_online = (
        await db.execute(
            select(func.count(Gateway.id)).where(Gateway.status == GatewayStatus.ONLINE)
        )
    ).scalar() or 0

    plugs_total = (await db.execute(select(func.count(Plug.id)))).scalar() or 0

    sessions_active = (
        await db.execute(
            select(func.count(ChargingSession.id)).where(
                ChargingSession.status == SessionStatus.ACTIVE
            )
        )
    ).scalar() or 0
    sessions_today = (
        await db.execute(
            select(func.count(ChargingSession.id)).where(
                ChargingSession.started_at >= today_start
            )
        )
    ).scalar() or 0
    sessions_total = (
        await db.execute(select(func.count(ChargingSession.id)))
    ).scalar() or 0

    today_row = (
        await db.execute(
            select(
                func.coalesce(func.sum(ChargingSession.energy_kwh), 0),
                func.coalesce(func.sum(ChargingSession.coins_spent), 0),
            ).where(and_(
                ChargingSession.started_at >= today_start,
                ChargingSession.status.in_(_REVENUE_STATUSES),
            ))
        )
    ).first()
    total_row = (
        await db.execute(
            select(
                func.coalesce(func.sum(ChargingSession.energy_kwh), 0),
                func.coalesce(func.sum(ChargingSession.coins_spent), 0),
            ).where(ChargingSession.status.in_(_REVENUE_STATUSES))
        )
    ).first()

    payout_row = (
        await db.execute(
            select(
                func.count(Payout.id),
                func.coalesce(func.sum(Payout.net_coins), 0),
            ).where(Payout.status == PayoutStatus.REQUESTED)
        )
    ).first()

    disputes_open = (
        await db.execute(
            select(func.count(SessionDispute.id)).where(
                SessionDispute.status == DisputeStatus.OPEN
            )
        )
    ).scalar() or 0

    return {
        "tenants": int(tenants_total),
        "users": {
            "total": sum(role_counts.values()),
            "drivers": role_counts.get(UserRole.DRIVER, 0),
            "cpos": role_counts.get(UserRole.CPO, 0),
            "admins": role_counts.get(UserRole.ADMIN, 0),
        },
        "gateways": {"total": int(gateways_total), "online": int(gateways_online)},
        "plugs": {"total": int(plugs_total)},
        "sessions": {
            "active": int(sessions_active),
            "today": int(sessions_today),
            "total": int(sessions_total),
        },
        "energy_kwh": {
            "today": round(float(today_row[0]), 3) if today_row else 0.0,
            "total": round(float(total_row[0]), 3) if total_row else 0.0,
        },
        "revenue_coins": {
            "today": round(float(today_row[1]), 2) if today_row else 0.0,
            "total": round(float(total_row[1]), 2) if total_row else 0.0,
        },
        "payouts": {
            "requested_count": int(payout_row[0]) if payout_row else 0,
            "requested_net_coins": round(float(payout_row[1]), 2) if payout_row else 0.0,
        },
        "disputes": {"open": int(disputes_open)},
    }


# ===========================================================================
# Tenants
# ===========================================================================


def _tenant_aggregate_query(cutoff_30d: datetime):
    """SELECT of (Tenant, per-tenant aggregate columns) — correlated scalar
    subqueries so one round trip serves the whole page (no N+1; the pattern
    the 2026-07-20 audit's batch-pricing fix established)."""
    user_count = (
        select(func.count(User.id))
        .where(User.tenant_id == Tenant.id)
        .correlate(Tenant).scalar_subquery()
    )
    gateway_count = (
        select(func.count(Gateway.id))
        .where(Gateway.tenant_id == Tenant.id)
        .correlate(Tenant).scalar_subquery()
    )
    gateways_online = (
        select(func.count(Gateway.id))
        .where(and_(Gateway.tenant_id == Tenant.id,
                    Gateway.status == GatewayStatus.ONLINE))
        .correlate(Tenant).scalar_subquery()
    )
    plug_count = (
        select(func.count(Plug.id))
        .join(Gateway, Plug.gateway_id == Gateway.id)
        .where(Gateway.tenant_id == Tenant.id)
        .correlate(Tenant).scalar_subquery()
    )
    sessions_30d = (
        select(func.count(ChargingSession.id))
        .where(and_(ChargingSession.tenant_id == Tenant.id,
                    ChargingSession.started_at >= cutoff_30d))
        .correlate(Tenant).scalar_subquery()
    )
    revenue_30d = (
        select(func.coalesce(func.sum(ChargingSession.coins_spent), 0))
        .where(and_(
            ChargingSession.tenant_id == Tenant.id,
            ChargingSession.started_at >= cutoff_30d,
            ChargingSession.status.in_(_REVENUE_STATUSES),
        ))
        .correlate(Tenant).scalar_subquery()
    )
    pending_payouts = (
        select(func.count(Payout.id))
        .where(and_(Payout.tenant_id == Tenant.id,
                    Payout.status == PayoutStatus.REQUESTED))
        .correlate(Tenant).scalar_subquery()
    )
    return select(
        Tenant, user_count, gateway_count, gateways_online, plug_count,
        sessions_30d, revenue_30d, pending_payouts,
    )


def _tenant_item(row) -> dict:
    (tenant, user_count, gateway_count, gateways_online, plug_count,
     sessions_30d, revenue_30d, pending_payouts) = row
    return {
        "id": tenant.id,
        "name": tenant.name,
        "created_at": _iso(tenant.created_at),
        "user_count": int(user_count or 0),
        "gateway_count": int(gateway_count or 0),
        "gateways_online": int(gateways_online or 0),
        "plug_count": int(plug_count or 0),
        "sessions_30d": int(sessions_30d or 0),
        "revenue_30d_coins": round(float(revenue_30d or 0), 2),
        "pending_payouts": int(pending_payouts or 0),
    }


def _payout_item(payout: Payout) -> dict:
    """Same shape as routers/cpo.py _payout_response — the admin console and
    the CPO portal must render the same payout row identically."""
    return {
        "id": payout.id,
        "tenant_id": payout.tenant_id,
        "period_start": _iso(payout.period_start),
        "period_end": _iso(payout.period_end),
        "gross_coins": float(payout.gross_coins),
        "platform_fee_coins": float(payout.platform_fee_coins),
        "net_coins": float(payout.net_coins),
        "status": payout.status.value,
        "requested_by_user_id": payout.requested_by_user_id,
        "requested_at": _iso(payout.requested_at),
        "paid_at": _iso(payout.paid_at),
        "note": payout.note,
    }


@router.get("/api/admin/tenants")
async def admin_list_tenants(
    user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
    q: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    """All tenants with per-tenant fleet/usage aggregates, newest first.
    Optional `q` substring-matches the tenant name (case-insensitive)."""
    limit, offset = _clamp_page(limit, offset)
    cutoff_30d = datetime.now(timezone.utc) - timedelta(days=30)

    conditions = []
    if q:
        conditions.append(Tenant.name.ilike(f"%{q}%"))

    total = (
        await db.execute(select(func.count(Tenant.id)).where(*conditions))
    ).scalar() or 0

    rows = await db.execute(
        _tenant_aggregate_query(cutoff_30d)
        .where(*conditions)
        .order_by(Tenant.created_at.desc(), Tenant.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return {"total": int(total), "items": [_tenant_item(r) for r in rows.all()]}


@router.get("/api/admin/tenants/{tenant_id}")
async def admin_tenant_detail(
    tenant_id: int,
    user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """One tenant: the list row's aggregates + GST identity, default tariff,
    its 10 most recent sessions, and its payout history."""
    cutoff_30d = datetime.now(timezone.utc) - timedelta(days=30)

    row = (
        await db.execute(_tenant_aggregate_query(cutoff_30d).where(Tenant.id == tenant_id))
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Tenant not found.")

    detail = _tenant_item(row)
    tenant = row[0]
    detail.update({
        # Contract key is gst_number; the column is Tenant.gstin.
        "gst_number": tenant.gstin,
        "legal_name": tenant.legal_name,
        "default_tariff_id": tenant.default_tariff_id,
    })

    session_rows = await db.execute(
        select(ChargingSession, Plug.name, User.email)
        .outerjoin(Plug, Plug.id == ChargingSession.plug_id)
        .outerjoin(User, User.id == ChargingSession.user_id)
        .where(ChargingSession.tenant_id == tenant_id)
        .order_by(ChargingSession.started_at.desc(), ChargingSession.id.desc())
        .limit(10)
    )
    detail["recent_sessions"] = [
        {
            "id": s.id,
            "plug_id": s.plug_id,
            "plug_name": plug_name if plug_name is not None else f"Plug #{s.plug_id}",
            "user_email": user_email if user_email is not None else "unknown",
            "started_at": _iso(s.started_at),
            "ended_at": _iso(s.ended_at),
            "energy_kwh": round(s.energy_kwh, 3),
            "coins_spent": round(float(s.coins_spent), 2),
            "status": s.status.value,
        }
        for s, plug_name, user_email in session_rows.all()
    ]

    payout_rows = await db.execute(
        select(Payout)
        .where(Payout.tenant_id == tenant_id)
        .order_by(Payout.requested_at.desc(), Payout.id.desc())
        .limit(50)
    )
    detail["payouts"] = [_payout_item(p) for p in payout_rows.scalars().all()]

    return detail


# ===========================================================================
# Users
# ===========================================================================


def _user_item(u: User, tenant_name: Optional[str]) -> dict:
    return {
        "id": u.id,
        "email": u.email,
        "full_name": u.full_name,
        "role": u.role.value,
        "tenant_id": u.tenant_id,
        "tenant_name": tenant_name,
        "coin_balance": float(u.coin_balance),
        "is_disabled": u.is_disabled,
        "created_at": _iso(u.created_at),
    }


@router.get("/api/admin/users")
async def admin_list_users(
    user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
    q: Optional[str] = None,
    role: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    """All accounts, newest first. `q` substring-matches email or full name
    (case-insensitive); `role` filters exactly (400 on an unknown role)."""
    limit, offset = _clamp_page(limit, offset)

    conditions = []
    if q:
        pattern = f"%{q}%"
        conditions.append(or_(User.email.ilike(pattern), User.full_name.ilike(pattern)))
    if role:
        try:
            conditions.append(User.role == UserRole(role))
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid role '{role}'. Valid: {[r.value for r in UserRole]}",
            )

    total = (
        await db.execute(select(func.count(User.id)).where(*conditions))
    ).scalar() or 0

    rows = await db.execute(
        select(User, Tenant.name)
        .outerjoin(Tenant, Tenant.id == User.tenant_id)
        .where(*conditions)
        .order_by(User.created_at.desc(), User.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return {
        "total": int(total),
        "items": [_user_item(u, tenant_name) for u, tenant_name in rows.all()],
    }


@router.patch("/api/admin/users/{user_id}")
async def admin_update_user(
    user_id: int,
    req: AdminUserUpdateRequest,
    user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Change a user's role and/or disabled flag.

    Self-protection: an admin may not demote or disable their OWN account
    (403) — otherwise a lone platform admin could lock everyone (including
    themselves) out of the console with one click.

    A role change or a disable bumps token_version (DB-side atomic increment,
    same lost-update rationale as /api/auth/logout), so every outstanding JWT
    for the target dies immediately — a demoted CPO can't keep using an
    admin-issued token that still carries the old role claim, and a disabled
    user is locked out on their very next request (get_current_user also
    checks is_disabled directly, belt and braces). Audited.
    """
    new_role: Optional[UserRole] = None
    if req.role is not None:
        try:
            new_role = UserRole(req.role)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid role '{req.role}'. Valid: {[r.value for r in UserRole]}",
            )

    if req.role is None and req.is_disabled is None:
        raise HTTPException(status_code=400, detail="Nothing to update.")

    result = await db.execute(select(User).where(User.id == user_id))
    target = result.scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")

    if target.id == user.id and (
        (new_role is not None and new_role != UserRole.ADMIN)
        or req.is_disabled is True
    ):
        raise HTTPException(
            status_code=403,
            detail="You cannot demote or disable your own admin account.",
        )

    changes = []
    values = {}
    role_changed = new_role is not None and new_role != target.role
    if role_changed:
        values["role"] = new_role
        changes.append(f"role: {target.role.value} -> {new_role.value}")
    disabled_changed = (
        req.is_disabled is not None and req.is_disabled != target.is_disabled
    )
    if disabled_changed:
        values["is_disabled"] = req.is_disabled
        changes.append(f"is_disabled: {target.is_disabled} -> {req.is_disabled}")

    # Revoke outstanding tokens on a role change or a disable (a re-enable
    # doesn't need one — the user simply signs in again).
    bump = role_changed or (disabled_changed and req.is_disabled is True)
    if values:
        if bump:
            values["token_version"] = User.token_version + 1
        await db.execute(
            update(User)
            .where(User.id == user_id)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        await db.commit()

    final_role = (new_role or target.role).value
    final_disabled = req.is_disabled if req.is_disabled is not None else target.is_disabled

    logger.info(
        f"Admin user update: user={user_id} ({target.email}) "
        f"{'; '.join(changes) or 'no-op'} by {user.email}"
    )

    if changes:
        # tenant_id may be NULL — a platform-level action on a tenant-less
        # driver/admin still gets its audit row (see AuditLog.tenant_id note).
        await try_record_audit(
            db,
            tenant_id=target.tenant_id,
            actor_user_id=user.id,
            action="user.update",
            target_type="user",
            target_id=user_id,
            detail="; ".join(changes),
        )

    return {
        "status": "updated",
        "id": user_id,
        "role": final_role,
        "is_disabled": final_disabled,
        "tokens_revoked": bump,
    }


@router.post("/api/admin/users/{user_id}/adjust-balance")
async def admin_adjust_balance(
    user_id: int,
    req: AdminAdjustBalanceRequest,
    user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Signed manual wallet adjustment (goodwill credit / clawback) with a
    mandatory reason. Writes a LedgerTransaction so the driver's wallet feed
    shows the movement — typed with the closest existing tx_type (`topup`
    for a credit, `session_debit` for a debit; the description carries the
    real story, matching how dispute refunds reuse `refund`).

    Race-safe the same way services/wallet.debit_wallet_clamped is: the
    balance is read as a COLUMN under SELECT ... FOR UPDATE (bypassing the
    identity map), the new balance computed and written while the row lock
    is held. A debit is floored at 0 — the DB CHECK constraint
    (ck_users_coin_balance_non_negative) forbids negative balances, so the
    ledger records the ACTUAL applied delta, not the requested one. Audited.
    """
    amount = to_money(req.amount_coins)
    if amount == ZERO_MONEY:
        raise HTTPException(status_code=400, detail="amount_coins must be non-zero.")

    row = (
        await db.execute(
            select(User.coin_balance, User.email, User.tenant_id)
            .where(User.id == user_id)
            .with_for_update()
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="User not found.")
    prev_balance, target_email, target_tenant_id = row

    prev_balance = to_money(prev_balance)
    if prev_balance < ZERO_MONEY:  # legacy rows predating the CHECK constraint
        prev_balance = ZERO_MONEY

    new_balance = prev_balance + amount
    if new_balance < ZERO_MONEY:
        new_balance = ZERO_MONEY  # floor: never below the DB CHECK
    applied = new_balance - prev_balance
    if applied == ZERO_MONEY:
        raise HTTPException(
            status_code=400,
            detail="Balance is already 0; there is nothing to debit.",
        )

    await db.execute(
        update(User)
        .where(User.id == user_id)
        .values(coin_balance=new_balance)
        .execution_options(synchronize_session=False)
    )
    db.add(LedgerTransaction(
        user_id=user_id,
        session_id=None,
        amount=applied,  # signed; the ACTUAL delta after the 0-floor
        transaction_type=(
            TransactionType.TOPUP if applied > ZERO_MONEY else TransactionType.SESSION_DEBIT
        ),
        description=f"Admin balance adjustment: {req.reason}",
        balance_after=new_balance,
    ))
    await db.commit()

    logger.info(
        f"Admin balance adjustment: user={user_id} ({target_email}) "
        f"requested={amount} applied={applied} balance {prev_balance} -> {new_balance} "
        f"by {user.email}"
    )

    await try_record_audit(
        db,
        tenant_id=target_tenant_id,
        actor_user_id=user.id,
        action="user.adjust_balance",
        target_type="user",
        target_id=user_id,
        detail=(
            f"requested={amount}, applied={applied}, "
            f"balance {prev_balance} -> {new_balance}; reason={req.reason}"
        ),
    )

    return {"new_balance": float(new_balance)}


# ===========================================================================
# Payouts / gateways / disputes / audit (cross-tenant reads)
# ===========================================================================


@router.get("/api/admin/payouts")
async def admin_list_payouts(
    user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    """Every tenant's payouts, newest request first, + tenant_name. Optional
    `status` filter (requested/paid/cancelled — 400 otherwise). The admin
    settles these via the existing POST /api/cpo/payouts/{id}/mark_paid."""
    limit, offset = _clamp_page(limit, offset)

    conditions = []
    if status:
        try:
            conditions.append(Payout.status == PayoutStatus(status))
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid status '{status}'. Valid: {[s.value for s in PayoutStatus]}",
            )

    total = (
        await db.execute(select(func.count(Payout.id)).where(*conditions))
    ).scalar() or 0

    rows = await db.execute(
        select(Payout, Tenant.name)
        .join(Tenant, Tenant.id == Payout.tenant_id)
        .where(*conditions)
        .order_by(Payout.requested_at.desc(), Payout.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return {
        "total": int(total),
        "items": [
            {**_payout_item(p), "tenant_name": tenant_name}
            for p, tenant_name in rows.all()
        ],
    }


@router.get("/api/admin/gateways")
async def admin_list_gateways(
    user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
    online: Optional[bool] = None,
    limit: int = 50,
    offset: int = 0,
):
    """Cross-tenant gateway fleet, most recently seen first. `online` derives
    from gateway_is_live (status flag AND fresh last_seen_at) — the raw flag
    alone lies after a missed LWT (crash outage), and the firmware's retained
    `online` replay re-asserts it on every broker restart."""
    limit, offset = _clamp_page(limit, offset)

    conditions = []
    if online is not None:
        # SQL mirror of gateway_is_live so the filter matches the derived field.
        cutoff = datetime.now(timezone.utc) - timedelta(
            seconds=GATEWAY_LIVENESS_WINDOW_SEC
        )
        live = and_(
            Gateway.status == GatewayStatus.ONLINE, Gateway.last_seen_at >= cutoff
        )
        conditions.append(
            live if online
            else or_(Gateway.status != GatewayStatus.ONLINE, Gateway.last_seen_at < cutoff)
        )

    total = (
        await db.execute(select(func.count(Gateway.id)).where(*conditions))
    ).scalar() or 0

    plug_count_sq = (
        select(func.count(Plug.id))
        .where(Plug.gateway_id == Gateway.id)
        .correlate(Gateway).scalar_subquery()
    )
    rows = await db.execute(
        select(Gateway, Tenant.name, plug_count_sq)
        .join(Tenant, Tenant.id == Gateway.tenant_id)
        .where(*conditions)
        .order_by(Gateway.last_seen_at.desc(), Gateway.id)
        .limit(limit)
        .offset(offset)
    )
    return {
        "total": int(total),
        "items": [
            {
                "id": gw.id,
                "gateway_id": gw.id,
                "name": gw.name,
                "tenant_id": gw.tenant_id,
                "tenant_name": tenant_name,
                "online": gateway_is_live(gw),
                "last_seen_at": _iso(gw.last_seen_at),
                "firmware_version": gw.firmware_version,
                "plug_count": int(plug_count or 0),
            }
            for gw, tenant_name, plug_count in rows.all()
        ],
    }


@router.post("/api/admin/gateways/inventory")
async def admin_mint_inventory_gateway(
    req: AdminGatewayMintRequest,
    user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Pre-register a preflashed gateway as UNCLAIMED inventory (tenant_id
    NULL) with a fresh claim code, for the "flash -> mint inventory row ->
    create broker account -> print claim code + label" manufacturing
    sequence (deploy/docs/preflashed_unit_runbook.md). The buyer/CPO later
    binds it to their tenant via POST /api/cpo/gateways/claim.
    """
    existing = await db.execute(select(Gateway).where(Gateway.id == req.gateway_id))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail=f"Gateway '{req.gateway_id}' already exists.")

    claim_code = await _generate_unique_claim_code(db)
    gateway_name = req.name or f"Unclaimed gateway {req.gateway_id}"

    # vpn_ip is a legacy overlay column (NOT NULL + UNIQUE) direct-MQTT
    # gateways don't use — same gateway_id fallback as cpo_create_gateway.
    gateway = Gateway(
        id=req.gateway_id,
        tenant_id=None,
        name=gateway_name,
        vpn_ip=req.gateway_id,
        claim_code=claim_code,
    )
    db.add(gateway)
    await db.commit()
    await db.refresh(gateway)

    gw_id = gateway.id  # snapshot before the audit write (see try_record_audit's docstring)

    logger.info(f"Admin minted inventory gateway {gw_id} (claim code ***{claim_code[-4:]}) by {user.email}")

    await try_record_audit(
        db,
        tenant_id=None,
        actor_user_id=user.id,
        action="gateway.mint_inventory",
        target_type="gateway",
        target_id=gw_id,
        detail=f"name={gateway_name}",
    )

    return {
        "status": "minted",
        "gateway_id": gw_id,
        "name": gateway_name,
        "claim_code": claim_code,
    }


@router.get("/api/admin/gateways/inventory")
async def admin_list_inventory_gateways(
    user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
    claimed: Optional[bool] = None,
    limit: int = 50,
    offset: int = 0,
):
    """
    Factory inventory: every gateway minted via POST .../inventory, claimed
    or not, newest first. Deliberately separate from GET /api/admin/gateways
    above — that view inner-joins Tenant (a live tenant-scoped fleet view),
    so it structurally can't show tenant_id IS NULL (unclaimed) rows.
    `claimed` filters on `claimed_at IS [NOT] NULL`.
    """
    limit, offset = _clamp_page(limit, offset)

    conditions = [Gateway.claim_code.is_not(None)]
    if claimed is not None:
        conditions.append(
            Gateway.claimed_at.is_not(None) if claimed else Gateway.claimed_at.is_(None)
        )

    total = (await db.execute(select(func.count(Gateway.id)).where(*conditions))).scalar() or 0

    rows = await db.execute(
        select(Gateway)
        .where(*conditions)
        .order_by(Gateway.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return {
        "total": int(total),
        "items": [
            {
                "gateway_id": gw.id,
                "name": gw.name,
                "claim_code": gw.claim_code,
                "claimed": gw.claimed_at is not None,
                "claimed_at": _iso(gw.claimed_at),
                "tenant_id": gw.tenant_id,
                "created_at": _iso(gw.created_at),
            }
            for gw in rows.scalars().all()
        ],
    }


@router.post("/api/admin/firmware-releases")
async def admin_create_firmware_release(
    req: AdminFirmwareReleaseCreateRequest,
    user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Register a firmware release for the CPO OTA version picker
    (feat/ota-version-picker — replaces hand-pasting a firmware URL).

    This does NOT upload/host the binary — `req.url` must already be a
    reachable published image (today: `gs://amphive-fw`, see
    docs/FIRMWARE.md §7 "Publishing an OTA image" /
    deploy/scripts/publish_firmware.ps1). Binary upload through this
    endpoint is a deliberate follow-up, not part of this cut.
    """
    existing = await db.execute(
        select(FirmwareRelease).where(FirmwareRelease.version == req.version)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=400,
            detail=f"Firmware release '{req.version}' is already registered.",
        )

    release = FirmwareRelease(version=req.version, url=req.url, notes=req.notes, is_active=True)
    db.add(release)
    await db.commit()
    await db.refresh(release)

    rel_id = release.id  # snapshot before the audit write (see try_record_audit's docstring)

    logger.info(f"Admin registered firmware release {req.version} (id={rel_id}) by {user.email}")

    await try_record_audit(
        db,
        tenant_id=None,
        actor_user_id=user.id,
        action="firmware_release.create",
        target_type="firmware_release",
        target_id=str(rel_id),
        detail=f"version={req.version}, url={req.url}",
    )

    return {
        "id": rel_id,
        "version": release.version,
        "url": release.url,
        "notes": release.notes,
        "is_active": release.is_active,
        "created_at": _iso(release.created_at),
    }


@router.get("/api/admin/firmware-releases")
async def admin_list_firmware_releases(
    user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
    active: Optional[bool] = None,
):
    """
    Every registered firmware release (active or not — deactivated ones
    stay visible here for admin audit; only GET /api/cpo/firmware-releases
    hides them from CPOs), newest-version-first. Ordering is semver-aware
    (services/versioning.version_sort_key), not a string sort — "2.10.0"
    must rank above "2.9.0". `active` optionally filters to just one bucket.
    """
    conditions = []
    if active is not None:
        conditions.append(FirmwareRelease.is_active.is_(active))

    rows = (
        await db.execute(select(FirmwareRelease).where(*conditions))
    ).scalars().all()
    rows = sorted(rows, key=lambda r: version_sort_key(r.version), reverse=True)

    return {
        "total": len(rows),
        "items": [
            {
                "id": r.id,
                "version": r.version,
                "url": r.url,
                "notes": r.notes,
                "is_active": r.is_active,
                "created_at": _iso(r.created_at),
            }
            for r in rows
        ],
    }


@router.post("/api/admin/firmware-releases/{release_id}/deactivate")
async def admin_deactivate_firmware_release(
    release_id: int,
    user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Soft-deactivate a firmware release: it drops out of the CPO version
    picker (GET /api/cpo/firmware-releases) immediately but the row (and any
    past OTA that referenced it) is kept, not deleted. Idempotent —
    deactivating an already-inactive release just confirms the state rather
    than erroring.
    """
    result = await db.execute(select(FirmwareRelease).where(FirmwareRelease.id == release_id))
    release = result.scalar_one_or_none()
    if release is None:
        raise HTTPException(status_code=404, detail="Firmware release not found.")

    release.is_active = False
    await db.commit()

    logger.info(f"Admin deactivated firmware release {release.version} (id={release_id}) by {user.email}")

    await try_record_audit(
        db,
        tenant_id=None,
        actor_user_id=user.id,
        action="firmware_release.deactivate",
        target_type="firmware_release",
        target_id=str(release_id),
        detail=f"version={release.version}",
    )

    return {"status": "deactivated", "id": release_id, "version": release.version}


@router.get("/api/admin/gateway-logs")
async def admin_list_gateway_logs(
    user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
    gateway_id: Optional[str] = None,
    tenant_id: Optional[int] = None,
    level: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    """Cross-tenant forwarded firmware log lines (TD#28 diagnostics feed —
    row shape mirrors GET /api/cpo/gateways/{id}/logs), newest first. Optional
    `gateway_id`/`tenant_id`/`level` filters."""
    limit, offset = _clamp_page(limit, offset)

    conditions = []
    if gateway_id:
        conditions.append(GatewayLog.gateway_id == gateway_id)
    if tenant_id is not None:
        conditions.append(GatewayLog.tenant_id == tenant_id)
    if level:
        conditions.append(GatewayLog.level == level)

    total = (
        await db.execute(select(func.count(GatewayLog.id)).where(*conditions))
    ).scalar() or 0

    rows = await db.execute(
        select(GatewayLog, Tenant.name)
        .outerjoin(Tenant, Tenant.id == GatewayLog.tenant_id)
        .where(*conditions)
        .order_by(GatewayLog.created_at.desc(), GatewayLog.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return {
        "total": int(total),
        "items": [
            {
                "id": log.id,
                "gateway_id": log.gateway_id,
                "tenant_id": log.tenant_id,
                "tenant_name": tenant_name,
                "level": log.level,
                "message": log.message,
                "created_at": _iso(log.created_at),
            }
            for log, tenant_name in rows.all()
        ],
    }


@router.get("/api/admin/disputes")
async def admin_list_disputes(
    user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    """Every tenant's disputes, newest first, + tenant_name / user_email /
    session_cost_coins context. Optional `status` filter (open/approved/
    rejected — 400 otherwise). Resolution stays on the existing tenant-scoped
    POST /api/cpo/disputes/{id}/resolve."""
    limit, offset = _clamp_page(limit, offset)

    conditions = []
    if status:
        try:
            conditions.append(SessionDispute.status == DisputeStatus(status))
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid status '{status}'. Valid: {[s.value for s in DisputeStatus]}",
            )

    total = (
        await db.execute(
            select(func.count(SessionDispute.id)).where(*conditions)
        )
    ).scalar() or 0

    rows = await db.execute(
        select(SessionDispute, Tenant.name, User.email, ChargingSession.coins_spent)
        .join(Tenant, Tenant.id == SessionDispute.tenant_id)
        .outerjoin(User, User.id == SessionDispute.driver_user_id)
        .outerjoin(ChargingSession, ChargingSession.id == SessionDispute.session_id)
        .where(*conditions)
        .order_by(SessionDispute.created_at.desc(), SessionDispute.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return {
        "total": int(total),
        "items": [
            {
                "id": d.id,
                "session_id": d.session_id,
                "tenant_id": d.tenant_id,
                "tenant_name": tenant_name,
                "driver_user_id": d.driver_user_id,
                "user_email": user_email if user_email is not None else "unknown",
                "session_cost_coins": (
                    round(float(session_cost), 2) if session_cost is not None else None
                ),
                "reason": d.reason,
                "status": d.status.value,
                "resolution_note": d.resolution_note,
                "refund_coins": float(d.refund_coins) if d.refund_coins is not None else None,
                "created_at": _iso(d.created_at),
                "resolved_at": _iso(d.resolved_at),
                "resolved_by_user_id": d.resolved_by_user_id,
            }
            for d, tenant_name, user_email, session_cost in rows.all()
        ],
    }


@router.get("/api/admin/audit")
async def admin_list_audit(
    user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
    tenant_id: Optional[int] = None,
    limit: int = 50,
    offset: int = 0,
):
    """Cross-tenant audit trail, newest first (row shape mirrors
    GET /api/cpo/audit + tenant context). Optional `tenant_id` narrows to one
    tenant; platform-level rows (admin user actions) carry tenant_id NULL and
    appear only in the unfiltered view."""
    limit, offset = _clamp_page(limit, offset)

    conditions = []
    if tenant_id is not None:
        conditions.append(AuditLog.tenant_id == tenant_id)

    total = (
        await db.execute(select(func.count(AuditLog.id)).where(*conditions))
    ).scalar() or 0

    rows = await db.execute(
        select(AuditLog, User.email, Tenant.name)
        .outerjoin(User, User.id == AuditLog.actor_user_id)
        .outerjoin(Tenant, Tenant.id == AuditLog.tenant_id)
        .where(*conditions)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return {
        "total": int(total),
        "items": [
            {
                "id": entry.id,
                "tenant_id": entry.tenant_id,
                "tenant_name": tenant_name,
                "actor_user_id": entry.actor_user_id,
                "actor_email": actor_email,
                "action": entry.action,
                "target_type": entry.target_type,
                "target_id": entry.target_id,
                "detail": entry.detail,
                "created_at": _iso(entry.created_at),
            }
            for entry, actor_email, tenant_name in rows.all()
        ],
    }
