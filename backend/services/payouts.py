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
FINISHED charging sessions (ChargingSession.status COMPLETED or PAID — the
same two-status definition of "finished revenue" already used by
routers/admin.py's _REVENUE_STATUSES, routers/cpo/_analytics.py, and
services/invoices.py's _INVOICEABLE_STATUSES; this module used to filter on
COMPLETED alone, which would have silently under-counted gross the moment
anything transitions a session to PAID) with ended_at in the window. Note
this SessionStatus.PAID is unrelated to Payout.status PAID a few paragraphs
below — one says "this charging session's invoice is settled", the other
says "this CPO's payout batch was wired". ChargingSession.tenant_id is
already denormalized (set at session-start from the plug's
gateway's tenant at that time), so this is a single aggregate query — no
join and no per-plug looping (the N+1 shape this repo avoids elsewhere; see
cpo_analytics_overview/revenue/energy in routers/cpo.py, which use the same
direct tenant_id filter for tenant-scoped revenue). It is also the *more
correct* scoping than joining through the plug's CURRENT gateway/tenant: if
a plug is ever reassigned to a different gateway/tenant, a historical
session must still be credited to whoever operated the plug when the
session actually ran. `sum_completed_session_coins` (windowed by ended_at)
remains the shared aggregate for LIFETIME totals (window omitted = unbounded)
and any other ended_at-windowed caller, but a tenant's UNSETTLED gross for a
payout is no longer computed this way — see "Settlement marking (gross)"
below for the row-ownership mechanism that replaced it.

Refunds (session disputes) net out of this same gross:
`sum_completed_session_coins` also subtracts the SUM(refund_coins) of every
APPROVED SessionDispute for this tenant whose resolved_at falls in the same
[window_start, window_end) (see `sum_approved_refund_coins`, which joins on
the same COMPLETED-or-PAID status set as the gross query above, so a
session's refund still nets out no matter which of the two finished
statuses it currently carries). The refund is
windowed by the dispute's own *resolved_at* — deliberately NOT by the
session's ended_at — because a dispute can be approved long after its
session was already settled into a REQUESTED/PAID payout. Keying the refund
to when it was resolved lands it in the CURRENT unsettled window, so it
claws back off the CPO's next payout/pool. Keying it to the session's
ended_at (the obvious mirror of the gross scoping) reopens the drain: once
the session's ended_at is behind the watermark — which happens the moment
any payout covering it is requested — a later refund would fall before every
future window and never be subtracted, letting a CPO get paid in full and
then approve the refund for free. `cpo_resolve_dispute`
(routers/cpo/_disputes.py) credits the driver's wallet and stamps
SessionDispute.refund_coins/resolved_at/status but deliberately never
touches ChargingSession.coins_spent (that column stays the driver's original
receipt/invoice) — so without this subtraction here, a refunded session
would silently keep paying the CPO out on coins it no longer collected. A
refund resolved in a window with too little fresh gross to absorb it clamps
the window to zero (below) and, because no payout is created when the payable
is non-positive (routers/cpo/_payouts.py), the watermark does not advance —
so the un-absorbed refund keeps suppressing payouts until later earnings
cover it (carry-forward, never over-paid).

Platform fee = PLATFORM_FEE_PCT percent of gross (env, default 10.0),
money-rounded via to_money; net = gross - fee (not gross * (1 - pct/100)),
so fee + net always foot back to gross exactly.

Watermark
---------
A tenant's settlement watermark = MAX(period_end) over its non-CANCELLED
(REQUESTED or PAID) payouts, or the epoch if it has none. Every non-cancelled
payout advances the watermark the instant it's REQUESTED (not only once
PAID), so a second request can't re-snapshot a window that's still pending
admin settlement; CANCELLED payouts are excluded, which frees their window
for a future request. See routers/cpo.py for how the request endpoint makes
the watermark-read + insert atomic per tenant. As of settlement-marking
(below), the watermark no longer decides which SESSIONS are unsettled for
GROSS — that's ``ChargingSession.settled_payout_id IS NULL`` now — but it is
still exactly what it always was for the REFUND clawback window and the
OFFLINE TOP-UP window (both below), and it still seeds each new payout's
``period_start``/``period_end`` display bounds.

Settlement marking (gross) — replaces the old watermark race
--------------------------------------------------------------
Gross is no longer "SUM(coins_spent) of finished sessions with ended_at in
[watermark, now)". Instead, ``ChargingSession.settled_payout_id`` (Alembic
revision 0028_payout_settlement_marking) is the row's OWN settlement state:
NULL means unsettled/eligible for the next payout, no matter what its
ended_at is or when a watermark last moved. `mark_unsettled_sessions_and_
sum_gross` claims every one of a tenant's unsettled FINISHED sessions for a
brand-new payout id and sums their coins_spent in a SINGLE
``UPDATE ... RETURNING`` statement — the read (which sessions are unsettled)
and the write (mark them settled) are the same statement, so they can never
disagree the way a separate SELECT-then-mark could under a concurrent
commit. `sum_unsettled_session_coins` is the read-only twin (SELECT SUM, no
mutation) used by the earnings preview, so merely looking at the dashboard
can never claim a session out from under a future payout.

