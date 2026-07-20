# AmpHive — Backend API Reference

*Verified against `backend/` on 2026-07-02; endpoint list refreshed 2026-07-10.*

Routes live in `backend/routers/*.py` (`{auth,groups,plugs,sessions,payments,
direct,cpo,notifications,reservations}.py`), each an `APIRouter` mounted on the FastAPI `app` in
`backend/main.py` (the 2026-07-07 `main.py` split — TD#7). Every path is
hard-coded under `/api` (no router `prefix=`). The app title is
**"AmpHive Shared EV Charging API"**, version **2.0.0**.
Interactive docs: `http://<host>:8000/docs`.

- **Auth:** routes marked **JWT** require `Authorization: Bearer <token>`
  (FastAPI `HTTPBearer` → `get_current_user`). The user is loaded fresh from the
  DB on every request, so balance/role are always current.
- Routes marked **cpo/admin** additionally require the caller's DB role to be
  `cpo` or `admin`, enforced by `require_role(...)` (`backend/services/rbac.py`).
- **CORS:** explicit allowlist (localhost dev origins, `amphive.app`, `cpo.amphive.app`) —
  locked down 2026-07-06.
- **86 `@router` route decorators total** across 9 routers (see Swagger
  `/docs` for the live, authoritative list): auth (6), cpo (43), direct (5),
  groups (2), notifications (6), payments (4), plugs (6), reservations (4),
  sessions (10).
  (The legacy SSE endpoint `/api/sessions/live/{id}` was retired 2026-07-07 —
  live telemetry is Socket.io only. The CPO gateway OTA-trigger endpoint was
  added 2026-07-07; the `/api/auth/logout` revocation endpoint 2026-07-08; the
  CPO events feed + ack endpoints 2026-07-10; the CPO audit-log endpoint
  2026-07-12; the driver notifications router and plug reservations router
  were added 2026-07-11/2026-07-12 respectively — see [Driver
  notifications](#driver-notifications-routersnotificationspy-2026-07-11) and
  [Plug reservations](#plug-reservations-routersreservationspy-2026-07-12)
  below.)

---

## Health

| Method | Path | Auth | Response |
|--------|------|------|----------|
| GET | `/api/health` | none | `{"status":"healthy","service":"amphive-backend","version":"2.0.0"}` |
| GET | `/api/config` | none | Public pricing/config so the UI doesn't hardcode it: `{coins_per_kwh, min_start_balance_coins, coin_inr_rate, currency}`. `min_start_balance_coins` matches the 402 the session-start path enforces (`MIN_START_BALANCE_COINS`, env). |

## Authentication (`services/auth.py`)

| Method | Path | Auth | Body | Response |
|--------|------|------|------|----------|
| POST | `/api/auth/register` | none | `{email, password, full_name}` | `{token, user}` — creates a `driver`, `coin_balance=0`. 400 on duplicate email. |
| POST | `/api/auth/login` | none | `{email, password}` | `{token, user}` — 401 on bad credentials. |
| GET | `/api/auth/me` | JWT | — | `{id, email, full_name, role, coin_balance, available_balance}` — `available_balance` (added 2026-07-12) is `coin_balance` minus coins held by the driver's OTHER active sessions' authorization holds (`services/wallet.py available_balance`); additive, `coin_balance` unchanged |
| POST | `/api/auth/logout` | JWT | — | Revokes every token for the caller (bumps `users.token_version`; "log out everywhere") → `{status:"logged_out"}`. |
| POST | `/api/auth/forgot-password` | none | `{email}` | Issues a single-use reset token (SHA-256 digest stored in `password_reset_tokens`, `RESET_TOKEN_TTL_MIN` expiry, prior unused tokens voided) and emails `FRONTEND_ORIGIN/reset-password?token=...` via `services/email.py` (SMTP if `SMTP_HOST` set, else the link is logged at WARNING). **Always the same generic 200** — no account enumeration. Rate-limited (`FORGOT_PASSWORD_RATE_LIMIT`). |
| POST | `/api/auth/reset-password` | none | `{token, password}` | Consumes the token: 8-72 char rule (as registration), bcrypt rehash, bumps `users.token_version` (revokes every session), stamps the token used. Uniform 400 for unknown/expired/already-used tokens. Rate-limited (`RESET_PASSWORD_RATE_LIMIT`). → `{status:"password_reset"}` |

`user` object shape: `{id, email, full_name, role, coin_balance}` (where `coin_balance` is a float; the `/api/auth/register`/`/api/auth/login` `AuthResponse.user` dict predates `available_balance` and hasn't been extended to it — only `GET /api/auth/me` (`UserResponse`) carries the new field today).
Token: HS256 JWT, claims `sub`/`role`/`email`/`iat`/`exp`, **7-day** expiry.

## Charger groups

| Method | Path | Auth | Body | Behaviour |
|--------|------|------|------|-----------|
| POST | `/api/groups/join` | JWT | `{access_code}` | Join a private group by code. 404 if unknown, 400 if the group is public or already joined. → `{status:"joined", group_id, group_name}` |
| GET | `/api/groups/my` | JWT | — | All public groups + private groups the user joined, deduped → `[{id, name, is_public, plug_count}]` |

## Plugs

| Method | Path | Auth | Body/Params | Behaviour |
|--------|------|------|-------------|-----------|
| GET | `/api/plugs/available` | JWT | — | Plugs in accessible groups **or** ungrouped (`group_id IS NULL`, visible to all) → `[{id, name, status, current_power_w, plug_model, group_name?, gateway_online, is_private, watching, reserved_now, reserved_now_by_me, reserved_until, next_reservation}]` — `gateway_online: bool` (added 2026-07-10) is whether the plug's gateway is live: `ONLINE` + `last_seen` within the liveness window; `is_private: bool` (added 2026-07-12) is true for plugs in a non-public group (necessarily one the caller joined) — drives the Home page's "Your chargers" vs "Public chargers" sections; `watching: bool` (added 2026-07-12) is whether the **caller** has an armed "notify me when free" watch on the plug (computed with ONE extra per-user query for the whole list, not per plug). The reservation fields (added 2026-07-12, all plugs batched into ONE grouped query so the endpoint stays N+1-free): `reserved_now: bool` = a BOOKED window covers right now (after lazy no-show expiry), `reserved_now_by_me: bool` = the caller holds it, `reserved_until: str?` = ISO end of that covering window, `next_reservation: {start_at, end_at}?` = the next strictly-future BOOKED window |
| GET | `/api/plugs/public` | **none (public)** | — | **Pre-signup discovery map (2026-07-16).** UNAUTHENTICATED. Returns ONLY **public**-group or ungrouped plugs that have a known location, with a deliberately minimal projection → `[{id, name, status, latitude, longitude, price_per_kwh, gateway_online}]`. Private/society plugs (non-public group) are **never** included — no per-user, session, or network fields are exposed. Rate-limited per IP (`PUBLIC_MAP_RATE_LIMIT`, default 60/60). Declared before `/api/plugs/{plug_id}` so the static path wins. Starting a charge still requires an account. |
| GET | `/api/plugs/{plug_id}` | JWT | path `plug_id:int` | Single plug; 404 if missing, 403 if in a private group the user hasn't joined. Response also carries `gateway_online`, `is_private`, `watching`, and the reservation fields (as above) |
| POST | `/api/plugs/{plug_id}/watch` | JWT | path `plug_id:int` | Arm a **one-shot "notify me when free" watch** (2026-07-12): when the plug next flips back to AVAILABLE (session end or CPO maintenance-clear), the caller gets a `plug_available` notification through the standard pipeline (feed + Socket.io + Web Push, `services/plug_watch.py`) and the watch deletes itself. Idempotent (re-arming returns the same 200; a concurrent double-tap is absorbed via the `UNIQUE(user_id, plug_id)` constraint). Access = the `GET /api/plugs/{id}` rule (403 for a non-member on a private-group plug; 404 unknown plug). Occupied/offline/maintenance plugs are all watchable; the one rejection is 409 when the plug is startable right now (AVAILABLE **and** its gateway live). → `{status:"watching", plug_id, watching:true}` |
| DELETE | `/api/plugs/{plug_id}/watch` | JWT | path `plug_id:int` | Disarm the caller's watch. Idempotent — a watch that doesn't exist (never armed / already fired / plug gone) is a no-op 200; deliberately no access check (the row is the caller's own — a driver who left the plug's private group can still clear it). → `{status:"not_watching", plug_id, watching:false}` |

> **Provisioning moved.** The old unauthenticated `POST /api/plugs/register` and
> `POST /api/gateways/register` have been **removed**. Gateways and plugs are now
> created through the RBAC-gated `POST /api/cpo/gateways` / `POST /api/cpo/plugs`
> (see [CPO Admin Portal](#cpo-admin-portal-apicpo)), which scope every write to
> the caller's tenant.

## Charging sessions

| Method | Path | Auth | Body/Params | Behaviour |
|--------|------|------|-------------|-----------|
| POST | `/api/sessions/start` | JWT | `{plug_id, max_duration_seconds=14400, max_kwh=30.0}` — `max_kwh` (0.1–100) / `max_duration_seconds` (1 s–24 h) are **user-set charging limits** (2026-07-12): persisted onto the session (`ChargingSession.max_kwh`/`max_duration_seconds`, Alembic `0015_session_limits`) and **enforced by a backend auto-stop** ("auto-stopped: energy limit reached" / "auto-stopped: time limit reached", ~1 s after the limit via the telemetry path, env `AUTO_STOP_ON_LIMITS`; the firmware additionally enforces both locally as relay watchdogs, and the reaper carries a duration backstop). Omitted fields get the defaults — the pre-limit behavior exactly | Reject if the user already has `MAX_ACTIVE_SESSIONS_PER_USER` (env, default 2) ACTIVE sessions (409; counted under a user-row lock so concurrent starts can't exceed the cap) → access check → reject if OCCUPIED/offline/gateway-dead (409) → **reservation gate** (2026-07-12, under the plug row lock: lazy no-show expiry first, then if a BOOKED window covers now and belongs to someone else → 409 "Plug is reserved until <end>"; the holder's own start marks the reservation FULFILLED and links `session_id` — see [Plug reservations](#plug-reservations-routersreservationspy-2026-07-12)) → resolve the billing rate → size + require an authorization hold ≥ 50 (402; **2026-07-12** — `min(available_balance, max_kwh × rate)`, `services/wallet.py available_balance`, replacing the old flat-balance floor — see MARKET_GAP_ANALYSIS.md §3; a smaller user `max_kwh` shrinks the hold too) → MQTT `ON` (500 on publish fail) → create session (snapshotting the hold onto `hold_coins` and the limits), mark plug OCCUPIED → `{status:"started", session_id, plug_id, plug_name, max_kwh, max_duration_seconds, message}` (the two limit fields echo the **effective** values, added 2026-07-12) |
| POST | `/api/sessions/stop` | JWT | `{session_id}` | Owner+active check → MQTT `OFF` (best-effort) → finalize from telemetry → debit wallet → ledger `session_debit` → plug AVAILABLE → the receipt payload `{status:"completed", session_id, plug_id, plug_name, energy_kwh, peak_power_w, price_per_kwh, coins_spent, shortfall_coins, balance_before, balance_remaining, duration_sec, started_at, ended_at, max_kwh, max_duration_seconds, reason}` — `max_kwh`/`max_duration_seconds` (added 2026-07-12; NULL for legacy pre-limit sessions) are the limits the session ran with, so the UI can say which one an auto-stop hit alongside `reason` |
| GET | `/api/sessions/active` | JWT | — | Retrieve **all** active sessions for the logged-in user, newest first → `{active:true, sessions:[{session_id, plug_id, plug_name, started_at, max_kwh, max_duration_seconds}, …], session_id, plug_id, plug_name, started_at}` (`max_kwh`/`max_duration_seconds` added 2026-07-12 — NULL for legacy sessions; the top-level single-session fields mirror the newest entry for older clients) or `{active:false, sessions:[]}` |
| — | `Socket.io` connection | JWT | connection query or auth dict | Real-time bi-directional channel for telemetry updates and session status (sole live-telemetry transport since 2026-07-07). |
| GET | `/api/sessions/history` | JWT | — | Last 50 sessions, newest first → `[{id, plug_id, started_at, ended_at, energy_kwh, coins_spent, status}]` |

### Socket.io Events Reference
- **Connection**: Pass JWT token via connection auth dict: `{ token: "<JWT_TOKEN>" }` or in connection query string: `?token=<JWT_TOKEN>`.
- **Subscribe Session**:
  - Event: `subscribe_session`
  - Payload: `{ "session_id": <int> }`
  - Responses:
    - On success: Emits `subscription_success` event: `{ "session_id": <int> }`
    - On failure: Emits `subscription_error` event: `{ "detail": "<error_message>" }`
- **Telemetry Stream**:
  - Event: `telemetry` (pushed from server to rooms)
  - Payload: `{ "plug_id": <int>, "power_w": <float>, "current_a": <float>, "voltage_v": <float>, "energy_kwh": <float>, "duration_sec": <int>, "cost_coins": <float>, "status": "charging"|"completed"|"starting", "relay_on": <bool>, "is_stale": <bool>, "age_sec": <float> }` — `relay_on` (the plug's actual relay state), `voltage_v`, `is_stale`, and `age_sec` added 2026-07-10
- **Unsubscribe Session**:
  - Event: `unsubscribe_session`
  - Payload: `{ "session_id": <int> }`


## Plug reservations (`routers/reservations.py`, 2026-07-12)

Book a future `[start_at, end_at)` window on a plug (the private
society/office use case — group members share one charger). During the
window only the holder can start a session (the gate in
`POST /api/sessions/start` above). **FREE in v1** — no coin hold. Statuses:
`booked → cancelled | fulfilled | expired`. Expiry is **lazy** (no
background sweep): every read path and the start gate first flip BOOKED
rows past `start_at + RESERVATION_NO_SHOW_GRACE_MIN` (or past `end_at`) to
EXPIRED, so a no-show never blocks anyone. Overlap exclusion is enforced
under a `SELECT ... FOR UPDATE` on the plug row, so concurrent bookings
serialize; per-user cap enforcement locks the user row (user → plug lock
order, same as session start).

| Method | Path | Auth | Body/Params | Behaviour |
|--------|------|------|-------------|-----------|
| POST | `/api/reservations` | JWT | `{plug_id, start_at, end_at}` (ISO; naive datetimes read as UTC) | Access check (ungrouped ∪ public group ∪ joined private group — 403), plug not MAINTENANCE (409; OFFLINE is bookable — the liveness gate still runs at start time), window rules (400: ≥15 min, ≤`RESERVATION_MAX_HOURS`, start ≥ now−2 min, start ≤ now+`RESERVATION_MAX_ADVANCE_DAYS`), per-user cap of BOOKED-with-future-end (`MAX_UPCOMING_RESERVATIONS_PER_USER` — 409), `[start,end)` intersection with a BOOKED window on the plug (409, back-to-back edges legal). Sends a confirmation notification. → reservation object |
| GET | `/api/reservations/my` | JWT | — | `{upcoming: [...], history: [...]}` — upcoming = BOOKED with `end_at` in the future, soonest first; history = everything else, newest first, capped at 20. Each entry: `{id, plug_id, plug_name, user_id, tenant_id, start_at, end_at, status, session_id, created_at, is_mine}` |
| POST | `/api/reservations/{id}/cancel` | JWT | path `id:int` | Owner, or cpo/admin **of the owning tenant** (anyone else → 404, existence not leaked). Only BOOKED cancels (409 otherwise). An operator cancel notifies the driver. → the cancelled reservation |
| GET | `/api/plugs/{plug_id}/reservations` | JWT | path `plug_id:int` | The plug's schedule for anyone with plug access (403 otherwise): current + upcoming BOOKED windows within the booking horizon (`RESERVATION_MAX_ADVANCE_DAYS`), soonest first, each with the holder's `user_name` + `is_mine` — how group members book around each other. (Lives in `routers/reservations.py`, not `plugs.py`.) |

## Payments — Razorpay (`services/payments.py`)

| Method | Path | Auth | Body | Behaviour |
|--------|------|------|------|----------|
| POST | `/api/payments/create-order` | JWT | `{amount_inr: float}` (₹10–₹10,000, supporting decimals) | Creates a Razorpay order → `{order_id, amount(paise), currency, key_id}`. 503 if Razorpay unconfigured. |
| POST | `/api/payments/verify` | JWT | `{razorpay_order_id, razorpay_payment_id, razorpay_signature}` (`amount_inr` is deprecated and **ignored**) | HMAC verify (400 if bad) → fetch the payment from Razorpay's API and credit the **Razorpay-confirmed amount**, never a client-sent one (502 if Razorpay unreachable; 409 if not yet captured — the webhook credits on capture; 403 if the payment's order was created for another user) → ledger `topup` → `{status:"success", coins_credited, new_balance}` |
| POST | `/api/payments/webhook` | none (HMAC-gated) | raw body + `X-Razorpay-Signature` | HMAC verify (400 if bad) → on `payment.captured`, auto-credit coins from the payment's `notes`/`amount` (atomic, row-locked, supporting decimals) → ledger `topup`. Idempotent vs. `/verify` (dedupes on `razorpay_payment_id`). → `{status:"credited"\|"already_credited"\|"ignored"\|"user_not_found"}` |
| GET | `/api/wallet/ledger` | JWT | `?limit` (default 100, max 500) | Unified wallet ledger for the user — top-up credits **and** session debits, newest first. Returns a list of `{id, amount(signed), transaction_type("topup"\|"session_debit"\|"refund"), direction("credit"\|"debit"), description, balance_after, session_id, razorpay_payment_id, created_at, available_balance}`. `available_balance` (added 2026-07-12, same figure as `/api/auth/me`) is the driver's CURRENT available balance, computed once and repeated on every row — unlike `balance_after` (a per-transaction historical snapshot), it is not tied to that row's point in time. Additive field; the endpoint's list shape is unchanged. |

## Driver notifications (`routers/notifications.py`, 2026-07-11)

Per-user feed written by `services/notifications.py` at the emit points
(session stop/auto-stop/reap/safety-cutoff, low balance, charger offline,
top-up credit, and — 2026-07-12 — `plug_available` when a watched plug frees
up, see `POST /api/plugs/{id}/watch`); delivered live over the Socket.io
`notification` event (the user's own room) and optionally by **Web Push**
(VAPID; enabled when `VAPID_PRIVATE_KEY` is set).

| Method | Path | Auth | Body/Params | Behaviour |
|--------|------|------|-------------|-----------|
| GET | `/api/notifications` | JWT | `?unread_only, ?limit` (default 50, max 200) | Newest-first feed → `{notifications:[{id, type, severity, title, body, plug_id, session_id, read, created_at}], unread_count}` (`unread_count` is the user's total, independent of the page) |
| POST | `/api/notifications/{id}/read` | JWT | — | Mark one read (404 if not the caller's) → `{status:"read"}` |
| POST | `/api/notifications/read-all` | JWT | — | Mark all read → `{status:"read", count}` |
| GET | `/api/notifications/push/public-key` | JWT | — | `{enabled, vapid_public_key}` — the browser `applicationServerKey`, derived from `VAPID_PRIVATE_KEY` at runtime (`enabled:false` when push unconfigured) |
| POST | `/api/notifications/push/subscribe` | JWT | `PushSubscription.toJSON()`: `{endpoint, keys:{p256dh, auth}}` | Upsert by unique `endpoint` (re-subscribes update in place; a subscription re-used by a new login moves to that user) → `{status:"subscribed"}` |
| DELETE | `/api/notifications/push/subscribe` | JWT | `{endpoint}` | Remove the caller's subscription row → `{status:"unsubscribed"}` |

Socket.io event (server → the user's room only): `notification` with the same
object shape as the feed entries. Dead push subscriptions (push service
returns 404/410) are pruned automatically on the next send.

## Direct Mode — Tapo P110 (dev/test, ESP32 bypass)

All require **JWT** *and* `DIRECT_MODE=true` with an initialized driver (else
503). The plug IP comes from the request body/query `plug_ip`, falling back to
the `TAPO_PLUG_IP` env (400 if neither). See [ARCHITECTURE.md](ARCHITECTURE.md#path-b)
and `backend/services/tapo_direct.py`.

| Method | Path | Body/Params | Response |
|--------|------|-------------|----------|
| POST | `/api/direct/plug/on` | `{plug_ip?}` | `{status:"on", plug_ip, message, mode:"direct"}` (502 on failure) |
| POST | `/api/direct/plug/off` | `{plug_ip?}` | `{status:"off", ...}` (502 on failure) |
| GET | `/api/direct/plug/info` | query `plug_ip?` | `{plug_ip, device_info, mode}` |
| GET | `/api/direct/plug/energy` | query `plug_ip?` | `{plug_ip, energy_usage, mode}` |
| GET | `/api/direct/plug/health` | query `plug_ip?` | `{plug_ip, health, mode}` (always 200) |

## CPO Admin Portal (`/api/cpo/*`)

Powers the operator dashboard (`frontend/src/pages/cpo/`). Except `setup`, every
endpoint requires the caller's **DB role** to be `cpo` or `admin`, enforced by
`require_role(...)` in `backend/services/rbac.py` (403 otherwise). All queries are
scoped to the caller's `tenant_id`, so operators only ever see their own assets.

| Method | Path | Auth | Body/Params | Behaviour |
|--------|------|------|-------------|-----------|
| POST | `/api/cpo/setup` | JWT | `{tenant_name}` | One-time: creates a tenant and promotes the caller to `cpo`. 400 if already tenant-linked or name taken. |
| GET | `/api/cpo/profile` | cpo/admin | — | Tenant info + counts `{gateway_count, plug_count, group_count}`. |
| GET | `/api/cpo/gateways` | cpo/admin | — | Tenant's gateways (each with `plug_count`). |
| POST | `/api/cpo/gateways` | cpo/admin | `{gateway_id, name, vpn_ip}` | Register a gateway under the tenant. |
| POST | `/api/cpo/gateways/{id}/ota` | cpo/admin | `{firmware_url}` (http(s), ≤512 chars) | Trigger an OTA firmware update. Requires the gateway `ONLINE` (409 if offline — gates on the status flag, **not** telemetry freshness, so a gateway with an unreachable plug can still be updated) and ≥1 plug (409 — the OTA command rides a per-plug command topic). Publishes the `OTA` command (502 on publish fail); the gateway downloads into its passive slot and reboots (rollback-protected), refusing mid-session. → `{status:"ota_triggered", gateway_id, firmware_url, message}` |
| GET | `/api/cpo/plugs` | cpo/admin | — | All plugs across the tenant's gateways (status, power, group). |
| POST | `/api/cpo/plugs` | cpo/admin | `{gateway_id, name, local_ip, plug_model?, group_id?}` | Register a plug (validates gateway + group ownership). |
| PUT | `/api/cpo/plugs/{id}` | cpo/admin | `{name?, group_id?, status?, local_ip?, max_current_a?}` | Update a plug (`group_id:0` = unassign). Changing `local_ip` (e.g. after a DHCP change) or `max_current_a` re-publishes the gateway's retained plug roster to the device. |
| GET | `/api/cpo/groups` | cpo/admin | — | Tenant's charger groups (with `plug_count`, `member_count`, `access_code`). |
| POST | `/api/cpo/groups` | cpo/admin | `{name, is_public?}` | Create a group; private groups get a generated access code. |
| PUT | `/api/cpo/groups/{id}` | cpo/admin | `{name?, is_public?, regenerate_access_code?}` | Update a group / rotate access code. |
| DELETE | `/api/cpo/groups/{id}` | cpo/admin | — | Delete a group; assigned plugs become ungrouped. |
| GET | `/api/cpo/analytics/overview` | cpo/admin | — | Plugs/gateways/active-session counts + today & all-time energy/revenue. |
| GET | `/api/cpo/analytics/sessions` | cpo/admin | query `plug_id?, status_filter?, days=30, limit=50` | Session history enriched with plug name, user email, duration. |
| GET | `/api/cpo/analytics/sessions.csv` | cpo/admin | query `plug_id?, status_filter?, days=30` | Same tenant scope/filters as above, returned as a downloadable `text/csv` attachment (capped 10k rows). |
| GET | `/api/cpo/analytics/revenue` | cpo/admin | query `days=30` | Daily `{date, revenue_coins, session_count}` series. |
| GET | `/api/cpo/analytics/energy` | cpo/admin | query `days=30` | Daily `{date, energy_kwh, session_count}` series. |
| GET | `/api/cpo/analytics/telemetry` | cpo/admin | query `plug_id?, days=1, bucket=hour` | `date_trunc`-bucketed time-series from `telemetry_readings` (bucket ∈ {minute, hour, day}; 400 otherwise) → `[{timestamp, avg_power_w, max_power_w, energy_kwh, avg_current_a, max_current_a, sample_count}]`. Powers the dashboard load graph (peak W + A). |
| GET | `/api/cpo/events` | cpo/admin | query `limit=50` (max 200), `unacknowledged_only?` (bool), `severity?` (e.g. `critical`) | Gateway/plug operational events (safety cutoffs, `UNAUTHORIZED_ON` alarms, OTA notices) for the CPO's tenant, newest first → `[{id, gateway_id, plug_id, event_type, severity, detail, acknowledged, created_at}]`. (Added 2026-07-10.) |
| POST | `/api/cpo/events/{event_id}/ack` | cpo/admin | path `event_id:int` | Acknowledge (clear from the active feed) one event; tenant-scoped. → `{status:"acknowledged", event_id}` |
| GET | `/api/cpo/audit` | cpo/admin | query `limit=50` (max 200), `offset=0` | Admin action audit trail (TD#26) for the CPO's tenant, newest first → `[{id, actor_user_id, actor_email, action, target_type, target_id, detail, created_at}]`. Covers gateway create, plug create, plug status change, plug maintenance enter/clear, group create/delete, access-code regen, and the payout money ops (`payout.request` / `payout.mark_paid` / `payout.cancel` — a payout audit row always lands in the *payout's* tenant, so an admin's mark_paid/cancel shows up in the owning CPO's trail) — written non-fatally by `services/audit.py` (a write failure is logged, never breaks the admin action). Gateway/plug delete aren't recorded yet — no such CPO endpoints exist. (Added 2026-07-12.) |
| GET | `/api/cpo/earnings` | cpo/admin | — | Lifetime + unsettled (settlement watermark → now) earnings for the caller's tenant → `{watermark, as_of, platform_fee_pct, lifetime:{gross_coins, platform_fee_coins, net_coins}, unsettled:{period_start, period_end, gross_coins, platform_fee_coins, net_coins}}`. Coins are ₹-equivalent (1 coin = ₹1); fee = `PLATFORM_FEE_PCT` (default 10%). Powers `/cpo/earnings`. |
| POST | `/api/cpo/payouts` | cpo/admin | — | Snapshot the tenant's unsettled earnings into a REQUESTED payout. 400 if nothing unsettled (net ≤ 0), 409 if a request is already pending (one at a time); race-safe via a tenant-row lock. Audited as `payout.request`. → payout object (below). |
| GET | `/api/cpo/payouts` | cpo/admin | — | The tenant's payouts, newest request first → `[{id, tenant_id, period_start, period_end, gross_coins, platform_fee_coins, net_coins, status, requested_by_user_id, requested_at, paid_at, note}]` (`status` ∈ requested/paid/cancelled). |
| POST | `/api/cpo/payouts/{id}/mark_paid` | **admin only** | path `id:int` | Record that a REQUESTED payout was settled **out-of-band** (bank/UPI outside the app — no money moves here). 404 unknown id, 409 if not REQUESTED (row-locked, replay-safe). Audited as `payout.mark_paid` into the payout's tenant. |
| POST | `/api/cpo/payouts/{id}/cancel` | owner cpo/admin | path `id:int` | Cancel a REQUESTED payout, freeing its window for a future request. Cross-tenant callers get 404 (indistinguishable from "doesn't exist"); 409 if not REQUESTED. Audited as `payout.cancel` into the payout's tenant. |
| GET | `/api/cpo/reservations` | cpo/admin | query `status?` (booked/cancelled/fulfilled/expired; 400 otherwise), `upcoming_only?` (bool), `limit=50` (max 200), `offset=0` | The tenant's plug reservations, newest window first, each with `plug_name` + the driver's `user_email`/`user_name`. Lazily expires lapsed holds first (so a no-show is never shown as BOOKED). Cancelling uses the shared `POST /api/reservations/{id}/cancel`, which admits the owning tenant's cpo/admin. (Added 2026-07-12.) |

---

## Environment variables the backend reads

| Var | Default | Purpose |
|-----|---------|---------|
| `DATABASE_URL` | `postgresql+asyncpg://postgres:amphive_dev@localhost:5432/amphive` | Async PostgreSQL (asyncpg) |
| `MQTT_BROKER_HOST` / `MQTT_BROKER_PORT` | `localhost` / `1883` | MQTT broker |
| `MQTT_USERNAME` / `MQTT_PASSWORD` | none | MQTT auth (broker is anonymous today) |
| `JWT_SECRET_KEY` | `amphive-dev-secret-change-in-production` | JWT signing key — **change in prod** |
| `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` / `RAZORPAY_WEBHOOK_SECRET` | `""` | Razorpay; payments disabled if unset |
| `COINS_PER_RUPEE` | `1.0` | Coin conversion rate |
| `DIRECT_MODE` | `false` | Enable `/api/direct/*` endpoints |
| `TAPO_USERNAME` / `TAPO_PASSWORD` / `TAPO_PLUG_IP` | `""` | Tapo account + default plug IP for Direct Mode |
| `TAPO_RELAY_URL` | none | If set, Direct Mode calls an HTTP relay instead of the local `tapo` lib |
| `TELEMETRY_FLUSH_INTERVAL_SEC` | `10.0` | How often the buffered telemetry flush task drains to `telemetry_readings` |
| `TELEMETRY_BUFFER_MAX` | `10000` | Max buffered readings; oldest dropped if the DB is unavailable |
| `TELEMETRY_RETENTION_DAYS` | `0` | Prune `telemetry_readings` older than N days. `0` = retention disabled (keep all) |
| `TELEMETRY_PRUNE_EVERY_N_FLUSHES` | `360` | Run the retention prune every N flushes (~hourly at the default interval) |
| `LOGIN_RATE_LIMIT` / `REGISTER_RATE_LIMIT` | `10/60` / `10/3600` | Auth rate limits, `"<attempts>/<window sec>"` per client IP (429 + Retry-After) |
| `FORGOT_PASSWORD_RATE_LIMIT` / `RESET_PASSWORD_RATE_LIMIT` | `5/3600` / `10/3600` | Password-reset rate limits, same format/mechanism as the login/register limits |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` / `SMTP_FROM` | `""` / `587` / `""` / `""` / `""` | Outbound email (STARTTLS) for password-reset links; `SMTP_HOST` unset = console fallback (link logged at WARNING). Login skipped when `SMTP_USER` empty |
| `FRONTEND_ORIGIN` | `https://amphive.app` | Base URL for links in outbound email (`/reset-password?token=...`) |
| `RESET_TOKEN_TTL_MIN` | `30` | Minutes a password-reset link stays valid (single use) |
| `VAPID_PRIVATE_KEY` / `VAPID_SUBJECT` | `""` / `mailto:admin@amphive.example` | Web Push signing key + contact; empty key = push disabled (feed + Socket.io still work) |
| `LOW_BALANCE_WARN_FRACTION` | `0.8` | Notify the driver once per session when accrued cost crosses this fraction of the wallet balance (`0` disables) |
| `PLATFORM_FEE_PCT` | `10.0` | Platform's cut of CPO gross earnings, percent — the fee/net split on `/api/cpo/earnings` and payout snapshots (`services/payouts.py`; falls back to the default on a malformed value) |
| `RESERVATION_MAX_HOURS` | `4` | Longest bookable reservation window (`services/reservations.py`) |
| `RESERVATION_MAX_ADVANCE_DAYS` | `7` | How far ahead a reservation may start (also the per-plug schedule horizon) |
| `MAX_UPCOMING_RESERVATIONS_PER_USER` | `2` | Per-user cap of BOOKED reservations with `end_at` in the future (409 past it) |
| `RESERVATION_NO_SHOW_GRACE_MIN` | `15` | Minutes after `start_at` an unfulfilled BOOKED reservation lazily flips to EXPIRED and stops blocking the plug |
