# AmpHive — Security Notes

*Verified against source on 2026-07-02. This is a developer-facing inventory of
known security gaps, not a formal audit. Items are roughly ordered by severity.*

> Several real secrets were committed to git history. **As of 2026-07-06 all four
> — WireGuard keypair, DuckDNS token, Tapo password, DB password — have been
> rotated at the source**, so the history copies are now dead. Removing them from
> `HEAD` was never remediation on its own; rotation was. A history scrub remains
> optional hygiene.

> **Recently fixed** (see [§7](#7-recently-fixed)): RBAC is now enforced on all
> `/api/cpo/*` routes, the unauthenticated `gateways/register` / `plugs/register`
> endpoints were removed, wallet updates are row-locked, and the Razorpay webhook
> credits idempotently. The items in §1–§6 are the gaps that **remain**.

---

## 1. Committed secrets (ROTATED 2026-07-06)

*Updated 2026-07-06: the secrets below were removed from tracked files
(env-var-ified / untracked) **and rotated at the source**. The old values remain
in **git history** but are now dead. Removing from HEAD was not remediation —
rotation was.*

| File | Secret | Status (2026-07-06) |
|------|--------|--------------------------|
| `deploy/config/amphive_tunnel.conf` **and** `deploy/docs/wireguard_tunnel_setup.md` | WireGuard **private key** | Config untracked + gitignored; the key was *also* hardcoded in the setup doc — now placeholdered. `amphive_tunnel.conf.example` added. **Keypair rotated 2026-07-06.** |
| `scripts/setup_duckdns.sh` | Live **DuckDNS token** | Requires `DUCKDNS_TOKEN` env var. **Token rotated 2026-07-06.** |
| `tools/local_tapo_test.py`, `tools/relay_server.py`, `tools/turn_on.py`, `tools/turn_off.py`, `tools/klap_probe.py` | **Tapo account email + password** | Env-var'd across all five tools, committed `3e20dbd`; read `TAPO_EMAIL` / `TAPO_PASSWORD` / `TAPO_PLUG_IP`. **Password rotated 2026-07-06**, set only in the gitignored `.env` |
| `deploy/scripts/deploy.ps1`, `docker-compose.prod.yml` | DB password (was `amphive_db_admin`) | **Rotated 2026-07-06** (`ALTER USER` on the live DB + `.env`); `deploy.ps1` now **rejects** the legacy value as insecure |
| `deploy/docs/*` (migration / setup / walkthrough) | DB password references | Old literal scrubbed to `<DB_PASSWORD>` placeholders 2026-07-06; password rotated |

✅ The application `.env` files are **gitignored** and *not* committed. Backend
Razorpay/JWT/Tapo secrets live only in the local/VM `.env`, not in the repo.

**Done 2026-07-06:** all four burned secrets — WireGuard keypair, DuckDNS token,
Tapo password, DB password — were rotated at the source. The old values remain in
git **history** but are now dead. **Optional:** scrub history (`git filter-repo` /
BFG + force-push) to purge the dead values entirely.

## 2. Default / weak credentials

- [Mitigated 2026-07-05] `JWT_SECRET_KEY`: the backend now **refuses** a
  missing/known-default secret and generates a random *ephemeral* key instead
  (sessions won't survive restarts until a real secret is set), and
  `deploy.ps1` **aborts the deploy** if `.env` has a missing/short/default
  `JWT_SECRET_KEY`. The old behavior (silently signing with the public literal
  `amphive-dev-secret-change-in-production`) is gone. Still set a strong
  secret in every environment.
- [Rotated 2026-07-06] The PostgreSQL `postgres` user password was rotated on the
  live DB and is set via `.env` `POSTGRES_PASSWORD`; `deploy.ps1` now **rejects**
  the old `amphive_db_admin` literal as insecure. (The dead old value remains in
  git history.)
- [Added 2026-07-08] **JWT revocation.** Tokens were previously irrevocable
  until their 7-day expiry — a leaked token stayed valid. Every token now
  carries the user's `token_version` epoch (`tv` claim), re-checked per
  request; `POST /api/auth/logout` bumps the epoch, killing all of that
  user's tokens server-side ("log out everywhere"). Expiry is now
  env-configurable (`JWT_EXPIRY_DAYS`, default 7). No blacklist table (the
  epoch is per-user, not per-token); single-device logout isn't distinguished
  from all-device. Legacy pre-`tv` tokens are treated as epoch 0 (valid until
  the first revoke), so the change didn't force-log-out existing sessions.

## 3. Open / unauthenticated surfaces

- **MQTT broker** — *largely closed as of 2026-07-08; TLS rollout in progress.*
  It *used* to be anonymous and reachable on **1883 from `0.0.0.0/0`** — anyone
  could publish/subscribe, send plug `ON`/`OFF`, and **forge telemetry that
  feeds billing**. Since then: public exposure closed 2026-07-06, auth enforced
  2026-07-07, TLS listener added 2026-07-08 (details below). Remaining: move
  every gateway to 8883, then bind plaintext 1883 internal-only.
  - [Done 2026-07-06] MQTT now binds to the VM overlay IP `100.87.241.70`
    (`MQTT_BIND_IP` in `.env`), and the GCP firewall rule was restricted to
    tcp:80 + tcp:8000 — **1883 is no longer publicly reachable**. The ESP32
    connects over the overlay and the backend uses the internal compose network,
    so nothing needs the public port. The unused websocket port 9001 is no longer
    published.
  - [Done 2026-07-07] Broker **auth is enforced**: `allow_anonymous false` +
    `password_file` (generated on the VM by `deploy.ps1` from `MQTT_USERNAME` /
    `MQTT_PASSWORD` — backend client — and `MQTT_GW_USERNAME` /
    `MQTT_GW_PASSWORD` — shared gateway account — in `.env`, all validated
    alphanumeric). The compose healthcheck authenticates; the backend sends
    its credentials via env; the firmware presents NVS `mqtt_user`/`mqtt_pwd`
    (provisioned via the portal's optional MQTT fields — a credential-less
    gateway can no longer connect). **Verified in prod:** anonymous
    `mosquitto_sub` → `Connection Refused: not authorised`; backend + the
    real ESP32 reconnected authenticated and telemetry flows.
  - [Added 2026-07-08, rollout in progress] Broker **TLS** listener on **8883**
    (`mosquitto.conf`): a self-signed CA + server cert (SAN `IP:100.87.241.70`,
    `deploy/config/gen_mqtt_certs.sh`); the gateway firmware embeds the CA and
    validates the broker cert (chain + IP SAN — dates aren't checked, no clock
    needed). Server-auth only; clients still present username/password.
    Rollout is staged for safety: the plaintext **1883** listener stays up
    during the transition (backend on the internal Docker network; OTA-
    rollback target for gateways), and is bound internal-only once every
    gateway is confirmed on 8883. TLS here is defense-in-depth — the WireGuard
    overlay already encrypts the transport.
- [Fixed + deployed 2026-07-06] **CORS** is restricted to an explicit allowlist
  (localhost, `amphive.duckdns.org`, VM IP; http+https) with the wildcard removed,
  in `backend/main.py:187`. Verified in prod: an allowed origin is echoed, a
  foreign origin gets no `Access-Control-Allow-Origin` header.
- **`/api/payments/webhook`** is unauthenticated by design but HMAC-gated. It now
  auto-credits coins on `payment.captured`; abuse via replay is mitigated by the
  HMAC signature check plus idempotency on `razorpay_payment_id`, but a leaked
  `RAZORPAY_WEBHOOK_SECRET` would allow forged credits — keep it secret and rotate
  if exposed.

## 4. AuthZ gaps

- [Resolved 2026-07-02; hardened 2026-07-09] **Live-telemetry auth.** The old
  SSE transport (retired 2026-07-07) authenticated via a `?token=` query
  parameter, and the note here proposed a short-lived single-use ticket to
  keep the JWT out of URLs. Moot now: Socket.io (the sole transport) carries
  the JWT in the **auth payload of the CONNECT packet body**, and the
  backend's leftover query-string token fallback — which no client used —
  was **removed 2026-07-09** (`socketio_manager.py:connect`), so a full JWT
  can no longer appear in proxy/access logs via the URL. Session ownership
  is still verified per subscription.

> RBAC across the `/api/cpo/*` surface is now enforced (`require_role`) — see
> [§7](#7-recently-fixed).

## 5. Data-integrity gaps

- [2026-07-09] Wallet writes are now **atomic DB-side updates** centralized in
  `backend/services/wallet.py` (credits: `UPDATE … SET coin_balance =
  coin_balance + :n RETURNING`; debits: fresh column read under `FOR UPDATE`
  + clamped write). Implementing the long-noted "consider a single atomic
  UPDATE" hardening surfaced that it was a **real lost-update bug**, not just
  hygiene: the request session already holds the auth-loaded `User` in its
  identity map, and SQLAlchemy returns that cached instance — stale balance
  and all — from a later `select(User).with_for_update()`, so the old
  read-modify-write silently overwrote any credit/debit committed between
  auth and the lock (e.g. a webhook top-up landing while a stop request was
  in flight). The logout `token_version` bump had the same shape and is also
  DB-side now. Postgres-backed regression tests: `backend/tests/test_wallet.py`
  (run in CI; local dev boxes run no DB by policy).
- [2026-07-06] Money columns migrated from `Float` to `Numeric(12,2)` (Decimal),
  and all wallet math goes through `services/money.to_money` — float rounding
  drift is closed.
- [2026-07-07] **DB-level non-negative-balance CHECK is in place**: Alembic
  revision `0002_wallet_non_negative` adds `ck_users_coin_balance_non_negative`
  on `users.coin_balance` (clamping legacy negative rows to 0 first), so no
  write path — buggy or otherwise — can drive a wallet negative.
- [2026-07-06] The `stop_charging_session` ledger now debits only what the wallet
  holds (`min(final_cost, balance)`) and records that same delta in `amount` /
  `balance_after` / `coins_spent`, so the ledger reconciles even when a bill
  exceeds the balance (the forgiven shortfall is logged).

## 6. Operational notes

- The **VM public IP is ephemeral** and is recorded inconsistently across docs
  (`35.200.131.98`, `34.100.200.152`, and others). The committed
  `amphive_tunnel.conf` endpoint will break whenever the VM IP changes. Prefer a
  stable hostname (DuckDNS) or a static IP, and always re-check with `gcloud`.
- **Firmware control plane defaults to Tailscale's public servers**, not the
  self-hosted Headscale, unless the host constants are overridden.

## 7. Recently fixed

*Previously listed as gaps here; kept for context so older references don't
read as still-open.*

**2026-07-06:**

- **All burned secrets rotated at the source** — WireGuard keypair, DuckDNS token,
  Tapo password, and DB password regenerated; the old values in git history are
  now dead, and the old DB-password literal was scrubbed from the deploy docs.
- **MQTT taken off the public internet** — broker bound to the overlay IP
  `100.87.241.70`; GCP firewall restricted to tcp:80/8000 (1883 no longer public).
- **CORS locked to an allowlist** in `backend/main.py` (wildcard removed) —
  committed and deployed to prod (verified: allowed origin echoed, foreign
  origin gets no ACAO header).
- **Money → `Numeric(12,2)`** across the wallet columns, math via
  `services/money.to_money` — float rounding drift closed.
- **Ledger reconciliation fix** in `stop_charging_session` — debits only the
  available balance and records a matching `amount`/`balance_after`.

**2026-07-05:**

- **Client-controlled payment amount (wallet inflation).** `/api/payments/verify`
  used to credit `amount_inr` straight from the request body — the Razorpay
  checkout signature covers only `(order_id, payment_id)`, *not* the amount, so
  a user could pay ₹10 and claim ₹10,000. The endpoint now fetches the payment
  from Razorpay's API and credits the confirmed captured amount, rejects
  order/payment mismatches, and refuses to credit a payment whose order was
  created for a different user. Covered by `backend/tests/test_payments.py`.
- **Insecure JWT defaults removed** (see §2) — backend + deploy.ps1 both refuse
  known-default/weak secrets.
- **Secrets stripped from tracked files** (see §1) — `tools/*` env-var'd and
  committed (`3e20dbd`); the WireGuard private key was also removed from
  `wireguard_tunnel_setup.md`. **Tapo password rotated**; the WireGuard keypair,
  DuckDNS token, and DB password still need rotation at the source.

**2026-07-02:**

- **RBAC is enforced.** `require_role("cpo","admin")` (`backend/services/rbac.py`)
  gates every `/api/cpo/*` route and checks the live DB role, not just the token.
- **Unauthenticated provisioning removed.** The old `POST /api/gateways/register`
  and `POST /api/plugs/register` are gone; provisioning now happens through the
  tenant-scoped, RBAC-gated `POST /api/cpo/gateways` / `POST /api/cpo/plugs`.
- **Wallet updates are row-locked** (`SELECT ... FOR UPDATE`) in the stop, verify,
  and webhook paths — no more balance race.
- **Webhook auto-credit is idempotent** — dedupes on `razorpay_payment_id` so the
  `/verify` and webhook paths can't double-credit.
- **SSE connection is authenticated** — the frontend passes `?token=` query parameter, and the backend validates the JWT and checks ownership.

---

## Quick remediation checklist

Status — open items and recently closed:
- [x] **Rotate** WireGuard keys, DuckDNS token, Tapo & DB passwords at the source
      (2026-07-06). Dead old values remain in git history — *optional* scrub.
- [x] **Commit + deploy** the CORS allowlist (2026-07-06) — live in prod.
- [x] Add MQTT broker **auth** (2026-07-07) — `allow_anonymous false` + passwd
      file; backend, healthcheck, and gateway firmware (NVS creds) all
      authenticate; verified in prod (see §3).
- [x] Set a strong `JWT_SECRET_KEY` in every environment — deploy.ps1 aborts on
      a missing/short/default secret (2026-07-05) and prod deploys since prove a
      strong key is set; the backend falls back to an ephemeral key elsewhere.
- [x] MQTT bound to the overlay IP + public 1883 firewall rule dropped (2026-07-06).
- [x] CORS restricted to an allowlist in `backend/main.py` (2026-07-06, deployed).
- [ ] MQTT broker **TLS** rollout completion: OTA every gateway to ≥ 1.2.0
      (mqtts://8883), then bind plaintext 1883 internal-only (see §3).
- [x] DB-level non-negative-balance CHECK (2026-07-07) — Alembic
      `0002_wallet_non_negative` adds `ck_users_coin_balance_non_negative`
      (see §5).
- [x] Unique `razorpay_payment_id` ledger column (2026-07-06) —
      `uq_ledger_razorpay_payment_id` + `IntegrityError` handling in
      `_credit_topup` closes the concurrent /verify + webhook double-credit race.

Done (2026-07-05):
- [x] Remove committed secrets from tracked files; add `.example` / env-var paths
      (`tools/*` committed `3e20dbd`, `setup_duckdns.sh`, `amphive_tunnel.conf`,
      `wireguard_tunnel_setup.md`).
- [x] Rotate the Tapo account password.
- [x] Server-authoritative payment amounts in `/api/payments/verify`.
- [x] Refuse known-default JWT secrets (backend + deploy gate).

Done (2026-07-02):
- [x] Implement role checks for CPO/admin actions (`require_role`).
- [x] Remove/authenticate `gateways/register` / `plugs/register`.
- [x] Make wallet credit/debit atomic (row lock).
- [x] Pass JWT token to SSE live telemetry connection to authenticate users.
