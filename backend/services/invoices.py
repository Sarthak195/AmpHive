"""
GST tax invoice issuance for a finished, billed ChargingSession.

India intra-state GST only: a single combined rate (env `GST_RATE_PCT`,
default 18.0) is charged INCLUSIVE in the coins the driver already paid (1
coin = ₹1) — the driver's wallet is never debited a second time to cover
tax. CGST/SGST (intra-state) vs. IGST (inter-state) splitting is explicitly
OUT OF SCOPE: this app has no way to verify which state the driver and the
CPO's plug are each in, so it cannot determine which pair applies. The
combined-rate taxable-value/GST-amount split computed here is still the
legally correct total, just not itemized into its two intra-state halves on
the printed invoice line — a known, deliberate limitation, not a bug.

Money flow recap (see services/money.py and services/session_lifecycle.py):
a session's `coins_spent` is already the final GST-inclusive amount actually
collected from the driver's wallet at stop time. Issuing an invoice never
moves money — it only produces the legally-required paper trail for a debit
that already happened.

See the `Invoice` model (backend/database/models.py) for the full schema
rationale (immutable seller/line snapshots, one-invoice-per-session,
sequential per-tenant numbering).
"""
import html
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional, Tuple

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.models import (
    ChargingSession,
    Invoice,
    SessionStatus,
    Tenant,
    User,
)
from backend.services.money import ZERO_MONEY, to_money
from backend.services.pricing import default_rate

logger = logging.getLogger("amphive.api")

_DEFAULT_GST_RATE_PCT = Decimal("18.0")

# Session statuses that count as "finished" for billing/invoicing purposes —
# matches the ChargingSession.status.in_([COMPLETED, PAID]) convention
# already used throughout routers/cpo.py's revenue/energy analytics.
_INVOICEABLE_STATUSES = (SessionStatus.COMPLETED, SessionStatus.PAID)


class SessionNotInvoiceableError(Exception):
    """Raised when `session_id` isn't eligible for an invoice: it doesn't
    exist, hasn't finished (COMPLETED/PAID), or billed nothing
    (coins_spent <= 0). Routers map this to HTTP 404/400."""


def gst_rate_pct() -> Decimal:
    """The GST rate applied to charging sessions, as a percent (env
    `GST_RATE_PCT`, default 18.0). Falls back to the default on a
    missing/malformed env value rather than crashing invoice issuance — same
    contract as services/payouts.py platform_fee_pct()."""
    raw = os.getenv("GST_RATE_PCT")
    if raw is None or raw == "":
        return _DEFAULT_GST_RATE_PCT
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return _DEFAULT_GST_RATE_PCT


def split_gst_inclusive(total: Decimal, rate_pct: Optional[Decimal] = None) -> Tuple[Decimal, Decimal]:
    """Split a GST-INCLUSIVE `total` into (taxable_value, gst_amount) at
    `rate_pct` percent (default: the current gst_rate_pct() env reading).

    taxable_value = total / (1 + rate/100); gst_amount is DERIVED as
    total - taxable_value (not independently rounded), so the two always
    foot back to `total` to the cent — the same technique
    services/payouts.py compute_fee_and_net uses for fee/net.
    """
    total = to_money(total)
    rate = to_money(rate_pct if rate_pct is not None else gst_rate_pct())
    divisor = Decimal("1") + (rate / Decimal("100"))
    taxable_value = to_money(total / divisor)
    gst_amount = to_money(total - taxable_value)
    return taxable_value, gst_amount


def _financial_year_label(at: datetime) -> str:
    """Indian financial year label (Apr 1 -> Mar 31), e.g. "2026-27" for any
    date from 2026-04-01 through 2027-03-31."""
    start_year = at.year if at.month >= 4 else at.year - 1
    return f"{start_year}-{str(start_year + 1)[-2:]}"


async def _get_invoice_by_session(db: AsyncSession, session_id: int) -> Optional[Invoice]:
    result = await db.execute(select(Invoice).where(Invoice.session_id == session_id))
    return result.scalar_one_or_none()


