"""
AmpHive tariff / pricing resolution
=====================================
Resolves the coins-per-kWh rate that applies to a plug — the per-CPO/per-site
tariff foundation replacing the old single global ``COINS_PER_KWH`` env var.

Fallback chain (first match wins), mirrored on ``Tariff`` in
``database/models.py``::

    plug.tariff -> plug's charger group's tariff -> tenant.default_tariff
    -> the global COINS_PER_KWH env var (legacy, pre-tariff behavior)

This module only *resolves* a rate — it never bills anything. The resolved
Decimal must be SNAPSHOTTED onto ``ChargingSession.rate_coins_per_kwh`` at
session start (``routers/sessions.py`` ``start_charging_session``) so a
tariff edit or reassignment mid-session never retroactively changes what an
in-flight or already-billed session is charged. Every downstream billing
path — ``finalize_charging_session``, the mqtt_manager
balance-exhaustion auto-stop, and the live ``TelemetryStore`` cost calc —
reads that snapshot and only calls back into this module's env fallback
(:func:`default_rate`) when the snapshot is NULL (legacy sessions predating
this column).

Pricing v2 (docs/PRICING_V2_SPEC.md) refines a resolved :class:`Tariff` with
optional time-of-day :class:`TariffSlot` windows: the flat ``price_per_kwh``
still applies whenever no slot covers the moment. :func:`resolve_rate_window`
returns both the applicable rate AND the wall-clock boundary at which it could
next change; :func:`max_rate_over_window` gives the worst-case rate across a
future interval (for auth-hold sizing). The pure, DB-free core is
:func:`_slot_rate_and_bound`.

Phase 2 wires this into billing: session start snapshots ``rate_valid_until``
from :func:`resolve_rate_window` and sizes the hold off :func:`max_rate_over_window`;
:func:`reprice_session_if_due` (below) closes out the current segment when that
boundary passes, called from the telemetry frame hook and the reaper backstop.
A flat tariff still resolves ``(rate, None)`` — no boundary, one segment, billed
exactly as before.
"""
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.models import (
    ChargerGroup,
    Gateway,
    Plug,
    Tariff,
    TariffSlot,
    Tenant,
)
from backend.services.money import to_money
from backend.services.telemetry import COINS_PER_KWH

# The zone a tenant's minute-of-day slots default to when Tenant.timezone is
# unset/blank/invalid — India, matching the column's server_default.
DEFAULT_TZ = "Asia/Kolkata"

# [Pricing v2 Phase 3] When on (default), an operator pricing edit reprices the
# tenant's ACTIVE sessions forward-only (mark_tenant_sessions_for_reprice). Set
# false so an edit only affects sessions that START after it.
AUTO_REPRICE_ACTIVE_SESSIONS = os.getenv(
    "AUTO_REPRICE_ACTIVE_SESSIONS", "true"
).lower() in ("1", "true", "yes")


def default_rate() -> Decimal:
    """The global env-configured rate (``COINS_PER_KWH``), as 2dp Decimal
    money. The last link in the fallback chain, and what legacy (pre-tariff)
    sessions with a NULL ``rate_coins_per_kwh`` snapshot still bill at."""
    return to_money(COINS_PER_KWH)


def _zone(tz_name) -> "timezone | ZoneInfo":
    """Return a tzinfo for ``tz_name``, falling back to ``DEFAULT_TZ`` and then
    UTC. A tenant.timezone typo (or a host without the IANA tz database) must
    never crash rate resolution."""
    for candidate in (tz_name, DEFAULT_TZ):
        if candidate:
            try:
                return ZoneInfo(candidate)
            except Exception:
                continue
    return timezone.utc


