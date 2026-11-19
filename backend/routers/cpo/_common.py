"""
Shared helpers for the backend.routers.cpo package: the tenant-scope guard
used by payouts/invoices/reservations, the cross-tenant tariff loader used by
the tariff-assignment endpoints, and the response-shape builders reused by
more than one route in their domain. Split out of the original monolithic
cpo.py (2026-07-21 package split, TD#7 follow-up) so each domain submodule
doesn't need to import from a sibling.
"""
import logging
import re
from typing import Optional, TypeVar

from fastapi import Depends, HTTPException, Request
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.models import Payout, PlugReport, SessionDispute, Tariff, TariffSlot, User
from backend.schemas import DisputeResponse, PlugReportResponse
from backend.services.auth import enforce_viewer_read_only, is_cpo_viewer
from backend.services.rbac import require_role

logger = logging.getLogger("amphive.api")

T = TypeVar("T")


# Ceiling on any caller-supplied `days` lookback window. Without one,
# `datetime.now() - timedelta(days=<huge>)` raises OverflowError inside the
# handler ("date value out of range" / "Python int too large to convert to
# C int") and, with no exception handler registered, surfaces as an opaque
# 500 — a one-request self-DoS-shaped error for any authenticated CPO
# (2026-08-18 audit). 10 years is far beyond TELEMETRY_RETENTION_DAYS (90)
# and beyond any real accounting export, so clamping is invisible to real
# callers. Clamped rather than 422-rejected to match how `limit`/`offset`
# are already handled on these same routes.
MAX_LOOKBACK_DAYS = 3660


# Ceiling for the list endpoints that had NO bound at all (fleet/history
# listings that grow with tenant size or account age). The default is
# deliberately generous so the response shape and contents are unchanged
# for every realistic dataset — this is a memory/latency ceiling, not
# pagination UX (the paginated routes use the house {total, items} shape
# with limit<=200 instead).
DEFAULT_LIST_LIMIT = 500
MAX_LIST_LIMIT = 1000


def clamp_list_window(limit: int, offset: int) -> tuple[int, int]:
    """Bound a caller-supplied list window to [1, MAX_LIST_LIMIT] / >=0."""
    return max(1, min(int(limit), MAX_LIST_LIMIT)), max(0, int(offset))


def clamp_days(days: int) -> int:
    """Bound a caller-supplied lookback window to [1, MAX_LOOKBACK_DAYS]."""
    return max(1, min(int(days), MAX_LOOKBACK_DAYS))


def _require_tenant_id(user: User) -> int:
    """Every /api/cpo/payouts* and /api/cpo/earnings route is tenant-scoped.
    A 'cpo' always has a tenant_id (set by cpo_setup); a bare 'admin' with no
    tenant attached hits this — same shape as cpo_setup's own checks."""
    if user.tenant_id is None:
        raise HTTPException(
            status_code=400,
            detail="You are not associated with a tenant.",
        )
    return user.tenant_id


async def require_cpo_tenant(
    request: Request,
    user: User = Depends(require_role("cpo", "admin", "cpo_viewer")),
) -> User:
    """Role gate for every tenant-scoped /api/cpo route (U4). The routes filter
    with `X.tenant_id == user.tenant_id`, which SQLAlchemy compiles to
    `X.tenant_id IS NULL` when the caller has no tenant, so a bare admin or a
    CPO not yet attached to a tenant would match unclaimed inventory gateways,
    their plugs and platform-level audit rows. Refuse such a caller before any
    query runs; platform admins manage unclaimed inventory via /api/admin/*.

    [F10] A read-only cpo_viewer is admitted for GET/HEAD/OPTIONS only. The
    global guard in get_current_user already refuses its writes; this second
    check keeps the CPO gate fail-closed on its own."""
    enforce_viewer_read_only(user, request.method, request.url.path)
    _require_tenant_id(user)
    return user


# [F10] PII patterns masked for a read-only cpo_viewer. Emails keep their
# first character and domain ("d***@example.com") so a demo still reads
# naturally. Phones: a run of 10-13 digits (optionally +country code and one
# space/dash group split), e.g. "+91 98765 43210" or "9876543210". Dates and
# ids in audit text never reach 10 contiguous digits, so they are left alone.
# IPv4 dotted quads (a plug LAN address, a legacy gateway vpn_ip) and the
# promo / claim code tails audit details carry ("hint=PQRS",
# "claim_code_tail=***WXYZ") are scrubbed too, as is the hint in the note a
# promo redemption stores on its offline top-up ("Promo code ...PQRS"). A
# three-part firmware version such as "2.9.0" is not a dotted quad, so it
# survives; an address ending a sentence ("at 192.168.1.50.") is still one.
_EMAIL_RE = re.compile(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
_PHONE_RE = re.compile(r"(?<![\w+])\+?\d{0,3}[ -]?\d{5}[ -]?\d{5}(?!\w)")
_IPV4_RE = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?!\.?\w)")
_CODE_TAIL_RE = re.compile(r"\b(hint|claim_code_tail)=[^\s,;]+")
_PROMO_NOTE_RE = re.compile(r"\b(Promo code \.\.\.)[^\s,;]+")


