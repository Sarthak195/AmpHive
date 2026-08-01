# AmpHive — Backend API Reference

*Verified against `backend/` on 2026-07-02; endpoint list refreshed 2026-07-21.*

Routes live in `backend/routers/*.py` (`{auth,groups,plugs,sessions,payments,
direct,cpo,notifications,reservations,admin}.py`), each an `APIRouter` mounted on the FastAPI `app` in
`backend/main.py` (the 2026-07-07 `main.py` split — TD#7). Every path is
hard-coded under `/api` (no router `prefix=`). The app title is
**"AmpHive Shared EV Charging API"**, version **2.0.0**.
Interactive docs: `http://<host>:8000/docs`.

- **Auth:** routes marked **JWT** require `Authorization: Bearer <token>`
  (FastAPI `HTTPBearer` → `get_current_user`). The user is loaded fresh from the
  DB on every request, so balance/role are always current. A **disabled** account
  (`users.is_disabled`, admin kill switch — migration `0025_user_disable`,
  redesign/ui-v3) is rejected with **403 `account_disabled`** on every request
  (and at login), so existing tokens die immediately.
- Routes marked **cpo/admin** additionally require the caller's DB role to be
  `cpo` or `admin`, enforced by `require_role(...)` (`backend/services/rbac.py`).
  Routes marked **admin** require the `admin` role (platform admins have
  `tenant_id NULL` by design).
- **Pagination convention** (redesign/ui-v3 contract §4): paginated list
  endpoints take `limit` (default 50, capped at 200) + `offset` query params and
  return `{"total": <full filtered count>, "items": [...]}` — `total` always
  describes the whole filtered set, never the page.
- **CORS:** explicit allowlist (localhost dev origins, `amphive.app`, `cpo.amphive.app`) —
  locked down 2026-07-06.
- **106 `@router` route decorators total** across 10 routers (see Swagger
  `/docs` for the live, authoritative list): admin (10), auth (6), cpo (48),
  direct (5), groups (3), notifications (6), payments (4), plugs (7),
  reservations (4), sessions (13). (`cpo` grew from 46 to 48 with the
  2026-07-21 offline top-up endpoints, `POST`/`GET /api/cpo/topups`.)
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
| GET | `/api/config` | none | Public pricing/config so the UI doesn't hardcode it: `{coins_per_kwh, min_start_balance_coins, coin_inr_rate, currency, google_login_enabled}`. `min_start_balance_coins` matches the 402 the session-start path enforces (`MIN_START_BALANCE_COINS`, env). `google_login_enabled` (added 2026-08-02) is `bool(GOOGLE_CLIENT_ID)` — gates the frontend's "Continue with Google" button; see the Google OAuth rows below. |

## Authentication (`services/auth.py`)

| Method | Path | Auth | Body | Response |
|--------|------|------|------|----------|
| POST | `/api/auth/register` | none | `{email, password, full_name}` | `{token, user}` — creates a `driver`, `coin_balance=0`. 400 on duplicate email. |
| POST | `/api/auth/login` | none | `{email, password}` | `{token, user}` — 401 on bad credentials; **403 `account_disabled`** for an admin-disabled account (checked AFTER the password, so it's only shown to the real owner — no disabled-account oracle; redesign/ui-v3, migration `0025_user_disable`). |
| GET | `/api/auth/me` | JWT | — | `{id, email, full_name, role, coin_balance, available_balance}` — `available_balance` (added 2026-07-12) is `coin_balance` minus coins held by the driver's OTHER active sessions' authorization holds (`services/wallet.py available_balance`); additive, `coin_balance` unchanged |
| POST | `/api/auth/logout` | JWT | — | Revokes every token for the caller (bumps `users.token_version`; "log out everywhere") → `{status:"logged_out"}`. |
| POST | `/api/auth/forgot-password` | none | `{email}` | Issues a single-use reset token (SHA-256 digest stored in `password_reset_tokens`, `RESET_TOKEN_TTL_MIN` expiry, prior unused tokens voided) and emails `FRONTEND_ORIGIN/reset-password?token=...` via `services/email.py` (SMTP if `SMTP_HOST` set, else the link is logged at WARNING). **Always the same generic 200** — no account enumeration. Rate-limited (`FORGOT_PASSWORD_RATE_LIMIT`). |
| POST | `/api/auth/reset-password` | none | `{token, password}` | Consumes the token: 8-72 char rule (as registration), bcrypt rehash, bumps `users.token_version` (revokes every session), stamps the token used. Uniform 400 for unknown/expired/already-used tokens. Rate-limited (`RESET_PASSWORD_RATE_LIMIT`). → `{status:"password_reset"}` |
| GET | `/api/auth/google/login` | none | — | **(2026-08-02)** "Sign in with Google" — backend-driven authorization-code flow, no JS SDK. 503 if `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET`/`GOOGLE_OAUTH_REDIRECT_URI` aren't all set. Sets a short-lived `google_oauth_state` CSRF-nonce cookie (httpOnly, Secure, SameSite=Lax, 10 min — the app's only cookie; auth stays bearer-JWT-only) and 302s to Google's consent screen. |
| GET | `/api/auth/google/callback` | none | query `code`, `state` | **(2026-08-02)** Google's redirect target. Validates `state` against the cookie (constant-time compare; 400 + cookie cleared on mismatch/absence), exchanges `code` server-side, verifies the ID token against Google's JWKS (`google-auth`), and rejects an unverified email (400). Existing account by email + no linked Google identity → links it; linked to a *different* Google account → 403; no account → creates a `driver` with an unusable random password hash (`auth_provider="google"`, same "dummy hash" trick as `_DUMMY_PASSWORD_HASH`) so `/api/auth/login` refuses it unchanged. Disabled accounts get the same `account_disabled` 403 as password login. On success, 302s to `FRONTEND_ORIGIN/auth/google/callback#token=<jwt>` — the JWT rides in the URL **fragment**, never a query string, so it never reaches server access logs. |

`user` object shape: `{id, email, full_name, role, coin_balance}` (where `coin_balance` is a float; the `/api/auth/register`/`/api/auth/login` `AuthResponse.user` dict predates `available_balance` and hasn't been extended to it — only `GET /api/auth/me` (`UserResponse`) carries the new field today).
Token: HS256 JWT, claims `sub`/`role`/`email`/`iat`/`exp`, **7-day** expiry.

## Charger groups

| Method | Path | Auth | Body | Behaviour |
|--------|------|------|------|-----------|
| POST | `/api/groups/join` | JWT | `{access_code}` | Join a private group by code. 404 if unknown, 400 if the group is public or already joined. → `{status:"joined", group_id, group_name}` |
| GET | `/api/groups/my` | JWT | — | All public groups + private groups the user joined, deduped → `[{id, name, is_public, plug_count}]` |
| DELETE | `/api/groups/{group_id}/leave` | JWT | path `group_id:int` | Leave a private group the caller previously joined — deletes their membership row (the `join` inverse; redesign/ui-v3). 404 when the caller isn't a member (covers unknown groups too; public groups have no memberships to leave). → `{status:"left", group_id}` |

## Plugs

| Method | Path | Auth | Body/Params | Behaviour |
|--------|------|------|-------------|-----------|
| GET | `/api/plugs/available` | JWT | — | Plugs in accessible groups **or** ungrouped (`group_id IS NULL`, visible to all) → `[{id, name, status, current_power_w, plug_model, group_name?, gateway_online, is_private, watching, reserved_now, reserved_now_by_me, reserved_until, next_reservation}]` — `gateway_online: bool` (added 2026-07-10) is whether the plug's gateway is live: `ONLINE` + `last_seen` within the liveness window; `is_private: bool` (added 2026-07-12) is true for plugs in a non-public group (necessarily one the caller joined) — drives the Home page's "Your chargers" vs "Public chargers" sections; `watching: bool` (added 2026-07-12) is whether the **caller** has an armed "notify me when free" watch on the plug (computed with ONE extra per-user query for the whole list, not per plug). The reservation fields (added 2026-07-12, all plugs batched into ONE grouped query so the endpoint stays N+1-free): `reserved_now: bool` = a BOOKED window covers right now (after lazy no-show expiry), `reserved_now_by_me: bool` = the caller holds it, `reserved_until: str?` = ISO end of that covering window, `next_reservation: {start_at, end_at}?` = the next strictly-future BOOKED window |
| GET | `/api/plugs/public` | **none (public)** | — | **Pre-signup discovery map (2026-07-16).** UNAUTHENTICATED. Returns ONLY **public**-group or ungrouped plugs that have a known location, with a deliberately minimal projection → `[{id, name, status, latitude, longitude, price_per_kwh, gateway_online}]`. Private/society plugs (non-public group) are **never** included — no per-user, session, or network fields are exposed. Rate-limited per IP (`PUBLIC_MAP_RATE_LIMIT`, default 60/60). Declared before `/api/plugs/{plug_id}` so the static path wins. Starting a charge still requires an account. |
| GET | `/api/plugs/{plug_id}` | JWT | path `plug_id:int` | Single plug; 404 if missing, 403 if in a private group the user hasn't joined. Response also carries `gateway_online`, `is_private`, `watching`, and the reservation fields (as above) |
| POST | `/api/plugs/{plug_id}/watch` | JWT | path `plug_id:int` | Arm a **one-shot "notify me when free" watch** (2026-07-12): when the plug next flips back to AVAILABLE (session end or CPO maintenance-clear), the caller gets a `plug_available` notification through the standard pipeline (feed + Socket.io + Web Push, `services/plug_watch.py`) and the watch deletes itself. Idempotent (re-arming returns the same 200; a concurrent double-tap is absorbed via the `UNIQUE(user_id, plug_id)` constraint). Access = the `GET /api/plugs/{id}` rule (403 for a non-member on a private-group plug; 404 unknown plug). Occupied/offline/maintenance plugs are all watchable; the one rejection is 409 when the plug is startable right now (AVAILABLE **and** its gateway live). → `{status:"watching", plug_id, watching:true}` |
| DELETE | `/api/plugs/{plug_id}/watch` | JWT | path `plug_id:int` | Disarm the caller's watch. Idempotent — a watch that doesn't exist (never armed / already fired / plug gone) is a no-op 200; deliberately no access check (the row is the caller's own — a driver who left the plug's private group can still clear it). → `{status:"not_watching", plug_id, watching:false}` |
| GET | `/api/plugs/{plug_id}/tariff-preview` | JWT (any role) | path `plug_id:int` | **(redesign/ui-v3)** The plug's EFFECTIVE price schedule, previewed before starting → `{base_price_per_kwh, price_now, slots:[{days, start_minute, end_minute, price_per_kwh}]}`. Resolves through the SAME chain billing uses (`services/pricing.py`: plug → group → tenant default → env fallback), so preview and billing can never disagree; `price_now` re-resolves at "now" in the tenant's local zone. `days` is weekday indices (0=Mon…6=Sun) expanded from the slot's `days_mask`; no tariff anywhere in the chain → the env default rate with `slots: []`. Visibility follows `GET /api/plugs/{id}` (403 on an unjoined private-group plug; 404 unknown). |
| POST | `/api/plugs/{plug_id}/request-capacity` | JWT | path `plug_id:int` | **(caps + circuits)** Arm a one-shot "request capacity" after a start was blocked by a full shared circuit (the `circuit_full` 409). When the circuit next has room (a session on it ends, or the operator raises the cap) the driver gets a `capacity_available` notification and the row self-clears (`services/capacity.py`). Idempotent (`UNIQUE(user_id, plug_id)`). 400 if the plug isn't on a capacity-capped circuit, or the circuit already has room right now. Access follows `GET /api/plugs/{id}`. → `{status:"requested", plug_id}` |

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
| PATCH | `/api/sessions/{session_id}/limits` | JWT | path `session_id:int`; `{max_kwh?, max_duration_seconds?}` (same bounds as `SessionStartRequest`; 400 if neither given) | "Start now, set the target later": update a RUNNING session's stop conditions. Owner + ACTIVE check (409 otherwise). Enforcement is backend-side and near-immediate (the telemetry-path auto-stop reads the session's limits fresh every ~1 s); best-effort pushes a firmware `SET_LIMITS` command so a RAISED limit also takes effect on the on-device relay watchdog (lowering is exact either way; billing is always metered energy so neither case can misbill). Re-sizes `hold_coins` under the user-row lock, same `min(available, max_kwh × worst-case rate over the window)` rule the start path uses. → `{status:"updated", session_id, max_kwh, max_duration_seconds}` |
| GET | `/api/sessions/active` | JWT | — | Retrieve **all** active sessions for the logged-in user, newest first → `{active:true, sessions:[{session_id, plug_id, plug_name, started_at, max_kwh, max_duration_seconds}, …], session_id, plug_id, plug_name, started_at}` (`max_kwh`/`max_duration_seconds` added 2026-07-12 — NULL for legacy sessions; the top-level single-session fields mirror the newest entry for older clients) or `{active:false, sessions:[]}` |
| — | `Socket.io` connection | JWT | connection query or auth dict | Real-time bi-directional channel for telemetry updates and session status (sole live-telemetry transport since 2026-07-07). |
| GET | `/api/sessions/history` | JWT | query `limit=50` (max 200), `offset=0` | The caller's past sessions, newest first. **Shape changed (redesign/ui-v3):** now paginated per the house convention → `{total, items:[{id, plug_id, plug_name, started_at, ended_at, energy_kwh, coins_spent, status}]}` (`plug_name` joined in — the list stays N+1-free). |
| GET | `/api/sessions/{session_id}` | JWT | path `session_id:int` | **(redesign/ui-v3)** Full receipt/detail for ONE session — the same field shape the stop response returns (so a receipt component renders either interchangeably): `{status, session_id, plug_id, plug_name, energy_kwh, peak_power_w, price_per_kwh, settled_cost_coins, coins_spent, shortfall_coins, balance_before, balance_remaining, duration_sec, started_at, ended_at, max_kwh, max_duration_seconds, reason}` — `status` carries the session's REAL status (a live session is viewable too). Access mirrors `/{id}/invoice`: the owning driver, or a cpo/admin of the owning tenant — anyone else gets 404 (existence not leaked). `balance_before`/`balance_remaining`/`reason` are recovered from the session's `SESSION_DEBIT` ledger row (None while ACTIVE / for pre-ledger rows). Registered LAST in the router so the static `/active`/`/history`/`/queued`/`/disputes/my` siblings always win; a non-integer segment 422s. |
| GET | `/api/me/stats` | JWT | — | **(redesign/ui-v3)** Current-UTC-calendar-month + lifetime charging aggregates for the caller → `{month:{energy_kwh, spend_coins, sessions}, lifetime:{…}}`. Only finished sessions count (status ≠ ACTIVE — `coins_spent` is only written at finalize). Lives in `routers/sessions.py`. |
| GET | `/api/sessions/disputes/my` | JWT | — | **(redesign/ui-v3)** The caller's disputes, newest first — the driver-side mirror of the CPO's `GET /api/cpo/disputes` (which owns resolution) → `[{id, session_id, status, reason, resolution_note, refund_coins, created_at, resolved_at}]` |
| POST | `/api/sessions/{session_id}/dispute` | JWT | path `session_id:int`; `{reason}` (10–1000 chars) | File a dispute against one of the caller's own **finished** sessions (404 if not owner, 409 if still ACTIVE). Coins-only remedy — no Razorpay money-out path (see `MARKET_GAP_ANALYSIS.md` §3 "Refunds"). At most one OPEN dispute per session (409, also DB partial-unique backstop). Reviewed by the owning CPO via `GET /api/cpo/disputes` / `POST /api/cpo/disputes/{id}/resolve`. → `DisputeResponse` `{id, session_id, tenant_id, driver_user_id, reason, status, resolution_note, refund_coins, created_at, resolved_at, resolved_by_user_id}` |
| GET | `/api/sessions/{session_id}/invoice` | JWT | path `session_id:int`; query `format?` (`html`) | GST tax invoice for a finished, billed session — issued on first call, idempotent thereafter (`services/invoices.py issue_invoice_for_session`). Access: the owning driver, or a cpo/admin of the owning tenant — anyone else 404 (existence not leaked). 400 if the session isn't invoiceable yet. `?format=html` returns a minimal printable invoice (inline CSS) instead of JSON; otherwise the same row shape as `GET /api/cpo/invoices` items. |

### Queued charges (`services/session_reaper.py`, queue-a-charge-during-an-outage)

Queue an auto-start on a plug whose gateway is ONLINE but whose line power is
out (the plug stopped reporting telemetry). When power returns and holds for
the CPO's debounce (`auto_start_delay_min`), the session reaper starts the
session with the snapshotted stop conditions. Funds are **not** locked at
queue time — only the balance floor is re-checked, then re-verified at
auto-start.

| Method | Path | Auth | Body/Params | Behaviour |
|--------|------|------|-------------|-----------|
| POST | `/api/sessions/queue` | JWT | `{plug_id, max_kwh?, max_duration_seconds?}` (same bounds/defaults as `SessionStartRequest`) | Structured 409 `detail.code` on each reject (like the caps `circuit_full` block): plug access (403 private-group non-member), `gateway_offline`, `plug_powered` (already has power — just start), `queue_disabled` (CPO hasn't enabled queued charging for it), 402 `insufficient_balance` (available balance below `MIN_START_BALANCE_COINS`), `queue_limit` (`MAX_QUEUED_CHARGES_PER_USER` WAITING already), `already_queued` (one WAITING queue per driver+plug, also a DB partial-unique backstop). Notifies the driver. → 201 `{id, plug_id, status, created_at, expires_at, max_kwh, max_duration_seconds}` |
| GET | `/api/sessions/queued` | JWT | — | The caller's WAITING queued charges, soonest-created first, with `plug_name` → `[{id, plug_id, plug_name, status, created_at, expires_at, max_kwh, max_duration_seconds}]` |
| DELETE | `/api/sessions/queue/{queued_charge_id}` | JWT | path `queued_charge_id:int` | Cancel a WAITING queued charge (owner only, row-locked + re-checked WAITING so a cancel racing the reaper's auto-start settles once — 409 if the reaper already started/expired it). 404 if missing/foreign. → `{status:"cancelled"}` |

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
| PUT | `/api/cpo/profile` | cpo/admin | `{queued_charging_enabled?, auto_start_delay_min?, queue_ttl_min?, gstin?, legal_name?, invoice_prefix?}` (all optional, omitted = unchanged) | Update the tenant-level settings: the queued-charge defaults every plug inherits unless it carries its own override (`PUT /api/cpo/plugs/{id}`), plus the GST seller identity stamped onto issued tax invoices (an empty string clears a field back to NULL). → `{status:"updated", queued_charging_enabled, auto_start_delay_min, queue_ttl_min, gstin, legal_name, invoice_prefix}` |
| GET | `/api/cpo/gateways` | cpo/admin | — | Tenant's gateways (each with `plug_count`). |
| POST | `/api/cpo/gateways` | cpo/admin | `{gateway_id, name, vpn_ip}` | Register a gateway under the tenant. |
| POST | `/api/cpo/gateways/{id}/ota` | cpo/admin | `{firmware_url}` (http(s), ≤512 chars) | Trigger an OTA firmware update. Requires the gateway `ONLINE` (409 if offline — gates on the status flag, **not** telemetry freshness, so a gateway with an unreachable plug can still be updated) and ≥1 plug (409 — the OTA command rides a per-plug command topic). Publishes the `OTA` command (502 on publish fail); the gateway downloads into its passive slot and reboots (rollback-protected), refusing mid-session. → `{status:"ota_triggered", gateway_id, firmware_url, message}` |
| GET | `/api/cpo/plugs` | cpo/admin | — | All plugs across the tenant's gateways (status, power, group). |
| POST | `/api/cpo/plugs` | cpo/admin | `{gateway_id, name, local_ip, plug_model?, group_id?}` | Register a plug (validates gateway + group ownership). |
| PUT | `/api/cpo/plugs/{id}` | cpo/admin | `{name?, group_id?, status?, local_ip?, max_current_a?}` | Update a plug (`group_id:0` = unassign). Changing `local_ip` (e.g. after a DHCP change) or `max_current_a` re-publishes the gateway's retained plug roster to the device. |
| POST | `/api/cpo/plugs/{id}/maintenance` | cpo/admin | path `id:int`; `{action, note?}` (`action` = `enter`\|`clear`, 400 otherwise) | Dedicated operator maintenance workflow (fault console) — distinct from the general status setter above. `enter` always succeeds; `clear` is refused (409) while the plug has an ACTIVE session. Always audited (`plug.maintenance_enter`/`plug.maintenance_clear`); emits the plug's new status over Socket.io, and a `clear` back to AVAILABLE fans out `plug_available` to any "notify me when free" watchers. → `{status:"updated", plug_id, action, plug_status}` |
| GET | `/api/cpo/groups` | cpo/admin | — | Tenant's charger groups (with `plug_count`, `member_count`, `access_code`). |
| POST | `/api/cpo/groups` | cpo/admin | `{name, is_public?}` | Create a group; private groups get a generated access code. |
| PUT | `/api/cpo/groups/{id}` | cpo/admin | `{name?, is_public?, regenerate_access_code?}` | Update a group / rotate access code. |
| DELETE | `/api/cpo/groups/{id}` | cpo/admin | — | Delete a group; assigned plugs become ungrouped. |
| GET | `/api/cpo/groups/{id}/members` | cpo/admin | path `id:int` | **(redesign/ui-v3)** Members of one of the tenant's groups (the drivers who joined via its access code), oldest joiner first → bare list `[{user_id, email, full_name, joined_at}]`. 404 for another tenant's group (indistinguishable from "doesn't exist"). |
| DELETE | `/api/cpo/groups/{id}/members/{user_id}` | cpo/admin | path ids | **(redesign/ui-v3)** Remove a member (revokes their access without rotating the code for everyone else). Tenant-scoped on the group; 404 if the user isn't a member. Audited as `group.member_remove`. → `{status:"removed", group_id, user_id}` |
| GET | `/api/cpo/analytics/overview` | cpo/admin | — | Plugs/gateways/active-session counts + today & all-time energy/revenue. |
| GET | `/api/cpo/analytics/sessions` | cpo/admin | query `plug_id?, status_filter?, days=30, limit=50` (max 200), `offset=0` | Session history enriched with plug name, user email, duration. **Shape changed (redesign/ui-v3):** paginated → `{total, totals:{count, energy_kwh, revenue_coins}, items, sessions}` — `totals` is computed SERVER-SIDE over the full filtered set (the page slice never truncates the aggregates); `sessions` aliases `items` for pre-contract callers. |
| GET | `/api/cpo/analytics/sessions.csv` | cpo/admin | query `plug_id?, status_filter?, days=30` | Same tenant scope/filters as above, returned as a downloadable `text/csv` attachment (capped 10k rows). |
| GET | `/api/cpo/analytics/revenue` | cpo/admin | query `days=30` | Daily `{date, revenue_coins, session_count}` series. |
| GET | `/api/cpo/analytics/energy` | cpo/admin | query `days=30` | Daily `{date, energy_kwh, session_count}` series. |
| GET | `/api/cpo/analytics/telemetry` | cpo/admin | query `plug_id?, days=1, bucket=hour` | `date_trunc`-bucketed time-series from `telemetry_readings` (bucket ∈ {minute, hour, day}; 400 otherwise) → `[{timestamp, avg_power_w, max_power_w, energy_kwh, avg_current_a, max_current_a, sample_count}]`. Powers the dashboard load graph (peak W + A). |
| GET | `/api/cpo/events` | cpo/admin | query `limit=50` (max 200), `offset=0`, `unacknowledged_only?` (bool), `severity?` (e.g. `critical`) | Gateway/plug operational events (safety cutoffs, `UNAUTHORIZED_ON` alarms, OTA notices) for the CPO's tenant, newest first. **Shape changed (redesign/ui-v3):** paginated → `{total, items:[{id, gateway_id, plug_id, event_type, severity, detail, acknowledged, created_at}]}` — `total` counts the full filtered set. (Added 2026-07-10.) |
| POST | `/api/cpo/events/{event_id}/ack` | cpo/admin | path `event_id:int` | Acknowledge (clear from the active feed) one event; tenant-scoped. → `{status:"acknowledged", event_id}` |
| GET | `/api/cpo/audit` | cpo/admin | query `limit=50` (max 200), `offset=0` | Admin action audit trail (TD#26) for the CPO's tenant, newest first → `[{id, actor_user_id, actor_email, action, target_type, target_id, detail, created_at}]`. Covers gateway create, plug create, plug status change, plug maintenance enter/clear, group create/delete, access-code regen, and the payout money ops (`payout.request` / `payout.mark_paid` / `payout.cancel` — a payout audit row always lands in the *payout's* tenant, so an admin's mark_paid/cancel shows up in the owning CPO's trail) — written non-fatally by `services/audit.py` (a write failure is logged, never breaks the admin action). Gateway/plug delete aren't recorded yet — no such CPO endpoints exist. (Added 2026-07-12.) |
| GET | `/api/cpo/earnings` | cpo/admin | — | Lifetime + unsettled (settlement watermark → now) earnings for the caller's tenant → `{watermark, as_of, platform_fee_pct, lifetime:{gross_coins, platform_fee_coins, net_coins}, unsettled:{period_start, period_end, gross_coins, platform_fee_coins, net_coins}, topup_pool:{available_coins, already_issued_coins}}`. Coins are ₹-equivalent (1 coin = ₹1); fee = `PLATFORM_FEE_PCT` (default 10%). `topup_pool.available_coins` (added 2026-07-21) is `unsettled.net_coins` minus offline top-ups already issued since the watermark — the same figure `POST /api/cpo/topups` is capped at and `POST /api/cpo/payouts` pays out. Powers `/cpo/earnings`. |
| POST | `/api/cpo/payouts` | cpo/admin | — | Snapshot the tenant's unsettled earnings into a REQUESTED payout. `net_coins` is the unsettled net **minus** offline top-ups already issued since the watermark (2026-07-21 — see `POST /api/cpo/topups` below), so the same earnings are never paid out twice; `gross_coins`/`platform_fee_coins` still reflect the true session earnings. 400 if nothing left to pay out (payable net ≤ 0 — including when top-ups already consumed the whole window), 409 if a request is already pending (one at a time); race-safe via a tenant-row lock shared with the top-up endpoint. Audited as `payout.request`. → payout object (below). |
| GET | `/api/cpo/payouts` | cpo/admin | — | The tenant's payouts, newest request first → `[{id, tenant_id, period_start, period_end, gross_coins, platform_fee_coins, net_coins, status, requested_by_user_id, requested_at, paid_at, note}]` (`status` ∈ requested/paid/cancelled). |
| POST | `/api/cpo/payouts/{id}/mark_paid` | **admin only** | path `id:int` | Record that a REQUESTED payout was settled **out-of-band** (bank/UPI outside the app — no money moves here). 404 unknown id, 409 if not REQUESTED (row-locked, replay-safe). Audited as `payout.mark_paid` into the payout's tenant. |
| POST | `/api/cpo/payouts/{id}/cancel` | owner cpo/admin | path `id:int` | Cancel a REQUESTED payout, freeing its window for a future request. Cross-tenant callers get 404 (indistinguishable from "doesn't exist"); 409 if not REQUESTED. Audited as `payout.cancel` into the payout's tenant. |
| POST | `/api/cpo/topups` | cpo/admin | body `{driver_email, amount_coins, note?}` | **(2026-07-21)** Credit `driver_email`'s coin wallet by `amount_coins`, funded entirely from the tenant's own `topup_pool.available_coins` — never coins from nothing. 404 if no driver account exists with that email; 409 (with the actual available figure) if the amount would exceed the pool. Row-locks the tenant (same row `POST /api/cpo/payouts` locks, so the two serialize against each other) then the driver, credits via `credit_wallet()`, writes a driver-side `LedgerTransaction` (`tx_type` = new enum value `cpo_topup`) and an `offline_topups` row, audited as `topup.create`, and notifies the driver. → `{id, tenant_id, actor_user_id, actor_email, driver_user_id, driver_email, amount_coins, note, created_at}`. |
| GET | `/api/cpo/topups` | cpo/admin | query `limit=50` (max 200), `offset=0` | **(2026-07-21)** The tenant's offline top-up history, newest first → `{total, items:[...same shape as POST above...]}`. |
| GET | `/api/cpo/reservations` | cpo/admin | query `status?` (booked/cancelled/fulfilled/expired; 400 otherwise), `upcoming_only?` (bool), `limit=50` (max 200), `offset=0` | The tenant's plug reservations, newest window first, each with `plug_name` + the driver's `user_email`/`user_name`. Lazily expires lapsed holds first (so a no-show is never shown as BOOKED). Cancelling uses the shared `POST /api/reservations/{id}/cancel`, which admits the owning tenant's cpo/admin. (Added 2026-07-12.) **Shape changed (redesign/ui-v3):** paginated → `{total, items}` — `total` counts the full filtered set. |
| GET | `/api/cpo/invoices` | cpo/admin | query `limit=50` (max 200), `offset=0` | The tenant's issued GST invoices, newest first. **Shape changed (redesign/ui-v3):** paginated → `{total, items}` (rows via `invoice_to_dict` — same shape as `GET /api/sessions/{id}/invoice`). |
| GET | `/api/cpo/invoices.csv` | cpo/admin | query `days?` | **(redesign/ui-v3)** Export the tenant's issued GST invoices as a downloadable `text/csv` attachment — mirrors `sessions.csv` (capped 10k rows). `days` is optional (invoices are a legal ledger, so the default export is the FULL history); pass `days=N` to window to invoices issued in the last N days. Columns: invoice_number, issued_at, session_id, driver_user_id, energy_kwh, rate_coins_per_kwh, amount_coins, taxable_value_inr, gst_rate_pct, gst_amount_inr, total_inr, seller_legal_name, seller_gstin. |

### Tariffs & pricing (Pricing v2 — `Tariff`/`TariffSlot`)

Per-CPO pricing plans, with optional time-of-day (TOD) slots. Billing resolves
the effective rate through `services/pricing.py`: **plug → charger group →
tenant default → env fallback**; see also `GET /api/plugs/{id}/tariff-preview`
above, which resolves through the same chain. Editing a tariff/slot's rate (or
deleting one) marks the tenant's in-flight sessions for a forward-only reprice
(`mark_tenant_sessions_for_reprice`) — never retroactive.

| Method | Path | Auth | Body/Params | Behaviour |
|--------|------|------|-------------|-----------|
| GET | `/api/cpo/tariffs` | cpo/admin | — | The tenant's tariffs, by name → `[{id, name, price_per_kwh, created_at, updated_at}]` |
| POST | `/api/cpo/tariffs` | cpo/admin | `{name, price_per_kwh}` (`price_per_kwh` 0 < x ≤ 1000) | Create a tariff under the caller's tenant. → `{status:"created", tariff_id, name, price_per_kwh}` |
| PUT | `/api/cpo/tariffs/{tariff_id}` | cpo/admin | path `tariff_id:int`; `{name?, price_per_kwh?}` | Update a tariff's name and/or flat rate (tenant-scoped, 404 cross-tenant). A rate change triggers the forward-only reprice. → `{status:"updated", tariff_id, name, price_per_kwh}` |
| DELETE | `/api/cpo/tariffs/{tariff_id}` | cpo/admin | path `tariff_id:int` | Delete a tariff. Any plug/group/tenant-default pointing at it falls back to the next link in the resolution chain (FKs are `ON DELETE SET NULL`) rather than dangling; triggers the forward-only reprice. → `{status:"deleted", tariff_id, name}` |
| GET | `/api/cpo/tariffs/{tariff_id}/slots` | cpo/admin | path `tariff_id:int` | The tariff's TOD slots, ordered by `start_min` → `[{id, tariff_id, start_min, end_min, price_per_kwh, days_mask}]` |
| POST | `/api/cpo/tariffs/{tariff_id}/slots` | cpo/admin | `{start_min, end_min, price_per_kwh, days_mask=127}` (half-open minute-of-day, `days_mask` Mon=bit0…Sun=bit6) | Add a TOD slot. 400 if `start_min >= end_min` (model a wrap-around window like 22:00–06:00 as two slots); 409 if it overlaps an existing slot on the tariff. Triggers the forward-only reprice. → `{status:"created", ...slot}` |
| PUT | `/api/cpo/tariffs/{tariff_id}/slots/{slot_id}` | cpo/admin | path ids; `{start_min?, end_min?, price_per_kwh?, days_mask?}` | Update a slot (omitted fields keep their current value); re-validates the resulting window (400/409 as above). Triggers the forward-only reprice. → `{status:"updated", ...slot}` |
| DELETE | `/api/cpo/tariffs/{tariff_id}/slots/{slot_id}` | cpo/admin | path ids | Delete a TOD slot — the parent tariff's flat price applies to that window again. Triggers the forward-only reprice. → `{status:"deleted", slot_id, tariff_id}` |
| PUT | `/api/cpo/plugs/{plug_id}/tariff` | cpo/admin | path `plug_id:int`; `{tariff_id?}` (`null` unassigns) | Assign/unassign the tariff a specific plug bills at. Both the plug and the tariff must belong to the caller's tenant (404 otherwise). → `{status:"updated", plug_id, tariff_id}` |
| PUT | `/api/cpo/groups/{group_id}/tariff` | cpo/admin | path `group_id:int`; `{tariff_id?}` | Assign/unassign the tariff a charger group's plugs fall back to when they carry no tariff of their own. Same tenant scoping as the plug endpoint. → `{status:"updated", group_id, tariff_id}` |
| PUT | `/api/cpo/tenant/default-tariff` | cpo/admin | `{tariff_id?}` | Assign/unassign the CPO's tenant-wide default tariff — the last link in the resolution chain before the global `COINS_PER_KWH` env fallback. → `{status:"updated", tenant_id, tariff_id}` |

### Session disputes (CPO side — `SessionDispute`)

Coins-only refund/dispute review for sessions billed on the CPO's own plugs.
Driver-side filing is `POST /api/sessions/{id}/dispute` (above); this is the
review/resolution half. See also `GET /api/admin/disputes` (cross-tenant,
read-only — resolution stays here).

| Method | Path | Auth | Body/Params | Behaviour |
|--------|------|------|-------------|-----------|
| GET | `/api/cpo/disputes` | cpo/admin | query `status_filter?` (`open`\|`approved`\|`rejected`, 400 otherwise), `limit=100` (max 500) | Disputes filed against sessions on the CPO's own plugs, newest first (`tenant_id` denormalized onto the row at creation, no join). → `[DisputeResponse]` (see the driver-side row shape above) |
| POST | `/api/cpo/disputes/{dispute_id}/resolve` | cpo/admin | path `dispute_id:int`; `{action, note?, refund_coins?}` (`action` = `approve`\|`reject`, 400 otherwise) | Resolve an OPEN dispute (404 cross-tenant/unknown, 409 if already resolved — the dispute row and, on approve, the session row are lock-and-recheck race-safe). **Reject:** status + `resolution_note` only. **Approve:** credits the driver's wallet and writes a `refund` ledger row referencing the session; `refund_coins` defaults to the session's `coins_spent` and the cumulative APPROVED refund for a session can never exceed it (400 if the session cost 0 coins and no override is given, or if `refund_coins ≤ 0`). → `DisputeResponse` |

## Platform Admin Console (`/api/admin/*`, `routers/admin.py`, redesign/ui-v3)

The admin console's API surface (implemented in `routers/admin.py`):
cross-tenant visibility plus the two platform-level user mutations. Every endpoint
requires the **`admin`** role via `require_role("admin")` and deliberately does
NOT require a `tenant_id` on the caller — platform admins have `tenant_id NULL`
by design. All paginated lists follow the house `{total, items}` + `limit`
(max 200) / `offset` convention. Mutations are audited via `services/audit.py`
(`AuditLog.tenant_id` was relaxed to nullable in migration `0025_user_disable`
so platform-level actions on tenant-less users are recordable; NULL rows never
surface in the tenant-scoped `GET /api/cpo/audit`).

| Method | Path | Auth | Body/Params | Behaviour |
|--------|------|------|-------------|-----------|
| GET | `/api/admin/stats/overview` | admin | — | Cross-tenant platform KPIs → `{tenants, users:{total, drivers, cpos, admins}, gateways:{total, online}, plugs:{total}, sessions:{active, today, total}, energy_kwh:{today, total}, revenue_coins:{today, total}, payouts:{requested_count, requested_net_coins}, disputes:{open}}`. Revenue/energy count COMPLETED/PAID sessions; "today" = UTC midnight. |
| GET | `/api/admin/tenants` | admin | query `q?` (name substring, case-insensitive), `limit`, `offset` | All tenants with per-tenant fleet/usage aggregates, newest first → items `{id, name, created_at, user_count, gateway_count, gateways_online, plug_count, sessions_30d, revenue_30d_coins, pending_payouts}` (correlated scalar subqueries — no N+1). |
| GET | `/api/admin/tenants/{id}` | admin | path `id:int` | The list row's aggregates + `{gst_number, legal_name, default_tariff_id, recent_sessions:[…10 rows with plug_name/user_email], payouts:[…50, same shape as GET /api/cpo/payouts]}`. 404 unknown. |
| GET | `/api/admin/users` | admin | query `q?` (email/name substring), `role?` (400 on unknown), `limit`, `offset` | All accounts, newest first → items `{id, email, full_name, role, tenant_id, tenant_name, coin_balance, is_disabled, created_at}`. |
| PATCH | `/api/admin/users/{id}` | admin | `{role?, is_disabled?}` (400 if both omitted / unknown role) | Change a user's role and/or disabled flag. **403 on self-demote/self-disable** (a lone admin can't lock themselves out). A role change or a disable bumps `token_version` (every outstanding JWT dies immediately; `get_current_user` also rejects disabled users directly — belt and braces). Audited as `user.update`. → `{status:"updated", id, role, is_disabled, tokens_revoked}` |
| POST | `/api/admin/users/{id}/adjust-balance` | admin | `{amount_coins, reason}` (signed, non-zero; reason 3–200 chars mandatory) | Manual wallet adjustment (goodwill credit / clawback). Row-locked; a debit is **floored at 0** (the DB CHECK forbids negative balances) and the ledger row records the ACTUAL applied delta (typed `topup` for a credit, `session_debit` for a debit; description carries "Admin balance adjustment: <reason>"). Audited as `user.adjust_balance`. → `{new_balance}` |
| GET | `/api/admin/payouts` | admin | query `status?` (requested/paid/cancelled; 400 otherwise), `limit`, `offset` | Every tenant's payouts, newest request first → items = the `GET /api/cpo/payouts` row shape + `tenant_name`. Settling stays on the existing admin-only `POST /api/cpo/payouts/{id}/mark_paid`. |
| GET | `/api/admin/gateways` | admin | query `online?` (bool), `limit`, `offset` | Cross-tenant gateway fleet, most recently seen first → items `{id, gateway_id, name, tenant_id, tenant_name, online, last_seen_at, firmware_version, plug_count}`. `online` derives from `Gateway.status == ONLINE` — the same flag the CPO OTA gate uses. |
| GET | `/api/admin/disputes` | admin | query `status?` (open/approved/rejected; 400 otherwise), `limit`, `offset` | Every tenant's disputes, newest first → items = dispute fields + `{tenant_name, user_email, session_cost_coins}`. Resolution stays on the tenant-scoped `POST /api/cpo/disputes/{id}/resolve`. |
| GET | `/api/admin/audit` | admin | query `tenant_id?`, `limit`, `offset` | Cross-tenant audit trail, newest first (row shape mirrors `GET /api/cpo/audit` + `tenant_id`/`tenant_name`). Platform-level rows (admin user actions) carry `tenant_id NULL` and appear only in the unfiltered view. |

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
| `FRONTEND_ORIGIN` | `https://amphive.app` | Base URL for links in outbound email (`/reset-password?token=...`) and the Google OAuth callback redirect (`/auth/google/callback#token=...`) |
| `RESET_TOKEN_TTL_MIN` | `30` | Minutes a password-reset link stays valid (single use) |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` / `GOOGLE_OAUTH_REDIRECT_URI` | `""` / `""` / `""` | **(2026-08-02)** OAuth client from the Google Cloud Console; `GOOGLE_OAUTH_REDIRECT_URI` must exactly match an "Authorized redirect URI" on that client (e.g. `https://amphive.app/api/auth/google/callback`). Any one unset = "Sign in with Google" hidden everywhere (`google_login_enabled: false`, `/api/auth/google/login` 503s) |
| `VAPID_PRIVATE_KEY` / `VAPID_SUBJECT` | `""` / `mailto:admin@amphive.example` | Web Push signing key + contact; empty key = push disabled (feed + Socket.io still work) |
| `LOW_BALANCE_WARN_FRACTION` | `0.8` | Notify the driver once per session when accrued cost crosses this fraction of the wallet balance (`0` disables) |
| `PLATFORM_FEE_PCT` | `10.0` | Platform's cut of CPO gross earnings, percent — the fee/net split on `/api/cpo/earnings` and payout snapshots (`services/payouts.py`; falls back to the default on a malformed value) |
| `RESERVATION_MAX_HOURS` | `4` | Longest bookable reservation window (`services/reservations.py`) |
| `RESERVATION_MAX_ADVANCE_DAYS` | `7` | How far ahead a reservation may start (also the per-plug schedule horizon) |
| `MAX_UPCOMING_RESERVATIONS_PER_USER` | `2` | Per-user cap of BOOKED reservations with `end_at` in the future (409 past it) |
| `RESERVATION_NO_SHOW_GRACE_MIN` | `15` | Minutes after `start_at` an unfulfilled BOOKED reservation lazily flips to EXPIRED and stops blocking the plug |
