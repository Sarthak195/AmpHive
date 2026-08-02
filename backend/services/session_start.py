"""
Shared "begin an ACTIVE charging session" helper + queued-charge config
resolvers.

`begin_active_session` is the single body — extracted verbatim (behavior-
preserving) from routers/sessions.py start_charging_session — that runs
whether a session is triggered by the driver's POST /start or the reaper's
queued-charge auto-start (services/session_reaper.py reap_queued_starts_once).
Keeping it in one place means the caps admission, rate snapshot, authorization
hold, and publish/rollback logic can never diverge between the two entry
points. See docs/proposals/queued-charge-offline-plug.md §4.

The caller owns the plug row lock, the concurrent-session cap, the group-access
check, the reservation gate, and the gateway-liveness / plug-power gates (they
differ between a walk-up start and an auto-start); this helper picks up at
circuit admission and finishes with the plug live and streaming telemetry.
"""
import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from backend import state
from backend.database.models import (
    ChargingSession,
    Gateway,
    Plug,
    PlugStatus,
    ReservationStatus,
    SessionStatus,
    Tenant,
    User,
)
from backend.services.caps import check_circuit_admission
from backend.services.money import ZERO_MONEY, energy_cost, to_money
from backend.services.pricing import max_rate_over_window, resolve_rate_window
from backend.services.session_lifecycle import set_plug_telemetry_interval
from backend.services.wallet import available_balance

logger = logging.getLogger("amphive.api")

# Minimum wallet balance (coins) required to START a session — a float so a
# session can't begin with too little credit to cover meaningful charging. Also
# exposed via GET /api/config so the UI shows the same number it enforces.
# [Auth holds] Checked against the session's AVAILABLE balance (coin_balance
# minus what other ACTIVE sessions already hold — services/wallet.py
# available_balance()), not the raw wallet balance: a driver with plenty of
# coins but most of it held by another running session must not be allowed
# to start a third session past what's actually left to reserve.
# `or "50"` (not a getenv default): compose passes the var through with plain
# ${VAR} interpolation, so an unset .env entry arrives as an EMPTY string —
# float("") would crash-loop the container (the GST_RATE_PCT lesson). Lives
# here (not the router) so both the start path and the queue endpoint /
# auto-start share one floor; routers/sessions.py re-exports it and main.py's
# GET /api/config still imports it from there.
MIN_START_BALANCE_COINS = float(os.getenv("MIN_START_BALANCE_COINS") or "50")


# --- Queued-charge config resolvers (Plug override -> Tenant default) ---

def queued_charging_enabled(tenant: Tenant, plug: Optional[Plug]) -> bool:
    """Whether queued charging is on for this plug: the plug's own override if
    set, else the tenant default (off by default). Mirrors the max_current_a
    Plug-override / Tenant-default precedent."""
    if plug is not None and plug.queued_charging_enabled is not None:
        return plug.queued_charging_enabled
    return bool(tenant.queued_charging_enabled)


def auto_start_delay(tenant: Tenant, plug: Optional[Plug]) -> int:
    """The continuous-power debounce (MINUTES) before a queued charge auto-
    starts: the plug override if set, else the tenant default."""
    if plug is not None and plug.auto_start_delay_min is not None:
        return plug.auto_start_delay_min
    return tenant.auto_start_delay_min


def queue_ttl(tenant: Tenant, plug: Optional[Plug]) -> int:
    """How long a WAITING queued charge lives (MINUTES) before it expires.
    Tenant-level only today (no per-plug override column); the plug argument is
    accepted for signature symmetry with the other resolvers."""
    return tenant.queue_ttl_min