def mask_pii(text: Optional[str]) -> Optional[str]:
    """Mask every email address, phone number, IPv4 address and code tail
    inside `text`."""
    if not text:
        return text
    text = _EMAIL_RE.sub(lambda m: f"{m.group(1)}***@{m.group(2)}", text)
    text = _PHONE_RE.sub("[phone hidden]", text)
    text = _IPV4_RE.sub("[ip hidden]", text)
    text = _CODE_TAIL_RE.sub(r"\1=[hidden]", text)
    return _PROMO_NOTE_RE.sub(r"\1[hidden]", text)


def mask_name(name: Optional[str]) -> Optional[str]:
    """A person's name as initials: "Sarthak Jain" -> "S. J.", a one-word
    name (a Google signup with no name claim stores the email local part,
    e.g. "sarthak.jain") -> "S.". At most three initials. None and blank
    pass through, so the frontend's `name || email` fallbacks keep working."""
    if not name or not name.strip():
        return name
    initials = []
    for token in name.split()[:3]:
        first = next((c for c in token if c.isalnum()), None)
        if first is not None:
            initials.append(f"{first.upper()}.")
    return " ".join(initials) or "?"


def redact_for(viewer: User, text: Optional[str]) -> Optional[str]:
    """[F10] The one place /api/cpo/* responses and CSV exports decide whether
    personal data is shown: emails, phone numbers, IPv4 addresses and code
    tails (whole fields, or inside free text such as audit details) are
    masked for a read-only cpo_viewer and returned unchanged for a cpo/admin."""
    return mask_pii(text) if is_cpo_viewer(viewer) else text


def redact_name_for(viewer: User, name: Optional[str]) -> Optional[str]:
    """[F10] Same decision as redact_for, for a driver's full name: initials
    (mask_name) for a read-only cpo_viewer, unchanged for a cpo/admin."""
    return mask_name(name) if is_cpo_viewer(viewer) else name


def hide_for(viewer: User, value: T) -> Optional[T]:
    """[F10] Same decision as redact_for, for a field the viewer must not see
    at all (plug LAN address, gateway vpn_ip, promo code hint, plug
    coordinates): None for a read-only cpo_viewer, unchanged otherwise."""
    return None if is_cpo_viewer(viewer) else value


async def _load_tenant_tariff(db: AsyncSession, tariff_id: int, tenant_id: int) -> Tariff:
    """Fetch a tariff, enforcing it belongs to `tenant_id`. Shared by the
    three assignment endpoints below so cross-tenant assignment (attaching
    another tenant's tariff to your plug/group/tenant-default) is rejected
    the same way everywhere."""
    result = await db.execute(
        select(Tariff).where(and_(Tariff.id == tariff_id, Tariff.tenant_id == tenant_id))
    )
    tariff = result.scalar_one_or_none()
    if not tariff:
        raise HTTPException(
            status_code=404,
            detail="Tariff not found or does not belong to your organization.",
        )
    return tariff


def _payout_response(payout: Payout) -> dict:
    return {
        "id": payout.id,
        "tenant_id": payout.tenant_id,
        "period_start": payout.period_start.isoformat() if payout.period_start else None,
        "period_end": payout.period_end.isoformat() if payout.period_end else None,
        "gross_coins": float(payout.gross_coins),
        "platform_fee_coins": float(payout.platform_fee_coins),
        "net_coins": float(payout.net_coins),
        "status": payout.status.value,
        "requested_by_user_id": payout.requested_by_user_id,
        "requested_at": payout.requested_at.isoformat() if payout.requested_at else None,
        "paid_at": payout.paid_at.isoformat() if payout.paid_at else None,
        "note": payout.note,
    }


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


def _fmt_min(m: int) -> str:
    """A minute-of-day as HH:MM (1440 -> 24:00) for operator-facing messages."""
    return f"{int(m) // 60:02d}:{int(m) % 60:02d}"


def _slot_dict(s: TariffSlot) -> dict:
    return {
        "id": s.id,
        "tariff_id": s.tariff_id,
        "start_min": s.start_min,
        "end_min": s.end_min,
        "price_per_kwh": float(s.price_per_kwh),
        "days_mask": s.days_mask,
    }
