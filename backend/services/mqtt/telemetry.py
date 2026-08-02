"""
MQTTManager collaborator: telemetry ingestion + TelemetryStore/DB handoff.

Extracted verbatim from services/mqtt_manager.py (god-object split): the
inbound telemetry handler, the gateway-liveness throttle, the DB persist of a
telemetry snapshot (energy/peak-power/plug-power-clock + the REC-02 orphan-OFF
reconciliation), and the two backend-side auto-stop mirrors (wallet exhaustion,
session limits) that ride on every persisted frame. Mixed into MQTTManager;
see services/mqtt/__init__.py for why this is a mixin rather than a delegating
collaborator object.

The three module-level env-flag constants read below (AUTO_STOP_ON_BALANCE_
EXHAUSTED, LOW_BALANCE_WARN_FRACTION, AUTO_STOP_ON_LIMITS) — and
GATEWAY_SEEN_BUMP_INTERVAL_SEC / PLUG_POWER_STALE_SEC — live in
services/mqtt_manager.py (the facade module), not here: tests monkeypatch them
there (e.g. `monkeypatch.setattr(mqtt_manager_module, "AUTO_STOP_ON_LIMITS",
False)`), so each read below is a fresh `from backend.services.mqtt_manager
import NAME` done at call time (matching this codebase's existing "import
here to avoid circular imports" convention) rather than a module-level import,
so it always sees the live value instead of a copy captured at import time.
"""
import asyncio
import functools
import logging
import math
import os
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Optional

logger = logging.getLogger("amphive.mqtt")

# Hard ceiling on inbound kwh magnitude. math.isfinite() below only rejects
# NaN/Infinity — a garbage or malicious frame reporting a huge but perfectly
# *finite* kwh (e.g. 1e30) would otherwise sail through, get pinned onto
# active_session.energy_kwh via the monotonic `max()` in _persist_telemetry,
# and then wedge session_cost()/to_money()'s Decimal.quantize() into a raw
# decimal.InvalidOperation on EVERY finalize path (the default Decimal
# context can't round a ~30-digit number to 2dp) — the session could never
# finalize and the plug would stay OCCUPIED forever. No real session gets
# remotely close to this (a home/commercial charger tops out at a few dozen
# kWh per session); env-overridable for exotic fleets or tests.
MAX_PLAUSIBLE_KWH = float(os.getenv("MAX_PLAUSIBLE_KWH", "1000.0"))

# Minimum drop (raw wire kwh) between consecutive LIVE frames required to
# treat a lower reading as a genuine counter reset rather than %.4f-rounding
# jitter on an essentially-flat reading (the firmware reports kwh to 3dp, so a
# same-value reading can wobble by a unit in the last digit frame to frame).
# Only ever compared against energy_counter_last_raw_kwh, which itself is only
# ever written from a LIVE frame — see _persist_telemetry.
ENERGY_COUNTER_RESET_DROP_KWH = float(os.getenv("ENERGY_COUNTER_RESET_DROP_KWH", "0.005"))


