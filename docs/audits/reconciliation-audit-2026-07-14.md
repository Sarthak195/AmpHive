# Reconciliation & billing-divergence audit — 2026-07-14

Status: **RESOLVED — `integration/reconciliation-audit-2026-07-14`.** Implemented 2026-07-14:
REC-01 (backend + agent), 02, 04, 05, 06, 07, 08, 09, 11, 13, 14, P1 fixed in code; REC-10 and
REC-12 accepted-and-documented per their own "or accept" directions; REC-03 addressed as
honest-UI only (on-device sub-16 A enforcement needs a signed firmware OTA — deferred). The
per-plug power foundation + start-gate safety fix (Phase 1 of the queued-charge proposal) landed
alongside. Original findings are preserved below unchanged. Each item is independent and assignable.
Author: dev + Claude

---

## Context

While investigating a reported bug — a driver can start a session on a plug that has
no line power because start gates on *gateway* liveness, not *plug* power (see
`docs/proposals/queued-charge-offline-plug.md`) — it became clear the bug is one
instance of a broader class. This audit sweeps the codebase for siblings of that
failure DNA:

- **Published ≠ executed** — the backend treats a command as done when only the MQTT
  broker publish was confirmed (`send_plug_command` returns `info.is_published()`).
- **Backend state ≠ physical reality** — a DB/UX transition assumes a device did
  something it never confirmed.
- **In-memory ≠ durable** — state that only lives in a process is lost or duplicated
  across a backend / service / agent restart.

The finding: the system is **well-defended transactionally** (money, DB locks) but
**thin on reconciliation** — exactly the seam the original bug sits in.

### Method

Three read-only audits run in parallel, each seeded with the two already-known issues
so they'd surface *new* ones:
1. Command-delivery divergence (stop/finalize, OTA, reprice, caps, auto-stops).
2. Restart / reconnect reconciliation & idempotency (in-memory state, double-actions,
   agent baseline, startup ordering).
3. Billing leaks & conflated signals (hold/clamp math, concurrency, energy attribution,
   payments/refunds).

### Explicitly excluded (already tracked elsewhere — do not re-file)

- **No per-plug power signal** at session start (start uses per-gateway
  `gateway_is_live`) — `docs/proposals/queued-charge-offline-plug.md`.
- **Unbilled offline tail** on long outages; the software agent has no local kWh cutoff
  — memory `gateway-offline-reconnect-reconciliation.md` + the proposal. *(Update
  2026-07-20: the agent-side half is closed — `agent/amphive_agent/core.py` now enforces
  `max_kwh`/`max_duration_seconds` locally, offline included.)*
- **Legacy `hold_coins IS NULL` sessions** keep the old whole-wallet exhaustion + forgiven
  overage — documented residual in `docs/SECURITY.md §5`.

---

## Summary

| ID | Finding | Tier | Confidence | Area |
|----|---------|------|-----------|------|
| REC-01 | Session energy written non-monotonically → under-billing | 1 | CONFIRMED (asymmetry) / PLAUSIBLE (trigger) | billing |
| REC-02 | Stop/auto-stop OFF is fire-once & unconfirmed; reconciliation only on reconnect edge | 1 | CONFIRMED | command / safety |
| REC-03 | Sub-default current caps not enforced on device → circuit overload | 1 | CONFIRMED (self-acknowledged) | caps / safety |
| REC-04 | Telemetry-path reprice + `rate_changed` notify run with no row lock | 2 | CONFIRMED (lock gap) / PLAUSIBLE (double-fire) | concurrency / billing |
| REC-05 | Idle frame (`session_id=""`, kWh 0) misattributed to current ACTIVE session | 2 | PLAUSIBLE | billing / reaper |
| REC-06 | `_republish_off_for_orphaned_plugs` snapshot race kills a newly-started session | 2 | PLAUSIBLE | command / reconnect |
| REC-07 | Editing only `max_duration` mid-session doesn't resize the auth hold | 2 | PLAUSIBLE | billing |
| REC-08 | Retained `offline` replay → duplicate "charger offline" notify | 3 | CONFIRMED | restart / UX |
| REC-09 | Retained `online` replay inflates `last_seen_at` liveness for 120s | 3 | PLAUSIBLE | restart / liveness |
| REC-10 | Backend restart resets `_low_balance_warned` → re-warn | 3 | CONFIRMED | restart / UX |
| REC-11 | Backend restart empties `TelemetryStore` mirror → live cost/timer diverge | 3 | CONFIRMED (display-only) | restart / display |
| REC-12 | Reservation "started" nudge skipped if backend down the whole window | 3 | PLAUSIBLE | restart / reservations |
| REC-13 | `MQTTManager` singleton could pin an unconfigured instance | 3 | PLAUSIBLE (latent) | startup |
| REC-14 | Dev-only `direct.py` energizes plug with no session and no role gate | 3 | CONFIRMED (low) | auth / billing |
| REC-P1 | `finalize`'s OFF uses `wait=True` (3s) on the event loop | perf | CONFIRMED | perf |