This closes what used to be documented here as the "watermark race (known
limitation)": `finalize_charging_session` (services/session_lifecycle.py)
stamps ``ChargingSession.ended_at`` immediately before it commits, and a
payout snapshot landing in the tiny window between that commit starting and
becoming visible could previously miss the session yet still advance the
watermark past its ended_at, dropping those earnings from every future
payout permanently. Under settlement marking there is no such thing as
"behind the watermark" for gross: a session that commits a heartbeat after
one payout's claiming UPDATE ran is simply still NULL, and gets claimed by
the NEXT payout instead of never. (An earlier attempt at a fix floored the
window at ``now - lag`` for gross, top-ups AND refunds; it was reverted
because a single shared watermark cannot lag the gross window without also
delaying — or double-counting — top-ups/refunds, which must settle
immediately for the top-up pool guard in routers/cpo/_topups.py to stay
correct. Settlement marking sidesteps that entirely by not touching the
refund/top-up windowing at all — those stay exactly as they were.)

`cpo_cancel_payout` (routers/cpo/_payouts.py) resets every session it had
claimed (``settled_payout_id == payout.id``) back to NULL before it commits
the CANCELLED status, so a cancelled payout's earnings aren't stranded —
they're simply claimable again by the next request, same as the watermark's
CANCELLED-payout exclusion always did for the window it used to define.

Legacy data: payouts created BEFORE settlement marking never claimed their
sessions, so on upgrade every historic already-paid-out session would have
counted as unsettled and been re-paid by the very next request. Migration
0028_payout_settlement_marking therefore backfills the old scheme's
attribution once: each FINISHED session whose ended_at falls inside a
non-CANCELLED payout's [period_start, period_end) window gets stamped with
that payout's id (see the migration's docstring for the full rationale,
including why race-lost sessions stay lost retroactively).