async def issue_invoice_for_session(db: AsyncSession, session_id: int) -> Invoice:
    """
    Issue (or, on any later call, simply fetch) the GST invoice for
    `session_id`. Idempotent — a session may only ever have one invoice.

    The common repeat-call path is the fast pre-check SELECT below; the
    actual safety net is `invoices.session_id`'s UNIQUE constraint plus the
    IntegrityError catch further down, so two concurrent *first* issues for
    the same session (both pre-checks racing and both missing) still can
    never mint two invoices.

    Sequential numbering: invoice_number is allocated under a
    `SELECT ... FOR UPDATE` on the tenant row, reading/writing
    Tenant.next_invoice_seq as plain columns rather than mutating a mapped
    entity — the same column-level lock-then-update shape
    services/wallet.py's debit_wallet_clamped uses to sidestep identity-map
    staleness (see that module's docstring). This serializes ALL concurrent
    issues for one tenant (not just same-session racers), so two different
    sessions invoiced at the same instant still get distinct sequential
    numbers.

    Raises SessionNotInvoiceableError if the session doesn't exist, hasn't
    finished (COMPLETED/PAID), or billed nothing (coins_spent <= 0).
    """
    existing = await _get_invoice_by_session(db, session_id)
    if existing is not None:
        return existing

    session_result = await db.execute(
        select(ChargingSession).where(ChargingSession.id == session_id)
    )
    session = session_result.scalar_one_or_none()
    if session is None:
        raise SessionNotInvoiceableError(f"Session {session_id} not found.")
    if session.status not in _INVOICEABLE_STATUSES:
        raise SessionNotInvoiceableError(
            f"Session {session_id} is not finished (status={session.status.value})."
        )
    total = to_money(session.coins_spent or ZERO_MONEY)
    if total <= ZERO_MONEY:
        raise SessionNotInvoiceableError(
            f"Session {session_id} billed nothing (coins_spent={total}); nothing to invoice."
        )

    rate = gst_rate_pct()
    taxable_value, gst_amount = split_gst_inclusive(total, rate)

    # Lock the tenant row and read the seller snapshot + numbering counter as
    # plain columns (not a mapped Tenant entity — see docstring above).
    tenant_row = (
        await db.execute(
            select(
                Tenant.next_invoice_seq, Tenant.invoice_prefix,
                Tenant.legal_name, Tenant.gstin,
            )
            .where(Tenant.id == session.tenant_id)
            .with_for_update()
        )
    ).one_or_none()
    if tenant_row is None:
        raise SessionNotInvoiceableError(f"Tenant {session.tenant_id} not found.")
    seq, prefix, legal_name, gstin = tenant_row

    now = datetime.now(timezone.utc)
    # invoice_number is GLOBALLY unique (one numbering namespace across all
    # tenants), but `seq` only resets/counts per TENANT -- so if the
    # fallback prefix were a bare constant ("INV"), two different tenants
    # that both leave invoice_prefix unset would produce the exact same
    # "INV-2026-27-00001" the first time each ever issues an invoice in a
    # given financial year (a likely collision across any two unconfigured
    # tenants, not just a rare race). Folding the tenant id into the
    # fallback keeps every UNCONFIGURED tenant's numbering namespace
    # disjoint by construction. A CPO who explicitly configures a custom
    # invoice_prefix that happens to collide with another tenant's is a
    # configuration conflict between two tenants, not something this
    # function can prevent; that (rare, self-inflicted) case surfaces as an
    # IntegrityError the same-session-race recovery below can't resolve
    # (there's no session_id collision to find the "winner" of) and
    # propagates as a 500 rather than silently misnumbering.
    prefix = prefix or f"INV{session.tenant_id}"
    invoice_number = f"{prefix}-{_financial_year_label(now)}-{seq:05d}"

    await db.execute(
        update(Tenant)
        .where(Tenant.id == session.tenant_id)
        .values(next_invoice_seq=Tenant.next_invoice_seq + 1)
        .execution_options(synchronize_session=False)
    )

    session_rate = (
        to_money(session.rate_coins_per_kwh)
        if session.rate_coins_per_kwh is not None
        else default_rate()
    )

    invoice = Invoice(
        tenant_id=session.tenant_id,
        session_id=session.id,
        driver_user_id=session.user_id,
        invoice_number=invoice_number,
        issued_at=now,
        amount_coins=total,
        taxable_value_inr=taxable_value,
        gst_rate_pct=to_money(rate),
        gst_amount_inr=gst_amount,
        total_inr=total,
        seller_legal_name=legal_name,
        seller_gstin=gstin,
        energy_kwh=session.energy_kwh or 0.0,
        rate_coins_per_kwh=session_rate,
    )
    db.add(invoice)
    try:
        await db.commit()
    except IntegrityError:
        # Lost the race to a concurrent first-issue for this same session --
        # rollback undoes BOTH the failed insert and the seq bump together
        # (still uncommitted), so no invoice_number is burned. Return the
        # winner's row.
        await db.rollback()
        winner = await _get_invoice_by_session(db, session_id)
        if winner is not None:
            return winner
        raise
    await db.refresh(invoice)

    logger.info(
        "Invoice issued",
        extra={
            "invoice_id": invoice.id, "invoice_number": invoice.invoice_number,
            "session_id": session_id, "tenant_id": session.tenant_id,
            "total_inr": float(total),
        },
    )
    return invoice


def invoice_to_dict(invoice: Invoice) -> dict:
    """JSON-serializable shape for GET /api/sessions/{id}/invoice and
    GET /api/cpo/invoices."""
    return {
        "id": invoice.id,
        "invoice_number": invoice.invoice_number,
        "tenant_id": invoice.tenant_id,
        "session_id": invoice.session_id,
        "driver_user_id": invoice.driver_user_id,
        "issued_at": invoice.issued_at.isoformat() if invoice.issued_at else None,
        "seller": {
            "legal_name": invoice.seller_legal_name,
            "gstin": invoice.seller_gstin,
        },
        "line_item": {
            "description": f"EV charging session #{invoice.session_id}",
            "energy_kwh": round(invoice.energy_kwh, 3),
            "rate_coins_per_kwh": float(invoice.rate_coins_per_kwh),
        },
        "amount_coins": float(invoice.amount_coins),
        "taxable_value_inr": float(invoice.taxable_value_inr),
        "gst_rate_pct": float(invoice.gst_rate_pct),
        "gst_amount_inr": float(invoice.gst_amount_inr),
        "total_inr": float(invoice.total_inr),
        "currency": "INR",
    }


