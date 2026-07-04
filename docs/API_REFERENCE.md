# AmpHive — Backend API Reference

*Verified against `backend/main.py` on 2026-07-02.*

All routes are defined directly on the FastAPI `app` in `backend/main.py` (there
is no `APIRouter`/prefix grouping). Every path is hard-coded under `/api`.
The app title is **"AmpHive Shared EV Charging API"**, version **2.0.0**.
Interactive docs: `http://<host>:8000/docs`.

- **Auth:** routes marked **JWT** require `Authorization: Bearer <token>`
  (FastAPI `HTTPBearer` → `get_current_user`). The user is loaded fresh from the
  DB on every request, so balance/role are always current.
- Routes marked **cpo/admin** additionally require the caller's DB role to be
  `cpo` or `admin`, enforced by `require_role(...)` (`backend/services/rbac.py`).
- **CORS:** wide open (`allow_origins=["*"]`) — flagged for production lockdown.
- **37 endpoints total**, grouped below: health (1), auth (3), groups (2),
  plugs (2), sessions (5), payments (3), Direct Mode (5), CPO portal (16).

---

## Health

| Method | Path | Auth | Response |
|--------|------|------|----------|
| GET | `/api/health` | none | `{"status":"healthy","service":"amphive-backend","version":"2.0.0"}` |

## Authentication (`services/auth.py`)

| Method | Path | Auth | Body | Response |
|--------|------|------|------|----------|
| POST | `/api/auth/register` | none | `{email, password, full_name}` | `{token, user}` — creates a `driver`, `coin_balance=0`. 400 on duplicate email. |
| POST | `/api/auth/login` | none | `{email, password}` | `{token, user}` — 401 on bad credentials. |
| GET | `/api/auth/me` | JWT | — | `{id, email, full_name, role, coin_balance}` |

`user` object shape: `{id, email, full_name, role, coin_balance}`.
Token: HS256 JWT, claims `sub`/`role`/`email`/`iat`/`exp`, **7-day** expiry.

## Charger groups

| Method | Path | Auth | Body | Behaviour |
|--------|------|------|------|-----------|
| POST | `/api/groups/join` | JWT | `{access_code}` | Join a private group by code. 404 if unknown, 400 if the group is public or already joined. → `{status:"joined", group_id, group_name}` |
| GET | `/api/groups/my` | JWT | — | All public groups + private groups the user joined, deduped → `[{id, name, is_public, plug_count}]` |

## Plugs

| Method | Path | Auth | Body/Params | Behaviour |
|--------|------|------|-------------|-----------|
| GET | `/api/plugs/available` | JWT | — | Plugs in accessible groups **or** ungrouped (`group_id IS NULL`, visible to all) → `[{id, name, status, current_power_w, plug_model, group_name?}]` |
| GET | `/api/plugs/{plug_id}` | JWT | path `plug_id:int` | Single plug; 404 if missing, 403 if in a private group the user hasn't joined |

