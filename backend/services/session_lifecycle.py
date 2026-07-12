"""
Session lifecycle helpers shared across routers and background services.

Moved verbatim out of main.py (2026-07-07, TD#7 split):
- set_plug_telemetry_interval — telemetry cadence control (routes + Socket.io)
- gateway_is_live — the session-start liveness gate
- check_and_speed_up_active_session — login/`me` hook
- finalize_charging_session — the one true stop/billing path (stop route +
  session reaper)
"""
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend import state
from backend.database.models import (
    ChargingSession, Gateway, GatewayStatus, LedgerTransaction, Plug,
    PlugStatus, SessionStatus, TransactionType,
)
from backend.services.money import energy_cost
from backend.services.telemetry import COINS_PER_KWH
from backend.services.wallet import debit_wallet_clamped

logger = logging.getLogger("amphive.api")

# A gateway counts as live only if it reported (status or telemetry) within
# this window. The DB status flag alone is not enough: seeded/mock rows can
# say ONLINE with no MQTT client behind them, and a missed LWT leaves a dead
# gateway ONLINE forever. Telemetry arrives every ~10 s from a healthy
# gateway (bumped into last_seen_at at most once a minute), so 120 s gives
# comfortable slack without letting sessions start against dead hardware.
GATEWAY_LIVENESS_WINDOW_SEC = int(os.getenv("GATEWAY_LIVENESS_WINDOW_SEC", "120"))


async def set_plug_telemetry_interval(db: AsyncSession, plug_id: int, interval_ms: int):
    """
    Update the polling interval for a specific plug in telemetry_store
    and publish the SET_INTERVAL MQTT command to the gateway.
    """
    if state.telemetry_store.get_interval(plug_id) == interval_ms:
        return

    result = await db.execute(select(Plug).where(Plug.id == plug_id))
    plug = result.scalar_one_or_none()
    if not plug:
        logger.warning(
            "Plug not found when trying to set telemetry interval",
            extra={"plug_id": plug_id},
        )
        return

    state.telemetry_store.set_interval(plug_id, interval_ms)

    from backend.services.mqtt_manager import MQTTManager
    manager = MQTTManager()
    if hasattr(manager, "client") and manager.client:
        manager.send_plug_interval(plug.gateway_id, plug_id, interval_ms)


def gateway_is_live(gateway: Gateway, now: Optional[datetime] = None) -> bool:
    """
    Whether a gateway is actually reachable, not just flagged ONLINE in the DB.
    Requires both the ONLINE status and a last_seen_at within
    GATEWAY_LIVENESS_WINDOW_SEC (status messages and telemetry both refresh it).
    """
    if gateway.status != GatewayStatus.ONLINE:
        return False
    if gateway.last_seen_at is None:
        return False
    last_seen = gateway.last_seen_at
    if last_seen.tzinfo is None:
        # Legacy rows written by the old naive-datetime onupdate hook.
        last_seen = last_seen.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return (now - last_seen).total_seconds() <= GATEWAY_LIVENESS_WINDOW_SEC


async def check_and_speed_up_active_session(db: AsyncSession, user_id: int):
    """
    Check if the user has active charging sessions, and if so, speed up the
    telemetry interval for each corresponding plug to 1000ms.

    A user can hold more than one ACTIVE session (starts are capped at
    MAX_ACTIVE_SESSIONS_PER_USER, default 2), so this must not assume a single
    row — scalar_one_or_none() here raised MultipleResultsFound, which broke
    /api/auth/login and /api/auth/me for that user.
    """
    result = await db.execute(
        select(ChargingSession).where(
            and_(
                ChargingSession.user_id == user_id,
                ChargingSession.status == SessionStatus.ACTIVE,
            )
        )
    )
    for active_session in result.scalars().all():
        await set_plug_telemetry_interval(db, active_session.plug_id, 1000)