Offline top-up pool
--------------------
A CPO can also credit a driver's wallet directly for cash collected offline
(routers/cpo/_topups.py POST /api/cpo/topups) — funded from the SAME
unsettled net earnings, never from thin air. `tenant_earnings_summary`'s
``available_pool_coins`` is therefore ``unsettled_net_coins`` minus every
OfflineTopup issued since the same watermark (clamped at zero), and
``cpo_request_payout`` (routers/cpo/_payouts.py) pays out that reduced figure
— not the raw unsettled net — so a CPO can never draw the same earnings out
twice: once as a cash top-up, once as a bank/UPI payout. Since settlement
marking, `cpo_request_payout` no longer calls `tenant_earnings_summary`
directly (its gross must come from the claiming `mark_unsettled_sessions_
and_sum_gross`, not the read-only preview), but it still calls the exact
same `sum_offline_topups` helper over the exact same
[watermark, now) window `tenant_earnings_summary` uses, so the dashboard's
``topup_pool.available_coins`` and a payout's actual payable net still can
never disagree (same principle as the gross/fee/net split above).
"""
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional, Tuple

from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.models import (
    ChargingSession,
    DisputeStatus,
    OfflineTopup,
    Payout,
    PayoutStatus,
    SessionDispute,
    SessionStatus,
)
from backend.services.money import ZERO_MONEY, to_money

# A tenant with no payout history yet has watermark = EPOCH, so "unsettled"
# runs from the beginning of time to now (i.e. all of its lifetime earnings).
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# ChargingSession statuses that count as "finished revenue" for the payout
# ledger — kept in lockstep with routers/admin.py's _REVENUE_STATUSES,
# routers/cpo/_analytics.py, and services/invoices.py's
# _INVOICEABLE_STATUSES (see the module docstring's "Earnings math" section).
FINISHED_SESSION_STATUSES = (SessionStatus.COMPLETED, SessionStatus.PAID)

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


async def sum_approved_refund_coins(
    db: AsyncSession,
    tenant_id: int,
    window_start: Optional[datetime] = None,
    window_end: Optional[datetime] = None,
) -> Decimal:
    """SUM(refund_coins) of APPROVED SessionDisputes for this tenant whose
    resolved_at is in the half-open window [window_start, window_end). The
    window is keyed off the dispute's OWN resolved_at — not the session's
    ended_at — so a refund approved after its session was already settled
    still claws back off the current unsettled window instead of vanishing
    before the watermark (see the module docstring's "Refunds" paragraph for
    the full rationale and the pay-then-refund drain it closes). Joins
    ChargingSession only to scope by tenant/status (COMPLETED or PAID — see
    FINISHED_SESSION_STATUSES), since refund_coins and resolved_at both live
    on SessionDispute."""
    conditions = [
        ChargingSession.tenant_id == tenant_id,
        ChargingSession.status.in_(FINISHED_SESSION_STATUSES),
        SessionDispute.status == DisputeStatus.APPROVED,
    ]
    if window_start is not None:
        conditions.append(SessionDispute.resolved_at >= window_start)
    if window_end is not None:
        conditions.append(SessionDispute.resolved_at < window_end)

    result = await db.execute(
        select(func.coalesce(func.sum(SessionDispute.refund_coins), 0))
        .select_from(SessionDispute)
        .join(ChargingSession, ChargingSession.id == SessionDispute.session_id)
        .where(and_(*conditions))
    )
    return to_money(result.scalar() or 0)


async def sum_completed_session_coins(
    db: AsyncSession,
    tenant_id: int,
    window_start: Optional[datetime] = None,
    window_end: Optional[datetime] = None,
) -> Decimal:
    """SUM(coins_spent) for this tenant's FINISHED sessions (status COMPLETED
    or PAID — see FINISHED_SESSION_STATUSES and the module docstring's
    "Earnings math" section) with ended_at in the half-open window
    [window_start, window_end), minus the SUM of any APPROVED
    SessionDispute.refund_coins against those same sessions (see
    `sum_approved_refund_coins` and the module docstring's "Refunds"
    paragraph) — clamped at zero so a data inconsistency can't produce a
    negative gross. Either bound omitted means unbounded on that side (omit
    both for lifetime earnings). This is the single shared aggregate both
    the earnings dashboard (gross/fee/net) and the offline top-up pool read,
    so a dispute refund is reflected everywhere coins_spent is, without
    mutating coins_spent itself (that stays the driver's original
    receipt/invoice). The name predates PAID being included here — kept
    as-is since it's the shared public entry point every payout/earnings
    caller already imports."""
    conditions = [
        ChargingSession.tenant_id == tenant_id,
        ChargingSession.status.in_(FINISHED_SESSION_STATUSES),
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
    gross = to_money(result.scalar() or 0)

    refunds = await sum_approved_refund_coins(db, tenant_id, window_start, window_end)
    return to_money(max(gross - refunds, ZERO_MONEY))


async def sum_unsettled_session_coins(db: AsyncSession, tenant_id: int) -> Decimal:
    """Read-only preview of what `mark_unsettled_sessions_and_sum_gross` would
    claim right now — SELECT SUM instead of UPDATE ... RETURNING, so GET
    /api/cpo/earnings can show the unsettled figure without ever claiming a
    session out from under a future payout request (display must never mutate
    settlement state). Same predicate as the claiming path: FINISHED_SESSION_
    STATUSES sessions with settled_payout_id IS NULL (see the module
    docstring's "Settlement marking (gross)" section) — no ended_at bound at
    all, since a session's eligibility no longer depends on timing. The
    approved-refund subtraction still runs over the tenant's current
    [watermark, now) window, exactly as it always has (see "Refunds" and
    "Watermark" in the module docstring) — that windowing is unchanged by
    settlement marking."""
    watermark = await tenant_settlement_watermark(db, tenant_id)
    now = datetime.now(timezone.utc)

    result = await db.execute(
        select(func.coalesce(func.sum(ChargingSession.coins_spent), 0)).where(
            and_(
                ChargingSession.tenant_id == tenant_id,
                ChargingSession.status.in_(FINISHED_SESSION_STATUSES),
                ChargingSession.settled_payout_id.is_(None),
            )
        )
    )
    gross = to_money(result.scalar() or 0)

    refunds = await sum_approved_refund_coins(db, tenant_id, window_start=watermark, window_end=now)
    return to_money(max(gross - refunds, ZERO_MONEY))


async def mark_unsettled_sessions_and_sum_gross(
    db: AsyncSession,
    tenant_id: int,
    payout_id: int,
    refund_window_start: datetime,
    refund_window_end: datetime,
) -> Decimal:
    """Atomically claim every one of this tenant's unsettled FINISHED
    sessions for `payout_id` and return their post-refund gross.

    The claim and the read are the SAME statement — a single
    ``UPDATE charging_sessions ... WHERE settled_payout_id IS NULL
    RETURNING coins_spent`` — so they can never disagree the way a separate
    "SELECT which sessions are unsettled" followed by "UPDATE those ids"
    could under a concurrent `finalize_charging_session` commit: whatever
    this one UPDATE's WHERE clause matches (still NULL at the instant
    Postgres evaluates each row) is claimed for THIS payout; anything that
    commits its own settled_payout_id-eligible row a moment later is simply
    left NULL, to be claimed by the NEXT payout instead of dropped forever.
    See the module docstring's "Settlement marking (gross)" section for the
    watermark race this replaces.

    Refund windowing is UNCHANGED and out of scope for this fix: the
    approved-dispute-refund subtraction below still runs over the tenant's
    [refund_window_start, refund_window_end) window — the caller's
    watermark -> now, i.e. the new payout's own period bounds — via
    `sum_approved_refund_coins`, exactly as the old ended_at-windowed gross
    path did (see "Refunds" in the module docstring for why that clawback
    must stay keyed to the dispute's own resolved_at, not to when a session
    gets claimed). The window MUST be passed in as the watermark the caller
    read BEFORE inserting/flushing the new Payout row: recomputing
    `tenant_settlement_watermark` here would see that flushed row inside the
    same transaction and return the new payout's OWN period_end — an almost
    empty refund window that silently skips every pending refund and
    over-pays the CPO.
    """
    result = await db.execute(
        update(ChargingSession)
        .where(
            and_(
                ChargingSession.tenant_id == tenant_id,
                ChargingSession.status.in_(FINISHED_SESSION_STATUSES),
                ChargingSession.settled_payout_id.is_(None),
            )
        )
        .values(settled_payout_id=payout_id)
        .returning(ChargingSession.coins_spent)
    )
    gross = to_money(sum(result.scalars().all(), ZERO_MONEY))

    refunds = await sum_approved_refund_coins(
        db, tenant_id, window_start=refund_window_start, window_end=refund_window_end
    )
    return to_money(max(gross - refunds, ZERO_MONEY))


async def sum_offline_topups(
    db: AsyncSession,
    tenant_id: int,
    window_start: Optional[datetime] = None,
    window_end: Optional[datetime] = None,
) -> Decimal:
    """SUM(amount_coins) of this tenant's OfflineTopup rows with created_at in
    the half-open window [window_start, window_end). Same shape as
    sum_completed_session_coins above — single aggregate query, no join
    (OfflineTopup.tenant_id is a direct column, not derived)."""
    conditions = [OfflineTopup.tenant_id == tenant_id]
    if window_start is not None:
        conditions.append(OfflineTopup.created_at >= window_start)
    if window_end is not None:
        conditions.append(OfflineTopup.created_at < window_end)

    result = await db.execute(
        select(func.coalesce(func.sum(OfflineTopup.amount_coins), 0)).where(
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
    """Lifetime + unsettled earnings for the CPO earnings dashboard
    (read-only — never claims a session; see `sum_unsettled_session_coins`).
    The unsettled leg is no longer a watermark->now window: it's every
    FINISHED session with settled_payout_id IS NULL (see the module
    docstring's "Settlement marking (gross)" section). `watermark`/`now` are
    still returned (and still drive the offline-topup pool figure below)
    since POST /api/cpo/payouts still uses them for the refund-clawback
    window and the payout's period_start/period_end."""
    now = datetime.now(timezone.utc)
    watermark = await tenant_settlement_watermark(db, tenant_id)

    lifetime_gross = await sum_completed_session_coins(db, tenant_id)
    lifetime_fee, lifetime_net = compute_fee_and_net(lifetime_gross)

    unsettled_gross = await sum_unsettled_session_coins(db, tenant_id)
    unsettled_fee, unsettled_net = compute_fee_and_net(unsettled_gross)

    # Offline top-ups already issued in this same unsettled window must come
    # back out of the pool available for further top-ups AND out of what a
    # bank payout can claim — see the module docstring's "Offline top-up
    # pool" section. Clamped at zero: a CPO can never issue more in top-ups
    # than the pool held (the 409 in POST /api/cpo/topups enforces that going
    # forward), but clamp anyway so a data inconsistency can't produce a
    # negative pool figure on this read-only dashboard endpoint.
    topups_since_watermark = await sum_offline_topups(
        db, tenant_id, window_start=watermark, window_end=now
    )
    available_pool_coins = to_money(max(unsettled_net - topups_since_watermark, ZERO_MONEY))

    return {
        "watermark": watermark,
        "now": now,
        "lifetime_gross_coins": lifetime_gross,
        "lifetime_platform_fee_coins": lifetime_fee,
        "lifetime_net_coins": lifetime_net,
        "unsettled_gross_coins": unsettled_gross,
        "unsettled_platform_fee_coins": unsettled_fee,
        "unsettled_net_coins": unsettled_net,
        "topups_since_watermark_coins": topups_since_watermark,
        "available_pool_coins": available_pool_coins,
    }