class MQTTTelemetryMixin:
    """Inbound telemetry handling: TelemetryStore feed, DB persistence, and
    the balance/limit auto-stop mirrors that ride on each persisted frame."""

    # -----------------------------------------------------------------------
    # Inbound telemetry handler — feeds TelemetryStore + persists to DB
    # -----------------------------------------------------------------------

    def _handle_gateway_telemetry(self, gateway_id: str, payload: Dict[str, Any]):
        """
        Process a telemetry payload from an ESP32 gateway.

        Expected MQTT payload (per the MQTT_CONTRACT):
        {
            "plug_id": 1,
            "watts": 1200.5,
            "kwh": 0.45,
            "voltage": 230.0,
            "current": 5.2,
            "status": "occupied" | "available",
            "session_id": "42"   # optional; echoed from the ON command, "" when idle
        }

        Actions:
        1. Feed the TelemetryStore so the SSE stream picks it up.
        2. Persist energy_kwh / peak_power_w to the active ChargingSession row
           and update the plug's current_power_w.
        """
        plug_id = payload.get("plug_id")
        if plug_id is None:
            logger.warning(
                "Telemetry missing plug_id, ignoring",
                extra={"gateway_id": gateway_id},
            )
            return
        try:
            plug_id = int(plug_id)
        except (TypeError, ValueError):
            logger.warning(
                "Telemetry has non-integer plug_id, ignoring",
                extra={"gateway_id": gateway_id, "raw_plug_id": repr(payload.get("plug_id"))},
            )
            return

        # Guarded casts (TD#25): a malformed value used to raise inside the
        # paho callback — the reading vanished with no log line and (paho
        # version depending) could kill message dispatch. Non-finite values
        # (NaN/inf parse fine as JSON-ish strings) are rejected too: NaN
        # watts would poison the peak-power comparison and session totals.
        try:
            watts = float(payload.get("watts", 0.0))
            kwh = float(payload.get("kwh", 0.0))
            voltage = float(payload.get("voltage", 230.0))
            current = float(payload.get("current", 0.0))
        except (TypeError, ValueError):
            logger.warning(
                "Telemetry has non-numeric fields, ignoring",
                extra={"gateway_id": gateway_id, "plug_id": plug_id, "payload": payload},
            )
            return
        if not all(math.isfinite(v) for v in (watts, kwh, voltage, current)):
            logger.warning(
                "Telemetry has non-finite values, ignoring",
                extra={"gateway_id": gateway_id, "plug_id": plug_id, "payload": payload},
            )
            return

        # Implausible kwh magnitude (or negative): drop the whole frame rather
        # than clamp it. Clamping would still let a garbage value inflate a
        # bill up to the clamp ceiling; dropping means it can never advance
        # billed energy or trigger relay actuation at all. See MAX_PLAUSIBLE_KWH
        # above for why this exists.
        if kwh < 0 or kwh > MAX_PLAUSIBLE_KWH:
            logger.warning(
                "Telemetry kwh outside plausible bounds, dropping frame",
                extra={
                    "gateway_id": gateway_id, "plug_id": plug_id,
                    "kwh": kwh, "max_plausible_kwh": MAX_PLAUSIBLE_KWH,
                },
            )
            return

        status = payload.get("status", "occupied")
        # Actual relay state as reported by the plug (firmware ≥ 1.5.0). Distinct
        # from `status` (our session state) — lets the UI show the physical relay.
        relay_on = bool(payload.get("relay", status == "occupied"))

        # Optional backend session id echoed by the firmware. Empty/absent when
        # the plug is idle, or on pre-session_id firmware. Used to attribute the
        # reading to the exact session rather than "the active session on this
        # plug" (matters if a reading arrives late / after the plug was reused).
        session_id = None
        raw_sid = payload.get("session_id")
        if raw_sid not in (None, ""):
            try:
                sid = int(raw_sid)
                session_id = sid if sid > 0 else None
            except (ValueError, TypeError):
                session_id = None

        # A resync frame drained from the gateway's offline buffer carries
        # `offline: true` (TD#24). It is a historical reading, so it may update
        # its own (still-ACTIVE) session's energy but must never drive live relay
        # actuation (REC-02) — see _persist_telemetry.
        is_offline = bool(payload.get("offline", False))

        # Map firmware status to telemetry store status
        telem_status = "charging" if status == "occupied" else "idle"

        logger.info(
            "Telemetry received",
            extra={
                "gateway_id": gateway_id,
                "plug_id": plug_id,
                "session_id": session_id,
                "watts": round(watts, 1),
                "kwh": round(kwh, 3),
                "current_a": round(current, 1),
                "voltage_v": round(voltage, 0),
                "status": status,
            },
        )

        # --- 1. Feed the in-memory TelemetryStore (for the live stream) ---
        # This callback runs on the paho network thread. TelemetryStore.update()
        # signals asyncio.Events that live on the server's event loop, and
        # asyncio.Event.set() is NOT thread-safe when called from another thread —
        # it can fail to wake stream() waiters or corrupt loop state. Marshal the
        # update onto the loop so the whole store stays single-threaded.
        # (cost_coins is left to TelemetryStore to auto-calc from this plug's
        # snapshotted per-session rate, falling back to COINS_PER_KWH.)
        if self.telemetry_store and self.event_loop:
            self.event_loop.call_soon_threadsafe(
                functools.partial(
                    self.telemetry_store.update,
                    plug_id, watts, current, kwh, telem_status,
                    voltage_v=voltage, relay_on=relay_on,
                )
            )
        elif self.telemetry_store:
            # No loop reference (e.g. unit tests): safe to call directly.
            self.telemetry_store.update(
                plug_id=plug_id,
                power_w=watts,
                current_a=current,
                energy_kwh=kwh,
                status=telem_status,
                voltage_v=voltage,
                relay_on=relay_on,
            )

        # --- 2. Build the raw sample for time-series persistence ---
        # Buffered + batch-flushed by TelemetryPersistenceService. This is where
        # voltage/current/status (parsed above but not used for session totals)
        # get persisted to telemetry_readings. When a DB is available the
        # enqueue is deferred into _persist_telemetry so it only happens after
        # the plug-ownership check (a gateway must not write history rows for
        # another gateway's plug); with no DB there is nothing to check
        # against, so enqueue directly (unit tests / standalone use).
        sample = None
        if self.telemetry_persistence:
            sample = {
                "plug_id": plug_id,
                "recorded_at": datetime.now(timezone.utc),
                "power_w": watts,
                "energy_kwh": kwh,
                "voltage_v": voltage,
                "current_a": current,
                "status": status,
            }
            if not (self.db_session_factory and self.event_loop):
                self.telemetry_persistence.enqueue(sample)

        # --- 3. Persist authoritative session totals (async, fire-and-forget) ---
        if self.db_session_factory and self.event_loop:
            asyncio.run_coroutine_threadsafe(
                self._persist_telemetry(gateway_id, plug_id, watts, kwh, session_id, sample, relay_on, is_offline),
                self.event_loop,
            )
            # Telemetry proves the gateway is alive — refresh its liveness
            # marker (throttled). Status messages alone only arrive on
            # connect/LWT, so a long-connected gateway would otherwise look
            # stale to the session-start liveness gate.
            if self._should_bump_gateway_seen(gateway_id):
                asyncio.run_coroutine_threadsafe(
                    self._persist_gateway_seen(gateway_id),
                    self.event_loop,
                )

    def _should_bump_gateway_seen(self, gateway_id: str) -> bool:
        """Rate-limit last_seen_at refreshes to one per gateway per
        GATEWAY_SEEN_BUMP_INTERVAL_SEC. Runs on the paho thread only."""
        from backend.services.mqtt_manager import GATEWAY_SEEN_BUMP_INTERVAL_SEC

        now = time.monotonic()
        last = self._gateway_seen_bumped.get(gateway_id)
        if last is not None and (now - last) < GATEWAY_SEEN_BUMP_INTERVAL_SEC:
            return False
        self._gateway_seen_bumped[gateway_id] = now
        return True

    async def _persist_gateway_seen(self, gateway_id: str):
        """Mark a gateway ONLINE + freshly seen because telemetry arrived from
        it (also heals a gateway stuck OFFLINE after a missed retained status)."""
        from sqlalchemy import select

        from backend.database.models import Gateway, GatewayStatus

        try:
            async with self.db_session_factory() as session:
                result = await session.execute(
                    select(Gateway).where(Gateway.id == gateway_id)
                )
                gateway = result.scalar_one_or_none()
                if gateway:
                    gateway.status = GatewayStatus.ONLINE
                    gateway.last_seen_at = datetime.now(timezone.utc)
                    await session.commit()
        except Exception as e:
            logger.error(
                "Failed to refresh last_seen_at for gateway",
                extra={"gateway_id": gateway_id, "error": str(e)},
            )

    async def _persist_telemetry(self, gateway_id: str, plug_id: int, watts: float, kwh: float,
                                 session_id: Optional[int] = None,
                                 sample: Optional[Dict[str, Any]] = None,
                                 relay_on: bool = False,
                                 is_offline: bool = False):
        """
        Persist the latest telemetry snapshot to the database:
        - Verify the claimed plug actually belongs to the publishing gateway
          (the broker ACLs scope *topics* to a gateway, not payload claims — a
          compromised gateway could otherwise attribute energy/billing to
          another tenant's plug).
        - Update `plugs.current_power_w` so the plug list shows real-time power.
        - Enqueue the raw `sample` for time-series persistence (deferred here
          from the handler so it sits behind the same ownership check).
        - Update the target `charging_sessions` row with cumulative energy and
          peak power, so that even if the server crashes, the last-known values
          are saved.

        Session selection: prefer the firmware-reported `session_id` (guarded so
        it must be ACTIVE and on this plug — never mutate a finalized, already
        billed session), and fall back to "the ACTIVE session on this plug" when
        no id was reported. The plug-id fallback is unambiguous in normal
        operation (one ACTIVE session per plug), but the explicit id avoids
        misattributing a late/replayed reading after the plug was reused.
        """
        # Import here to avoid circular imports at module level
        from backend.database.models import ChargingSession, Plug, SessionStatus
        from backend.services.mqtt_manager import PLUG_POWER_STALE_SEC

        # Captured for the post-commit balance/limit checks (see below).
        updated_session_id: Optional[int] = None
        updated_user_id: Optional[int] = None
        updated_rate: Optional[Decimal] = None
        updated_hold_coins: Optional[Decimal] = None
        updated_max_kwh: Optional[float] = None
        updated_max_duration: Optional[int] = None
        updated_started_at: Optional[datetime] = None
        # The session's MONOTONIC total (active_session.energy_kwh, post-max —
        # see the REC-01 clamp below), NOT the raw per-frame `kwh`. A mid-session
        # gateway reboot resets the device counter to ~0, so a raw frame value
        # can be far below the already-billed total; feeding that raw value to
        # the auto-stop mirrors would let a reset silently defeat both the
        # wallet-exhaustion and session-limit auto-stops.
        updated_energy_kwh: Optional[float] = None
        # [Pricing v2] Segment accrual state, captured AFTER an in-frame reprice
        # so the exhaustion check and the live-cost mirror both see the new
        # rate. `reprice` is (new_rate, boundary) when a TOD boundary closed a
        # segment this frame, else None.
        updated_settled: Optional[Decimal] = None
        updated_segment_start: Optional[float] = None
        reprice = None

        try:
            async with self.db_session_factory() as session:
                from sqlalchemy import and_, select

                # Ownership check: the payload's plug_id must name a plug of
                # the gateway that published on this topic.
                plug_result = await session.execute(
                    select(Plug).where(Plug.id == plug_id)
                )
                plug = plug_result.scalar_one_or_none()
                if plug is None or plug.gateway_id != gateway_id:
                    logger.warning(
                        "Telemetry plug ownership mismatch, dropping reading",
                        extra={
                            "gateway_id": gateway_id,
                            "plug_id": plug_id,
                            "actual_owner_gateway_id": plug.gateway_id if plug else None,
                        },
                    )
                    return

                # [Plug power] Stamp the per-plug liveness clock. powered_since
                # re-baselines to now whenever telemetry resumes after a gap
                # longer than PLUG_POWER_STALE_SEC (a mains/relay power-cycle);
                # last_telemetry_at is the freshness signal plug_is_powered()
                # reads. Distinct from the never-written plugs.last_seen_at.
                # Skipped for offline-resync frames (TD#24): those are buffered
                # HISTORICAL readings, not proof the plug is live right now — a
                # backlog of replayed frames must not make a de-powered plug
                # look freshly powered to plug_is_powered().
                if not is_offline:
                    now = datetime.now(timezone.utc)
                    prev = plug.last_telemetry_at
                    if prev is None or (now - prev).total_seconds() > PLUG_POWER_STALE_SEC:
                        plug.powered_since = now
                    plug.last_telemetry_at = now

                # Update plug's current power reading
                plug.current_power_w = watts

                # Raw time-series sample — enqueue now that ownership is proven.
                if self.telemetry_persistence and sample is not None:
                    self.telemetry_persistence.enqueue(sample)

                # Find the session to update (see docstring for selection rules).
                if session_id is not None:
                    where_clause = and_(
                        ChargingSession.id == session_id,
                        ChargingSession.plug_id == plug_id,
                        ChargingSession.status == SessionStatus.ACTIVE,
                    )
                else:
                    where_clause = and_(
                        ChargingSession.plug_id == plug_id,
                        ChargingSession.status == SessionStatus.ACTIVE,
                    )
                # [REC-04] Lock the row the way the reaper reprice does
                # (session_reaper.py) so the forward-only reprice + rate_changed
                # emit below can't race a concurrent frame or the reaper's sweep.
                sess_result = await session.execute(
                    select(ChargingSession).where(where_clause).with_for_update()
                )
                active_session = sess_result.scalar_one_or_none()

                # [REC-05] An idle frame — no session_id AND relay off (firmware
                # emits session_id="" + session_kwh=0 when its session_active is
                # false) — reached an ACTIVE session only via the plug-id
                # fallback. It must NOT be attributed to that session: overwriting
                # its energy (with 0) or refreshing its staleness clock would
                # zero/keep-alive a freshly-started session this frame predates.
                # Require a matching session_id or a live relay to touch it.
                frame_is_idle = session_id is None and not relay_on
                if active_session is not None and not frame_is_idle:
                    # [REC-01] Energy is monotonic: never bill down. A raw wire
                    # reading below the last-seen raw value on a LIVE frame is
                    # a genuine counter reset (device reboot/reflash, or the
                    # ESP32 losing its NVS baseline resets its session-relative
                    # counter near 0) rather than rounding jitter — see
                    # ENERGY_COUNTER_RESET_DROP_KWH above. When detected, the
                    # pre-reset raw value is banked into energy_reset_offset_kwh
                    # so billed energy keeps climbing instead of freezing at
                    # the pre-reset peak until the raw counter climbs back past
                    # it (the old gap: energy delivered in between went
                    # unbilled). Only a LIVE frame can be trusted to detect
                    # this: an offline-resync frame (is_offline) legitimately
                    # replays older buffered readings out of order, so it must
                    # never be mistaken for a reset — it skips detection
                    # entirely, and energy_counter_last_raw_kwh (which tracks
                    # only the live counter's trajectory) is left untouched.
                    if not is_offline:
                        last_raw = active_session.energy_counter_last_raw_kwh
                        if last_raw is not None and kwh < last_raw - ENERGY_COUNTER_RESET_DROP_KWH:
                            active_session.energy_reset_offset_kwh = (
                                active_session.energy_reset_offset_kwh or 0.0
                            ) + last_raw
                            logger.warning(
                                "Energy counter regression detected -- re-baselining session energy",
                                extra={
                                    "session_id": active_session.id,
                                    "plug_id": plug_id,
                                    "prior_raw_kwh": last_raw,
                                    "new_raw_kwh": kwh,
                                    "banked_offset_kwh": active_session.energy_reset_offset_kwh,
                                },
                            )
                        active_session.energy_counter_last_raw_kwh = kwh
                    active_session.energy_kwh = max(
                        active_session.energy_kwh or 0.0,
                        (active_session.energy_reset_offset_kwh or 0.0) + kwh,
                    )
                    # Track peak power — only update if this reading is higher
                    if watts > active_session.peak_power_w:
                        active_session.peak_power_w = watts
                    # Staleness signal read by the session reaper.
                    active_session.last_telemetry_at = datetime.now(timezone.utc)
                    # [Pricing v2] Forward-only reprice if a TOD slot boundary
                    # has passed. Cheap: a flat session (rate_valid_until None)
                    # or one still inside its segment returns with no DB query;
                    # only the crossing frame re-resolves the tariff and closes
                    # out the segment in place, so the captures below — and the
                    # same-frame exhaustion check — bill at the new rate.
                    from backend.services.pricing import reprice_session_if_due
                    reprice = await reprice_session_if_due(session, active_session, plug)
                    updated_session_id = active_session.id
                    updated_user_id = active_session.user_id
                    updated_energy_kwh = active_session.energy_kwh
                    updated_rate = active_session.rate_coins_per_kwh
                    updated_settled = active_session.settled_cost_coins
                    updated_segment_start = active_session.rate_segment_start_kwh
                    updated_hold_coins = active_session.hold_coins
                    updated_max_kwh = active_session.max_kwh
                    updated_max_duration = active_session.max_duration_seconds
                    updated_started_at = active_session.started_at

                # [REC-02] Level-triggered OFF reconciliation. The relay is
                # reported ON but no ACTIVE session owns this plug — a prior OFF
                # was lost/failed while the gateway stayed connected, so the
                # reconnect-only _republish_off_for_orphaned_plugs never fired.
                # Re-send OFF (idempotent — a no-op on an already-off plug),
                # using the same best-effort wait=False publish the reconnect
                # path uses so we don't block the loop on the broker ack.
                # Skipped for offline-resync frames (TD#24): those are historical
                # readings, not the plug's live relay state — an id-scoped lookup
                # that misses a since-finalized session must not actuate the relay.
                elif active_session is None and relay_on and not is_offline:
                    # [REC-02 race guard] The lookup above can miss even though
                    # a DIFFERENT session now legitimately owns this plug: a
                    # frame carrying a STALE claimed session_id (that session
                    # already finalized) arrives after a new session started on
                    # the same plug, so the id-scoped where_clause finds
                    # nothing even though the plug IS validly ACTIVE under the
                    # new session. Re-check plug-scoped (no id filter) before
                    # actuating OFF so we never cut power out from under a
                    # session that's really running.
                    other_active_result = await session.execute(
                        select(ChargingSession).where(
                            and_(
                                ChargingSession.plug_id == plug_id,
                                ChargingSession.status == SessionStatus.ACTIVE,
                            )
                        )
                    )
                    if other_active_result.scalar_one_or_none() is None:
                        self.send_plug_command(
                            gateway_id, plug_id, "OFF", local_ip=plug.local_ip, wait=False
                        )
                        logger.info(
                            "Republished OFF for relay-on plug with no ACTIVE session",
                            extra={"gateway_id": gateway_id, "plug_id": plug_id},
                        )

                await session.commit()
        except Exception as e:
            logger.error(
                "Failed to persist telemetry",
                extra={"gateway_id": gateway_id, "plug_id": plug_id, "error": str(e)},
            )
            return

        # Prepaid protection: if the accrued energy cost has reached the driver's
        # wallet balance, auto-stop so they can't keep charging for free past a
        # drained wallet (the finalize path only clamps the debit — it doesn't
        # stop the session). Done in a separate txn after the persist commit.
        if updated_session_id is not None and updated_user_id is not None:
            # [REC-11] After a restart the live-cost mirror is empty (start_session
            # ran in the now-dead process), so update() billed this frame at the
            # env default and restarted the elapsed timer. Rebuild the mirror from
            # the row we just loaded so subsequent frames stream the session's real
            # rate/segment/started_at. No-op once the plug is already tracked.
            if self.telemetry_store is not None:
                self.telemetry_store.hydrate_session(
                    plug_id, updated_rate, updated_settled,
                    updated_segment_start, updated_started_at,
                )
            # [Pricing v2] A TOD boundary closed a segment this frame: keep the
            # live-cost mirror exact and tell the driver (forward-only). Both
            # best-effort, post-commit — the billing state already persisted.
            if reprice is not None:
                new_rate, _boundary = reprice
                if self.telemetry_store is not None:
                    self.telemetry_store.set_segment_state(
                        plug_id, updated_settled, updated_segment_start, new_rate
                    )
                from backend.services.billing import rate_changed_body
                from backend.services.notifications import notify
                await notify(
                    updated_user_id, "rate_changed", "Charging rate changed",
                    rate_changed_body(new_rate), severity="info",
                    plug_id=plug_id, session_id=updated_session_id,
                )
            await self._maybe_auto_stop_on_exhaustion(
                updated_session_id, updated_user_id, updated_energy_kwh, updated_rate,
                updated_hold_coins, updated_settled, updated_segment_start,
            )
            # [Session limits] User-set stop conditions, mirrored backend-side
            # (see _maybe_auto_stop_on_limits). Sequenced AFTER the exhaustion
            # check deliberately: if both trip on the same frame the
            # exhaustion path finalizes first (its reason wins — revenue
            # protection) and this call's finalize re-check returns None; if
            # a limit fires here instead, the earlier exhaustion call was a
            # covered no-op (at most a low-balance warning) — either way the
            # session settles exactly once via the shared finalize row lock.
            await self._maybe_auto_stop_on_limits(
                updated_session_id, updated_user_id, updated_energy_kwh,
                updated_max_kwh, updated_max_duration, updated_started_at,
            )

    async def _maybe_auto_stop_on_exhaustion(self, session_id: int, user_id: int, energy_kwh: float,
                                              rate_coins_per_kwh: Optional[Decimal] = None,
                                              hold_coins: Optional[Decimal] = None,
                                              settled_cost_coins: Optional[Decimal] = None,
                                              rate_segment_start_kwh: Optional[float] = None):
        """Finalize an ACTIVE session once its accrued cost meets/exceeds its
        exhaustion threshold. No-op when disabled, when the threshold still
        covers the energy, or when the session is already gone (finalize
        re-checks ACTIVE under a row lock, so this is race-safe).

        `rate_coins_per_kwh` is the session's SNAPSHOTTED rate (see
        services/pricing.py resolve_rate_for_plug / models.py
        ChargingSession.rate_coins_per_kwh) — the accrued cost must use the
        same rate this session will actually be billed at, not the global
        default, or a tariff'd session could auto-stop too early/late. NULL
        (legacy sessions with no snapshot) falls back to the env default.

        [Auth holds] `hold_coins` is the session's own authorization-hold
        reservation (ChargingSession.hold_coins, snapshotted at start — see
        routers/sessions.py start_charging_session / services/wallet.py
        available_balance). When set, THIS — not the driver's whole wallet
        balance — is the exhaustion threshold: a concurrent second session
        may be holding the rest of that balance, so this session must only
        auto-stop when it exhausts its OWN reservation, never a figure a
        sibling session is also counting on. No DB read is needed in that
        case (the threshold is already in hand). NULL (legacy sessions
        predating this column) falls back to the live wallet balance,
        matching the pre-hold behavior exactly."""
        from backend.services.mqtt_manager import (
            AUTO_STOP_ON_BALANCE_EXHAUSTED,
            LOW_BALANCE_WARN_FRACTION,
        )

        if not AUTO_STOP_ON_BALANCE_EXHAUSTED or not self.db_session_factory:
            return
        from types import SimpleNamespace

        from backend.services.billing import session_cost
        from backend.services.money import to_money
        from backend.services.telemetry import COINS_PER_KWH

        try:
            # [Pricing v2] Accrue against the session's segment state (settled
            # closed-segment coins + open segment at the current rate) so a
            # session that crossed a TOD boundary stops at the RIGHT accrued
            # cost, not a flat re-multiply. NULL segment fields (flat/legacy)
            # collapse session_cost to energy_cost(energy_kwh, rate) exactly.
            accrued_cost = session_cost(
                SimpleNamespace(
                    rate_coins_per_kwh=rate_coins_per_kwh,
                    settled_cost_coins=settled_cost_coins,
                    rate_segment_start_kwh=rate_segment_start_kwh,
                ),
                energy_kwh,
            )
            if accrued_cost <= 0:
                return
            if hold_coins is not None:
                threshold = hold_coins
            else:
                from sqlalchemy import select

                from backend.database.models import User
                async with self.db_session_factory() as db:
                    user = (await db.execute(
                        select(User).where(User.id == user_id)
                    )).scalar_one_or_none()
                    if user is None:
                        return
                    threshold = user.coin_balance
            if accrued_cost < threshold:
                # Still covered — maybe warn (once per session) as the cost
                # approaches the threshold, so the driver sees the auto-stop
                # coming even with the app closed.
                if (
                    LOW_BALANCE_WARN_FRACTION > 0
                    and float(accrued_cost) >= float(threshold) * LOW_BALANCE_WARN_FRACTION
                    and session_id not in self._low_balance_warned
                ):
                    if len(self._low_balance_warned) > 1000:
                        self._low_balance_warned.clear()
                    self._low_balance_warned.add(session_id)
                    remaining = to_money(threshold - accrued_cost)
                    kwh_left = float(remaining) / COINS_PER_KWH if COINS_PER_KWH else 0.0
                    from backend.services.notifications import notify
                    await notify(
                        user_id,
                        "low_balance",
                        "Balance running low",
                        f"Your current session has used most of your wallet — "
                        f"~{remaining:.2f} coins (≈{kwh_left:.2f} kWh) left before "
                        f"charging auto-stops. Top up to keep charging.",
                        severity="warning",
                        session_id=session_id,
                    )
                return
            # Threshold exhausted — stop through the shared finalize path
            # (own txn; row-locks + re-checks ACTIVE so a concurrent user stop
            # or the reaper settles this exactly once).
            from backend.services.session_lifecycle import finalize_charging_session
            reason = (
                "auto-stopped: session hold exhausted" if hold_coins is not None
                else "auto-stopped: wallet balance exhausted"
            )
            async with self.db_session_factory() as db:
                outcome = await finalize_charging_session(
                    db, session_id, reason=reason
                )
            self._low_balance_warned.discard(session_id)
            if outcome is not None:
                logger.warning(
                    "Auto-stopped session: exhaustion threshold reached",
                    extra={
                        "session_id": session_id,
                        "user_id": user_id,
                        "energy_kwh": outcome["energy_kwh"],
                        "coins_spent": outcome["coins_spent"],
                        "reason": reason,
                    },
                )
        except Exception:
            logger.exception(
                "Balance-exhaustion auto-stop failed",
                extra={"session_id": session_id, "user_id": user_id},
            )

    async def _maybe_auto_stop_on_limits(self, session_id: int, user_id: int, energy_kwh: float,
                                          max_kwh: Optional[float] = None,
                                          max_duration_seconds: Optional[int] = None,
                                          started_at: Optional[datetime] = None):
        """[Session limits] Finalize an ACTIVE session once it reaches ITS OWN
        stop conditions (ChargingSession.max_kwh / max_duration_seconds,
        snapshotted from the start request — routers/sessions.py
        start_charging_session).

        Backend mirror of the firmware's local watchdogs: the gateway gets
        the same limits in the MQTT ON payload and cuts the relay when they
        trip, but publishes NO alarm for those cutoffs — so without this
        check the session would sit ACTIVE (plug pinned OCCUPIED, driver
        unbilled) until the staleness reaper. Telemetry arrives ~every 1 s
        during an active session, so this stops within ~a second of crossing
        the limit — usually BEFORE the firmware cutoff even matters.

        Same contract as _maybe_auto_stop_on_exhaustion above: runs in its
        own txn after the telemetry persist commit, goes through the shared
        finalize path (row-locked ACTIVE re-check → race-safe against a
        concurrent user stop / reaper / the exhaustion auto-stop), and is a
        no-op when disabled or when no limit has been reached. The energy
        limit is checked first: when both trip on one frame, "energy limit
        reached" is the truer reason (energy is measured; elapsed time is
        merely implied by it). NULL limits (legacy sessions predating the
        columns) disable the corresponding check — matching their pre-limit
        behavior exactly. A naive legacy started_at is treated as UTC (same
        convention as gateway_is_live / finalize_charging_session)."""
        from backend.services.mqtt_manager import AUTO_STOP_ON_LIMITS

        if not AUTO_STOP_ON_LIMITS or not self.db_session_factory:
            return

        reason: Optional[str] = None
        if max_kwh is not None and energy_kwh >= max_kwh:
            reason = "auto-stopped: energy limit reached"
        elif max_duration_seconds is not None and started_at is not None:
            started = started_at
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            elapsed_sec = (datetime.now(timezone.utc) - started).total_seconds()
            if elapsed_sec >= max_duration_seconds:
                reason = "auto-stopped: time limit reached"
        if reason is None:
            return

        try:
            from backend.services.session_lifecycle import finalize_charging_session
            async with self.db_session_factory() as db:
                outcome = await finalize_charging_session(db, session_id, reason=reason)
            if outcome is not None:
                logger.info(
                    "Auto-stopped session: user-set charging limit reached",
                    extra={
                        "session_id": session_id,
                        "user_id": user_id,
                        "energy_kwh": outcome["energy_kwh"],
                        "coins_spent": outcome["coins_spent"],
                        "max_kwh": max_kwh,
                        "max_duration_seconds": max_duration_seconds,
                        "reason": reason,
                    },
                )
        except Exception:
            logger.exception(
                "Charging-limit auto-stop failed",
                extra={"session_id": session_id, "user_id": user_id},
            )