---

## Tier 1 — Real money / safety leaks

### REC-01 — Session energy is written non-monotonically → silent under-billing
- **Class:** backend state trusts a device value that can regress.
- **Location:** `backend/services/mqtt_manager.py:703` (`active_session.energy_kwh = kwh`,
  unconditional) vs. the *sibling* guarded write `:705` (`if watts > active_session.peak_power_w`).
  Telemetry is subscribed at **QoS 0** (`:134`). `finalize`'s `max(live, persisted)`
  (`backend/services/session_lifecycle.py:169`) does not help — both derive from the same
  overwritten field. TOD makes it worse: open-segment energy is
  `max(0.0, energy_kwh - rate_segment_start_kwh)` (`backend/services/billing.py:74`).
  Agent-restart trigger: baseline captured once at ON (`agent/amphive_agent/core.py:125-129`),
  used as `max(0.0, energy - baseline)` (`:198`); `energy_kwh` is lifetime/cumulative
  (`agent/amphive_agent/model.py:19`).
- **Failure scenario:** `energy_kwh` is assumed monotonic but each frame overwrites it.
  A late/duplicate QoS-0 frame with a lower kWh landing last-before-stop, a meter
  wrap/reset, or an agent re-baseline after a plug power-cycle sets `energy_kwh` to the
  lower value → the delivered-but-regressed energy is forgiven at finalize. Under a TOD
  segment, a reading below the segment start bills the open segment as 0.
- **Confidence:** CONFIRMED that the energy write is unguarded while the sibling peak-power
  write is explicitly guarded (the asymmetry is the tell); trigger frequency PLAUSIBLE.
  *Found independently by two of the three audits.*
- **Direction:** make it monotonic like peak power —
  `active_session.energy_kwh = max(active_session.energy_kwh or 0.0, kwh)` — and treat a
  real drop beyond an epsilon as a re-baseline event rather than a bill.

### REC-02 — Stop/auto-stop OFF is fire-once & unconfirmed; reconciliation is edge-triggered only on reconnect
- **Class:** state transition (session→COMPLETED, plug→AVAILABLE) assumes the relay went OFF.
- **Location:** `backend/services/session_lifecycle.py:136-149` (OFF best-effort, proceeds on
  failure), `:178` (COMPLETED) and `:256` (AVAILABLE) commit regardless; `send_plug_command`
  returns broker-only `is_published()` (`backend/services/mqtt_manager.py:1253-1271`). The
  only relay reconciliation is `_republish_off_for_orphaned_plugs` (`:1063-1108`), fired
  **only** on a gateway `online` transition (`:1019-1020`). The telemetry handler reads
  `relay_on` (`:257`, `:302`) for display only and drops frames for non-ACTIVE sessions
  (`:698-701`) without issuing a corrective OFF.
- **Failure scenario:** hold-exhaustion auto-stop finalizes at, say, 5 kWh (the firmware
  watchdog is 30 kWh). The OFF publish is broker-accepted but the gateway→plug actuation
  fails, or broker→gateway delivery is lost with the MQTT session still connected (no
  reconnect event). Session is billed & closed; the device still has `session_active=true`,
  so the firmware's UNAUTHORIZED_ON guard doesn't engage and the relay keeps delivering to
  the firmware watchdog — the 5→30 kWh delta is unbilled, and can bleed into the next user.
  No retry, no level-triggered sweep. (Distinct from the known offline-tail: this leaks even
  on a firmware device with a local cutoff, while the gateway stays online.)
- **Confidence:** CONFIRMED (traced).
- **Direction:** add a **level-triggered** reconciliation — a telemetry frame reporting
  `relay_on=true` for a plug with no ACTIVE session publishes OFF (idempotent, same as the
  reconnect path) — instead of firing that logic only on the reconnect edge.

### REC-03 — Sub-default current caps are admission-math only, never enforced on the device → circuit overload
- **Class:** DB/UX state (the cap and every admission decision on it) assumes a device
  current limit that was never sent or confirmed.