def _to_local(dt: datetime, tz) -> datetime:
    """Project ``dt`` into zone ``tz``. A naive ``dt`` is read as UTC — the
    same convention finalize/gateway_is_live use for legacy tz-less rows."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz)


def _slot_rate_and_bound(slots, at_local: datetime):
    """
    PURE, DB-FREE slot resolution — the unit-testable core.

    Given TariffSlot-like objects (each exposing ``start_min`` / ``end_min`` /
    ``days_mask`` / ``price_per_kwh``) and a tz-aware LOCAL datetime
    ``at_local``, return ``(rate, boundary)``:

      - ``rate``: 2dp Decimal price of the slot COVERING ``at_local``, or
        ``None`` when no slot covers it (a gap, or no slot for this weekday).
      - ``boundary``: the next wall-clock datetime (in ``at_local``'s zone) at
        which the applicable rate could change —
          * covering a slot -> that slot's ``end_min`` today (the rate is
            re-resolved at the slot's end, even if the neighbour repeats it);
          * in a gap with a later slot today -> the ``start_min`` of the next
            slot that begins later today;
          * otherwise -> the start of the earliest slot on the NEXT weekday that
            has any slot (scanning up to 7 days ahead), so a session sitting in an
            end-of-day gap or on a slot-less weekday still reprices when the next
            applicable window opens rather than freezing at the current rate.
        ``None`` only when no slot exists on ANY weekday (a genuinely flat set).

    Covering test for a slot S: S is active on ``at_local``'s weekday
    (``(S.days_mask >> weekday) & 1``, Mon=bit0) AND
    ``S.start_min <= minute_of_day < S.end_min`` (half-open [start, end)).

    Determinism: only slots active on today's weekday are considered; ties are
    broken by (start_min, end_min) so overlapping/malformed data still yields a
    stable result. A midnight-wrapping window is expected to be two slots
    ([1320,1440) + [0,360)); ``end_min`` == 1440 resolves to next-day midnight.
    """
    weekday = at_local.weekday()                       # Mon=0 .. Sun=6
    minute_of_day = at_local.hour * 60 + at_local.minute
    # Local midnight of at_local's date; slot boundaries are wall-clock minute
    # offsets within this day (minute 1440 -> next-day midnight). Fine for the
    # no-DST default zone; India (Asia/Kolkata) never shifts.
    day_start = at_local.replace(hour=0, minute=0, second=0, microsecond=0)

    def _bound_at(minute):
        return day_start + timedelta(minutes=int(minute))

    # Only slots active on today's weekday can affect today's rate/boundary.
    todays = [s for s in slots if (int(s.days_mask) >> weekday) & 1]

    # 1. A slot covering now? (half-open [start_min, end_min))
    covering = [s for s in todays if int(s.start_min) <= minute_of_day < int(s.end_min)]
    if covering:
        slot = min(covering, key=lambda s: (int(s.start_min), int(s.end_min)))
        return to_money(slot.price_per_kwh), _bound_at(slot.end_min)

    # 2. In a gap — the boundary is the start of the next slot later today.
    upcoming = [s for s in todays if int(s.start_min) > minute_of_day]
    if upcoming:
        nxt = min(upcoming, key=lambda s: int(s.start_min))
        return None, _bound_at(nxt.start_min)

    # 3. No covering slot and nothing else starts later today. Roll forward to the
    #    next weekday that bears any slot (up to 7 days) and return its earliest
    #    start, so a session in an end-of-day gap — or on a weekday with no slots —
    #    still reprices when the next applicable window opens (per-weekday support).
    for delta in range(1, 8):
        d = day_start + timedelta(days=delta)
        wd = d.weekday()
        day_slots = [s for s in slots if (int(s.days_mask) >> wd) & 1]
        if day_slots:
            return None, d + timedelta(minutes=min(int(s.start_min) for s in day_slots))

    # No slot on any weekday at all — genuinely flat, no further boundary.
    return None, None


async def _resolve_tariff_and_tz(db: AsyncSession, plug: Plug):
    """
    Resolve ``(tariff, tz_name)`` for ``plug``: the applicable :class:`Tariff`
    via the SAME fallback chain as before (plug -> plug's group -> tenant
    default; first match wins, ``None`` if the chain is empty) PLUS the owning
    tenant's timezone (via the gateway, the authoritative owner link — a plug
    has no direct tenant_id). The resolved rate is identical to the legacy
    chain; the tenant is always loaded here only to read its zone.
    """
    # Owning tenant (for its timezone) via the gateway.
    tz_name = DEFAULT_TZ
    tenant = None
    gateway = (
        await db.execute(select(Gateway).where(Gateway.id == plug.gateway_id))
    ).scalar_one_or_none()
    if gateway is not None:
        tenant = (
            await db.execute(select(Tenant).where(Tenant.id == gateway.tenant_id))
        ).scalar_one_or_none()
        if tenant is not None and tenant.timezone:
            tz_name = tenant.timezone

    # Tariff via the fallback chain, first match wins.
    tariff = None
    if plug.tariff_id is not None:
        tariff = (
            await db.execute(select(Tariff).where(Tariff.id == plug.tariff_id))
        ).scalar_one_or_none()
    if tariff is None and plug.group_id is not None:
        group = (
            await db.execute(select(ChargerGroup).where(ChargerGroup.id == plug.group_id))
        ).scalar_one_or_none()
        if group is not None and group.tariff_id is not None:
            tariff = (
                await db.execute(select(Tariff).where(Tariff.id == group.tariff_id))
            ).scalar_one_or_none()
    if tariff is None and tenant is not None and tenant.default_tariff_id is not None:
        tariff = (
            await db.execute(select(Tariff).where(Tariff.id == tenant.default_tariff_id))
        ).scalar_one_or_none()

    return tariff, tz_name


async def resolve_rate_window(db: AsyncSession, plug: Plug, at: datetime = None):
    """
    Resolve ``(rate, boundary)`` applicable to ``plug`` at instant ``at``
    (default: now, UTC).

    Picks the tariff via the usual chain, loads its time-of-day slots + the
    tenant's timezone, projects ``at`` into that zone, and delegates to the
    pure :func:`_slot_rate_and_bound`:
      - ``rate``: the covering slot's price, else the tariff's flat
        ``price_per_kwh`` — 2dp Decimal money.
      - ``boundary``: the wall-clock instant the rate could next change today,
        or ``None``.

    No tariff anywhere in the chain -> ``(default_rate(), None)`` (the legacy
    env fallback, with no time boundary). Read-only — snapshots nothing.
    """
    tariff, tz_name = await _resolve_tariff_and_tz(db, plug)
    if tariff is None:
        return default_rate(), None

    if at is None:
        at = datetime.now(timezone.utc)
    at_local = _to_local(at, _zone(tz_name))

    slots = (
        await db.execute(select(TariffSlot).where(TariffSlot.tariff_id == tariff.id))
    ).scalars().all()
    slot_rate, boundary = _slot_rate_and_bound(slots, at_local)
    # NOTE: explicit None check (not `slot_rate or ...`) — a legitimate 0.00
    # slot rate is falsy but must still win over the flat price.
    rate = slot_rate if slot_rate is not None else to_money(tariff.price_per_kwh)
    return rate, boundary


async def resolve_rate_for_plug(db: AsyncSession, plug: Plug) -> Decimal:
    """
    Resolve the coins-per-kWh rate for ``plug`` via the fallback chain:
    plug.tariff -> plug's group.tariff -> tenant.default_tariff -> env default.

    Returns 2dp Decimal money. Read-only — does not mutate or snapshot
    anything; callers that start a session must persist the result onto
    ``ChargingSession.rate_coins_per_kwh`` themselves.

    Now a thin wrapper over :func:`resolve_rate_window` that discards the time
    boundary — existing callers only need the scalar rate, and with no slots
    configured (Phase 1) this resolves exactly the flat rate it always did.
    """
    return (await resolve_rate_window(db, plug))[0]


async def max_rate_over_window(
    db: AsyncSession, plug: Plug, start_at: datetime, max_duration_seconds: int
) -> Decimal:
    """
    The MAX coins-per-kWh ``plug``'s tariff could charge at ANY point over the
    interval ``[start_at, start_at + max_duration_seconds]``, in the tenant's
    local wall-clock. Resolves the tariff + slots ONCE. Returns 2dp Decimal.

    Later phases size an authorization hold off this so a session that may span
    a price change can never under-reserve. With no slots (Phase 1) it is just
    the tariff's flat ``price_per_kwh`` (or the env default when the chain is
    empty).

    An interval spanning >= 24h necessarily touches every weekday's slots at
    some point, so the answer is simply ``max(base, all slot rates)``.
    Otherwise the max is taken over ``base`` plus only the slots whose active
    window overlaps the interval on the local calendar dates it touches.
    """
    tariff, tz_name = await _resolve_tariff_and_tz(db, plug)
    if tariff is None:
        return default_rate()

    base = to_money(tariff.price_per_kwh)
    slots = (
        await db.execute(select(TariffSlot).where(TariffSlot.tariff_id == tariff.id))
    ).scalars().all()
    if not slots:
        return base

    # >= 24h: every day's slots are reached at some instant -> max over all.
    if max_duration_seconds >= 24 * 3600:
        return max([base] + [to_money(s.price_per_kwh) for s in slots])

    tz = _zone(tz_name)
    start_local = _to_local(start_at, tz)
    end_local = start_local + timedelta(seconds=max_duration_seconds)

    rates = [base]
    # Walk each local calendar date the (sub-24h) interval touches — at most a
    # couple — and test each active slot's window on that date for overlap.
    day = start_local.replace(hour=0, minute=0, second=0, microsecond=0)
    last_day = end_local.replace(hour=0, minute=0, second=0, microsecond=0)
    while day <= last_day:
        weekday = day.weekday()
        for s in slots:
            if not ((int(s.days_mask) >> weekday) & 1):
                continue
            slot_start = day + timedelta(minutes=int(s.start_min))
            slot_end = day + timedelta(minutes=int(s.end_min))
            # Half-open [slot_start, slot_end) overlaps [start_local, end_local].
            if slot_start < end_local and slot_end > start_local:
                rates.append(to_money(s.price_per_kwh))
        day = day + timedelta(days=1)
    return max(rates)


def slot_overlaps(start_a, end_a, days_a, start_b, end_b, days_b) -> bool:
    """PURE, DB-FREE: do two TOD slots conflict? They overlap iff they share at
    least one weekday (``days_mask`` AND is non-zero) AND their half-open
    ``[start, end)`` minute windows intersect. Touching at a boundary
    (``a.end == b.start``) is NOT an overlap — same half-open convention as the
    resolver. The operator slot-CRUD validation core (routers/cpo.py)."""
    if not (int(days_a) & int(days_b)):
        return False
    return int(start_a) < int(end_b) and int(start_b) < int(end_a)


async def resolve_price_display(db: AsyncSession, plug: Plug, at: datetime = None):
    """
    [Pricing v2 — driver transparency] Resolve ``(rate, boundary, next_rate)``
    for a plug's price display: the rate in effect at ``at`` (default now), the
    wall-clock instant it next changes at a TOD slot boundary, and the rate on
    the far side of that boundary. Loads the tariff + slots ONCE — same DB cost
    as :func:`resolve_rate_for_plug`, so swapping the plug list/detail endpoints
    onto it adds NO query over the rate they already resolve.

    ``boundary`` and ``next_rate`` are BOTH ``None`` for a flat tariff (no
    slots), when no boundary remains today, or when the rate doesn't actually
    change across the next boundary (adjacent slots repeating a price) — so a
    caller renders a "next price" hint only on a real, imminent change.
    ``boundary`` is tz-aware in the tenant's local zone (``isoformat`` carries
    the offset). Read-only — snapshots nothing.

    LIST endpoints must use :func:`resolve_price_display_batch` instead —
    fixed two-query cost for the whole list rather than per-plug chain lookups.
    """
    tariff, tz_name = await _resolve_tariff_and_tz(db, plug)
    if tariff is None:
        return default_rate(), None, None

    if at is None:
        at = datetime.now(timezone.utc)
    at_local = _to_local(at, _zone(tz_name))
    slots = (
        await db.execute(select(TariffSlot).where(TariffSlot.tariff_id == tariff.id))
    ).scalars().all()
    return _price_display_from_slots(slots, at_local, to_money(tariff.price_per_kwh))


def _price_display_from_slots(slots, at_local: datetime, flat: Decimal):
    """
    PURE, DB-FREE display core shared by :func:`resolve_price_display` and
    :func:`resolve_price_display_batch`: given a tariff's slots, a tz-aware
    LOCAL instant and the tariff's flat price, return ``(rate, boundary,
    next_rate)`` with exactly the semantics documented on
    :func:`resolve_price_display`.
    """
    slot_rate, boundary = _slot_rate_and_bound(slots, at_local)
    rate = slot_rate if slot_rate is not None else flat
    # Only preview a change happening LATER THE SAME LOCAL DAY. _slot_rate_and_bound
    # can return a cross-day boundary (the next applicable weekday) for the
    # repricing path, but a bare "@ HH:MM" driver ribbon can't convey a future
    # date, so a tomorrow/next-week boundary must not surface as a "next price".
    if boundary is None or boundary.date() != at_local.date():
        return rate, None, None

    # The rate just past the boundary: a pure re-resolve over the SAME slots at
    # the boundary instant (half-open [start, end) means the ending slot no
    # longer covers there, so the next slot — or the flat base in a gap — wins).
    next_slot_rate, _ = _slot_rate_and_bound(slots, boundary)
    next_rate = next_slot_rate if next_slot_rate is not None else flat
    if next_rate == rate:
        return rate, None, None
    return rate, boundary, next_rate


def _pick_tariff(candidate_ids, tariffs_by_id):
    """
    PURE: the batched equivalent of :func:`_resolve_tariff_and_tz`'s chain —
    the first candidate id (chain order: plug -> group -> tenant default) that
    resolves to a LIVE row wins; a non-None id whose row is gone (dangling FK)
    falls through to the next link, exactly like the per-plug lookups. ``None``
    when the whole chain is empty or fully dangling (callers then fall back to
    the env default rate).
    """
    for tid in candidate_ids:
        if tid is not None and tid in tariffs_by_id:
            return tariffs_by_id[tid]
    return None


async def resolve_price_display_batch(db: AsyncSession, items, at: datetime = None):
    """
    [Audit: plug-list N+1] :func:`resolve_price_display` for a whole plug LIST
    at a fixed query cost: ONE ``Tariff`` IN-query over every chain candidate +
    ONE ``TariffSlot`` IN-query over the winning tariffs, instead of up to five
    chain queries per plug.

    ``items``: iterable of ``(plug, group_tariff_id, tenant)`` —
    ``group_tariff_id`` is the plug's charger group's ``tariff_id`` (``None``
    when ungrouped or the group has no tariff) and ``tenant`` the plug's owning
    tenant via its gateway (``None`` tolerated: the chain then skips the
    tenant-default link and the zone falls back to ``DEFAULT_TZ``). The list
    endpoints' joins already carry all three, so batching adds no per-plug
    loads.

    Returns ``{plug.id: (rate, boundary, next_rate)}`` covering every item,
    each triple identical to what :func:`resolve_price_display` returns for
    that plug — same chain fallthrough (including dangling tariff ids), same
    tenant-zone projection, same same-local-day preview rule. All plugs are
    priced at the SAME instant ``at`` (default now): one consistent snapshot
    for the whole list. Read-only — snapshots nothing.
    """
    items = list(items)
    if at is None:
        at = datetime.now(timezone.utc)

    # Per-plug candidate chain, in fallback order (plug -> group -> tenant).
    chains = {
        plug.id: (
            plug.tariff_id,
            group_tariff_id,
            tenant.default_tariff_id if tenant is not None else None,
        )
        for plug, group_tariff_id, tenant in items
    }
    candidate_ids = {tid for chain in chains.values() for tid in chain if tid is not None}

    tariffs_by_id = {}
    if candidate_ids:
        tariffs_by_id = {
            t.id: t
            for t in (
                await db.execute(select(Tariff).where(Tariff.id.in_(candidate_ids)))
            ).scalars()
        }

    # Slots only for tariffs that actually WON a chain (dangling ids fell
    # through above; chain losers are never priced) — keeps the IN-list minimal.
    winner_ids = {
        t.id
        for chain in chains.values()
        if (t := _pick_tariff(chain, tariffs_by_id)) is not None
    }
    slots_by_tariff = {}
    if winner_ids:
        for s in (
            await db.execute(select(TariffSlot).where(TariffSlot.tariff_id.in_(winner_ids)))
        ).scalars():
            slots_by_tariff.setdefault(s.tariff_id, []).append(s)

    out = {}
    for plug, group_tariff_id, tenant in items:
        tariff = _pick_tariff(chains[plug.id], tariffs_by_id)
        if tariff is None:
            out[plug.id] = (default_rate(), None, None)
            continue
        tz_name = tenant.timezone if (tenant is not None and tenant.timezone) else DEFAULT_TZ
        at_local = _to_local(at, _zone(tz_name))
        out[plug.id] = _price_display_from_slots(
            slots_by_tariff.get(tariff.id, []), at_local, to_money(tariff.price_per_kwh)
        )
    return out


async def reprice_session_if_due(db: AsyncSession, session, plug: Plug, at: datetime = None):
    """
    [Pricing v2] Forward-only reprice for one ACTIVE session at ``at`` (default
    now). Returns ``(new_rate, boundary)`` when the rate actually changed (the
    caller then emits a ``rate_changed`` notification), else ``None``.

    Cheap by design: a flat session (``rate_valid_until`` is None) or one whose
    current segment has not yet expired (``at < rate_valid_until``) returns
    immediately with **no DB query** — only the crossing frame re-resolves the
    tariff. On a real change it delegates to
    :func:`services.billing.close_out_segment`, which freezes the just-consumed
    energy at the old rate and repoints the open segment at the session's
    current ``energy_kwh`` / the new rate (so past energy is never re-priced).
    A boundary that lands on the SAME rate (adjacent slots repeating a price)
    just advances ``rate_valid_until`` to the next boundary — no segment churn,
    no notification. Mutates ``session`` in place; the caller commits.
    """
    from backend.services.billing import close_out_segment

    valid_until = session.rate_valid_until
    if valid_until is None:
        return None
    if at is None:
        at = datetime.now(timezone.utc)
    if valid_until.tzinfo is None:          # legacy naive row -> treat as UTC
        valid_until = valid_until.replace(tzinfo=timezone.utc)
    if at < valid_until:
        return None

    new_rate, boundary = await resolve_rate_window(db, plug, at=at)
    at_energy = session.energy_kwh or 0.0
    if new_rate != session.rate_coins_per_kwh:
        close_out_segment(session, new_rate, at_energy, boundary)
        return new_rate, boundary
    # Same rate across the boundary — just move the next check point forward so
    # we don't re-resolve every frame until the day rolls over.
    session.rate_valid_until = boundary
    return None


async def mark_tenant_sessions_for_reprice(db: AsyncSession, tenant_id: int) -> None:
    """
    [Pricing v2 Phase 3] After an operator changes pricing (a tariff's rate, its
    TOD slots, or — on delete — the fallback a plug resolves to), expire the
    current rate segment of EVERY ACTIVE session in the tenant by stamping
    ``rate_valid_until = now``. The telemetry frame hook / reaper backstop then
    run :func:`reprice_session_if_due`, which re-resolves each session and closes
    a segment forward-only + notifies ONLY where the rate actually changed;
    sessions the edit didn't affect re-resolve to the same rate and just advance
    their boundary (a flat one back to NULL). This deliberately reverses the old
    "an assignment/edit never touches an in-flight session" behavior for the rate
    itself — past energy still keeps its price (forward-only).

    One UPDATE, no per-session resolution here (that's deferred to the frame
    hook). Marking the whole tenant — rather than only the plugs that resolve to
    the edited tariff — is intentional: it needs no reverse-resolution and is
    safe because an unaffected session is a no-op reprice. Gated by
    ``AUTO_REPRICE_ACTIVE_SESSIONS``. The caller commits (same txn as the edit).
    """
    if not AUTO_REPRICE_ACTIVE_SESSIONS:
        return
    from backend.database.models import ChargingSession, SessionStatus

    await db.execute(
        update(ChargingSession)
        .where(
            and_(
                ChargingSession.tenant_id == tenant_id,
                ChargingSession.status == SessionStatus.ACTIVE,
            )
        )
        .values(rate_valid_until=datetime.now(timezone.utc))
    )
