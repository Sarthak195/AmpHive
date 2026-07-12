"""
CPO payout / settlement earnings math.

Manual settlement only: there is NO bank/UPI/payment-gateway integration
here (unlike the driver-side Razorpay top-up flow in services/payments.py).
A CPO requests a payout (routers/cpo.py POST /api/cpo/payouts), which
snapshots its currently-unsettled earnings into a Payout row (status
REQUESTED); the platform operator (admin) marks it PAID once the transfer
has happened out-of-band. This module is the pure earnings arithmetic shared
by the earnings-summary and payout-request endpoints so the two can never
disagree.

Earnings math
-------------
A tenant's gross earnings over a window = SUM(coins_spent) of that tenant's
COMPLETED charging sessions with ended_at in the window. ChargingSession.
tenant_id is already denormalized (set at session-start from the plug's
gateway's tenant at that time), so this is a single aggregate query — no
join and no per-plug looping (the N+1 shape this repo avoids elsewhere; see
cpo_analytics_overview/revenue/energy in routers/cpo.py, which use the same
direct tenant_id filter for tenant-scoped revenue). It is also the *more
correct* scoping than joining through the plug's CURRENT gateway/tenant: if
a plug is ever reassigned to a different gateway/tenant, a historical
session must still be credited to whoever operated the plug when the
session actually ran.

Platform fee = PLATFORM_FEE_PCT percent of gross (env, default 10.0),
money-rounded via to_money; net = gross - fee (not gross * (1 - pct/100)),
so fee + net always foot back to gross exactly.

Watermark
---------
A tenant's settlement watermark = MAX(period_end) over its non-CANCELLED
(REQUESTED or PAID) payouts, or the epoch if it has none. "Unsettled"
earnings always run watermark -> now: every non-cancelled payout advances
the watermark the instant it's REQUESTED (not only once PAID), so a second
request can't re-snapshot a window that's still pending admin settlement;
CANCELLED payouts are excluded, which frees their window for a future
request. See routers/cpo.py for how the request endpoint makes the
watermark-read + insert atomic per tenant.
"""
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional, Tuple

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.models import ChargingSession, Payout, PayoutStatus, SessionStatus
from backend.services.money import to_money

# A tenant with no payout history yet has watermark = EPOCH, so "unsettled"
# runs from the beginning of time to now (i.e. all of its lifetime earnings).
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

_DEFAULT_PLATFORM_FEE_PCT = Decimal("10.0")


def platform_fee_pct() -> Decimal:
    """The platform's cut, as a percent of gross (env `PLATFORM_FEE_PCT`,
    default 10.0). Falls back to the default on a missing/malformed env
    value rather than crashing every payout endpoint."""
    raw = os.getenv("PLATFORM_FEE_PCT")
    if raw is None or raw == "":
        return _DEFAULT_PLATFORM_FEE_PCT
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return _DEFAULT_PLATFORM_FEE_PCT


def compute_fee_and_net(gross: Decimal) -> Tuple[Decimal, Decimal]:
    """Split gross coins into (platform_fee, net) at the current fee rate.
    Each leg is money-rounded independently via to_money, and net is derived
    as gross - fee so the two numbers always foot to the cent."""
    gross = to_money(gross)
    fee = to_money(gross * platform_fee_pct() / Decimal("100"))
    net = to_money(gross - fee)
    return fee, net


async def sum_completed_session_coins(
    db: AsyncSession,
    tenant_id: int,
    window_start: Optional[datetime] = None,
    window_end: Optional[datetime] = None,
) -> Decimal:
    """SUM(coins_spent) for this tenant's COMPLETED sessions with ended_at in
    the half-open window [window_start, window_end). Either bound omitted
    means unbounded on that side (omit both for lifetime earnings). Single
    aggregate query — see the module docstring for why no join is needed."""
    conditions = [
        ChargingSession.tenant_id == tenant_id,
        ChargingSession.status == SessionStatus.COMPLETED,
    ]
    if window_start is not None:
        conditions.append(ChargingSession.ended_at >= window_start)
    if window_end is not None:
        conditions.append(ChargingSession.ended_at < window_end)

    result = await db.execute(
        select(func.coalesce(func.sum(ChargingSession.coins_spent), 0)).where(
            and_(*conditions)
        )
    )
    return to_money(result.scalar() or 0)


async def tenant_settlement_watermark(db: AsyncSession, tenant_id: int) -> datetime:
    """MAX(period_end) over this tenant's non-CANCELLED payouts, or EPOCH if
    it has none yet."""
    result = await db.execute(
        select(func.max(Payout.period_end)).where(
            and_(
                Payout.tenant_id == tenant_id,
                Payout.status != PayoutStatus.CANCELLED,
            )
        )
    )
    watermark = result.scalar()
    return watermark or EPOCH


async def tenant_earnings_summary(db: AsyncSession, tenant_id: int) -> dict:
    """Lifetime + unsettled (watermark -> now) earnings for the CPO earnings
    dashboard. Shared by GET /api/cpo/earnings and (for the unsettled leg)
    POST /api/cpo/payouts, so the two can never disagree."""
    now = datetime.now(timezone.utc)
    watermark = await tenant_settlement_watermark(db, tenant_id)

    lifetime_gross = await sum_completed_session_coins(db, tenant_id)
    lifetime_fee, lifetime_net = compute_fee_and_net(lifetime_gross)

    unsettled_gross = await sum_completed_session_coins(
        db, tenant_id, window_start=watermark, window_end=now
    )
    unsettled_fee, unsettled_net = compute_fee_and_net(unsettled_gross)

    return {
        "watermark": watermark,
        "now": now,
        "lifetime_gross_coins": lifetime_gross,
        "lifetime_platform_fee_coins": lifetime_fee,
        "lifetime_net_coins": lifetime_net,
        "unsettled_gross_coins": unsettled_gross,
        "unsettled_platform_fee_coins": unsettled_fee,
        "unsettled_net_coins": unsettled_net,
    }