> **Provisioning moved.** The old unauthenticated `POST /api/plugs/register` and
> `POST /api/gateways/register` have been **removed**. Gateways and plugs are now
> created through the RBAC-gated `POST /api/cpo/gateways` / `POST /api/cpo/plugs`
> (see [CPO Admin Portal](#cpo-admin-portal-apicpo)), which scope every write to
> the caller's tenant.

## Charging sessions

| Method | Path | Auth | Body/Params | Behaviour |
|--------|------|------|-------------|-----------|
| POST | `/api/sessions/start` | JWT | `{plug_id, max_duration_seconds=14400, max_kwh=30.0}` | Access check → require balance ≥ 50 (402) → reject if OCCUPIED (409) → MQTT `ON` (500 on publish fail) → create session, mark plug OCCUPIED → `{status:"started", session_id, plug_id, plug_name, message}` |
| POST | `/api/sessions/stop` | JWT | `{session_id}` | Owner+active check → MQTT `OFF` (best-effort) → finalize from telemetry → debit wallet → ledger `session_debit` → plug AVAILABLE → `{status:"completed", session_id, energy_kwh, coins_spent, balance_remaining}` |
| GET | `/api/sessions/active` | JWT | — | Retrieve the currently active session for the logged-in user, if any (returns the most recent active session) → `{active:true, session_id, plug_id, plug_name, started_at}` or `{active:false}` |
| GET | `/api/sessions/live/{session_id}` | JWT* | path `session_id:int` | **SSE** (`text/event-stream`); emits named `telemetry` events `{event:"telemetry", data:<json>}` |
| GET | `/api/sessions/history` | JWT | — | Last 50 sessions, newest first → `[{id, plug_id, started_at, ended_at, energy_kwh, coins_spent, status}]` |

\* The frontend opens the SSE stream with `EventSource`, which **cannot send the
`Authorization` header**. The code comments intend a `?token=` query param but
do not actually append it — see [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md).

## Payments — Razorpay (`services/payments.py`)

| Method | Path | Auth | Body | Behaviour |
|--------|------|------|------|-----------|
| POST | `/api/payments/create-order` | JWT | `{amount_inr}` (₹10–₹10,000) | Creates a Razorpay order → `{order_id, amount(paise), currency, key_id}`. 503 if Razorpay unconfigured. |
| POST | `/api/payments/verify` | JWT | `{razorpay_order_id, razorpay_payment_id, razorpay_signature, amount_inr}` | HMAC verify (400 if bad) → credit coins (`COINS_PER_RUPEE`) → ledger `topup` → `{status:"success", coins_credited, new_balance}` |
| POST | `/api/payments/webhook` | none (HMAC-gated) | raw body + `X-Razorpay-Signature` | HMAC verify (400 if bad) → on `payment.captured`, auto-credit coins from the payment's `notes`/`amount` (atomic, row-locked) → ledger `topup`. Idempotent vs. `/verify` (dedupes on `razorpay_payment_id`). → `{status:"credited"\|"already_credited"\|"ignored"\|"user_not_found"}` |

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
| GET | `/api/cpo/plugs` | cpo/admin | — | All plugs across the tenant's gateways (status, power, group). |
| POST | `/api/cpo/plugs` | cpo/admin | `{gateway_id, name, local_ip, plug_model?, group_id?}` | Register a plug (validates gateway + group ownership). |
| PUT | `/api/cpo/plugs/{id}` | cpo/admin | `{name?, group_id?, status?}` | Update a plug (`group_id:0` = unassign). |
| GET | `/api/cpo/groups` | cpo/admin | — | Tenant's charger groups (with `plug_count`, `member_count`, `access_code`). |
| POST | `/api/cpo/groups` | cpo/admin | `{name, is_public?}` | Create a group; private groups get a generated access code. |
| PUT | `/api/cpo/groups/{id}` | cpo/admin | `{name?, is_public?, regenerate_access_code?}` | Update a group / rotate access code. |
| DELETE | `/api/cpo/groups/{id}` | cpo/admin | — | Delete a group; assigned plugs become ungrouped. |
| GET | `/api/cpo/analytics/overview` | cpo/admin | — | Plugs/gateways/active-session counts + today & all-time energy/revenue. |
| GET | `/api/cpo/analytics/sessions` | cpo/admin | query `plug_id?, status_filter?, days=30, limit=50` | Session history enriched with plug name, user email, duration. |
| GET | `/api/cpo/analytics/revenue` | cpo/admin | query `days=30` | Daily `{date, revenue_coins, session_count}` series. |
| GET | `/api/cpo/analytics/energy` | cpo/admin | query `days=30` | Daily `{date, energy_kwh, session_count}` series. |
| GET | `/api/cpo/analytics/telemetry` | cpo/admin | query `plug_id?, days=1, bucket=hour` | `date_trunc`-bucketed time-series from `telemetry_readings` (bucket ∈ {minute, hour, day}; 400 otherwise) → `[{timestamp, avg_power_w, max_power_w, energy_kwh, sample_count}]`. Powers the dashboard load graph. |

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
</content>
