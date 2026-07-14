# Proposal: Per-plug power sensing + queue-charge-during-outage

Status: **Draft plan (not yet approved / not built)**
Author: dev + Claude
Date: 2026-07-14

---

## Context

A driver can start a charging session on a plug that has **no line power**, as long
as the plug's **gateway** is still online (e.g. the ESP32 gateway is USB-powered
off a switch on battery backup while the mains feeding the plug is out). The site
shows the plug as available, the ON command publishes into the void, and the driver
gets an `ACTIVE` session that never charges — it just sits until the staleness
reaper force-closes it as "telemetry lost."

Two things come out of this:

1. **A correctness bug (must fix):** session-start must not succeed on a plug that
   can't actually deliver power.
2. **A feature (the valuable part):** turn that same situation into a deliberate
   **"queue my charge during the outage → auto-start when power returns"** — after a
   CPO-configurable debounce so a 5-second line-test blip doesn't energize the plug.

### Root cause (verified in code)

Session start (`backend/routers/sessions.py:167-227`) gates on exactly two signals,
**neither of which reflects physical plug power**:

- `plug.status == AVAILABLE` — a **manual** DB flag. Set on session start/stop, by
  CPO action, or safety cutoffs. Nothing flips it based on line power. (`plug.status`
  transitions enumerated in `backend/database/models.py`; grep-confirmed none are
  telemetry-driven.)
- `gateway_is_live(gateway)` — `backend/services/session_lifecycle.py:64-79`. True iff
  `gateway.status == ONLINE` **and** `gateway.last_seen_at` within
  `GATEWAY_LIVENESS_WINDOW_SEC` (120s). This is a **per-gateway** signal.

**Why the bug is worse than a 120s window:** `gateway.last_seen_at` is bumped by *any*
telemetry frame from the gateway (throttled, `mqtt_manager.py:356-383`). A gateway can
host multiple plugs. If plug A loses power but plug B on the same gateway keeps
reporting, the gateway stays "live" and **plug A looks startable indefinitely**. Even
with a single plug there's a ~120s exploit window before liveness goes stale.

**Also confirmed:** `plug.last_seen_at` is written once at row creation and **never
updated** (`mqtt_manager._persist_telemetry` only writes `plug.current_power_w` at
`models.py`-backed line ~680). So there is **no working per-plug heartbeat today** —
the system cannot answer "is this specific plug powered right now."

### Why the feature is cheap to build

When a Tapo/Kasa plug loses mains, the agent's poll of it throws and it simply
**stops publishing telemetry for that plug** (`agent/amphive_agent/core.py:184-193` —
`get_state()` raises → `continue`, skipping `_publish_telemetry`). When power returns,
the poll succeeds and telemetry resumes. So:

- **"plug is powered"** = fresh per-plug telemetry within a short window.
- **"power restored, debounced"** = telemetry has flowed *continuously* for ≥ X minutes.

No firmware change required. The recurring worker already exists
(`backend/services/session_reaper.py`) and already has a near-identical sweep
(`reap_reservation_starts_once` — "a booking window just opened → act on it").

### Decisions locked (from the requester)

- **Scope:** produce this plan; do not build yet.
- **Config placement:** per-CPO default **plus** per-plug override (mirrors how
  tariffs and current-caps already work: `Tenant` default + `Plug` override).
- **Balance:** check at queue time **and** re-check at auto-start; do **not** lock
  funds.

---

## Design overview

```
Telemetry frame ──► mqtt_manager._persist_telemetry
                      • plug.last_telemetry_at = now
                      • plug.powered_since = now  (only on the resume edge:
                        prior last_telemetry_at was None or stale)

Session start ──► sessions.py start gate
                      • existing gateway_is_live check stays
                      • NEW: require plug_is_powered(plug); if not powered →
                        409 (points the driver at "queue" when the CPO allows it)

Queue endpoint ──► POST /api/sessions/queue
                      • validate: gateway online, plug currently UNpowered,
                        CPO has queued-charging enabled, balance ≥ min, not
                        already queued, under per-user queue cap
                      • create QueuedCharge(status=WAITING, expires_at)

Reaper tick (60s) ──► reap_queued_starts_once()   (NEW sweep)
                      for each WAITING queued charge (row-locked, re-checked):
                        • expired?                     → EXPIRED + notify
                        • plug_is_powered AND
                          now - powered_since ≥ delay? → begin session via the
                                                          shared start helper:
                             success → STARTED + link session + notify
                             balance/caps fail → EXPIRED/FAILED + notify
                        • else                         → leave WAITING (retry)
```

