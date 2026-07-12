# AmpHive — Backend API Reference

*Verified against `backend/` on 2026-07-02; endpoint list refreshed 2026-07-10.*

Routes live in `backend/routers/*.py` (`{auth,groups,plugs,sessions,payments,
direct,cpo}.py`), each an `APIRouter` mounted on the FastAPI `app` in
`backend/main.py` (the 2026-07-07 `main.py` split — TD#7). Every path is
hard-coded under `/api` (no router `prefix=`). The app title is
**"AmpHive Shared EV Charging API"**, version **2.0.0**.
Interactive docs: `http://<host>:8000/docs`.

- **Auth:** routes marked **JWT** require `Authorization: Bearer <token>`
  (FastAPI `HTTPBearer` → `get_current_user`). The user is loaded fresh from the
  DB on every request, so balance/role are always current.
- Routes marked **cpo/admin** additionally require the caller's DB role to be
  `cpo` or `admin`, enforced by `require_role(...)` (`backend/services/rbac.py`).
- **CORS:** explicit allowlist (localhost, `amphive.duckdns.org`, VM IP) —
  locked down 2026-07-06.
- **41 endpoints total**, grouped below: health (1), auth (4), groups (2),
  plugs (2), sessions (4), payments (3), Direct Mode (5), CPO portal (20).
  (The legacy SSE endpoint `/api/sessions/live/{id}` was retired 2026-07-07 —
  live telemetry is Socket.io only. The CPO gateway OTA-trigger endpoint was
  added 2026-07-07; the `/api/auth/logout` revocation endpoint 2026-07-08; the
  CPO events feed + ack endpoints 2026-07-10; the CPO audit-log endpoint
  2026-07-12. This count predates a few other endpoints added alongside it —
  not re-audited here.)

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
| GET | `/api/auth/me` | JWT | — | `{id, email, full_name, role, coin_balance}` |
| POST | `/api/auth/logout` | JWT | — | Revokes every token for the caller (bumps `users.token_version`; "log out everywhere") → `{status:"logged_out"}`. |

`user` object shape: `{id, email, full_name, role, coin_balance}` (where `coin_balance` is a float).
Token: HS256 JWT, claims `sub`/`role`/`email`/`iat`/`exp`, **7-day** expiry.

## Charger groups

| Method | Path | Auth | Body | Behaviour |
|--------|------|------|------|-----------|
| POST | `/api/groups/join` | JWT | `{access_code}` | Join a private group by code. 404 if unknown, 400 if the group is public or already joined. → `{status:"joined", group_id, group_name}` |
| GET | `/api/groups/my` | JWT | — | All public groups + private groups the user joined, deduped → `[{id, name, is_public, plug_count}]` |

## Plugs

| Method | Path | Auth | Body/Params | Behaviour |
|--------|------|------|-------------|-----------|
| GET | `/api/plugs/available` | JWT | — | Plugs in accessible groups **or** ungrouped (`group_id IS NULL`, visible to all) → `[{id, name, status, current_power_w, plug_model, group_name?, gateway_online}]` — `gateway_online: bool` (added 2026-07-10) is whether the plug's gateway is live: `ONLINE` + `last_seen` within the liveness window |
| GET | `/api/plugs/{plug_id}` | JWT | path `plug_id:int` | Single plug; 404 if missing, 403 if in a private group the user hasn't joined. Response also carries `gateway_online: bool` (as above) |

> **Provisioning moved.** The old unauthenticated `POST /api/plugs/register` and
> `POST /api/gateways/register` have been **removed**. Gateways and plugs are now
> created through the RBAC-gated `POST /api/cpo/gateways` / `POST /api/cpo/plugs`
> (see [CPO Admin Portal](#cpo-admin-portal-apicpo)), which scope every write to
> the caller's tenant.

## Charging sessions

| Method | Path | Auth | Body/Params | Behaviour |
|--------|------|------|-------------|-----------|
| POST | `/api/sessions/start` | JWT | `{plug_id, max_duration_seconds=14400, max_kwh=30.0}` | Reject if the user already has `MAX_ACTIVE_SESSIONS_PER_USER` (env, default 2) ACTIVE sessions (409; counted under a user-row lock so concurrent starts can't exceed the cap) → access check → require balance ≥ 50 (402) → reject if OCCUPIED (409) → MQTT `ON` (500 on publish fail) → create session, mark plug OCCUPIED → `{status:"started", session_id, plug_id, plug_name, message}` |
| POST | `/api/sessions/stop` | JWT | `{session_id}` | Owner+active check → MQTT `OFF` (best-effort) → finalize from telemetry → debit wallet → ledger `session_debit` → plug AVAILABLE → `{status:"completed", session_id, energy_kwh, coins_spent, balance_remaining}` |
| GET | `/api/sessions/active` | JWT | — | Retrieve **all** active sessions for the logged-in user, newest first → `{active:true, sessions:[{session_id, plug_id, plug_name, started_at}, …], session_id, plug_id, plug_name, started_at}` (the top-level single-session fields mirror the newest entry for older clients) or `{active:false, sessions:[]}` |
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


## Payments — Razorpay (`services/payments.py`)

| Method | Path | Auth | Body | Behaviour |
|--------|------|------|------|----------|
| POST | `/api/payments/create-order` | JWT | `{amount_inr: float}` (₹10–₹10,000, supporting decimals) | Creates a Razorpay order → `{order_id, amount(paise), currency, key_id}`. 503 if Razorpay unconfigured. |
| POST | `/api/payments/verify` | JWT | `{razorpay_order_id, razorpay_payment_id, razorpay_signature}` (`amount_inr` is deprecated and **ignored**) | HMAC verify (400 if bad) → fetch the payment from Razorpay's API and credit the **Razorpay-confirmed amount**, never a client-sent one (502 if Razorpay unreachable; 409 if not yet captured — the webhook credits on capture; 403 if the payment's order was created for another user) → ledger `topup` → `{status:"success", coins_credited, new_balance}` |
| POST | `/api/payments/webhook` | none (HMAC-gated) | raw body + `X-Razorpay-Signature` | HMAC verify (400 if bad) → on `payment.captured`, auto-credit coins from the payment's `notes`/`amount` (atomic, row-locked, supporting decimals) → ledger `topup`. Idempotent vs. `/verify` (dedupes on `razorpay_payment_id`). → `{status:"credited"\|"already_credited"\|"ignored"\|"user_not_found"}` |
| GET | `/api/wallet/ledger` | JWT | `?limit` (default 100, max 500) | Unified wallet ledger for the user — top-up credits **and** session debits, newest first. Returns a list of `{id, amount(signed), transaction_type("topup"\|"session_debit"\|"refund"), direction("credit"\|"debit"), description, balance_after, session_id, razorpay_payment_id, created_at}`. |

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
| PUT | `/api/cpo/plugs/{id}` | cpo/admin | `{name?, group_id?, status?}` | Update a plug (`group_id:0` = unassign). |
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
| GET | `/api/cpo/audit` | cpo/admin | query `limit=50` (max 200), `offset=0` | Admin action audit trail (TD#26) for the CPO's tenant, newest first → `[{id, actor_user_id, actor_email, action, target_type, target_id, detail, created_at}]`. Covers gateway create, plug create, plug status change, group create/delete, and access-code regen — written non-fatally by `services/audit.py` (a write failure is logged, never breaks the admin action). Gateway/plug delete aren't recorded yet — no such CPO endpoints exist. (Added 2026-07-12.) |

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