async def finalize_charging_session(
    db: AsyncSession,
    session_id: int,
    *,
    expected_user_id: Optional[int] = None,
    reason: Optional[str] = None,
) -> Optional[dict]:
    """
    Shared stop path for /api/sessions/stop and the session reaper: bill the
    session, debit the wallet, write the ledger row, free the plug, and tear
    down the telemetry stream.

    The session row is locked (SELECT ... FOR UPDATE) and its ACTIVE status
    re-checked under the lock, so concurrent finalizers (double stop click,
    user racing the reaper) settle exactly once — the loser gets None.
    Returns None when the session doesn't exist (for the caller's filter) or
    is no longer ACTIVE; otherwise returns the stop-response payload.
    """
    filters = [ChargingSession.id == session_id]
    if expected_user_id is not None:
        filters.append(ChargingSession.user_id == expected_user_id)
    result = await db.execute(
        select(ChargingSession).where(and_(*filters)).with_for_update()
    )
    session = result.scalar_one_or_none()
    if not session or session.status != SessionStatus.ACTIVE:
        return None

    # Load the plug
    plug_result = await db.execute(select(Plug).where(Plug.id == session.plug_id))
    plug = plug_result.scalar_one()

    # 1. Send MQTT OFF command (best-effort — the gateway may be gone; its
    #    on-device watchdogs bound the hardware side)
    success = state.mqtt_manager.send_plug_command(
        gateway_id=plug.gateway_id,
        plug_id=plug.id,
        action="OFF",
        local_ip=plug.local_ip,
    )

    if not success:
        logger.warning(
            "Failed to send OFF command, proceeding with DB cleanup",
            extra={"session_id": session.id, "plug_id": plug.id, "gateway_id": plug.gateway_id},
        )

    # 2. Determine final energy, then derive cost from it.
    #    Prefer the live in-memory snapshot, but fall back to the energy
    #    persisted on the session row (updated from inbound MQTT telemetry by
    #    MQTTManager._persist_telemetry). This matters after a backend restart:
    #    TelemetryStore is empty, and session.coins_spent is NEVER written
    #    mid-session — so the old `latest.cost_coins if latest else
    #    session.coins_spent` billed 0 for any session that outlived a restart.
    #    Take the max so a stale/empty store can't bill LESS than what was
    #    already recorded, and always compute cost from energy * rate (the
    #    same formula TelemetryStore uses) for a single source of truth.
    #    The rate is the one SNAPSHOTTED on the session at start (services/
    #    pricing.py resolve_rate_for_plug, via routers/sessions.py) — a
    #    tariff edit or reassignment made mid-session must not change what
    #    this session bills. Only a legacy session (started before the
    #    rate_coins_per_kwh column existed) falls back to the env default.
    latest = state.telemetry_store.get_latest(plug.id)
    persisted_energy = session.energy_kwh or 0.0
    live_energy = latest.energy_kwh if latest else 0.0
    final_energy = max(live_energy, persisted_energy)
    rate = session.rate_coins_per_kwh if session.rate_coins_per_kwh is not None else COINS_PER_KWH
    final_cost = energy_cost(final_energy, rate)  # Decimal, 2 dp

    # 3. Finalize session
    session.status = SessionStatus.COMPLETED
    session.ended_at = datetime.now(timezone.utc)
    session.energy_kwh = final_energy

    # 4. Deduct coins from user wallet and create ledger entry (Atomic).
    #    debit_wallet_clamped locks and reads the balance as a column, fresh
    #    from the DB — an entity re-select here (even with_for_update) would
    #    return the request session's identity-mapped User loaded at auth
    #    time, whose stale balance silently undoes writes committed since
    #    (e.g. a webhook top-up landing mid-request).
    #
    #    [Auth holds] With an authorization hold (session.hold_coins,
    #    reserved at start — see routers/sessions.py start_charging_session /
    #    services/wallet.py available_balance), the amount REQUESTED from
    #    debit_wallet_clamped is capped to min(final_cost, hold_coins): the
    #    hold already reserved this session's worst-case cost (available
    #    balance at start, itself capped by max_kwh * rate), so a debit
    #    bounded by it can never exceed what was actually reserved — closing
    #    the old forgiven-overage revenue leak (SECURITY.md §5) for every
    #    held session. Any unspent remainder (final_cost < hold_coins, e.g.
    #    the session stopped before using its full reservation) is simply
    #    released: no money ever moved for the hold itself, so there is
    #    nothing to move back — the reservation just stops counting the
    #    moment this session leaves ACTIVE (available_balance's SUM only
    #    includes ACTIVE sessions). debit_wallet_clamped still independently
    #    clamps to whatever the DB balance actually holds right now, so the
    #    hold cap here is defense-in-depth, not a replacement for that clamp.
    #    Legacy sessions (hold_coins IS NULL, started before this column
    #    existed) keep the exact pre-hold behavior: request the full
    #    final_cost, clamped only by the live wallet balance — a ledger row
    #    whose `amount` disagrees with the real balance delta (as `max(0,
    #    ...)` used to, while still recording -final_cost) breaks
    #    reconciliation, so any forgiven shortfall is logged for
    #    observability in exactly (and only) this legacy path.
    has_hold = session.hold_coins is not None
    requested_debit = min(final_cost, session.hold_coins) if has_hold else final_cost
    actual_debit, new_balance = await debit_wallet_clamped(
        db, session.user_id, requested_debit
    )
    shortfall = final_cost - actual_debit
    session.coins_spent = actual_debit  # what was actually collected from the wallet

    description = f"Charging session on {plug.name}: {final_energy:.3f} kWh"
    if reason:
        description += f" [{reason}]"
    if shortfall > 0:
        if has_hold:
            # Capped by this session's OWN hold, not by an empty wallet — by
            # design (see above). Deliberately no wallet-exhaustion warning
            # here; that stays scoped to the legacy NULL-hold path below,
            # where it signals the real forgiven-overage bug the hold
            # mechanism exists to close.
            description += (
                f" (capped at the {session.hold_coins:.2f}-coin hold; "
                f"{shortfall:.2f} coins beyond it not collected)"
            )
        else:
            description += f" (shortfall {shortfall:.2f} coins uncollected)"
            logger.warning(
                "Session billed more than the wallet held; shortfall forgiven",
                extra={
                    "session_id": session.id, "user_id": session.user_id,
                    "billed_coins": float(final_cost), "collected_coins": float(actual_debit),
                    "shortfall_coins": float(shortfall),
                },
            )

    ledger_entry = LedgerTransaction(
        user_id=session.user_id,
        session_id=session.id,
        amount=-actual_debit,  # Negative = debit; matches the real balance delta
        transaction_type=TransactionType.SESSION_DEBIT,
        description=description,
        balance_after=new_balance,
    )
    db.add(ledger_entry)

    # 5. Update plug status back to available
    plug.status = PlugStatus.AVAILABLE
    await db.commit()

    # Broadcast so other clients' plug lists flip back to AVAILABLE live.
    from backend.services.socketio_manager import emit_plug_status
    await emit_plug_status(plug.id, PlugStatus.AVAILABLE.value)

    # 6. End telemetry stream
    state.telemetry_store.end_session(plug.id)
    await set_plug_telemetry_interval(db, plug.id, 10000)

    logger.info(
        "Session stopped",
        extra={
            "session_id": session.id, "user_id": session.user_id, "plug_id": plug.id,
            "energy_kwh": round(final_energy, 3), "coins_spent": float(actual_debit),
            "reason": reason,
        },
    )

    # Duration for the receipt (started_at is tz-aware; ended_at was just set).
    duration_sec = None
    if session.started_at and session.ended_at:
        started = session.started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        duration_sec = int((session.ended_at - started).total_seconds())

    # Driver notification — one shot here covers every stop path (user stop,
    # balance auto-stop, reaper). Best-effort by contract: notify() never
    # raises into the billing path.
    from backend.services.notifications import notify
    if reason and "balance exhausted" in reason:
        n_title, n_severity = "Charging auto-stopped — balance used up", "warning"
    elif reason and "telemetry lost" in reason:
        n_title, n_severity = "Charging ended — charger connection lost", "warning"
    elif reason and reason.startswith("safety cutoff"):
        n_title, n_severity = "Charging stopped for safety", "critical"
    elif reason and "limit reached" in reason:
        # [Session limits] A user-set stop condition (energy/time) — an
        # expected, driver-chosen outcome, so informational; the specific
        # limit type rides into the body via the reason string below.
        n_title, n_severity = "Charging stopped — your limit was reached", "info"
    else:
        n_title, n_severity = "Charging complete", "info"
    minutes = (duration_sec or 0) // 60
    n_body = (
        f"{plug.name}: {final_energy:.3f} kWh in {minutes} min — "
        f"{actual_debit:.2f} coins charged, {new_balance:.2f} left."
    )
    if reason and (n_severity == "critical" or "limit reached" in reason):
        n_body += f" ({reason})"
    await notify(
        session.user_id,
        "session_stopped",
        n_title,
        n_body,
        severity=n_severity,
        plug_id=plug.id,
        session_id=session.id,
    )

    return {
        "status": "completed",
        "session_id": session.id,
        "plug_id": plug.id,
        "plug_name": plug.name,
        "energy_kwh": round(final_energy, 3),
        "peak_power_w": round(session.peak_power_w or 0.0, 1),
        # The coins-per-kWh rate this session was actually billed at (its
        # snapshot, or the env default for a legacy pre-snapshot session) —
        # surfaced on the receipt so a driver can see what they paid per kWh.
        "price_per_kwh": float(rate),
        "coins_spent": round(actual_debit, 2),
        "shortfall_coins": round(shortfall, 2),
        # debit_wallet_clamped guarantees new_balance = prev_balance - actual_debit
        "balance_before": round(new_balance + actual_debit, 2),
        "balance_remaining": round(new_balance, 2),
        "duration_sec": duration_sec,
        "started_at": session.started_at.isoformat() if session.started_at else None,
        "ended_at": session.ended_at.isoformat() if session.ended_at else None,
        # [Session limits] The stop conditions this session ran with (NULL
        # for legacy pre-limit sessions) — surfaced on the receipt so the UI
        # can say which limit an auto-stop hit, alongside `reason`.
        "max_kwh": session.max_kwh,
        "max_duration_seconds": session.max_duration_seconds,
        "reason": reason,
    }