---

## Detailed changes

### 1. Per-plug power heartbeat (the foundation — fixes the "no heartbeat" gap)

**`backend/database/models.py` — `Plug`:** add two columns.
- `last_telemetry_at: datetime | None` — last per-plug telemetry frame (the working
  heartbeat we lack today; do **not** reuse the confusingly-named never-written
  `last_seen_at` — leave it alone to avoid churn, or deprecate in a follow-up).
- `powered_since: datetime | None` — set to "now" on the telemetry *resume edge*;
  the anchor for the continuous-power debounce.

**`backend/services/mqtt_manager.py` — `_persist_telemetry` (~line 616-680):** it
already loads the plug row and sets `current_power_w`. Add, using the value already
in hand before overwriting:
```python
prev = plug.last_telemetry_at
if prev is None or (now - prev) > timedelta(seconds=PLUG_POWER_STALE_SEC):
    plug.powered_since = now          # resume edge → reset the debounce anchor
plug.last_telemetry_at = now
```
New module constant `PLUG_POWER_STALE_SEC` (env-overridable, default ~90s — a few
missed polls; the agent poll interval `self._poll_s` is short, ~10-15s).

**`backend/services/session_lifecycle.py` (or a small `plug_power.py`):** add a pure
helper mirroring `gateway_is_live`:
```python
def plug_is_powered(plug, now=None) -> bool:
    if plug.last_telemetry_at is None: return False
    ts = plug.last_telemetry_at
    if ts.tzinfo is None: ts = ts.replace(tzinfo=timezone.utc)  # legacy-naive convention
    return (now or utcnow()) - ts <= timedelta(seconds=PLUG_POWER_STALE_SEC)
```

### 2. Safety fix at the start gate

**`backend/routers/sessions.py`** — after the existing `gateway_is_live` block
(line 223) add:
```python
if not plug_is_powered(plug):
    raise HTTPException(409, detail=(
        "This charger has no power right now. "
        + ("You can queue your charge to start automatically when power returns."
           if queued_charging_enabled(tenant, plug)
           else "Try again once power is restored.")))
```
This closes the multi-plug / 120s hole with a real per-plug check.

### 3. QueuedCharge model + lifecycle (mirror `PlugWatch` + `Reservation`)

**`backend/database/models.py`:** new enum + table (keep it a **separate table** — do
**not** add a `QUEUED` value to `SessionStatus`; that would thread new state through
billing, holds, analytics, and the reaper's ACTIVE-only queries. A separate table is
the lower-risk pattern, exactly like `Reservation` and `PlugWatch`).
```python
class QueuedChargeStatus(str, enum.Enum):
    WAITING = "waiting"; STARTED = "started"
    CANCELLED = "cancelled"; EXPIRED = "expired"; FAILED = "failed"

class QueuedCharge(Base):
    id, tenant_id (FK), user_id (FK), plug_id (FK)
    max_kwh, max_duration_seconds        # snapshot the driver's requested limits
    status = WAITING (default)
    created_at, expires_at               # expires_at = created_at + ttl
    started_session_id (nullable FK)     # link once started
    # unique partial index (plug_id, user_id) WHERE status = WAITING → one live queue
    # per (driver, plug); prevents duplicate enqueues.
```

### 4. Shared "begin session" helper (avoid duplicating start logic)

The start body (`sessions.py:229-364`: caps admission → rate resolve → hold sizing →
create `ChargingSession(ACTIVE)` → `plug.status = OCCUPIED` → commit → publish ON →
rollback-on-publish-failure) must run identically whether triggered by the driver's
`POST /start` or the reaper's auto-start.

**Extract** it into `backend/services/session_start.py::begin_active_session(db, user,
plug, gateway, max_kwh, max_duration_seconds, covering_reservation=None)` returning the
session (or raising the same typed failures). Refactor `start_charging_session` to call
it. The reaper's sweep calls the same helper, so billing/hold/caps logic can never
diverge between the two entry points. **This refactor is behavior-preserving and should
land as its own commit** with the existing `test_session_start_*` tests green.

