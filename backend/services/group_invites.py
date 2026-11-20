"""
[C9] Email invites to a private charger group.

A CPO invites an address to one of its private groups; the invitee gets a
link (FRONTEND_ORIGIN/groups?invite=<token>) through the existing outbound
mail path (services/email.py send_email, the self-hosted relay in prod).

- Digest-only: the raw token (secrets.token_urlsafe(32), about 256 bits) is
  mailed and never stored or logged; group_invites.token_hash holds its
  SHA-256 hex digest (same scheme as EmailVerificationToken).
- Single-use and expiring: INVITE_TTL_DAYS (7). Consumed by one conditional
  UPDATE, so two concurrent accepts cannot both win.
- Bound to the invitee email (stored lowercased, compared case-insensitively):
  accepting needs a signed-in account whose VERIFIED email equals it. A wrong
  account or an unverified one is refused without consuming the invite.
- A newer invite for the same group and email supersedes the older one.
- Rate-limited to protect the mail server's reputation: per CPO
  (GROUP_INVITES_PER_DAY, default 20 in any 24 h) and platform-wide
  (GROUP_INVITES_GLOBAL_PER_DAY, default 50), both counted from the table
  itself (indexed range counts), so the caps survive a backend restart. Only a
  tenant that owns at least one claimed gateway may send invites: becoming a
  CPO is self-serve, claiming hardware is not, so throwaway accounts cannot
  turn the relay into a mailer. The group name stays out of the Subject line.
- The address must be one mask_pii (routers/cpo/_common.py) masks in full:
  an ASCII local part from [A-Za-z0-9._%+-] and an alphabetic TLD of two or
  more letters, so a cpo_viewer never sees it unmasked.
- The create response never depends on whether the address has an account
  (no user lookup happens), so it is not an enumeration oracle.
"""
import asyncio
import hashlib
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional, Set, Tuple

from fastapi import HTTPException
from sqlalchemy import and_, exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.models import (
    ChargerGroup,
    Gateway,
    GroupInvite,
    GroupMembership,
    OcppChargePoint,
    User,
)
from backend.services.billing_emails import _sanitize_operator_note
from backend.services.email import frontend_origin, send_email

logger = logging.getLogger("amphive.api")

INVITE_TTL_DAYS = 7
# `or "20"` (not a getenv default): an unset compose ${VAR} arrives as "".
GROUP_INVITES_PER_DAY = int(os.getenv("GROUP_INVITES_PER_DAY") or "20")
GROUP_INVITES_GLOBAL_PER_DAY = int(os.getenv("GROUP_INVITES_GLOBAL_PER_DAY") or "50")

# The anchored form of mask_pii's email pattern (routers/cpo/_common.py), so
# every accepted address is one the viewer masker fully masks. Not EmailStr:
# it rejects reserved TLDs such as .test that real seeded accounts use; the
# binding check at accept time is exact equality anyway.
_EMAIL_RE = re.compile(r"^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}$")

INVALID_INVITE = "Invalid or expired invite link."

# Strong refs for in-flight sends (a bare create_task result can be GC'd).
_pending: Set[asyncio.Task] = set()