async def render_invoice_html(db: AsyncSession, invoice: Invoice) -> str:
    """Minimal printable HTML rendering of `invoice` (inline CSS, no PDF
    dependency — meant to be saved/printed via the browser's own print
    dialog). Looks up the driver's display name/email live (not snapshotted
    on Invoice — only the SELLER identity is legally frozen; see the model
    docstring), so this needs `db`.

    User-controlled strings (tenant legal name/GSTIN, driver name/email) are
    HTML-escaped before interpolation — a CPO or driver profile field is not
    a trusted string just because it's our own data.
    """
    driver_result = await db.execute(select(User).where(User.id == invoice.driver_user_id))
    driver = driver_result.scalar_one_or_none()
    driver_name = html.escape(driver.full_name) if driver else "Driver"
    driver_email = html.escape(driver.email) if driver else ""

    seller_name = html.escape(invoice.seller_legal_name) if invoice.seller_legal_name else "(legal name not configured)"
    seller_gstin = html.escape(invoice.seller_gstin) if invoice.seller_gstin else "(GSTIN not configured)"
    invoice_number = html.escape(invoice.invoice_number)
    issued_at = invoice.issued_at.strftime("%d %b %Y, %H:%M UTC") if invoice.issued_at else ""

    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Invoice {invoice_number}</title>
<style>
  body {{ font-family: Arial, Helvetica, sans-serif; color: #1a1a1a; max-width: 720px;
         margin: 40px auto; padding: 0 20px; }}
  h1 {{ font-size: 20px; border-bottom: 2px solid #333; padding-bottom: 8px; }}
  .meta {{ display: flex; justify-content: space-between; margin: 16px 0; font-size: 14px; }}
  .meta .right {{ text-align: right; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 16px; }}
  th, td {{ border: 1px solid #ccc; padding: 8px; text-align: left; font-size: 13px; }}
  th {{ background: #f2f2f2; }}
  .totals {{ margin-top: 8px; width: auto; margin-left: auto; }}
  .totals td {{ border: none; padding: 3px 8px; font-size: 13px; }}
  .totals .label {{ text-align: right; color: #444; }}
  .totals .grand td {{ font-weight: bold; border-top: 1px solid #999; padding-top: 6px; }}
  .footer {{ margin-top: 28px; font-size: 11px; color: #666; line-height: 1.5; }}
  @media print {{ body {{ margin: 0; }} }}
</style>
</head>
<body>
  <h1>Tax Invoice</h1>
  <div class="meta">
    <div>
      <strong>{seller_name}</strong><br>
      GSTIN: {seller_gstin}
    </div>
    <div class="right">
      Invoice No: <strong>{invoice_number}</strong><br>
      Date: {issued_at}
    </div>
  </div>
  <div>Bill To: {driver_name} ({driver_email})</div>
  <table>
    <thead>
      <tr>
        <th>Description</th>
        <th>Energy (kWh)</th>
        <th>Rate (coins/kWh)</th>
        <th>Taxable Value (Rs.)</th>
        <th>GST ({invoice.gst_rate_pct:.2f}%) (Rs.)</th>
        <th>Total (Rs.)</th>
      </tr>
    </thead>
    <tbody>
      <tr>
        <td>EV charging session #{invoice.session_id}</td>
        <td>{invoice.energy_kwh:.3f}</td>
        <td>{invoice.rate_coins_per_kwh:.2f}</td>
        <td>{invoice.taxable_value_inr:.2f}</td>
        <td>{invoice.gst_amount_inr:.2f}</td>
        <td>{invoice.total_inr:.2f}</td>
      </tr>
    </tbody>
  </table>
  <table class="totals">
    <tr><td class="label">Taxable Value:</td><td>Rs. {invoice.taxable_value_inr:.2f}</td></tr>
    <tr><td class="label">GST ({invoice.gst_rate_pct:.2f}%):</td><td>Rs. {invoice.gst_amount_inr:.2f}</td></tr>
    <tr class="grand"><td class="label">Total:</td><td>Rs. {invoice.total_inr:.2f}</td></tr>
  </table>
  <div class="footer">
    1 coin = Rs. 1. Amounts above are GST-inclusive of the total already
    collected from the driver's wallet at session end -- issuing this
    invoice does not charge anything further. This invoice reflects a
    single intra-state GST rate; CGST/SGST vs. IGST is not itemized
    separately. Generated by AmpHive.
  </div>
</body>
</html>
"""
