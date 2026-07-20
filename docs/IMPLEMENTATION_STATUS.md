# AmpHive — Implementation Status & Discrepancies

*Verified against source on 2026-07-04. This page reconciles the aspirational
product specs ([requirements.md](../requirements.md), [features_list.md](../features_list.md))
with what the code actually does. Legend: ✅ works · 🟡 partial · 🟦 stub/mock ·
❌ not implemented.*

---

## 1. Status matrix

### Backend
| Capability | Status | Notes |
|------------|:------:|-------|
| REST API (auth, groups, plugs, sessions, payments, direct, CPO portal) | ✅ | 43 endpoints (2026-07-10: CPO events feed `GET /api/cpo/events` + `POST /api/cpo/events/{id}/ack`, unified `GET /api/wallet/ledger`, public `GET /api/config`, `GET /api/cpo/analytics/sessions.csv` export) — see [API_REFERENCE.md](API_REFERENCE.md) |
| JWT auth + bcrypt | ✅ | Env-configurable expiry (`JWT_EXPIRY_DAYS`, default 7); user loaded fresh per request. **Revocable** via the `token_version` epoch (`tv` claim, re-checked per request; `POST /api/auth/logout` bumps it — "log out everywhere"). **Rate-limited** (2026-07-11): login/register enforce a per-IP sliding window (`LOGIN_RATE_LIMIT` 10/60s, `REGISTER_RATE_LIMIT` 10/3600s → 429 + Retry-After; `services/rate_limit.py`) — closes SEC §8.6. |
| Role-based access control | ✅ | Enforced via `services/rbac.py` `require_role(...)` on all `/api/cpo/*` routes (checks the DB role, not just the token) |
| MQTT command publish (ON/OFF) | ✅ | QoS 1, 3 s wait |
| MQTT inbound telemetry/status handling | ✅ | Telemetry updates TelemetryStore and session DB (now incl. `voltage`/`relay`). Status updates gateway state in DB. |
| MQTT inbound alarm handling + CPO events feed | ✅ | Subscribes `amphive/gateways/+/alarms` (2026-07-10): `_handle_gateway_alarm` maps `{"error"\|"event"}` → severity, persists a `gateway_events` row (tenant resolved from the gateway), and broadcasts a `gateway_alarm` Socket.io event. CPO reads via `GET /api/cpo/events` (filters: `unacknowledged_only`, `severity`) and clears with `POST /api/cpo/events/{id}/ack`. **Verified live in prod 2026-07-10** (synthetic `UNAUTHORIZED_ON` → persisted event id, retrieved + acked via API). |
| Plug maintenance workflow (fault console) | ✅ | **2026-07-12**: a THERMAL_CUTOFF/OVERCURRENT_CUTOFF alarm now auto-enters the plug into MAINTENANCE (`MQTTManager._auto_enter_maintenance`; excludes `UNAUTHORIZED_ON` and OTA events; env-gate `AUTO_MAINTENANCE_ON_CRITICAL_ALARM`, default on) — session-start already 409s any non-AVAILABLE plug, so this blocks new sessions until an operator clears it. Operators drive enter/clear via `POST /api/cpo/plugs/{id}/maintenance` (`clear` 409s while an ACTIVE session exists); both paths audit (`plug.maintenance_enter`/`plug.maintenance_clear`) and broadcast the status flip like any other plug-status change. |
| Gateway reachability in the driver plug API | ✅ | `GET /api/plugs/available` and `/api/plugs/{id}` now return `gateway_online` (via `gateway_is_live`: ONLINE + `last_seen_at` within the liveness window), so the driver UI can flag an unreachable charger before a start attempt rather than only via a 409. **Verified in prod** (live gateways → true, never-connected gateway → false). |
| Gateway firmware-version tracking | ✅ | The `fw` field on the gateway's `online` status is now persisted to `gateways.firmware_version` (Alembic `0006_gateway_firmware_version`; LWT/offline never clobbers it), exposed by `GET /api/cpo/gateways`, and shown in the CPO plugs table (online dot + `fw <ver>`) so an operator can see which gateways need an OTA. |
| Live-stream staleness flag | ✅ | The Socket.io `telemetry` payload now carries `is_stale`/`age_sec` (age since the last MQTT write vs `TELEMETRY_STALE_AFTER_SEC`, default 15 s), plus `relay_on`/`voltage_v`, so the frontend shows a "reconnecting" banner instead of freezing on stale values. |
| Live telemetry / Socket.io | ✅ | Streams real telemetry from TelemetryStore via Socket.io (sole transport — the legacy SSE endpoint + `sse-starlette` dep were retired 2026-07-07). Automatically triggers the plug to report telemetry at 1s intervals when there are active listeners or an active session. **Correction 2026-07-05:** the stream task called a non-existent `await sio.get_participants(room=...)` API, which raised on every iteration and killed each stream before the first emit — the earlier "fully functional/verified" claim was wrong. Fixed (room-manager membership check) with a regression test in `backend/tests/test_socketio.py`. |
| Time-series telemetry persistence | ✅ | Persistent `telemetry_readings` table fed by a buffered background batch-flush from the MQTT handler (`services/telemetry_persistence.py`); queried by `GET /api/cpo/analytics/telemetry` via `date_trunc`. Plain Postgres (no TimescaleDB) — hypertables/continuous-aggregates noted as a future upgrade; row retention is env-driven (`TELEMETRY_RETENTION_DAYS=90` in prod as of 2026-07-07). Live Socket.io still uses the in-memory TelemetryStore. |
| Razorpay create-order + verify | ✅ | HMAC-verified; credits coins + ledger. Supports decimal INR amounts and coin balances (money columns are `Numeric(12,2)`/Decimal as of 2026-07-06). **2026-07-05:** `/verify` now credits the **Razorpay-confirmed** amount fetched server-side (the client-sent `amount_inr` is deprecated/ignored — it was previously trusted, allowing arbitrary wallet inflation). |
| Razorpay webhook auto-credit | ✅ | Credits coins on `payment.captured`; atomic + idempotent vs. `/verify` (dedupes on the UNIQUE `razorpay_payment_id` via `IntegrityError`). Money columns are `Numeric(12,2)`/Decimal (2026-07-06). |
| Wallet debit on stop + ledger | ✅ | Row-locked (`SELECT ... FOR UPDATE`) in stop/verify/webhook paths |
| Prepaid protection: auto-stop on balance exhaustion | ✅ | On each telemetry write, if the accrued energy cost (`kwh × rate`) reaches the session's exhaustion threshold, the session is auto-stopped via the shared `finalize_charging_session` path (own txn, row-locked, race-safe with a user stop / the reaper). Caps free charging past a drained wallet to ≤ one telemetry interval. **2026-07-12:** the threshold is now the session's own authorization hold (`ChargingSession.hold_coins`) when set, not the driver's whole wallet balance — a concurrent second session may be holding the rest of it (see the "Authorization hold" row below). Legacy `hold_coins IS NULL` sessions keep the old live-wallet-balance threshold. Env-toggle `AUTO_STOP_ON_BALANCE_EXHAUSTED` (default on). Tests in `test_mqtt_manager.py`, `test_auth_holds.py`. |
| Session-sized authorization hold (MARKET_GAP_ANALYSIS.md §3) | ✅ | **2026-07-12:** `POST /api/sessions/start` reserves `min(available_balance, max_kwh × rate)` onto `ChargingSession.hold_coins` (Alembic `0013_auth_holds`, nullable — NULL = legacy pre-hold session), replacing the old flat `MIN_START_BALANCE_COINS` floor. `available_balance` (`services/wallet.py`, a single aggregate query) is `coin_balance` minus the SUM of `hold_coins` across the user's OTHER ACTIVE sessions — computed under the same user-row lock the start path already takes, so concurrent starts can't double-reserve the same coins; a hold never touches `coin_balance` itself (logical reservation only, so the non-negative CHECK and every credit/debit path are unchanged). `finalize_charging_session` now debits `min(final_cost, hold_coins)`, closing the old forgiven-overage revenue leak (SECURITY.md §5) for every held session — unspent hold is simply released, no money moves. `/api/auth/me` and `GET /api/wallet/ledger` additionally expose `available_balance` alongside `coin_balance`. Legacy sessions (`hold_coins IS NULL`) finalize with the exact pre-hold behavior. Tests: `backend/tests/test_auth_holds.py`. |
| Driver notifications (feed + Socket.io + Web Push) | ✅ | 2026-07-11: `notifications`/`push_subscriptions` tables (Alembic `0007`), `services/notifications.py` (persist → Socket.io user room → VAPID Web Push via `pywebpush`, dead subscriptions pruned on 404/410). Emit points: every session stop (user/auto-stop/reaper/safety cutoff), low balance once per session (`LOW_BALANCE_WARN_FRACTION`, default 0.8), gateway-offline mid-session, top-up credit. Endpoints `/api/notifications*` (feed + read + push-subscription CRUD). **Behavior change:** `THERMAL_CUTOFF`/`OVERCURRENT_CUTOFF` alarms now finalize the plug's ACTIVE session immediately (bills recorded energy, frees the plug) instead of leaving it for the reaper. Tests in `test_notifications.py`. **Verified live in prod 2026-07-11** (migration `0007` applied at startup; billed fake-plug session #24 → `session_stopped` notification with the exact receipt figures (0.234 kWh / 1.17 coins / 497.79 left), unread_count 1 → read → 0; push public-key endpoint `enabled:true`). Browser-side push delivery needs a manual browser opt-in — not yet exercised. |
| "Notify me when free" plug watches | ✅ | **2026-07-12:** one-shot watch on an occupied/offline plug (the society/office "someone's on my charger" case). `plug_watches` table (Alembic `0014_plug_watches`; UNIQUE `(user_id, plug_id)` + per-plug index), armed/disarmed via `POST`/`DELETE /api/plugs/{id}/watch` (both idempotent; POST enforces the same private-group access rule as `GET /api/plugs/{id}` via the shared `ensure_plug_group_access` helper, and 409s only a plug that is startable *right now* — AVAILABLE with a live gateway; occupied/offline/maintenance are all watchable). When the plug flips back to AVAILABLE — `finalize_charging_session` (the just-stopped driver excluded) or the CPO maintenance-clear — `services/plug_watch.py notify_watchers_plug_available` sends each watcher a `plug_available` notification through the existing pipeline (feed + Socket.io + Web Push) and deletes exactly the fired rows (one-shot; never raises into the billing path, rolls the shared session back on failure). Plug list/detail responses carry a per-caller `watching: bool` (ONE extra query for the whole list — the endpoint stays N+1-free); the Home plug card shows a bell toggle on every non-startable plug (optimistic, reverts on failure, cleared live by the `plug_status→available` broadcast). Tests: `backend/tests/test_plug_watch.py` (24), Home bell cases in `Home.test.jsx`. |
| Per-CPO/per-site tariff model + time-of-day pricing | ✅ | New `Tariff` model (`tenant_id`, `name`, `price_per_kwh` `Numeric(12,2)`; Alembic `0010_tariffs`), resolved plug → its charger group → the tenant's default tariff → the legacy global `COINS_PER_KWH` env var (`services/pricing.py resolve_rate_for_plug`), SNAPSHOTTED onto `ChargingSession.rate_coins_per_kwh` at session start so a later tariff *edit/reassignment* never changes an in-flight/already-billed session. CPO CRUD + assign/unassign at `/api/cpo/tariffs*` (tenant-scoped; cross-tenant assignment rejected); `price_per_kwh` exposed on the plug list/detail API and the session receipt. **Pricing v2 — time-of-day + segmented billing (Phase 2 wired 2026-07-14, docs/PRICING_V2_SPEC.md):** a `Tariff` can now carry `tariff_slots` (minute-of-day windows in the tenant's timezone). `resolve_rate_window` returns the in-effect rate **and** the wall-clock boundary it next changes at; session start snapshots that boundary and sizes the auth hold at the worst-case rate over the session window (`max_rate_over_window`). All three billing paths bill via `services/billing.py session_cost` (frozen closed-segment cost + open segment at the current rate), and `reprice_session_if_due` closes a segment **forward-only** when the boundary passes — driven by the `_persist_telemetry` frame hook (~1 s), an operator-edit trigger *(still Phase 3)*, and the session-reaper backstop — emitting a `rate_changed` driver notification. A **flat tariff resolves no boundary → one segment → bills byte-identically to before** (safe to deploy before any slot exists). **Phase 4 shipped 2026-07-14:** operator TOD-slot editor at `/cpo/tariffs` (list/create/delete tariffs + add/remove `tariff_slots` sub-resources `GET/POST/PUT/DELETE /api/cpo/tariffs/{id}/slots`, window+overlap validated via `services/pricing.py slot_overlaps`; tenant timezone shown read-only from `GET /api/cpo/profile`), and driver current+next price — `PlugResponse.price_next_per_kwh`/`price_changes_at` (`resolve_price_display`, one slot-load per plug, no new N+1) rendered as the Home ribbon's "→ 6 @ 18:00" hint. **Phase 3 (2026-07-14):** an operator tariff/slot edit reprices already-ACTIVE tenant sessions forward-only — `services/pricing.py mark_tenant_sessions_for_reprice` stamps `rate_valid_until=now` from the tariff update/delete + slot create/update/delete endpoints (env `AUTO_REPRICE_ACTIVE_SESSIONS`, default on), and the frame hook/reaper then reprice + notify where the rate moved; PATCH-`/limits` sizes the hold at `max_rate_over_window`. (Tariff *reassignment* still uses the start snapshot by design.) Tests: `backend/tests/test_pricing.py`, `test_pricing_v2.py`, `test_telemetry_store.py`; frontend `CpoTariffs.test.jsx`, `Home.test.jsx`. |