- **Location:** cap edits persist to DB only — `backend/routers/cpo.py:527-529` (plug),
  `:786-788` (group); admission trusts the DB value at `backend/services/caps.py:38-41` /
  `:64-108`; `caps.py:12-15` self-admits "firmware enforcement of a SUB-default plug cap is
  a pending OTA … admission-advisory below it." The firmware watchdog
  (`firmware/main/main.c:1015-1027`) enforces duration/kWh/thermal/hardware-overcurrent
  only — no per-plug current cap, and no command carries one.
- **Failure scenario:** operator lowers plug A from 16A→8A believing the shared circuit is
  protected, then admits more plugs because `circuit_load_a` counts A as 8A. A still draws
  to its 16A hardware cutoff → Σ real draw exceeds `group.max_current_a` → overload / breaker
  trip. The "hard guarantee" holds only at the default; below it it is silently advisory.
- **Confidence:** CONFIRMED (traced; self-acknowledged in `caps.py`).
- **Direction:** push the effective cap to the device and gate admission on ack, or make the
  operator UI state plainly that sub-default caps are advisory until the enforcement OTA ships.

---

## Tier 2 — Concurrency / correctness

### REC-04 — Telemetry-path reprice + its `rate_changed` notify run with NO row lock
- **Class:** double-action / divergence across concurrent service paths.
- **Location:** `backend/services/mqtt_manager.py:698-701` (session read via
  `scalar_one_or_none()`, **no** `with_for_update`), reprice `:716`, notify `:743-755`.
  Contrast the reaper reprice, which locks: `backend/services/session_reaper.py:267-275`.
  Every reaper sweep is correctly guarded; the telemetry path is not a sweep and does not lock.