async def begin_active_session(
    db: AsyncSession,
    user: User,
    plug: Plug,
    gateway: Gateway,
    max_kwh: Optional[float],
    max_duration_seconds: Optional[int],
    covering_reservation=None,
) -> ChargingSession:
    """
    Admit and start an ACTIVE charging session on an already-locked, already-
    gated plug: circuit admission -> rate resolve/snapshot -> authorization
    hold sizing (+ MIN_START_BALANCE_COINS floor) -> create the
    ChargingSession(ACTIVE) -> claim the plug OCCUPIED -> commit -> publish the
    MQTT ON -> roll the claim back if the publish fails -> start the telemetry
    stream. Returns the committed session.

    [Opt-in charging limits] `max_kwh`/`max_duration_seconds` are None when
    the driver set no explicit stop condition — the session then charges
    until stopped (driver, or a safety net: balance exhaustion, gateway-
    offline reaping, overcurrent, plug caps). None is persisted onto the
    session verbatim (the DB/API meaning of "no limit"), but the gateway
    ON command gets the firmware-safe UNLIMITED sentinels instead of None
    (see services/mqtt_manager.py firmware_duration/firmware_max_kwh) — the
    firmware's own JSON parsing has no way to represent "no limit" for these
    fields.

    Raises the same typed failures the start path always did, so both entry
    points surface identically (the reaper maps them to EXPIRED/FAILED):
      - HTTPException(409) — circuit at capacity (check_circuit_admission).
      - HTTPException(402) — available balance below the start floor.
      - HTTPException(500) — the gateway ON publish failed (claim rolled back).
    """
    # [Caps] Circuit admission: refuse the start if the plug's group is at its
    # shared current capacity (Σ active plug caps + this plug's cap > line max).
    # Under the plug lock; the helper locks the group row so concurrent starts
    # on the same circuit serialize. No-op for an ungrouped plug or a group with
    # no cap configured. This is where the "Request capacity" flow hooks in.
    await check_circuit_admission(db, plug)

    # Resolve the coins-per-kWh rate for this plug (plug's own tariff -> its
    # group's tariff -> the tenant's default tariff -> the global
    # COINS_PER_KWH env fallback) and SNAPSHOT it onto the session now, so a
    # tariff edit or reassignment made mid-session never retroactively
    # changes what this session bills (see services/pricing.py).
    #
    # [Pricing v2] resolve_rate_window also returns rate_valid_until: the
    # wall-clock instant this rate could next change at a TOD slot boundary
    # (None for a flat tariff → the session stays one segment and bills exactly
    # as before). Snapshotted below so MQTTManager._persist_telemetry can close
    # out the segment forward-only when the boundary passes.
    now = datetime.now(timezone.utc)
    rate_coins_per_kwh, rate_valid_until = await resolve_rate_window(db, plug, at=now)

    # Size the session's authorization hold (MARKET_GAP_ANALYSIS.md §3): reserve
    # this session's worst-case cost up front — the smaller of (a) what's
    # actually left to spend right now (coin_balance net of whatever this user's
    # OTHER active sessions already hold, so two concurrent starts serialized on
    # the user-row lock can never double-reserve the same coins) and (b) the most
    # this session could possibly bill, its own max_kwh cap at the resolved rate.
    # The 402 floor gates on this AVAILABLE figure, not the raw balance.
    available = await available_balance(db, user.id)
    # The floor gates the driver's AVAILABLE balance, not the hold: the hold is
    # capped by the session's own max_kwh, so a small session legitimately
    # reserves less than the floor — rejecting on the hold would block modest
    # sessions for fully-funded wallets (found live in prod 2026-07-12). The
    # floor's job is only to keep dust wallets from starting.
    if available < MIN_START_BALANCE_COINS:
        raise HTTPException(
            status_code=402,
            detail=(
                f"Insufficient available balance. You have {available} coins "
                f"available for this session (wallet balance "
                f"{user.coin_balance} coins; the difference, if any, is held "
                f"by another active session). Minimum {MIN_START_BALANCE_COINS:g} "
                "required to start."
            ),
        )
    # [Pricing v2] Size the hold at the WORST-CASE rate the session could hit
    # over its own max_duration window, not just the start rate: if a TOD
    # boundary raises the rate mid-charge, finalize's min(final_cost, hold) must
    # not forgive the overage. With no slots configured max_rate_over_window
    # returns the flat rate, so this is identical to the old sizing.
    max_rate = await max_rate_over_window(
        db, plug, now, max_duration_seconds or 24 * 3600
    )
    # [Opt-in charging limits] No energy cap (max_kwh is None, the new default)
    # means there's no energy_cost() ceiling to size against — energy_cost(None,
    # ...) would raise (services/money.py coerces via Decimal(str(value))). The
    # worst case is then bounded only by what's actually available to spend, so
    # the hold is simply the available balance — the exact behavior an
    # "unlimited" session needs balance-exhaustion auto-stop to still work off
    # of (services/mqtt/telemetry.py _maybe_auto_stop_on_exhaustion treats
    # hold_coins as an opaque threshold either way).
    hold = (
        to_money(available)
        if max_kwh is None
        else to_money(min(available, energy_cost(max_kwh, max_rate)))
    )

    session = ChargingSession(
        tenant_id=gateway.tenant_id,
        user_id=user.id,
        plug_id=plug.id,
        status=SessionStatus.ACTIVE,
        rate_coins_per_kwh=rate_coins_per_kwh,
        hold_coins=hold,
        # [Pricing v2] Open the first billing segment: nothing settled yet, the
        # segment starts at 0 kWh, and it stays in effect until rate_valid_until
        # (None for a flat tariff = never reprices).
        settled_cost_coins=ZERO_MONEY,
        rate_segment_start_kwh=0.0,
        rate_valid_until=rate_valid_until,
        # [Session limits] Snapshot the request's stop conditions; the hold
        # sizing above already uses max_kwh, so a smaller user limit shrinks the
        # auth hold too.
        max_kwh=max_kwh,
        max_duration_seconds=max_duration_seconds,
    )
    db.add(session)
    if covering_reservation is not None:
        # [Reservations] The holder is starting inside their own window:
        # fulfil the booking and link the session (flush first so session.id
        # exists). Same transaction as the plug claim, so a crash can never
        # leave a FULFILLED reservation pointing at no session.
        await db.flush()
        covering_reservation.status = ReservationStatus.FULFILLED
        covering_reservation.session_id = session.id
    plug.status = PlugStatus.OCCUPIED
    # Commit the claim + session BEFORE touching hardware, releasing the lock.
    await db.commit()
    await db.refresh(session)

    # Broadcast the claim so other clients' plug lists flip to OCCUPIED live.
    from backend.services.socketio_manager import emit_plug_status
    await emit_plug_status(plug.id, PlugStatus.OCCUPIED.value)

    # Now command the gateway. If this fails, undo the claim so the plug doesn't
    # stay OCCUPIED with a live ACTIVE session nobody can drive.
    from backend.services.caps import effective_plug_cap
    from backend.services.mqtt_manager import firmware_duration, firmware_max_kwh

    # [Opt-in charging limits] Resolve None (no explicit limit) to the
    # firmware-safe "unlimited" sentinels before publishing — see
    # services/mqtt_manager.py firmware_duration/firmware_max_kwh for why
    # neither omitting these fields nor sending 0 is safe to encode "no
    # limit" on the gateway's local relay watchdog. An explicit limit passes
    # through unchanged.
    success = await asyncio.to_thread(lambda: state.mqtt_manager.send_plug_command(
        gateway_id=plug.gateway_id,
        plug_id=plug.id,
        action="ON",
        max_duration=firmware_duration(max_duration_seconds),
        max_kwh=firmware_max_kwh(max_kwh),
        session_id=session.id,
        local_ip=plug.local_ip,
        # Plug's effective current cap for on-device enforcement (the plug
        # measures real current). Its own max_current_a, or DEFAULT_PLUG_CAP_A.
        max_current_a=effective_plug_cap(plug),
    ))
    if not success:
        session.status = SessionStatus.CANCELLED
        session.ended_at = datetime.now(timezone.utc)
        plug.status = PlugStatus.AVAILABLE
        if covering_reservation is not None:
            # [Reservations] The start never actually happened — give the
            # holder their window back instead of burning it as FULFILLED.
            covering_reservation.status = ReservationStatus.BOOKED
            covering_reservation.session_id = None
        await db.commit()
        await emit_plug_status(plug.id, PlugStatus.AVAILABLE.value)
        raise HTTPException(
            status_code=500,
            detail="Failed to publish start command to the gateway. The gateway may be offline.",
        )

    # Initialize the telemetry stream for this plug, seeded with the same
    # snapshotted rate so the live cost_coins preview matches what
    # finalize_charging_session will actually bill.
    state.telemetry_store.start_session(plug.id, rate_coins_per_kwh=rate_coins_per_kwh)
    await set_plug_telemetry_interval(db, plug.id, 1000)

    return session