def hash_invite_token(token: str) -> str:
    """SHA-256 hex digest, what group_invites.token_hash stores."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def normalize_invite_email(raw: str) -> str:
    """Trimmed, lowercased address; 422 when it does not look like one."""
    email = (raw or "").strip().lower()
    if len(email) > 255 or not _EMAIL_RE.fullmatch(email):
        raise HTTPException(status_code=422, detail="Enter a valid email address.")
    return email


async def invites_sent_last_day(db: AsyncSession, cpo_user_id: int, now: datetime) -> int:
    """How many invites this CPO created in the last 24 h (indexed on
    invited_by_user_id, created_at)."""
    return (
        await db.execute(
            select(func.count(GroupInvite.id)).where(
                and_(
                    GroupInvite.invited_by_user_id == cpo_user_id,
                    GroupInvite.created_at > now - timedelta(days=1),
                )
            )
        )
    ).scalar() or 0


async def invites_sent_last_day_global(db: AsyncSession, now: datetime) -> int:
    """How many invites anyone created in the last 24 h (indexed on
    created_at)."""
    return (
        await db.execute(
            select(func.count(GroupInvite.id)).where(
                GroupInvite.created_at > now - timedelta(days=1)
            )
        )
    ).scalar() or 0


async def tenant_has_gateway(db: AsyncSession, tenant_id: int) -> bool:
    """True when the tenant owns a claimed hardware gateway. The Gateway row
    behind an OCPP charge point does not count: any CPO can mint one itself
    while OCPP_CSMS_ENABLED is on, so it would not anchor the invite mail."""
    return (
        await db.execute(
            select(Gateway.id)
            .where(
                and_(
                    Gateway.tenant_id == tenant_id,
                    ~exists().where(OcppChargePoint.gateway_id == Gateway.id),
                )
            )
            .limit(1)
        )
    ).scalar() is not None


async def create_invite(
    db: AsyncSession, cpo_user: User, group: ChargerGroup, email: str
) -> Tuple[GroupInvite, str]:
    """Rate-check, supersede any pending invite for the same group+email,
    insert the new digest row and commit. Returns (invite, raw_token); the
    caller mails the token and never logs it."""
    now = datetime.now(timezone.utc)
    if not await tenant_has_gateway(db, group.tenant_id):
        raise HTTPException(
            status_code=409,
            detail="Claim a charger first: email invites open once your organisation has one.",
        )
    if await invites_sent_last_day(db, cpo_user.id, now) >= GROUP_INVITES_PER_DAY:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Invite limit reached ({GROUP_INVITES_PER_DAY} a day). "
                "Try again tomorrow, or share the access code instead."
            ),
        )
    if await invites_sent_last_day_global(db, now) >= GROUP_INVITES_GLOBAL_PER_DAY:
        raise HTTPException(
            status_code=429,
            detail=(
                "Email invites are paused for today. "
                "Try again tomorrow, or share the access code instead."
            ),
        )
    await db.execute(
        update(GroupInvite)
        .where(
            and_(
                GroupInvite.group_id == group.id,
                GroupInvite.email == email,
                GroupInvite.accepted_at.is_(None),
                GroupInvite.revoked_at.is_(None),
            )
        )
        .values(revoked_at=now)
        .execution_options(synchronize_session=False)
    )
    token = secrets.token_urlsafe(32)
    invite = GroupInvite(
        group_id=group.id,
        tenant_id=group.tenant_id,
        email=email,
        token_hash=hash_invite_token(token),
        invited_by_user_id=cpo_user.id,
        expires_at=now + timedelta(days=INVITE_TTL_DAYS),
    )
    db.add(invite)
    await db.commit()
    await db.refresh(invite)
    return invite, token


def invite_link(token: str) -> str:
    return f"{frontend_origin()}/groups?invite={token}"


def invite_email_body(group_name: str, link: str) -> Tuple[str, str]:
    """(subject, body). The group name is operator-typed text mailed to a
    third party, so it is flattened and capped like an operator note, and kept
    out of the Subject line (the first thing a phishing lure would use)."""
    name = _sanitize_operator_note(group_name) or "a private charger group"
    subject = "You're invited to a private charger group on AmpHive"
    body = "\n".join([
        "Hi,",
        "",
        f"You've been invited to join the private charger group \"{name}\" on AmpHive.",
        "",
        "Sign in (or create an account) with this email address, verify it,",
        "then open this link to join:",
        "",
        f"  {link}",
        "",
        f"The link works once and expires in {INVITE_TTL_DAYS} days.",
        "If you weren't expecting this, you can ignore this email.",
    ])
    return subject, body


async def _send(to_addr: str, subject: str, body: str) -> None:
    try:
        await asyncio.to_thread(send_email, to_addr, subject, body)
    except Exception:
        logger.exception("Group invite email failed")


def schedule_invite_email(to_addr: str, group_name: str, token: str) -> Optional[asyncio.Task]:
    """Fire-and-forget send (the request never waits on SMTP); never raises."""
    subject, body = invite_email_body(group_name, invite_link(token))
    try:
        task = asyncio.get_running_loop().create_task(_send(to_addr, subject, body))
    except RuntimeError:
        return None
    _pending.add(task)
    task.add_done_callback(_pending.discard)
    return task


async def accept_invite(db: AsyncSession, user: User, token: str) -> Tuple[GroupInvite, ChargerGroup, bool]:
    """Consume an invite for `user` and create their GroupMembership in the
    same transaction. Returns (invite, group, created) where created is False
    when the user was already a member (the invite is still consumed).

    Unknown, used, superseded and expired tokens share one generic 400. An
    unverified account or a different email gets a 403 and the invite stays
    usable; the invited address is never echoed back.
    """
    now = datetime.now(timezone.utc)
    invite = (
        await db.execute(
            select(GroupInvite).where(GroupInvite.token_hash == hash_invite_token(token))
        )
    ).scalar_one_or_none()
    if (
        invite is None
        or invite.accepted_at is not None
        or invite.revoked_at is not None
        or invite.expires_at <= now
    ):
        raise HTTPException(status_code=400, detail=INVALID_INVITE)
    if not user.email_verified:
        raise HTTPException(
            status_code=403,
            detail="Verify your email address first, then open the invite link again.",
        )
    if (user.email or "").strip().lower() != invite.email:
        raise HTTPException(
            status_code=403,
            detail="This invite was sent to a different email address. Sign in with that account.",
        )

    consumed = await db.execute(
        update(GroupInvite)
        .where(
            and_(
                GroupInvite.id == invite.id,
                GroupInvite.accepted_at.is_(None),
                GroupInvite.revoked_at.is_(None),
            )
        )
        .values(accepted_at=now, accepted_by_user_id=user.id)
        .execution_options(synchronize_session=False)
    )
    if not getattr(consumed, "rowcount", 0):
        raise HTTPException(status_code=400, detail=INVALID_INVITE)

    group = (
        await db.execute(select(ChargerGroup).where(ChargerGroup.id == invite.group_id))
    ).scalar_one()
    existing = (
        await db.execute(
            select(GroupMembership.id).where(
                and_(GroupMembership.user_id == user.id, GroupMembership.group_id == group.id)
            )
        )
    ).scalar_one_or_none()
    created = existing is None
    if created:
        db.add(GroupMembership(user_id=user.id, group_id=group.id))
    await db.commit()
    return invite, group, created