- **Failure scenario:** a session crosses a TOD boundary; the crossing telemetry frame reads
  the session unlocked, closes the segment and post-commit emits `rate_changed`. If the 60s
  reprice sweep (or a second in-flight `_persist_telemetry` coroutine — each inbound frame is
  `run_coroutine_threadsafe`'d) overlaps, both read the pre-advance snapshot → the driver gets
  **two** `rate_changed` notifications and the two `close_out_segment` writes are
  last-writer-wins (one segment close lost / settled recomputed off a stale base — a small
  mis-bill).
- **Confidence:** CONFIRMED the telemetry path takes no lock while every peer does; PLAUSIBLE
  for the double-fire (needs the sweep tick or a second frame to overlap the crossing).
- **Direction:** take `with_for_update()` on the session in `_persist_telemetry` (as the
  reaper reprice already does), or gate the emit on a locked re-read.

### REC-05 — Idle frame (`session_id=""`, kWh 0) misattributed to the current ACTIVE session
- **Class:** an idle/misordered device frame attributed across a session boundary because the
  fallback lacks a session guard.
- **Location:** `backend/services/mqtt_manager.py:693-703` — with no `session_id` (`""`→`None`,
  parsed `:263-270`) the where-clause degrades to "the ACTIVE session on this plug", then
  `energy_kwh = kwh` (`:703`) and `last_telemetry_at` refresh (`:708`). The firmware emits
  `session_id=""`, `session_kwh=0.0` whenever its own `session_active` is false
  (`firmware/main/main.c:1044-1057`).
- **Failure scenario:** the device is idle while the backend holds a fresh ACTIVE session B on
  that plug (e.g. after REC-06, or B's ON was lost). Each idle frame matches B via the fallback,
  **overwrites `B.energy_kwh` to 0** and refreshes `last_telemetry_at` → the staleness reaper
  won't reap B. B is pinned OCCUPIED at 0 energy, effectively un-reapable. (The id-bearing case
  is safe — a late frame with the OLD id is dropped at `:687-692` because that session isn't
  ACTIVE.) REC-01's `max()` fixes the energy zeroing but **not** the staleness-clock refresh.
- **Confidence:** PLAUSIBLE (fallback match confirmed; needs the device-idle-while-ACTIVE state).
- **Direction:** ignore idle frames (status "available" / relay off) for energy *and* staleness,
  or require the frame's `session_id` to match before touching either field.

### REC-06 — `_republish_off_for_orphaned_plugs` snapshot race kills a newly-started session
- **Class:** a stale OFF (decided before a plug was re-claimed) applied to a new session; neither
  publisher nor firmware checks session_id on OFF.
- **Location:** `backend/services/mqtt_manager.py:1087-1099` — `active_plug_ids` is read inside
  one `async with`, the block closes (await boundary), then the loop publishes OFF to every plug
  not in that snapshot; the OFF carries no `session_id` and the firmware OFF handler
  (`firmware/main/main.c:786-798`) unconditionally clears `session_active` + powers off.
- **Failure scenario:** gateway reconnects; plug 1 snapshotted as orphaned. Before the loop
  reaches it, driver B starts on plug 1 (gateway now live) and publishes ON. The republished OFF
  lands after B's ON → relay off + `session_active=false`, but B stays ACTIVE/OCCUPIED. B can't
  charge and (per REC-05) idle frames keep it un-reapable.
- **Confidence:** PLAUSIBLE (narrow event-loop interleaving window).
- **Direction:** re-check "no ACTIVE session" immediately before each OFF (or carry+verify a
  session_id / relay-state precondition). **Also touches the queued-charge feature's orphan-OFF
  coordination — fix them together** (see the proposal's Reconnect-reconciliation section).

### REC-07 — Editing only `max_duration` mid-session doesn't resize the auth hold
- **Class:** X (overage-forgiven) with a Y flavor (`hold_coins` no longer means "worst-case
  cost over the window").
- **Location:** `backend/routers/sessions.py:544-563` — the hold recompute
  (`max_rate_over_window` + `min(headroom, energy_cost(max_kwh, max_rate))`) is inside
  `if "max_kwh" in updates:`; a PATCH changing only `max_duration_seconds` skips it. Finalize
  forgives via `min(final_cost, hold)` (`session_lifecycle.py:213`).
- **Failure scenario:** start with `max_duration=4h` over a rate-5 slot → `hold = max_kwh×5`.
  PATCH `max_duration→8h` pushes into a rate-20 slot; the hold isn't recomputed over the longer
  window → the difference is forgiven at finalize. Largely masked by the default
  `AUTO_STOP_ON_BALANCE_EXHAUSTED=true` (session stops early instead); unmasked when that toggle
  is off or for legacy null-hold sessions.
- **Confidence:** PLAUSIBLE.
- **Direction:** recompute `hold_coins` whenever `max_duration_seconds` changes too.

---

## Tier 3 — Restart hygiene / low severity

### REC-08 — Retained `offline` replay → duplicate "charger offline" notify `CONFIRMED`
`backend/services/mqtt_manager.py:1019-1022` calls `_notify_drivers_gateway_offline`
(`:1024-1061`) on *any* offline status, with no "was already offline" guard; subscriptions are
re-issued on every connect (`:130-140`). A backend/broker reconnect during an outage replays the
retained LWT `offline` → re-notifies every ACTIVE-session driver on that gateway. **Direction:**
notify only on a real ONLINE→OFFLINE transition (compare stored `Gateway.status` before overwrite).

### REC-09 — Retained `online` replay inflates `last_seen_at` liveness `PLAUSIBLE`
`mqtt_manager.py:997` stamps `last_seen_at = now` on any status message, including a retained
`online` replayed on reconnect; `gateway_is_live` (`session_lifecycle.py:64-79`) then reads fresh
for 120s. A connected-but-silent gateway (wedged poll loop) looks startable. Same class as the
known per-gateway-liveness conflation; self-heals in 120s. **Direction:** don't treat a bare
retained `online` as fresh liveness — require telemetry to bump it (or use the message timestamp).

### REC-10 — Backend restart resets `_low_balance_warned` → re-warn `CONFIRMED`
In-memory one-shot set `mqtt_manager.py:96` (used `:840-847`, cleared `:874`). A restart mid-zone
re-fires the "balance running low" warning once. Auto-stop itself is safe (re-reads DB under lock).
**Direction:** persist a `low_balance_warned_at` on the session, or accept the dup (warning only).

### REC-11 — Backend restart empties the `TelemetryStore` mirror → live cost/timer diverge `CONFIRMED`
Per-process dicts `telemetry.py:74-91` (`_session_rates/_settled/_segment_start/_start_times`) are
only populated by `start_session`; never rehydrated. After a restart an in-flight session's live
stream computes cost at the env-default rate (`:136-140`) and restarts the timer (`:143-146`).
Display-only — finalize reads the DB row. **Direction:** lazily hydrate from the ACTIVE session row
on the first post-restart frame.

### REC-12 — Reservation "started" nudge skipped if backend down the whole window `PLAUSIBLE`
`session_reaper.py:330-346` requires `start_at <= now AND end_at > now`. A backend outage spanning
the entire window means the reservation never becomes a candidate → holder gets no nudge and an
overrun isn't force-stopped by this path. The lazy no-show *expiry* is restart-safe
(`services/reservations.py:59-105`, runs on every read). **Direction:** accept, or process
just-lapsed windows within a grace on the first sweep after startup.

### REC-13 — `MQTTManager` singleton could pin an unconfigured instance `PLAUSIBLE (latent)`
`mqtt_manager.py:61-79` (`__new__` returns the existing instance; `__init__` early-returns if
already built). `session_lifecycle.py:59` constructs a no-arg `MQTTManager()`. Today lifespan
(`main.py:109-119`) builds the configured singleton first, so the no-arg call returns it — fine.
But any import-time/pre-lifespan `MQTTManager()` would pin an instance with `broker_host=localhost`,
no `db_session_factory`/`event_loop`/`telemetry_store`, and lifespan's real construction becomes a
no-op → telemetry silently discarded. **Direction:** make lifespan the sole constructor;
`set_plug_telemetry_interval` should read `state.mqtt_manager` instead of re-instantiating.

### REC-14 — Dev-only `direct.py` energizes a plug with no session and no role gate `CONFIRMED (low)`
`backend/routers/direct.py:86` (`direct_plug_on`) / `:119` (`direct_plug_off`) are gated only by
`DIRECT_MODE` env + `Depends(get_current_user)` (no `require_role`). They bypass the session flow,
so energy is unmetered/unbilled, and any authenticated driver could actuate the plug *if*
`DIRECT_MODE` were ever true in prod (defaults false, documented dev/test-only). **Direction:** gate
behind `require_role("admin")` and keep out of prod `.env`.

### REC-P1 — `finalize`'s OFF blocks the event loop `CONFIRMED (perf)`
`finalize_charging_session`'s OFF uses `send_plug_command(..., wait=True)` (default), which does
`info.wait_for_publish(timeout=3.0)` (`mqtt_manager.py:1263`), and finalize runs on the event loop
(stop endpoint, auto-stops, reaper). A slow/unreachable broker blocks the whole loop up to 3s per
stop. **Direction:** use `wait=False` there, like the reconnect path already does.

---

## Checked and found sound (negative space)

- **Concurrent same-user starts / balance races:** user-row `FOR UPDATE` in `sessions.py` step 0
  serializes starts; `available_balance` nets active holds; `credit_wallet`/`debit_wallet_clamped`
  lock the user row → no double-spend, consistent reads across a concurrent finalize.
- **Payments (`routers/payments.py`, `services/payments.py`):** idempotent on the unique
  `razorpay_payment_id` (fast-path + `IntegrityError` race), server-authoritative captured amount,
  order-owner check, DB-side `+=` credit, non-negative CHECK + clamped debit.
- **Dispute refunds (`routers/cpo.py:2106`):** dispute + session rows locked and re-checked,
  cumulative APPROVED refund capped at `session.coins_spent` — no double-credit.
- **Telemetry ownership / wrong-plug (`mqtt_manager.py:668`, `:687`):** `plug.gateway_id ==
  <topic gateway>` + session selection requiring `id AND plug_id AND ACTIVE` → a forged/foreign
  `session_id` drops the frame rather than misattributing.
- **Caps admission (`services/caps.py`):** group-row `FOR UPDATE` serializes same-circuit starts;
  the starting plug is excluded from the load sum → no over-admit. (Residual: the sum uses configured
  caps as a proxy for real draw — see REC-03.)
- **Reaper sweeps:** `reap_once`, `reap_time_limited_once`, `reap_reservation_starts_once`,
  `reap_reprice_once` are all lock+re-check guarded (`session_lifecycle.py:126-129`,
  `session_reaper.py:368`, `:275`) — idempotent against concurrent stops/frames.

---

## Suggested triage order

1. **REC-01** — nearly free (`max()`), stops a live revenue leak, highest confidence.
2. **REC-02 / REC-05 / REC-06** — cluster in the telemetry handler + orphan-OFF; a
   level-triggered reconciliation + session-guarded fallback addresses all three, and REC-06
   should land with the queued-charge feature.
3. **REC-04** — one lock in `_persist_telemetry`.
4. **REC-03** — larger (needs firmware/OTA or an honest-UI decision); safety-relevant.
5. **Tier 3** — batch as restart-hygiene cleanup (REC-08/09/10/11 share the retained-message /
   in-memory-state theme).