| User-set charging limits + backend auto-stop ([MARKET_GAP_ANALYSIS.md](MARKET_GAP_ANALYSIS.md) §5 "Stop at target kWh") | ✅ | **2026-07-12:** `POST /api/sessions/start`'s `max_kwh`/`max_duration_seconds` (long accepted, forwarded to the gateway in the MQTT ON payload, and enforced by the firmware as local relay watchdogs — but never sent by the UI, and the firmware publishes NO alarm on those cutoffs, so a firmware cutoff left the session ACTIVE until the reaper) are now SNAPSHOTTED onto `ChargingSession.max_kwh`/`max_duration_seconds` (Alembic `0015_session_limits`, nullable — NULL = legacy pre-limit session, never limit-auto-stopped) and mirrored backend-side: on every telemetry persist, `MQTTManager._maybe_auto_stop_on_limits` finalizes via the shared `finalize_charging_session` path at `energy_kwh >= max_kwh` ("auto-stopped: energy limit reached") or elapsed `>= max_duration_seconds` ("auto-stopped: time limit reached") — telemetry is ~1 s apart during a session, so the stop lands within ~1 s of the limit; sequenced after the balance-exhaustion check (its reason wins a same-frame tie), race-safe via finalize's row-locked ACTIVE re-check. The session reaper adds a duration **backstop** sweep (`reap_time_limited_once`, same reason string). Env toggle `AUTO_STOP_ON_LIMITS` (default on, honored by both paths). Hold sizing already used the request's `max_kwh`, so a small user limit automatically shrinks the auth hold too. Limits exposed on the start response, `GET /api/sessions/active`, and the finalize receipt; the session-stopped notification maps the new reasons ("Charging stopped — your limit was reached", reason in the body). Tests: `backend/tests/test_session_limits.py`. |
| Session dispute / refund flow (coins-only) | ✅ | Driver files one at a time per session (`POST /api/sessions/{id}/dispute`, finished sessions only; a partial unique DB index backs the one-OPEN-dispute-per-session rule). The owning CPO reviews tenant-scoped (`GET /api/cpo/disputes`) and resolves (`POST /api/cpo/disputes/{id}/resolve`): approve credits the driver's wallet via `services/wallet.credit_wallet` + a reconciling `REFUND` ledger row (cumulative refunds capped at the session's `coins_spent`, row-locked against concurrent resolves); reject just records a note. No Razorpay money-out. Tests: `backend/tests/test_disputes.py`. **UI shipped 2026-07-16:** driver files via a "Report an issue" action on each History session row → `DisputeModal` (reason ≥10 chars, inline 409 for an already-open dispute); the owning operator reviews/resolves on the new `/cpo/disputes` page (status filter, Approve/Reject with confirm, inline error). Tests: `DisputeModal.test.jsx`, `CpoDisputes.test.jsx`, History dispute-modal case. |
| Direct Mode Tapo endpoints | ✅ | Gated by `DIRECT_MODE`; lib or relay mode |
| CPO payout / settlement ledger | ✅ | **Record-keeping only — no bank/UPI/payment-gateway integration** (money still moves out-of-band; the admin marks a payout PAID after the transfer). `Payout` model (Alembic `0009_payouts`) + `services/payouts.py` compute a tenant's gross/fee/net earnings (`SUM(coins_spent)` of its COMPLETED sessions, single aggregate query on the already-denormalized `charging_sessions.tenant_id`) and a rolling settlement watermark (`MAX(period_end)` over non-CANCELLED payouts). `GET /api/cpo/earnings` (lifetime + unsettled), `POST`/`GET /api/cpo/payouts` (request/list; 400 if nothing unsettled, 409 if a request is already pending, race-safe via a `SELECT ... FOR UPDATE` tenant-row lock so concurrent requests can't double-settle the same window), admin-only `POST .../mark_paid`, and owner-or-admin `POST .../cancel` (frees the window). **2026-07-12:** flipped 🟡→✅ — the CPO-portal Earnings & Payouts page shipped (see the Frontend table below), and all three payout money ops now write `payout.request`/`payout.mark_paid`/`payout.cancel` rows into the TD#26 audit trail (`services/audit.py`, tenant = the payout's tenant so an admin's action lands in the owning CPO's `GET /api/cpo/audit`; detail carries the window + gross/fee/net). Closes [requirements.md §5.1](../requirements.md)'s "withdraw earnings" promise at the record-keeping level; see [MARKET_GAP_ANALYSIS.md](MARKET_GAP_ANALYSIS.md) §1.6/§7. Tests: `backend/tests/test_payouts.py` (incl. audit assertions). |
| Plug reservations (bookable time slots) | ✅ | **2026-07-12** (feat/reservations — the first prospective client's private society/office use case): new `Reservation` model + `reservation_status` enum (Alembic `0016_reservations`, renumbered from 0014 at merge — `0014_plug_watches`/`0015_session_limits` landed first; `tenant_id` denormalized like `charging_sessions`; half-open `[start_at, end_at)` windows; index `(plug_id, start_at)`). **Free in v1 — no coin hold.** `POST /api/reservations` (same plug-access rule as session start; plug not MAINTENANCE; window 15 min–`RESERVATION_MAX_HOURS` (4), start ≤ `RESERVATION_MAX_ADVANCE_DAYS` (7) ahead / ≥ now−2 min; per-user cap `MAX_UPCOMING_RESERVATIONS_PER_USER` (2) of BOOKED-with-future-end → 409; `[start,end)` overlap vs BOOKED on the plug under a `SELECT ... FOR UPDATE` plug-row lock → 409; confirmation notification), `GET /api/reservations/my` (upcoming + last-20 history), `POST /api/reservations/{id}/cancel` (owner or owning tenant's cpo/admin — operator cancel notifies the driver; anyone else 404), `GET /api/plugs/{plug_id}/reservations` (the schedule, for anyone with plug access), tenant-scoped `GET /api/cpo/reservations`. **Session-start gate** (inside the plug-locked section of `start_charging_session`): lazy no-show expiry FIRST (`services/reservations.py expire_lapsed_reservations`, shared by the gate/overlap/list paths — a BOOKED row past `start_at + RESERVATION_NO_SHOW_GRACE_MIN` (15) flips EXPIRED, so no background sweep is needed), then a covering BOOKED window 409s a non-holder ("Plug is reserved until <end>") while the holder's start marks it FULFILLED + links `session_id` (reverted to BOOKED if the MQTT publish fails). Plug list/detail APIs carry `reserved_now`/`reserved_now_by_me`/`reserved_until`/`next_reservation` (list computes them in ONE grouped query — stays N+1-free). Reservations ≠ scheduled start — nothing switches the plug on at `start_at`. **2026-07-13 — the noted `SessionReaperService` follow-up shipped** (`reap_reservation_starts_once`, a third sweep alongside the stale/duration ones): when a window opens it nudges the holder once ("Your reservation has started", idempotent via the new nullable `Reservation.started_notified_at`, Alembic `0017_reservation_started`, claimed under a `SELECT … FOR UPDATE SKIP LOCKED` so a racing holder-start never blocks the janitor) **and force-stops any non-holder session still running on the plug** — the walk-up overrun the start gate structurally can't catch (that session started legally *before* the window; the gate only 409s *new* non-holder starts). The force-stop goes through the shared `finalize_charging_session` (own txn, row-locked, bills persisted energy, frees the plug) with an `"auto-stopped: plug reserved"` reason that routes finalize's stop notification to a "Charging stopped — plug reserved" title; env `RESERVATION_FORCE_STOP_WALKUP` (default on) gates the stop, the holder nudge fires regardless. To make that stop fair, `start_charging_session` also warns a walk-up up front (best-effort, off the plug-locked section) when it starts on a plug another member has booked within the session's projected window (`services/reservations.py next_conflicting_reservation`, env `RESERVATION_WALKUP_WARN_LOOKAHEAD_MIN` default 180). Tests: `backend/tests/test_reservations.py`, `backend/tests/test_reservation_reaper.py`. |
| Circuit caps + admission control | ✅ | **2026-07-14** (feat/caps-admission-control — backend foundation): two nullable amperage columns (Alembic `0019_current_caps`) — `plugs.max_current_a` (per-plug cap; NULL = env `DEFAULT_PLUG_CAP_A`, 16 A P110 cutoff) and `charger_groups.max_current_a` (the shared circuit/line capacity; NULL = no limit). `services/caps.py check_circuit_admission` gates `start_charging_session` under the plug lock (+ a group-row lock so concurrent same-circuit starts serialize): a start is admitted iff Σ(effective caps of the group's already-ACTIVE plugs) + the new plug's cap ≤ the circuit limit → else 409 with the free-amps figure. **Hard guarantee at the 16 A default** (a plug can't exceed its cap; no modulation — P110 is relay+meter); a SUB-16 A per-plug cap is admission-advisory until the firmware watchdog enforces it via an ON-payload current threshold (pending OTA). Env `ENFORCE_CIRCUIT_ADMISSION` (default on). Operator setters on `PUT /api/cpo/plugs/{id}` + `PUT /api/cpo/groups/{id}` (`max_current_a`, send 0 to clear); `GET /api/cpo/plugs` returns the plug cap and `GET /api/cpo/groups` returns `max_current_a` + live `current_load_a`. A ChargerGroup doubles as the circuit (one-society-one-line primary case; a dedicated Circuit entity is the upgrade path). **Operator UI shipped 2026-07-14** (feat/caps-operator-ui): the CpoGroups edit modal sets the circuit capacity (A) and each group card shows live `current_load_a / max_current_a`; the CpoPlugs edit modal sets the per-plug "Max current (A)" (blank = default) — both PUT `max_current_a` (0 clears). **Request-capacity flow shipped 2026-07-14** (feat/caps-request-capacity): a start refused by admission returns a structured `409 {code:"circuit_full"}` (the frontend api client now surfaces `err.code`), and `ChargeSetupModal` offers a **"Request capacity"** button → `POST /api/plugs/{id}/request-capacity` arms a one-shot `capacity_requests` row (Alembic `0020`, UNIQUE user+plug, mirrors `plug_watches`). When the circuit next has room — `finalize_charging_session` (a session ended) or the operator raising the cap (`PUT /api/cpo/groups`) — `services/capacity.py notify_capacity_available` fans out a `capacity_available` notification to every requester whose plug now fits and clears those rows (first-come-first-served; the start-time admission gate arbitrates the actual starts). Operators see demand on the Groups page (`pending_capacity_requests` count → "⚡ N waiting for capacity"). Only remaining: firmware sub-16 A per-plug enforcement (pending OTA — admission is a hard guarantee at the 16 A default regardless). **Operator-facing caveat (be explicit with CPOs):** lowering a plug's per-plug cap below the 16 A hardware default (e.g. 16 A → 8 A) is *admission math only* — it is NOT enforced on the P110, which still draws to its 16 A cutoff, so a shared circuit can be overloaded even though the operator set a lower cap. The circuit-protection guarantee is HARD only when every plug sits at the 16 A default; sub-default caps are advisory until the firmware watchdog OTA ships. The CpoPlugs / CpoGroups edit modals now surface this inline (feat/caps-honest-ui). Tests: `backend/tests/test_caps.py` (admission + fan-out), frontend `CpoPlugs.test.jsx`, `ChargeSetupModal.test.jsx`. |
| GST tax invoices | ✅ | **2026-07-12**: new `Invoice` model + four GST columns on `Tenant` (`gstin`, `legal_name`, `invoice_prefix`, `next_invoice_seq`; Alembic `0012_gst_invoices`). `services/invoices.py issue_invoice_for_session` is idempotent (one invoice per session, UNIQUE `session_id` + `IntegrityError` catch backs the pre-check, not just the pre-check itself) and computes an INCLUSIVE GST split off `ChargingSession.coins_spent` (`taxable_value = total / (1 + rate/100)`, `gst = total - taxable_value`, both via `to_money` so the two always foot to the cent; rate from env `GST_RATE_PCT`, default 18.0) with immutable seller (tenant legal name + GSTIN) and line (`energy_kwh`, rate) snapshots so a later `Tenant` edit never rewrites an issued invoice. `invoice_number` (`{tenant's invoice_prefix, or a tenant-scoped fallback if unset}-{FY}-{seq:05d}` — the fallback avoids two unconfigured tenants colliding on the same globally-unique number) is allocated under a `SELECT ... FOR UPDATE` on the tenant row so concurrent issues never collide. `GET /api/sessions/{id}/invoice` (driver who owns the session, or cpo/admin of the tenant; issues on first call; `?format=html` for a minimal printable copy, no PDF dependency) and tenant-scoped `GET /api/cpo/invoices`. **India intra-state GST only** — CGST/SGST vs. IGST is not split out (no geo-verification of driver/CPO state). **UI shipped 2026-07-16 (🟡→✅):** a CPO **Settings** page (`/cpo/settings`) configures GSTIN/legal name/invoice prefix (extended `PUT`/`GET /api/cpo/profile` — the seller fields were add-only there), a CPO **Invoices** page (`/cpo/invoices`) browses issued invoices with a per-row printable "View", and the driver **History** sessions table gains an "Invoice" action (authenticated raw-fetch of the `?format=html` copy, shown only for completed/paid sessions that billed > 0). Tests: `CpoSettings.test.jsx`, `CpoInvoices.test.jsx`, History invoice case, `test_invoices.py` profile-schema cases. See [MARKET_GAP_ANALYSIS.md](MARKET_GAP_ANALYSIS.md) §1.5/§3. |

### Frontend
| Capability | Status | Notes |
|------------|:------:|-------|
| Login/register, protected routes | ✅ | |
| Plug-ID start + available-plugs list | ✅ | **2026-07-12:** QR/deep-link start — visiting `/?plug=<id>` prefills the Plug ID input and scrolls/focuses it; still fully auth-gated (an anonymous visitor is redirected to `/login`, which now returns the driver to their original destination — including the query string — via router `state.from`, shared by `ProtectedRoute`/`CpoProtectedRoute`). An unknown/inaccessible id shows a small inline notice rather than blocking the page. No in-app camera scanning (per [requirements.md](../requirements.md) §4) — the QR (rendered CPO-side, see below) just encodes this URL for the phone's own camera. **Sectioned list (2026-07-12, later the same day):** the flat charger list is now collapsible sections — "Your chargers" (plugs whose group is non-public, via the new `is_private` on the plug APIs; the society/office primary use case) first, then "Public chargers" (collapsed by default when private ones exist), each header showing live per-status counts; plug cards also show the resolved `price_per_kwh`. |
| Driver reservations UI (Home) | ✅ | **2026-07-12** (feat/reservations): each plug card gets a "Reserve" action → `ReserveModal` (date + start time + 30 min/1 h/2 h/4 h duration presets → `POST /api/reservations` with ISO strings; the plug's upcoming windows are listed in the modal via `GET /api/plugs/{id}/reservations` so members book around each other; 409s — slot taken / cap reached — render inline). Cards show a "Reserved until HH:MM" badge (local time) when a booking covers now — distinct from occupied; the holder sees "Reserved for you" and the card stays startable, while a plug inside someone else's window stops inviting the start click (the server would 409 anyway). A "Your reservations" strip lists upcoming bookings (plug, window, cancel). Times render in the viewer's local timezone (`utils/reservationTime.js`). Tests: `ReserveModal.test.jsx`, `Home.test.jsx` ("Home — reservations"). |
| Live session monitor (Socket.io) | ✅ | Real Socket.io client with token-auth. **2026-07-10:** client-side ticking elapsed clock (no longer freezes between frames), a "reconnecting" staleness banner (server `is_stale` OR no frame for 15 s), voltage + actual-relay secondary line, and a per-plug `gateway_alarm` warning banner (e.g. unauthorized-on). Tests in `SessionMonitor.test.jsx`. |
| Driver plug list: gateway-offline UX | ✅ | Home marks a plug whose gateway is unreachable (`gateway_online === false`) as "charger offline", dims it, and disables start — no more blind 409s at session start. |
| CPO alert feed | ✅ | `CpoAlerts` on the dashboard fetches `GET /api/cpo/events?unacknowledged_only=true`, merges live `gateway_alarm` broadcasts, and dismisses via the ack endpoint. Severity-styled (critical/warning/info). |
| CPO fault console | ✅ | **2026-07-12**: new `/cpo/faults` page (sidebar link) — `GET /api/cpo/events` with severity + unacknowledged-only filters, a top strip of plugs currently in maintenance, acknowledge, and "Put in maintenance" / "Clear maintenance" on plug-scoped safety faults via the new maintenance endpoint. Live-updates by re-pulling on the existing `gateway_alarm` Socket.io broadcast (`useSession().alarms`, same mechanism as `CpoAlerts`) rather than polling. Tests in `CpoFaults.test.jsx`. |
| CPO earnings & payouts page | ✅ | **2026-07-12**: new `/cpo/earnings` page (sidebar "Earnings") — lifetime + unsettled gross/fee/net summary cards with the unsettled window dates (`GET /api/cpo/earnings`; coins labelled ₹-equivalent, 1 coin = ₹1), "Request payout" with a confirm dialog (backend 400 "nothing unsettled" / 409 "already pending" surfaced inline), and the payout history table (period, gross/fee/net, status badge, requested/paid timestamps) with Cancel on REQUESTED rows and an admin-only "Mark paid" (role from AuthContext's `/api/auth/me` user). Footnotes state the transfer itself happens outside the app (bank/UPI). Tests in `CpoEarnings.test.jsx`. |
| CPO reservations page | ✅ | **2026-07-13**: new `/cpo/reservations` page (sidebar "Reservations") over the tenant-scoped `GET /api/cpo/reservations` — a table of every booking (plug, driver name/email, window in the viewer's local timezone, status badge, linked session id) with a status filter (booked/fulfilled/cancelled/expired) + "Upcoming only" toggle and a live "N shown · M active" count. Operators cancel a still-BOOKED slot inline (`POST /api/reservations/{id}/cancel`, `window.confirm` first since the driver is notified; a 409 on a terminal-state row surfaces inline). Closes the reservations frontend follow-up (the driver-side Home UI already shipped 2026-07-12). Tests in `CpoReservations.test.jsx`. |
| CPO tariff & TOD-slot editor page | ✅ | **2026-07-14** (Pricing v2 Phase 4): new `/cpo/tariffs` page (sidebar "Tariffs") — list/create/delete tariffs (name + base coins/kWh), and per-tariff time-of-day slots managed inline (`GET/POST/DELETE /api/cpo/tariffs/{id}/slots`) via native `<input type="time">` (HH:MM→minute-of-day; a window ending at midnight maps to 1440). Backend validates start<end (400) and non-overlap (409, `services/pricing.py slot_overlaps`). The tenant timezone the slots are read in is surfaced read-only from `GET /api/cpo/profile`. **Per-weekday slots shipped 2026-07-16:** the add-slot form has Mon–Sun toggle chips → a `days_mask` (Mon=bit0…Sun=bit6, default every day; ≥1 day enforced client- + server-side) sent on create, and the slot list shows each slot's days ("Every day"/"Weekdays"/"Weekends"/day list). The backend already stored/validated `days_mask` (disjoint days don't conflict); the one gap closed alongside was `_slot_rate_and_bound` rolling its next-boundary forward to the next applicable weekday (so a session in an end-of-day gap or on a slot-less weekday still reprices when the next window opens — the driver "next price" ribbon stays same-day only). Tests in `CpoTariffs.test.jsx`, `test_pricing_v2.py`. |
| CPO session CSV export | ✅ | `GET /api/cpo/analytics/sessions.csv` (same filters as the JSON endpoint) → downloadable `text/csv`; "Export CSV" button on the Sessions page (authenticated fetch → blob). |
| CPO load analytics: current (amps) | ✅ | `/api/cpo/analytics/telemetry` now also aggregates `avg_current_a`/`max_current_a` per bucket; the dashboard load chart header shows peak W **and** A. |
| Razorpay top-up flow | ✅ | CDN script + `window.Razorpay`; key comes from backend order. Formats and displays decimal coin balances. |
| Pricing clarity | ✅ | `GET /api/config` (public) feeds a `ConfigProvider`; Home shows the tariff (`coins_per_kwh`) + what the driver's balance covers (≈ kWh) with a top-up nudge below the minimum, and the session monitor reads the rate from config instead of hardcoding it. The session-start minimum is now env-driven (`MIN_START_BALANCE_COINS`) and the 402 message matches the displayed number (2026-07-10). |
| Low-balance live warning | ✅ | The session monitor warns (with remaining coins ≈ kWh) as accrued cost nears the wallet balance, pairing with the backend auto-stop so the driver sees it coming. Tests in `SessionMonitor.test.jsx`. |
| Driver-set charging limit at start (kWh / time / ₹ coins) | ✅ | **2026-07-12:** collapsed-by-default "Set a charging limit (optional)" control on Home's start card (`ChargeLimitControl`) — quick presets + a custom value in kWh, hours, or ₹/coins; a coins cap is converted client-side to kWh at the **target plug's own `price_per_kwh`** (config `coins_per_kwh` fallback; conversion at send time via `utils/chargeLimits.js computeChargeLimits`, so a card-click start uses that plug's rate, and the derived kWh is previewed). Sent as `max_kwh`/`max_duration_seconds` through `SessionContext.startSession`; no limit chosen → nothing sent → backend defaults apply. The session monitor shows live progress toward the focused session's limits ("0.42 / 1.00 kWh · stops automatically", `focusedLimits` restored from `/api/sessions/active`), presented like the low-balance warning. Tests in `ChargeLimitControl.test.jsx`, `Home.test.jsx`, `SessionMonitor.test.jsx`, `SessionContext.test.jsx`. |
| Driver notification bell + Web Push opt-in | ✅ | 2026-07-11: navbar `NotificationBell` — unread badge, dropdown feed (`GET /api/notifications`), live prepend from the Socket.io `notification` user-room event, mark-read/mark-all, and an enable-push flow (permission → `sw.js` service worker → `pushManager.subscribe` with the backend-derived VAPID key). `frontend/public/sw.js` is push-only (no fetch interception). Tests in `NotificationBell.test.jsx`. |
| Post-session receipt | ✅ | `finalize_charging_session` now returns a full receipt (plug name, energy, peak power, duration, coins charged + any forgiven shortfall, balance before → after, timestamps, stop reason); the Session page shows a `SessionReceipt` card on stop (with an auto-stop notice when applicable) and refreshes the wallet. **Verified live end-to-end 2026-07-10** — a real billed session on the fake plug (0.101 kWh → 0.51 coins, balance 499.47 → 498.96, reconciled in the ledger). Tests in `SessionReceipt.test.jsx`. |
| Charger groups (join/list) | ✅ | |
| CPO operator portal (setup, dashboard, plugs, groups, sessions) | ✅ | `pages/cpo/*` behind `CpoProtectedRoute`; charts via `recharts` |
| Map of available plugs | ✅ | Leaflet/OpenStreetMap `MapComponent` on Home. Plug geolocation is now persisted (`Plug.latitude`/`longitude`, falling back to the gateway's coords); markers use real coordinates and plugs without a known location are omitted — the old `Math.random()` fallback (which also jittered markers on every re-render) is gone. **2026-07-12:** markers are now color-coded (Available/In use/Offline, `utils/plugAvailability.js`) with a legend showing live counts next to the map, plus availability and group-name filters (shared state) that narrow both the list and the map markers together. **Later the same day:** the map moved to the **bottom of Home** in a collapsed-by-default section (the product's primary society/office users know where their charger is; tiles aren't fetched until opened), plots **public plugs only**, and auto-fits its initial view to the plotted markers (falling back to the India-wide view when nothing has coords). There is still no power-rating field anywhere in the plug data model, so (unlike ChargePoint/PlugShare) power/connector/amenity filtering isn't offered — see [MARKET_GAP_ANALYSIS.md §2](MARKET_GAP_ANALYSIS.md). |
| Public charger discovery map (pre-signup) | ✅ | **2026-07-16:** a **public `/map` route** (not auth-gated) lets a visitor browse nearby **public** chargers before signing up (PlugShare-style discovery funnel). Backend: new UNAUTHENTICATED `GET /api/plugs/public` returning only public-group/ungrouped located plugs with a minimal safe projection (`{id,name,status,latitude,longitude,price_per_kwh,gateway_online}`) — private/society plugs are never exposed, and no per-user/session/network fields leak; rate-limited per IP (`PUBLIC_MAP_RATE_LIMIT`, default 60/60). Frontend: `PublicMap.jsx` reuses `MapComponent` (new optional `selectLabel` prop → "Sign up to charge"); every action routes to sign-in ("browse without an account, act with one"). A "🗺️ Map" navbar link (shown to everyone, incl. on `/login`) is the anonymous entry point. Tests: `PublicMap.test.jsx`, `backend/tests/test_public_plugs.py`. |
| CPO per-plug QR code | ✅ | **2026-07-12:** `/cpo/plugs` has a "QR" action per row rendering a printable QR (`qrcode.react`) for `{origin}/?plug=<id>` (origin read from `window.location.origin` at render time, never hardcoded) in a modal with the plug name/id and a print stylesheet limited to the code itself. |
| "View History" button (WalletCard) | ✅ | `WalletCard` button → `/history` route → `History.jsx`, now **tabbed**: "Charging Sessions" (`GET /api/sessions/history`) and "Wallet Ledger" (`GET /api/wallet/ledger`) — the latter is the unified money trail (top-up credits **and** session debits, signed amount + running `balance_after`), closing the old "debits-only" gap (2026-07-10). |
| CPO gateways management + OTA-from-UI | ✅ | New `/cpo/gateways` page (sidebar link) lists each gateway's status, reported firmware, last-seen, and plug count, with an "Update Firmware" action that POSTs `/api/cpo/gateways/{id}/ota` (https image URL; button disabled unless the gateway is online with ≥1 plug). Completes the OTA loop the fw-tracking surfaced (2026-07-10). |
| TypeScript usage | — | **Decided against 2026-07-07** (TD#14): toolchain removed; all app code is plain `.jsx`/`.js` by policy. ESLint now actually lints `js/jsx` (the old config matched only `ts,tsx` — zero files). |
| Frontend tests (Vitest + RTL) | ✅ | 127 tests: AuthContext, ProtectedRoute/CpoProtectedRoute (incl. the `state.from` redirect target), Login (returns to the preserved origin, incl. `?plug=`), TopUp payment handler (no client amount on `/verify`), multi-session SessionContext (incl. limit payloads + `focusedLimits` restore), History, Home (QR/deep-link prefill + auth gate + unknown-id notice, private/public sections + collapsed-map behavior, availability/group filters, legend + section counts, charging-limit payloads incl. per-plug coin conversion, notify-when-free bell: shown only on non-startable cards, optimistic POST/DELETE with revert-on-failure, cleared by a live `plug_status→available` flip, reservations: "Reserved until"/"Reserved for you" badge + blocked start click, Reserve modal opening, "Your reservations" strip + cancel), ReserveModal (schedule fetch/render, booking payload ISO strings + duration presets, inline 409, default preset), ChargeLimitControl (presets, coins→kWh conversion + clamping, previews), SessionMonitor limit-progress display, MapComponent (marker color per state, fit-bounds vs country-wide fallback), CpoPlugs QR modal, CpoEarnings (summary, request-payout incl. 409 path, cancel, history). `npm test` runs in CI. |

### Firmware
| Capability | Status | Notes |
|------------|:------:|-------|
| **Direct MQTT transport (fw ≥ 1.3.0, default)** | ✅ | `AMPHIVE_DIRECT_MQTT=1`: outbound `mqtts://8.231.81.12:8883` right after Wi-Fi — no overlay, NAT/CGNAT-immune, esp-mqtt owns reconnects. **Verified on-device 2026-07-10** (~3.3 s power-on→connected through a symmetric-NAT router; telemetry at 10 s cadence). Binary shrank ~50% (microlink linked out). |
| **Unauthorized physical-on guard (fw ≥ 1.5.0)** | ✅ | The relay ON with no active session (physical button / Tapo app / stale NVS resume) is forced OFF locally every poll and alarmed once per episode (`UNAUTHORIZED_ON`, rising-edge). Uses the plug's real `device_on` (previously read but discarded). **Live on the real gateway 2026-07-10** (OTA'd to `1.5.0-direct`); the backend ingests the alarm end-to-end (verified in prod). The remote out-of-band physical-press trigger itself is unit-tested + by-construction (no LAN path to press the button remotely). |
| **Richer telemetry: relay state + trapezoidal energy (fw ≥ 1.5.0)** | ✅ | Telemetry now carries the actual `relay` (device_on) state alongside derived `current`/nominal `voltage`; the driver-side kWh integrator switched from left-rectangle to the **trapezoidal rule** (averages consecutive power samples) for lower error on ramping loads at the 10 s cadence. **Verified on the wire 2026-07-10** (real gateway telemetry shows `"relay":false`). |
| `microlink` Tailscale client (Noise/ts2021, DERP, DISCO, STUN, WG) | 🟡 | **Demoted to legacy transport** (`AMPHIVE_DIRECT_MQTT=0`): works for full-cone NATs, but symmetric NAT defeats DISCO hole-punching (root-caused 2026-07-09) — the reason for the direct-MQTT pivot. Kept compilable for rollback/comparison. |
| MQTT control loop + topic contract | ✅ | Matches backend topics. **Multi-plug (TD#20), shipped fw 1.7.1-direct, verified on-device 2026-07-12**: `main.c` keeps a `plugs_mutex`-guarded per-plug slot table (each slot = DB `plug_id` + LAN IP + per-plug `tapo_plug_t` KLAP context + its own session/watchdog state) and `tapo_protocol.c` moved the KLAP session and energy integrator into that per-plug context, so a command for plug B can no longer actuate plug A and telemetry is published under each plug's own id. As of **fw 2.0.0-direct** the gateway learns each plug's IP from the backend's **retained plug roster** on `amphive/gateways/{gw}/config` (`handle_plug_roster` builds/reconciles the slot table on connect; the ON/OFF `local_ip` is now a refresh/fallback), so idle telemetry flows for every plug with no provisioned plug IP and no boot-time provisional slot (the removed slot's old job of keeping the liveness gate fresh — a 1.7.0 regression once fixed in 1.7.1 — is now the roster's). **Single-plug charging regression verified end-to-end on the real gateway** (OTA to 1.7.1, billed session: 0.014 kWh → 0.07 coins, ledger reconciled); the **2.0.0-direct roster path is verified live on the real gateway 2026-07-15** (OTA'd from 1.9.0; the plug is learned from `.../config` and idle telemetry flows — `last_seen` advancing), with **two-real-plug validation still pending** (single plug on the bench). (§3.50, §3.57, TD#20, SEC §8.5) |
| Captive portal provisioning | ✅ | `AmpHive_Setup_XXXX` → NVS → reboot. **As of fw 2.0.0-direct** it collects only Wi-Fi + Tapo account + per-gateway MQTT password — the "Target Plug IP" field was removed; plug IPs now arrive from the backend's retained roster (§3.57). **Locked down fw 1.6.0** (SEC §8.1 closed): WPA2 AP + `/save` gated by a per-device setup code (NVS-persisted, printed over serial for the unit label), AP-only interface, 10-min idle timeout → reboot/STA retry. **Live on the real gateway 2026-07-11** (OTA `1.5.0-direct` → signed `1.6.0-direct`, rollback cancelled); the locked portal itself is verified by construction/build only — exercising it needs physical access to force a Wi-Fi-loss fallback. |
| Edge watchdogs (duration/energy/thermal/over-current) | ✅ | Thermal + over-current now use the plug's `overheat_status`/`overcurrent_status` flags (the P110 has no °C sensor) |
| Over-current cutoff | ✅ | Enforced via the plug's `overcurrent_status` flag → local OFF + `OVERCURRENT_CUTOFF` alarm |
| Tapo P110 driver (KLAP/AES) | ✅ | **Real KLAP v2** (mbedTLS SHA/AES + `esp_http_client`); fully verified on-device; builds on **ESP-IDF v5.3** (not v6) |
| Session persistence in NVS / offline resync | ✅ | `session_nvs` module persists active session to NVS; `offline_log` ring buffer (64 entries) caches telemetry during MQTT outages; resync on reconnect |
| OTA updates | ✅ | Dual OTA app slots (`partitions_ota.csv`) + `esp_https_ota` with bootloader rollback (`ota_update.c`). Triggered by the `OTA` MQTT command / `POST /api/cpo/gateways/{id}/ota`; refuses mid-session; cancels rollback only once the new image re-reaches the broker. **Verified end-to-end on-device 2026-07-08** (`1.1.0 → 1.1.1`) and again **over the direct-MQTT path 2026-07-10** (`1.3.0 → 1.3.1`, image on a public URL, slot swap + `marking image valid`, no overlay). |
| OTA hardening: signed images + https-only | ✅ | **Rolled out 2026-07-10** (fw ≥ 1.4.0): ECDSA signed-app verification on update (`SECURE_SIGNED_ON_UPDATE_NO_SECURE_BOOT`, key gitignored), plain http refused by firmware + backend (`CpoGatewayOtaRequest` `^https://`, **deployed**), images on the public HTTPS bucket `gs://amphive-fw`. The real gateway `1cc3abb4fb54` was OTA'd end-to-end over the direct-MQTT path from `1.3.2-direct` → signed **`1.5.0-direct`** (`OTA_OK_REBOOTING` → offline → back online on 1.5.0, rollback cancelled). From 1.4.0 onward only signed images install. |
| Headscale (vs Tailscale defaults) | 🟡 | `/key` fetch supports it, but default host constants point at Tailscale |

### Infra / deploy
| Capability | Status | Notes |
|------------|:------:|-------|
| Docker Compose on GCP VM | ✅ | Live/canonical. **2026-07-12:** the frontend nginx now sends explicit `Cache-Control` — `no-cache` on `index.html`/`sw.js` (revalidate on every launch; previously *no* cache header meant browsers heuristic-cached the SPA shell, so phones kept launching stale bundles days after a deploy) and `public, max-age=31536000, immutable` on the content-hashed `/assets/*`. |
| `deploy.ps1` + `scripts/*.bat` helpers | ✅ | VM start/stop + remote compose/logs (in `scripts/`) |
| K8s/K3s manifests | — | **Retired 2026-07-07** (TD#15): banner-marked unmaintained reference (`deploy/k8s/README.md`); Compose-on-VM is the only deployment model |
| Web HTTPS (Caddy front door) | ✅ | **Deployed + verified in prod 2026-07-11.** `deploy.ps1` ships `docker-compose.tls.yml` by default (`-NoTls` = plain-HTTP rollback): Caddy is the only public web entrypoint on 80/443 with an auto-renewed Let's Encrypt cert for `CADDY_DOMAIN` (Caddyfile generated on the VM from `.env`); the frontend container publishes no host port; bare-IP/unknown-Host requests are *served* (not redirected) so a DNS-provider outage can't take the site down. The tls compose was brought to prod parity first (8883/passwd/ACL/cert mounts — it predated direct-MQTT and would have broken the broker). **Verified live**: `https://amphive.duckdns.org` 200 with a validated LE cert (CN match, valid to 2026-10-09), domain http→https 308, `/api` + Socket.io handshake over https, CPO login + gateway list; broker untouched — both gateways stayed online throughout. Rollout hit a real **DuckDNS authoritative-nameserver outage** (cert issuance blocked ~1 h; Caddy auto-retried it in — incident log: `deploy/docs/web_tls_rollout.md`). **Follow-ups closed 2026-07-11**: public `:8000` firewalled (tcp:80-only rule; VM-local access kept) + **HSTS** (max-age=31536000), verified live. Remaining: a **real domain** (DuckDNS is a demonstrated SPOF), then flip bare-IP serve→redirect. |
| Backups (DB dumps + config + disk snapshots) | ✅ | **Live + verified end-to-end 2026-07-11.** Nightly cron (21:00 UTC) on the VM runs `backup_db.sh` (shipped by `deploy.ps1`): `pg_dump -Fc` of `amphive` + ops-config tarball (`.env`, Caddyfile, mosquitto passwd/ACL — per-gateway hashes exist only on the VM — broker certs) → `gs://amphive-db-backups` (private, 30-day lifecycle), keeping the last 3 sets locally either way. Daily **disk snapshots** (policy `amphive-daily-snapshot`, 14-day retention) attached to the VM disk. VM uploads keylessly (scope raised to `devstorage.read_write`, ~48 s owner-approved downtime; bucket IAM = objectCreator+Viewer only — a **`gsutil rm` from the VM is denied**, so a compromised VM can't destroy backups). **Verified**: upload set in `gs://amphive-db-backups/2026/07/`; **restore tested** into a scratch DB (row counts matched live). Runbook + quarterly drill: `deploy/docs/db_backup_restore.md`. |
| Mosquitto broker | ✅ | Auth enforced (`allow_anonymous false` + passwd file) **+ topic ACLs** (2026-07-10). **8883 TLS is PUBLIC — the primary "direct MQTT" transport** (devices dial outbound `mqtts://8.231.81.12:8883`, NAT-immune; per-gateway accounts scoped to `amphive/gateways/<id>/#` via `add_gateway_user.ps1`). Overlay-bound plaintext 1883 stays as the legacy/transition path. Verified in prod: ESP32 fw 1.3.0-direct connected from the public internet through a symmetric-NAT router in ~3.3 s power-on→connected; ACL isolation cross-checked. See SECURITY.md §3, MQTT_CONTRACT.md. |

---

## 2. End-to-end reality check

- **Path A (ESP32 + MQTT)** is the operating path and has now been run
  **end-to-end on physical hardware** (2026-07-06): a real ESP32 gateway + P110
  drove a billed session over MQTT — the plug delivered the correct energy and
  real telemetry flowed through TelemetryStore → DB → the live stream →
  wallet debit (`DIRECT_MODE=false`).
- **Billing correction (2026-07-06, verified on-device):** the first hardware run
  surfaced a **session overbilling bug** — the firmware published its *lifetime*
  energy integrator as the telemetry `kwh`, so every session after the first billed
  the plug's entire accumulated history (`kwh × COINS_PER_KWH`). The firmware now
  reports **session-relative** energy (`meter − session_baseline`, the same value
  the watchdog uses); idle reports 0. **Reflashed and re-verified later the same
  day** (ESP-IDF v5.3.3 toolchain now installed at `C:\esp\v5.3.3`): consecutive
  billed sessions (#77–79) each started at `kwh = 0.0000` — the second/third did
  **not** inherit the first's accrual — and raw broker payloads confirmed both the
  session-relative `kwh` and the `session_id` echo on the wire.
  Also fixed alongside: the inbound telemetry `TelemetryStore.update()` was called
  from the paho thread, invoking `asyncio.Event.set()` cross-thread; it is now
  marshaled onto the event loop (`tests/test_mqtt_manager.py`).
- **Operational gotcha found during the 2026-07-06 reflash:** the gateway's NVS
  held the **pre-rotation Tapo password** (secrets were rotated 2026-07-06 after
  the board was provisioned), so every KLAP handshake failed with `handshake1
  auth mismatch` until the `tapo_pwd` NVS key was rewritten (done via a one-off
  fixer app; no NVS erase / re-provisioning needed). Rotating the Tapo account
  password **always** requires updating the provisioned copy on each gateway.
- **Path B (Direct Mode + WireGuard relay)** has been **retired** — the WireGuard
  tunnel is no longer used. `tapo_direct` and the `/direct/*` endpoints remain in
  code but are dormant (`DIRECT_MODE=false` makes them return 503).

## 3. Full discrepancy list (doc says X → code does Y)

1. [Resolved 2026-07-02] The README API section now summarizes all **35**
   endpoints (including the CPO portal) and links to [API_REFERENCE.md](API_REFERENCE.md).
2. [Partly resolved 2026-07-02] Time-series telemetry **is** now persisted to the
   `telemetry_readings` table via a buffered batch-flush from the MQTT handler
   (`backend/services/telemetry_persistence.py`), queried by
   `GET /api/cpo/analytics/telemetry`. **TimescaleDB specifically** is still not
   used — plain Postgres + `date_trunc` aggregation was chosen deliberately;
   hypertables/native-retention/continuous-aggregates remain a possible future
   upgrade.
3. [Resolved] ~~LWT offline alerts on the backend~~ — the README no longer
   makes this claim (LWT is published by the *firmware*; the backend persists
   gateway status when received).
4. [Resolved] ~~Python version mismatch~~ — README now says 3.11, matching
   the Dockerfile.
5. [Resolved 2026-07-07] ~~`schema.sql`/`schema_v2.sql` are not executed~~ —
   schema management moved to **Alembic** (`backend/migrations/`, frozen-DDL
   baseline `0001_baseline`); the drifted SQL files are deleted and
   `create_all` + `_INPLACE_UPGRADES` retired. Startup stamps pre-Alembic
   databases at the baseline, then upgrades to head. CI verifies
   baseline == models against real Postgres (`backend/tests/test_migrations.py`).
6. [Resolved 2026-07-02] RBAC is enforced. Self-registration still creates a
   `driver`, but a driver self-promotes to `cpo` via `POST /api/cpo/setup`, and
   `require_role("cpo","admin")` (`backend/services/rbac.py`) gates all `/api/cpo/*`
   routes against the live DB role.
7. [Resolved 2026-07-02] The unauthenticated `gateways/register` /
   `plugs/register` endpoints were removed; provisioning now goes through the
   RBAC-gated, tenant-scoped `POST /api/cpo/gateways` / `POST /api/cpo/plugs`.
8. [Resolved] ~~Direct Mode enabled in the committed config~~ — Path B is
   retired: `DIRECT_MODE=false` in `.env.template`, and the `/api/direct/*`
   endpoints return 503 (see §2).
9. [Resolved] `charging_sessions.peak_power_w` is now populated.
10. [Resolved 2026-07-02] Wallet updates are now row-locked (`SELECT ... FOR
    UPDATE`) in the stop, verify, and webhook paths.
11. [Resolved 2026-07-04] **Real-time Communication (Socket.io):** Replaced live SSE with Socket.io for session telemetry updates. Auth is verified using JWT token on connection (via auth payload or query parameters).
12. [Resolved 2026-07-06] A Leaflet/OpenStreetMap map (`MapComponent`) is on Home,
    and plug **geolocation is now in the data model** (`Plug.latitude`/`longitude`,
    with a fallback to the gateway's coords). Markers use real coordinates; plugs
    without a known location are omitted. The old `Math.random()` fallback — which
    also moved markers on every re-render — is gone.
13. [Resolved 2026-07-02] The dead `frontend/src/api/mockSse.js` leftover has been
    deleted.
14. [Resolved 2026-07-04] The firmware Tapo driver is now a **real KLAP v2**
    implementation (mbedTLS SHA/AES + `esp_http_client`), fully verified on-device.
    The project builds on **ESP-IDF v5.3.3** (v6.0.1 has breaking changes that cause a
    LoadProhibited panic on custom netif registration in `netif_callback_fn`).
15. [Resolved 2026-07-07] ~~No OTA / single-app partition table~~ — the
    partition table is now dual-OTA (`partitions_ota.csv`: `ota_0`/`ota_1` +
    `otadata`, NVS at its pre-OTA offset so provisioning survives the
    migration flash), and `esp_https_ota` + bootloader rollback are wired in
    (`ota_update.c`), triggered by the `OTA` MQTT command /
    `POST /api/cpo/gateways/{id}/ota`. **Verified end-to-end on-device
    2026-07-08** (1.1.0 → 1.1.1 over MQTT: download into `ota_1`, reboot,
    reconnect, rollback cancelled). Note: the overlay took ~3 min to
    re-establish after the OTA reboot (DERP/WireGuard re-handshake), during
    which the image stayed `PENDING_VERIFY` — correct behavior (commit only
    on broker reach), just a wide rollback-armed window.
16. [Resolved 2026-07-02] **NVS session register / offline telemetry resync** now
    implemented via `session_nvs.c` (persists active session params) and
    `offline_log.c` (64-entry NVS ring buffer). Watchdogs enforce limits locally
    even when MQTT is down; buffered telemetry is resynced on reconnect.
17. [Resolved 2026-07-03] **Over-current cutoff** is now enforced on the firmware via
    the plug's `overcurrent_status` flag (local OFF + `OVERCURRENT_CUTOFF` alarm).
18. **Telemetry topic shape** in `requirements.md` (`.../plugs/{id}/telemetry`)
    doesn't match the implemented per-gateway topic — but firmware & backend agree.
19. [Largely resolved 2026-07-08] ~~MQTT plain `mqtt://`, secured only by the
    overlay~~ — the broker now has a **TLS listener on 8883** and firmware
    ≥ 1.2.0 dials `mqtts://` (self-signed CA, chain + IP SAN validated). Not
    the spec's Noise, but transport is now TLS-encrypted + broker-authenticated
    (on top of the overlay). Plaintext 1883 stays up during the staged rollout.
20. [Resolved 2026-07-07] ~~K8s vs VM divergence~~ — moot: the manifests are
    **retired** (TD#15) and banner-marked as unmaintained reference in
    `deploy/k8s/README.md`, which records the divergence.
21. **Committed VM public IP differs across docs** — the IP is ephemeral (see
    [SECURITY.md](SECURITY.md)); stale literals now remain only in
    historical/retired runbooks (`wireguard_tunnel_setup.md`,
    `gcp_migration_runbook.md`), which are banner-marked as such.
22. [Resolved 2026-07-07] ~~Stale "EC2" wording in `deployment_checklist.md`~~
    — reworded to GCP.
23. [Resolved 2026-07-05] ~~mosquitto 9001 published but not served~~ — the
    port is no longer published (see §3.28 hardening).
24. [Resolved 2026-07-07] ~~Relay port mismatch~~ — `wireguard_tunnel_setup.md`
    now carries a RETIRED banner (Path B is gone) noting the correction:
    relay mode uses `relay_server.py` on `:8000` (`RELAY_PORT`); `:80` was
    lib mode's plug portproxy.
25. [Resolved 2026-07-07] ~~`frontend/README.md` is the stock Vite template~~
    — replaced with real package docs (stack, commands, env vars, layout,
    conventions; canonical docs stay in `docs/`).
26. [Resolved 2026-07-05] **Socket.io telemetry was non-functional** despite
    being documented as verified: `stream_telemetry_task` awaited a
    non-existent `sio.get_participants(room=...)` API and died before the
    first emit. Fixed via the room manager's registry; regression-tested.
27. [Resolved 2026-07-05] **`/api/payments/verify` trusted the client-sent
    `amount_inr`** (the checkout signature does not cover the amount). It now
    fetches and credits the Razorpay-confirmed captured amount, and rejects
    payments whose order was created for a different user.
28. [Changed 2026-07-05] Security hardening: JWT known-default secrets are
    refused (backend generates an ephemeral key; `deploy.ps1` aborts),
    committed credentials were stripped from `tools/`, `setup_duckdns.sh`,
    and `amphive_tunnel.conf` (now untracked, `.example` added), the DB
    password and MQTT bind interface are `.env`-driven
    (`POSTGRES_PASSWORD`, `MQTT_BIND_IP`), and mosquitto's unused 9001 port
    is no longer published. **Rotation of the burned secrets is still
    pending** — see [SECURITY.md](SECURITY.md).
29. [Resolved 2026-07-06] **CORS** locked to an explicit allowlist (wildcard
    removed) in `backend/main.py:187`; committed and deployed.
30. [Resolved 2026-07-06] **`stop_charging_session` ledger reconciliation.** The
    `max(0, …)` clamp used to forgive debt while still writing `amount = -final_cost`
    / `balance_after = 0`, so the ledger didn't reconcile. It now debits
    `min(final_cost, balance)` and records that same delta in `amount`,
    `balance_after`, and `coins_spent`; a forgiven shortfall is logged.
31. [Resolved 2026-07-06] **Money is now `Numeric(12,2)`** (Decimal), not `Float`,
    for `coin_balance`, `coins_spent`, ledger `amount`/`balance_after`. Wallet math
    routes through `services/money.to_money` (half-up, 2 dp); columns migrated in
    place via a guarded `ALTER … TYPE` in `db.py:_INPLACE_UPGRADES`. Energy/power
    stay `Float`. The DB-level non-negative-balance CHECK landed 2026-07-07 as
    Alembic revision `0002_wallet_non_negative` (the first post-baseline
    revision): clamps legacy negative rows to 0, then adds
    `ck_users_coin_balance_non_negative` (also declared on the `User` model).
32. [Resolved 2026-07-06] **`CpoSetup` redirect-during-render** replaced with a
    declarative `<Navigate … replace />` (the render-body `navigate()` triggered a
    React "update during render" warning and could loop under StrictMode).
33. [Resolved 2026-07-06, flashed + verified] **Firmware command parsing** moved
    from `strstr`/`sscanf` to cJSON, and the MQTT buffers were widened (topic 256,
    data 512) with an oversized/fragmented-payload guard, so a command carrying a
    `session_id` no longer truncates/corrupts. Verified on-device: ON commands
    carrying `session_id` parsed correctly through three E2E sessions (#77–79).
34. [Resolved 2026-07-06, flashed] **Firmware energy meter reset on reboot** —
    `s_energy_wh` now persists to NVS (restored on `tapo_init`, written throttled
    per 50 Wh), so post-reboot `consumed_kwh` no longer goes negative and the
    energy safety watchdog stays armed. Flashed and running; the cross-reboot
    restore itself hasn't been explicitly exercised yet (needs ≥ 50 Wh accrued to
    hit the throttled write — test sessions drew only ~3–9 W).
35. [Resolved 2026-07-06, flashed + verified] **Firmware billed the lifetime
    energy integrator, not the session.** `telemetry_task` published the raw
    monotonic `telemetry.energy_kwh` (a cross-reboot cumulative meter) as the
    telemetry `kwh`, while the backend bills that field as session energy. The
    first session on a fresh plug billed ~correctly; every subsequent one overbilled
    by the plug's entire history. Firmware now publishes session-relative energy
    (`telemetry.energy_kwh − active_session.start_energy_kwh`, clamped ≥ 0) in both
    the live payload and the offline-log buffer (`firmware/main/main.c`).
    **Verified on-device 2026-07-06:** sessions #77–79 each started at
    `kwh = 0.0000` despite prior accrual in the same boot (raw broker payloads
    checked via `mosquitto_sub`); idle telemetry reports 0; each session billed
    only its own energy.
36. [Resolved 2026-07-06] **Inbound telemetry crossed a thread boundary unsafely.**
    `_handle_gateway_telemetry` runs on the paho network thread and called
    `TelemetryStore.update()` inline, which calls `asyncio.Event.set()` to wake
    stream consumers — not thread-safe cross-thread (can miss wakeups / corrupt loop
    state). The store update is now marshaled onto the event loop via
    `loop.call_soon_threadsafe`, keeping the store single-threaded. Regression test:
    `backend/tests/test_mqtt_manager.py`.
37. [Resolved 2026-07-06, flashed] **Firmware energy
    integrator updated outside the KLAP mutex.** `tapo_get_telemetry` mutated
    `s_energy_wh`/`s_energy_last_tick`/`s_energy_persisted_wh` after releasing
    `s_mutex`; the telemetry task and the ON-handler baseline read can overlap and
    race the read-modify-write (double-count / drop a slice). The integrator update
    and throttled NVS persist now run inside the lock, with the kWh snapshotted for
    the caller (`firmware/main/tapo_protocol.c`).
38. [Resolved 2026-07-06] **`session_id` now round-trips on the live path.** The
    backend sends the DB session id (string) on `ON`; the firmware echoes it in
    telemetry; `_persist_telemetry` attributes the reading to that exact session
    (guarded ACTIVE + same plug), falling back to the plug's active session when
    absent. Previously the firmware parsed/persisted a `session_id` the backend
    never sent, so the crash-recovery field was always empty. Offline-resynced
    readings still attribute by `plug_id` (the NVS ring-buffer entry has no room
    for the id). Backend parsing regression-tested in `test_mqtt_manager.py`;
    **firmware echo verified on the wire 2026-07-06** (`mosquitto_sub` showed
    `"session_id":"79"` echoed in live telemetry; DB rows attributed to the
    correct sessions, with post-stop idle rows correctly NULL via the
    ACTIVE-session guard).
39. [Resolved 2026-07-06] **Login/`/me` crashed with `MultipleResultsFound` for
    users holding >1 ACTIVE session.** `check_and_speed_up_active_session`
    (called on every `/api/auth/login` and `/api/auth/me`) fetched "the" active
    session with `scalar_one_or_none()`, but nothing limits a user to one active
    session — a user with several (e.g. stale sessions on offline gateways) hit
    a 500 on login and session restore, locking them out of the app (observed in
    prod with 3 concurrent ACTIVE sessions). Now iterates `scalars().all()`.
    Regression tests: `backend/tests/test_active_session_speedup.py`. Related
    open gap: there is no backend session reaper — see [TODO.md](TODO.md).
40. [Resolved 2026-07-06] **Sessions could start against dead gateways and sit
    ACTIVE forever.** `send_plug_command` only confirms the broker PUBACK, so a
    start against a mock/offline gateway "succeeded", pinning the plug OCCUPIED
    with no telemetry and no way to time out server-side (prod sessions 72–76).
    `/api/sessions/start` now 409s unless `gateway_is_live`: status ONLINE
    **and** `last_seen_at` within `GATEWAY_LIVENESS_WINDOW_SEC` (default 120 s).
    Telemetry refreshes `last_seen_at` (throttled 1/min per gateway); the
    `Gateway.last_seen_at` `onupdate=now` hook was removed so unrelated row
    edits can't fake liveness. Tests: `backend/tests/test_gateway_liveness.py`.
41. [Resolved 2026-07-06] **Duplicate-insert races returned 500.**
    `/api/auth/register` and `/api/cpo/setup` did exists-check-then-insert; a
    concurrent duplicate slipped past the SELECT and surfaced the unique-index
    `IntegrityError` as a raw 500. Both now catch it at commit/flush, roll
    back, and return the same 400 as the sequential duplicate path (same
    pattern `_credit_topup` already used). Tests:
    `backend/tests/test_registration_races.py`.
44. [Resolved 2026-07-07] **Structural backlog cleared** in one round, all
    deployed + verified in prod: `main.py` split (2,384 → 221 lines; routes
    verbatim in `backend/routers/`, schemas in `schemas.py`, runtime handles
    in `state.py`, session helpers in `services/session_lifecycle.py`;
    OpenAPI parity 36 operations before/after) · N+1 queries eliminated in
    `get_my_groups`/`get_available_plugs`/`cpo_list_plugs`/
    `cpo_analytics_sessions` (driver endpoints verified byte-identical against
    prod baselines) · legacy SSE endpoint + `sse-starlette` retired ·
    `TELEMETRY_RETENTION_DAYS=90` set in prod. Remaining from that list:
    broker TLS only (deferred — the overlay already encrypts transport).
42. [Resolved 2026-07-06] **No backend session reaper** — a gateway dying
    mid-session left the session ACTIVE forever (the only timeout lived in the
    firmware, on the dead device). A lifespan-owned `SessionReaperService`
    (`services/session_reaper.py`) now auto-finalizes ACTIVE sessions with no
    telemetry for `SESSION_STALE_TIMEOUT_SEC` (default 300 s; sessions carry a
    `last_telemetry_at` stamp fed by `_persist_telemetry`, falling back to
    `started_at`). Reaped sessions bill persisted energy through the same
    `finalize_charging_session` used by `/api/sessions/stop`, which now locks
    the session row and re-checks ACTIVE — also fixing the pre-existing
    **double-stop double-debit race** (two concurrent stops both passed the
    unlocked ACTIVE check and each debited the wallet). Tests:
    `backend/tests/test_session_reaper.py`.
43. [Resolved 2026-07-07] **Zombie relay after gateway power loss.** OFF
    commands aren't retained, so a gateway that was dead when its session got
    finalized never received one — its `session_nvs` crash recovery resumed the
    session on reboot with the relay ON and nobody billing (observed on real
    hardware after the reaper finalized session 82). On a gateway `online`
    status, the backend now re-sends OFF (best-effort, no broker-ack wait) to
    each of that gateway's plugs lacking an ACTIVE session. Tests:
    `backend/tests/test_reconnect_off_republish.py`.
45. [Resolved 2026-07-07, deployed + verified in prod] **Concurrent-session
    policy decided: max 2 per user.** Nothing used to limit how many ACTIVE sessions a user could hold,
    while `/api/sessions/active` and the UI surfaced only the newest — older
    active sessions were unreachable/un-stoppable by the user (and >1 ACTIVE
    session previously crashed login, §3.39). `/api/sessions/start` now
    enforces `MAX_ACTIVE_SESSIONS_PER_USER` (env, default 2) with a 409,
    counting under a `SELECT … FOR UPDATE` user-row lock so two simultaneous
    starts serialize (lock order user → plug is consistent with the finalize
    path's session → user → plug; no cycle). `/api/sessions/active` returns
    all active sessions newest-first (the legacy top-level single-session
    fields mirror the newest entry), and the frontend tracks the full list:
    one Home banner per session, a Session-page switcher to refocus the live
    monitor, and stop acts on the focused session. Tests:
    `backend/tests/test_max_active_sessions.py`. **Verified in prod
    2026-07-07** (CI green; OpenAPI still 36 operations; seeded driver gets
    the new `{"active":false,"sessions":[]}` shape; the start path passes
    the cap check under the user lock; served frontend bundle carries the
    multi-session context). The 409-at-cap itself is unit-tested only — a
    live check would need two real sessions on the physical plug.
46. [Resolved 2026-07-09] **Wallet lost-update via the stale identity map.**
    The row-locked read-modify-write in `_credit_topup` and
    `finalize_charging_session` looked race-safe, but the request session
    already held the auth-loaded `User` (get_current_user shares the
    request's `db`), and SQLAlchemy returns that **cached instance without
    refreshing attributes** from a later `select(User).with_for_update()` —
    the lock was taken, the arithmetic ran on the balance as of auth time.
    A credit/debit committed between auth and the lock (webhook top-up
    during a stop request, reaper debit during /verify) was silently
    overwritten. Wallet writes are now DB-side atomics centralized in
    `services/wallet.py` (`credit_wallet`, `debit_wallet_clamped`); the
    logout `token_version` bump had the same shape and is also an atomic
    `UPDATE … RETURNING` now. Postgres-backed regression tests
    (stale-instance scenarios, duplicate-topup rollback, clamp, lock
    serialization): `backend/tests/test_wallet.py` — these are the first
    tests to use CI's postgres service for billing correctness (the purpose
    it was provisioned for).

### 2026-07-06 follow-up audit (statuses re-checked 2026-07-11)

*Found by a code audit on 2026-07-06. Cross-referenced to
[TECH_DEBT.md](TECH_DEBT.md) (`TD#n`) and [SECURITY.md](SECURITY.md) (`SEC §n`).
Several items were fixed by the 2026-07-08…11 work (PRs #4–#7) before this
audit merged; statuses below are as of 2026-07-11.*

47. **[Resolved 2026-07-10] Firmware safety alarms were dropped.** The firmware
    publishes `THERMAL_CUTOFF`/`OVERCURRENT_CUTOFF` (and, fw ≥ 1.5.0,
    `UNAUTHORIZED_ON`) to `amphive/gateways/{id}/alarms`, but `MQTTManager`
    subscribed only to `+/telemetry` + `+/status`, so cutoffs were unrecorded
    and un-alerted. Fixed: `+/alarms` is subscribed, alarms persist as
    `gateway_events` rows and broadcast to the CPO events feed
    (`GET /api/cpo/events` + ack) — see the alarm-handling row above.
    (TD#21)
48. **[Resolved 2026-07-11] Backend trusts the payload `plug_id`.**
    `_persist_telemetry` now verifies `plug.gateway_id == <topic gateway>` and
    drops (with a warning) readings claiming a foreign or nonexistent plug —
    the raw time-series enqueue was moved behind the same check, so neither
    session totals nor `telemetry_readings` history can be written across
    gateways. Residual: the in-memory live-stream store is still fed before
    the DB check (UI-only, no billing effect). (SEC §3, §8.5)
49. **[Resolved 2026-07-11] Unguarded telemetry casts.** `plug_id` is
    int-coerced and the `float(...)` casts in `_handle_gateway_telemetry` are
    guarded with a warn-and-drop path; non-finite values (NaN/inf) are
    rejected too. A malformed reading now logs a warning instead of throwing
    inside the paho callback. (TD#25)
50. **[Resolved 2026-07-12 — code-complete + builds clean, on-device verify
    pending] ESP32 firmware was single-plug.** Was: `main.c` (`target_plug_ip`,
    `active_session`, `active_plug_id`) and `tapo_protocol.c` (global `s_sess`,
    `s_energy_wh`) were single-instance, so commands for a second plug actuated
    the first and telemetry was published under the last-commanded id. Fixed:
    `main.c` now holds a `plugs_mutex`-guarded per-plug slot table and
    `tapo_protocol.c` exposes a per-plug `tapo_plug_t` context (its own KLAP
    session + NVS-persisted energy meter `wh_<plug_id>`); `session_nvs` persists
    **all** per-plug sessions (one atomic blob, each carrying `plug_id` +
    `local_ip`) so crash recovery restores every plug. The gateway learns a
    plug's IP from the `local_ip` the backend now ships on ON/OFF
    (`send_plug_command(..., local_ip=…)`) — no on-device roster *then* (later
    superseded by the backend-pushed retained roster, §3.57), keeping the
    per-gateway broker ACLs and the backend `plug.gateway_id` check intact
    (SEC §8.5). **Regression caught + fixed on-device 2026-07-12:** the first
    build (1.7.0) dropped the pre-multi-plug "poll the provisioned plug from
    boot" behaviour, so a session-less gateway published no idle telemetry and
    fell out of the liveness window (session starts 409'd "gateway offline").
    Fixed in **1.7.1** by pre-registering the provisioned plug at boot
    (provisional id `1`, corrected to the backend's real id by IP-adoption on the
    first command). **Single-plug path then verified end-to-end on the real
    gateway** (OTA `1.7.0`→`1.7.1`, telemetry resumed, billed session 0.014 kWh →
    0.07 coins, balance reconciled). Two-real-plug behaviour still needs a second
    unit. (TD#20, SEC §8.5)
51. **[Resolved 2026-07-11] Sessions startable on OFFLINE/MAINTENANCE plugs.**
    `start_charging_session` now 409s on any non-`AVAILABLE` status (OCCUPIED
    keeps its "in use" message; OFFLINE/MAINTENANCE get "out of service").
    Behavior change: new plugs default to OFFLINE, so a freshly registered
    plug must be set AVAILABLE in the CPO portal before its first session
    (finalize already resets used plugs to AVAILABLE). (TD#22)
52. **[Resolved 2026-07-16, fw 2.1.0-direct — on-device verify pending]
    Crash-recovery resets the duration watchdog.** `start_time_s` is tick-based
    (resets to 0 on reboot), so the recovered session's time cap used to restart
    from zero. Fixed by persisting elapsed-so-far: `session_params_t` gains
    `elapsed_s`, the telemetry task re-persists active sessions on a 30 s throttle
    (event-only persists left a long session at `elapsed_s=0`), and recovery
    restores `start_time_s = now − elapsed_s` (unsigned modular arithmetic recovers
    the true elapsed; if the session already overran, it trips on the first sweep).
    Worst-case overrun is now one throttle interval. Chose this over an SNTP
    wall-clock baseline (which would over-count power-off time). **fw
    2.2.0-direct** additionally persists the session's `max_current_a` (as
    `max_current_ma`) in the same blob, so crash recovery re-arms the
    OVERCURRENT_CAP watchdog at the session's own cap instead of the gateway
    default; the blob-size change safely voids pre-2.2.0 records (fail-closed
    load — and OTA is refused mid-session, so nothing is lost). The backend's
    `send_plug_limits` now also carries `max_current_a=effective_plug_cap(plug)`
    (from `PATCH /api/sessions/{id}/limits`), so a mid-session cap change lands
    on-device via SET_LIMITS. (TD#23)
53. **[Resolved 2026-07-16, fw 2.1.0-direct — on-device verify pending]
    Offline-resync telemetry can bill the wrong session.** The `offline_log` ring
    entry now stores a compact `uint32_t session_id` (18→22 B; `ring_meta_t` gains
    a `format_ver` so old-layout entries are cleared on init), and the resync
    payload echoes it plus `relay`/`offline:true`. The backend attributes each
    buffered reading to its exact session — dropping it if that session already
    finalized — and a new `is_offline` flag keeps a historical frame from tripping
    the REC-02 OFF-republish. Idle buffered frames omit the id + set `relay:false`
    so the idle guard drops them. Tests: `backend/tests/test_mqtt_manager.py`
    (resync attribution + stale-id inert). (TD#24)
54. **[Open, reduced scope] Device / provisioning security.** Still open:
    no flash-encryption (plaintext NVS secrets — Wi-Fi, Tapo account,
    per-gateway MQTT creds, setup code) and the boot-time fallback into the
    portal (now LOW — the portal itself is locked, see below). Since the
    audit: OTA images are **signed** (ECDSA verify-on-update) and HTTPS-only,
    the reusable-overlay-key + anonymous-broker item is **gone** (devices left
    the overlay for direct MQTT with per-gateway credentials + topic ACLs +
    TLS, 2026-07-10), and the **open setup AP + unauthenticated `/save` was
    closed in fw 1.6.0** (2026-07-11): WPA2 AP + per-device setup code on
    `/save` (constant-time check, 1 s throttle + 403 on mismatch), AP-only
    interface, 10-min idle timeout → reboot/STA retry. (SEC §8)
55. **[Partially resolved] Observability / onboarding polish.** Fixed since the
    audit: unified wallet ledger (endpoint + History tab, 2026-07-10 — TD#29),
    gateway staleness (read-time `gateway_is_live` + `gateway_online` in the
    driver API + session reaper — TD#27), the shared-`Event` latency nit
    (closed by retiring SSE 2026-07-07 — TD#32), registration validation
    (`EmailStr` + password rule, 2026-07-11 — TD#30), the portal input CSS
    (`box-sizing:border-box`, fw 1.6.0 — the `width:100%%` diagnosis was
    wrong: the HTML is printf-formatted, so `%%` already rendered as `%`),
    **backend structured logging** (2026-07-12 — TD#28, backend half):
    `backend/logging_config.py` installs a JSON-lines formatter on the root
    logger (`ts`/`level`/`logger`/`msg`/`correlation_id` + structured `extra`
    fields; env `LOG_LEVEL`/`LOG_FORMAT`), a `correlation_id` ContextVar +
    `logging.Filter` stamp every record, and a FastAPI middleware
    (`backend/main.py`) binds it from/echoes it to `X-Request-ID` per
    request — tracing an HTTP request through to the MQTT command it
    triggers for same-task calls (the paho callback thread and background
    services log `-`). Hot-path f-strings converted in `routers/auth.py`,
    `routers/sessions.py`, `services/session_lifecycle.py`,
    `services/mqtt_manager.py`. The broker log is now also mirrored to a file
    on the previously-unused `mosquitto_log` volume (durable across container
    recreation) with the primary stdout stream size/count-bounded via the
    compose `logging:` driver. Tests: `backend/tests/test_logging.py`.
    And the **CPO admin audit log** (2026-07-12 — TD#26): a new `audit_logs`
    table + `services/audit.py` (`record_audit`/`try_record_audit`, non-fatal
    — a write failure is logged, never breaks the admin action) records
    gateway/plug/group create, plug status change, group delete, and
    access-code regen with actor/tenant/target, readable via
    `GET /api/cpo/audit`. Gateway/plug **delete** are pre-named in the action
    taxonomy but have no endpoint yet to hook (no such CPO routes exist).
    The **portal Wi-Fi pre-check** (TD#31, second half) shipped in fw
    2.1.0-direct: `/save` briefly associates in AP+STA mode to the submitted
    SSID/password (fail-open — only a definite association failure blocks the
    save; ≤ 20 s bound; the single radio may briefly drop the installer's
    phone). The plug-IP half is moot — plug IPs come from the retained roster
    (fw ≥ 2.0.0), so the portal no longer collects them. Still open: firmware
    `ESP_LOGI` WARN/ERROR now forward to the `/logs` topic (fw 2.1.0), but the
    backend does not subscribe/persist them yet (TD#28, firmware half
    partially closed). (TD#26, TD#28, TD#31)
56. **[Resolved 2026-07-12] No session-sized authorization hold — overage
    forgiven past the wallet.** `/api/sessions/start` only checked a flat
    `MIN_START_BALANCE_COINS` floor, and `finalize_charging_session` billed
    `min(final_cost, live balance)` — so a session could start on ₹50 and
    rack up an arbitrarily larger bill, with everything past the wallet
    silently forgiven (MARKET_GAP_ANALYSIS.md §3; SECURITY.md §5). Fixed:
    the start path now reserves `min(available_balance, max_kwh × rate)`
    onto the new nullable `ChargingSession.hold_coins` (Alembic
    `0013_auth_holds`, chained onto `0011_disputes` — the actual head by the
    time this branch was cut, re-chaining the orchestrator's own note that a
    sibling "0011" migration was still in flight when this feature's task
    brief was written), and `finalize_charging_session` caps the debit at
    `min(final_cost, hold_coins)` — the revenue leak is closed for every
    held session. `available_balance` (`services/wallet.py`) — coin_balance
    minus what the user's OTHER active sessions already hold — is computed
    under the existing user-row lock, so two concurrent starts can't
    double-reserve the same coins; a hold is purely a read-time reservation
    and never touches `coin_balance` or its non-negative CHECK. The
    mqtt_manager balance-exhaustion auto-stop threshold switched from the
    whole wallet balance to the session's own hold for the same reason (a
    sibling session's balance isn't this session's to spend past). Legacy
    sessions (`hold_coins IS NULL`) keep the old behavior exactly — this is
    forward-only. Tests: `backend/tests/test_auth_holds.py`.
57. **[Done 2026-07-15 — shipped: PR #52 merged, backend deployed, fw
    `2.0.0-direct` OTA'd + verified live] Multi-plug was implicit — the captive
    portal collected one "Target Plug IP".** A gateway drives up to 4 P110s, but provisioning asked for a
    single plug IP and the firmware only learned additional plugs lazily from the
    `local_ip` on an ON/OFF command (plus a boot-time provisional slot for idle
    telemetry) — so multi-plug read as one-plug-per-ESP and duplicated
    `plugs.local_ip` on-device, going stale on a DHCP change. Fixed by a
    **backend-pushed retained plug roster** on `amphive/gateways/{gw}/config`
    (`{plug_id, local_ip, max_current_a}`, no name — keeps 4 plugs under the
    512 B inbound buffer): `MQTTManager.publish_plug_roster` /
    `_publish_roster_for_gateway` publish it on gateway `online`, after a plug
    create/update (`routers/cpo.py _publish_gateway_roster`), and after an agent
    discovery upsert. The firmware (`2.0.0-direct`) subscribes on connect and
    reconciles its slot table (`handle_plug_roster`: add / re-IP / flag-remove;
    the telemetry task frees a dropped plug via the new `tapo_plug_destroy`, never
    an active session), and the captive-portal plug-IP field, the `target_plug`
    NVS key, and the provisional boot slot were removed. Operators can now fix a
    plug's IP after DHCP drift (`local_ip` added to `PUT /api/cpo/plugs/{id}`).
    No broker ACL change (the topic is inside `amphive/gateways/%u/#`) and no DB
    migration. **Rollout is ordered: the backend must be deployed before the
    firmware OTA** — the firmware now relies on the retained roster for idle
    telemetry, the job the removed provisional boot slot used to do. **Shipped
    2026-07-15:** PR #52 merged (`6350350`), backend deployed, gateway
    `1cc3abb4fb54` OTA'd `1.9.0`→`2.0.0-direct` (built on ESP-IDF v5.3.3);
    `last_seen` then advanced on 2.0.0, confirming the device learned plug 1 from
    the retained roster and idle telemetry flows (validation with 2+ physical
    plugs still pending — only one plug is on the bench). Tests:
    `backend/tests/test_plug_roster.py` + the roster cases in
    `test_mqtt_manager.py`. (TD#20, SEC §8.5)