### 5. Reaper sweep

**`backend/services/session_reaper.py`:**
- Register `reap_queued_starts_once()` in `_run()` (after the reservation sweep, line
  118), wrapped in the same try/except-log contract.
- Sweep: SELECT `WAITING` ids in one session; then **one txn per row**, `with_for_update()`
  + re-check `status == WAITING` (race-safe, same contract as the other sweeps):
  - `now >= expires_at` → `EXPIRED`, notify driver.
  - `plug_is_powered(plug)` **and** `now - plug.powered_since >= auto_start_delay(plug)`
    → call `begin_active_session(...)`:
    - success → `STARTED`, set `started_session_id`, notify "your queued charge started."
    - balance floor / caps / publish failure → `FAILED` (or `EXPIRED` for balance),
      notify with the reason. (Caps contention could alternatively leave it `WAITING`
      to retry next tick — decide in impl; default: fail fast + notify so the driver
      isn't left guessing.)
  - otherwise → leave `WAITING` (retry next tick).

### 6. CPO config (Tenant default + Plug override)

Follow the `max_current_a` precedent exactly (`Tenant`/`Plug` nullable column +
`cpo_update_plug`).

**Models:**
- `Tenant`: `queued_charging_enabled: bool = False`, `auto_start_delay_min: int = 2`,
  `queue_ttl_min: int = 720` (12h).
- `Plug`: `queued_charging_enabled: bool | None`, `auto_start_delay_min: int | None`
  (NULL = inherit tenant).
- Resolver helpers `queued_charging_enabled(tenant, plug)` /
  `auto_start_delay(plug)` / `queue_ttl(tenant, plug)` (plug override → tenant default).

**Endpoints (`backend/routers/cpo.py`):**
- Extend `cpo_update_plug` (line 476-553) + `CpoPlugUpdateRequest` (`schemas.py:297`)
  with the two nullable plug overrides (mirrors how `max_current_a` is applied at 527).
- Add a tenant-settings `PUT` (or extend the existing CPO profile update) for the three
  tenant-level fields, returned read-only in the CPO profile like `timezone`.

### 7. Driver-facing API

- `PlugResponse` (`schemas.py:105-152`): add `plug_powered: bool` (from
  `plug_is_powered`) and `queue_available: bool` (gateway online + plug unpowered +
  CPO enabled) so the UI can render the queue CTA. Populated in
  `GET /api/plugs/available` (`routers/plugs.py:146-232`) and `/api/plugs/{id}`.
- `POST /api/sessions/queue` — body `{plug_id, max_kwh, max_duration_seconds}`; validates
  gateway online, plug **unpowered**, CPO enabled, balance ≥ `MIN_START_BALANCE_COINS`
  (`available_balance`), per-user queue cap, no existing WAITING row; creates the
  `QueuedCharge`.
- `GET /api/sessions/queued` — the driver's WAITING queued charges (with `expires_at`).
- `DELETE /api/sessions/queue/{id}` — cancel (owner-only) → `CANCELLED`.

### 8. Frontend

- `frontend/src/utils/plugAvailability.js` — currently collapses `gateway_online === false
  || status === 'offline'` into `'offline'`. Add a distinct `'unpowered'` result when
  `gateway_online === true` but `plug_powered === false`, so the card can differentiate
  "charger offline" from "no power — queue available."
- `frontend/src/pages/Home.jsx` `renderPlugCard` (320-448) — for an `unpowered` +
  `queue_available` plug, show a **"Queue charge"** CTA (reusing `ChargeSetupModal` for
  the kWh/duration inputs) instead of the "Notify me when free" bell. Also gate the
  typed-Plug-ID path (`handleStartFromInput`, 547-563), which currently does **no**
  availability check.
- Queued-charge list + cancel + expiry countdown (small section on Home or in
  `SessionContext`), fed by `GET /api/sessions/queued`.
- Notifications: reuse `services/notifications.py notify()` (persistent feed +
  Socket.io user room + Web Push) for `queued`, `queued_charge_started`,
  `queued_charge_expired`, `queued_charge_failed` — the same plumbing `plug_watch`
  and the reprice path already use.
- CPO portal: add the enable toggle + delay + TTL to `CpoPlugs.jsx` edit form and a
  tenant-level settings control (`CpoSetup.jsx` or a settings page).

### 9. Migration

New `backend/migrations/versions/0020_queued_charge.py` (`down_revision =
"0019_current_caps"`), idempotent per the house style (`CREATE TABLE IF NOT EXISTS` /
`information_schema.columns` guards — see `0018`/`0019` headers):
- create `queued_charges` (+ the partial unique index);
- add `plugs.last_telemetry_at`, `plugs.powered_since`,
  `plugs.queued_charging_enabled`, `plugs.auto_start_delay_min`;
- add `tenants.queued_charging_enabled`, `tenants.auto_start_delay_min`,
  `tenants.queue_ttl_min`.
`test_migrations.py` diffs migrated schema against `models.py` (indexes included) —
keep them in lockstep.

---

## Edge cases & decisions

- **Multi-plug gateway:** the whole point — per-plug `last_telemetry_at`, not the
  gateway signal, decides power. A dead plug on a live gateway is now correctly unpowered.
- **Blip during line testing:** the `powered_since` reset-on-resume-edge + the
  `auto_start_delay` window means a 5s blip never satisfies "continuously powered for X
  min." A blip *during* the wait resets `powered_since`, restarting the debounce.
- **Balance drops before auto-start:** re-checked in `begin_active_session`; on failure
  the queued charge is `FAILED`/`EXPIRED` + the driver is notified (funds were never
  locked — the locked decision).
- **Concurrent-session cap:** enforced at auto-start (inside the shared helper's step-0
  cap check), **not** at queue time — a driver can queue while at their active-session
  cap; the start defers/fails per the cap.
- **Reservation interplay:** auto-start goes through the same helper, so a `BOOKED`
  window covering "now" still blocks/fulfils exactly as a manual start does.
- **Plug set to MAINTENANCE / removed while queued:** the WAITING row's re-check +
  `begin_active_session` gate will refuse; mark `FAILED` + notify.
- **TTL default:** `queue_ttl_min = 720` (12h) at tenant level; a queued charge that
  never sees restored power expires and notifies rather than lingering forever.

---

## Reconnect reconciliation & orphan-OFF interaction (⚠️ must coordinate)

The backend already has a reconnect-reconciliation path, and the queued-charge feature
**collides with it** — this must be handled or auto-start will be immediately undone.

### What exists today
- Gateway drop → broker fires the agent's retained LWT `{"status":"offline"}`
  (`agent/core.py:55-57`) → `_persist_gateway_status("offline")` marks it OFFLINE and
  notifies mid-session drivers (`mqtt_manager.py:1022-1061`).
- Gateway reconnect → `_persist_gateway_status("online")` calls
  **`_republish_off_for_orphaned_plugs`** (`mqtt_manager.py:1063-1108`): it sends `OFF`
  to every plug on that gateway that has **no `ACTIVE` session**. This is the safety net
  that kills a plug whose session was finalized (e.g. by the reaper) while the gateway
  was dark — otherwise the plug's crash-recovery resumes the relay ON with nobody billing
  (the 2026-07-07 incident the docstring cites).
- Telemetry for a non-`ACTIVE` session is silently dropped
  (`_persist_telemetry` where-clause requires `status == ACTIVE`,
  `mqtt_manager.py:687-697`) — a finalized session can't be resurrected or double-billed.

### The collision
A **`WAITING` queued charge is not an `ACTIVE` session.** Scenario B (gateway + plug both
lose power, power returns, the Tapo auto-resumes its relay, the gateway reboots and
reconnects) is *exactly* the situation the feature wants to allow — but
`_republish_off_for_orphaned_plugs` will see "no ACTIVE session" and **turn the plug
right back off**, fighting the intended debounced auto-start.

### Required change
Teach the orphan sweep about queued charges. In `_republish_off_for_orphaned_plugs`,
exclude a plug from the OFF list when it has a live `WAITING` `QueuedCharge` **and** it's
mid-debounce (`not plug_is_powered` yet, or `now - powered_since < auto_start_delay`) —
i.e. leave it OFF but do **not** clear it; the queue sweep will do a *proper* start
(hold, caps, ON with a fresh `session_id`) once the debounce elapses. Concretely:
`active_plug_ids` at `mqtt_manager.py:1093` becomes "plugs the backend intends to be
on" = ACTIVE-session plugs ∪ queued-and-eligible plugs. Note the OFF is still correct
*during* the debounce (we don't want a self-resumed relay charging unbilled before the
queue starts it), so the change is narrow: don't let orphan-OFF *cancel/ignore* the
queued charge, and let the queue sweep own the actual energize.

### Out-of-scope but related gaps (flag, don't fix here)
- **Unbilled offline tail:** on an outage longer than `SESSION_STALE_TIMEOUT_SEC`, the
  session is finalized but the relay keeps delivering energy until reconnect (orphan-OFF).
  The software agent has **no local kWh cutoff** (`core.py:121-130` — only the ESP32
  firmware enforces limits locally), so that tail is unbilled and unbounded by kWh. The
  duration backstop still finalizes the DB session on wall-clock but can't actuate an
  offline relay. Consider a separate follow-up (operator alert on orphan-OFF, and/or a
  local kWh limit in the agent).

---

## Testing / verification

**Unit / integration (pytest, repo-root `.venv`; DB-gated tests run in CI):**
- `plug_is_powered` freshness boundary; `powered_since` resume-edge reset logic in
  `_persist_telemetry` (gap → reset; continuous → unchanged).
- Start gate now 409s on an unpowered plug (extend `test_session_start_plug_status.py`).
- `reap_queued_starts_once`: not-yet-debounced (stays WAITING) → debounced (STARTED);
  expired → EXPIRED; balance-drop → FAILED; blip-mid-wait resets the clock.
- `begin_active_session` refactor: existing `test_session_start_*` stay green
  (behavior-preserving).
- Migration round-trip in `test_migrations.py`.

**End-to-end on the fake-plug rig (hardware-free):** `tools/fake_plug.py` is registered
as gateway `fakeplug-gw-01` / plug_id 2 (see project memory).
1. Set a short `auto_start_delay_min` on that plug for the test.
2. **Simulate outage:** stop the fake-plug process → per-plug telemetry stops → after
   `PLUG_POWER_STALE_SEC` the plug reads unpowered; confirm the driver UI shows "no
   power — queue available" and `POST /start` now 409s.
3. Queue a charge (driver `driver@amphive.test`, has 500 coins per memory).
4. **Restore power:** restart the fake plug → telemetry resumes, `powered_since` set.
5. Confirm the reaper auto-starts the session only after the delay elapses, links
   `started_session_id`, and the driver gets the `queued_charge_started` notification.
6. Confirm a <delay blip (stop/start quickly) does **not** trigger a start.
7. **Orphan-OFF coordination:** with a `WAITING` queued charge mid-debounce, force a
   gateway reconnect (restart the fake plug's agent) and confirm the queued charge is
   **not** cancelled and the plug isn't spuriously reset — then confirm the queue sweep
   still starts it cleanly once the debounce elapses. Also confirm a plug with **no**
   queued charge still gets orphan-OFF'd on reconnect (existing behavior preserved).

Do **not** run the stack/DB locally — exercise via the GCP VM per AGENTS.md /
`deploy/scripts/deploy.ps1`.

---

## Suggested build phasing (when approved)

1. **Foundation + safety fix (ship first):** per-plug heartbeat columns +
   `_persist_telemetry` change + `plug_is_powered` + start-gate 409 + `PlugResponse`
   `plug_powered`. Migration `0020` (columns only). Closes the bug on its own.
2. **`begin_active_session` refactor** (behavior-preserving, own commit).
3. **Queue backend:** `QueuedCharge` model + queue/list/cancel endpoints + reaper
   sweep + notifications + CPO config fields + the
   `_republish_off_for_orphaned_plugs` coordination (see Reconnect reconciliation).
4. **Frontend:** driver queue CTA + queued list, CPO config UI.

---

## Open questions (non-blocking; sensible defaults chosen above)

- Per-user cap on simultaneous WAITING queued charges (proposed: small, e.g. 2, matching
  `MAX_ACTIVE_SESSIONS_PER_USER`).
- On caps contention at auto-start: fail-fast + notify (chosen) vs. retry-until-TTL.
- Whether to surface queued charges to the CPO console (proposed: yes, read-only, later).
