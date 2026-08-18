"""
Transactional billing email: charging-session bills and credit top-up
receipts, sent to the driver's account email.

Design constraints:

- Fire-and-forget. Billing paths (finalize_charging_session, the CPO top-up
  route) must never wait on SMTP — smtplib's timeout is 15 s. Callers use
  schedule(), which wraps the coroutine in asyncio.create_task and keeps a
  strong reference until done (bare create_task results can be GC'd
  mid-flight).
- Never raises into the caller. Address lookup and delivery problems are
  logged and swallowed (services/email.send_email already swallows SMTP
  failures); seeded `.test` addresses simply bounce at the provider.
- Env kill-switch: BILLING_EMAILS=off|0|false|disabled silences everything
  (default on). Distinct from SMTP_HOST being unset — that falls back to
  services/email's console logging, which would spam WARNING per session.
- Copy says "charging credit", never "wallet" — the closed-loop reframe
  (see /terms) is load-bearing for the payment-provider application.
"""
import asyncio
import logging
import os
from typing import Optional, Set

from sqlalchemy import select

from backend.services.email import frontend_origin, send_email

logger = logging.getLogger("amphive.email")

# Strong refs for in-flight sends; discarded on completion.
_pending: Set[asyncio.Task] = set()


def enabled() -> bool:
    return os.getenv("BILLING_EMAILS", "on").strip().lower() not in (
        "off", "0", "false", "disabled",
    )


def schedule(coro) -> Optional[asyncio.Task]:
    """Run a billing-email coroutine in the background (or drop it when the
    feature is off / no loop is running, e.g. sync test contexts)."""
    if not enabled():
        coro.close()
        return None
    try:
        task = asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        coro.close()
        return None
    _pending.add(task)
    task.add_done_callback(_pending.discard)
    return task


def _fmt_duration(duration_sec: Optional[int]) -> str:
    minutes = int(duration_sec or 0) // 60
    if minutes >= 60:
        return f"{minutes // 60} h {minutes % 60} min"
    return f"{minutes} min"


async def send_session_bill(user_id: int, receipt: dict) -> None:
    """Email the driver their bill for a finalized charging session.

    `receipt` is finalize_charging_session's stop-response payload — the
    single source the app itself shows the driver, so the email can never
    disagree with the in-app receipt. Opens its own DB session for the
    address lookup (this runs detached from the caller's transaction).
    """
    try:
        from backend.database.db import async_session_factory
        from backend.database.models import User

        async with async_session_factory() as db:
            row = (await db.execute(
                select(User.email, User.full_name).where(User.id == user_id)
            )).first()
        if not row or not row[0]:
            return
        to_addr, full_name = row[0], row[1]

        energy = receipt.get("energy_kwh") or 0.0
        coins = receipt.get("coins_spent") or 0.0
        balance = receipt.get("balance_remaining")
        rate = receipt.get("price_per_kwh")
        plug_name = receipt.get("plug_name") or f"plug {receipt.get('plug_id')}"

        lines = [
            f"Hi {full_name or 'there'},",
            "",
            "Here's your bill for the charging session that just ended.",
            "",
            f"  Charger:        {plug_name}",
            f"  Energy:         {energy:.3f} kWh",
            f"  Duration:       {_fmt_duration(receipt.get('duration_sec'))}",
        ]
        if rate:
            lines.append(f"  Rate:           {rate:g} coins/kWh")
        lines.append(f"  Amount billed:  {coins:.2f} coins")
        if balance is not None:
            lines.append(f"  Credit left:    {balance:.2f} coins")
        if receipt.get("started_at"):
            lines.append(f"  Started:        {receipt['started_at']}")
        if receipt.get("ended_at"):
            lines.append(f"  Ended:          {receipt['ended_at']}")
        lines += [
            "",
            f"Session #{receipt.get('session_id')} — full history and GST "
            f"invoices: {frontend_origin()}/activity",
            "",
            "Thanks for charging with AmpHive.",
        ]

        await asyncio.to_thread(
            send_email,
            to_addr,
            f"AmpHive charging bill — {coins:.2f} coins ({energy:.3f} kWh)",
            "\n".join(lines),
        )
    except Exception:
        # Best-effort by contract: a billing email must never surface an
        # error into (or after) the billing path.
        logger.exception(
            "Session bill email failed", extra={"user_id": user_id}
        )


# An offline-top-up note is free text typed by a CPO and delivered to a
# DRIVER's inbox from the AmpHive sender. Flattened to one line with control
# characters stripped so an operator cannot compose multi-line content that
# reads like part of the receipt (or like a separate message from us), and
# capped well under the column's 500-char limit.
_MAX_NOTE_CHARS = 200


def _sanitize_operator_note(note: str) -> str:
    """One line, printable characters only, length-capped."""
    flat = " ".join(str(note).split())
    flat = "".join(ch for ch in flat if ch.isprintable())
    if len(flat) > _MAX_NOTE_CHARS:
        flat = flat[:_MAX_NOTE_CHARS].rstrip() + "\u2026"
    return flat


async def send_topup_receipt(
    to_addr: str,
    full_name: Optional[str],
    amount_coins: float,
    new_balance: float,
    credited_by: str,
    note: Optional[str] = None,
) -> None:
    """Email the driver a receipt for a cash top-up a CPO just credited."""
    try:
        lines = [
            f"Hi {full_name or 'there'},",
            "",
            f"{credited_by} added {amount_coins:.2f} coins of charging "
            "credit to your AmpHive account (cash payment).",
            "",
            f"  Amount:      {amount_coins:.2f} coins",
            f"  New balance: {new_balance:.2f} coins",
        ]
        safe_note = _sanitize_operator_note(note) if note else ""
        if safe_note:
            # Labelled as the operator's words, not ours.
            lines.append(f"  Operator note: {safe_note}")
        lines += [
            "",
            f"View your credit and history: {frontend_origin()}/credit",
            "",
            "Thanks for charging with AmpHive.",
        ]

        await asyncio.to_thread(
            send_email,
            to_addr,
            f"AmpHive top-up receipt — {amount_coins:.2f} coins",
            "\n".join(lines),
        )
    except Exception:
        logger.exception(
            "Top-up receipt email failed", extra={"to": to_addr}
        )
