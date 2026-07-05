# AmpHive — Security Notes

*Verified against source on 2026-07-02. This is a developer-facing inventory of
known security gaps, not a formal audit. Items are roughly ordered by severity.*

> Several real secrets are committed to the git history. Even after removing them
> from `HEAD`, they remain in history and must be **rotated**, not just deleted.

> **Recently fixed** (see [§7](#7-recently-fixed)): RBAC is now enforced on all
> `/api/cpo/*` routes, the unauthenticated `gateways/register` / `plugs/register`
> endpoints were removed, wallet updates are row-locked, and the Razorpay webhook
> credits idempotently. The items in §1–§6 are the gaps that **remain**.

---

## 1. Committed secrets (ROTATION STILL REQUIRED)

*Updated 2026-07-05: the secrets below were removed from the tracked files
(env-var-ified / untracked), but they remain in **git history** — every one of
them must still be rotated. Removing them from HEAD is not remediation.*

| File | Secret | HEAD status (2026-07-05) |
|------|--------|--------------------------|
| `deploy/config/amphive_tunnel.conf` **and** `deploy/docs/wireguard_tunnel_setup.md` | WireGuard **private key** | Config untracked + gitignored; the key was *also* hardcoded in the setup doc — now placeholdered (2026-07-05). `amphive_tunnel.conf.example` added |
| `scripts/setup_duckdns.sh` | Live **DuckDNS token** | Now requires `DUCKDNS_TOKEN` env var |
| `tools/local_tapo_test.py`, `tools/relay_server.py`, `tools/turn_on.py`, `tools/turn_off.py`, `tools/klap_probe.py` | **Tapo account email + password** | Env-var'd across all five tools, committed `3e20dbd`; read `TAPO_EMAIL` / `TAPO_PASSWORD` / `TAPO_PLUG_IP`. **Password rotated 2026-07-05**, set only in the gitignored `.env` |
| `deploy/scripts/deploy.ps1`, `docker-compose.prod.yml` | DB password `amphive_db_admin` | Now interpolated from `.env` `POSTGRES_PASSWORD` (deploy.ps1 falls back to the legacy value with a warning until rotated) |
| `scripts/start-vm.bat`, `deploy/docs/gcp_migration_runbook.md` | DB password references | Still present (docs/scripts) — rotate the password itself |

✅ The application `.env` files are **gitignored** and *not* committed. Backend
Razorpay/JWT/Tapo secrets live only in the local/VM `.env`, not in the repo.

**Still to do:** rotate the WireGuard keypair, the DuckDNS token, and the DB
password **at the source** (all burned — history retains them). The Tapo account
password was rotated 2026-07-05. Scrub git history if feasible.

## 2. Default / weak credentials

- [Mitigated 2026-07-05] `JWT_SECRET_KEY`: the backend now **refuses** a
  missing/known-default secret and generates a random *ephemeral* key instead
  (sessions won't survive restarts until a real secret is set), and
  `deploy.ps1` **aborts the deploy** if `.env` has a missing/short/default
  `JWT_SECRET_KEY`. The old behavior (silently signing with the public literal
  `amphive-dev-secret-change-in-production`) is gone. Still set a strong
  secret in every environment.
- The PostgreSQL container on the VM uses user `postgres` with the known
  password `amphive_db_admin` unless `POSTGRES_PASSWORD` is set in `.env` —
  **rotate it** (the old value is in git history).

## 3. Open / unauthenticated surfaces

- **MQTT broker is anonymous + no TLS** (`mosquitto.conf`: `allow_anonymous true`)
  and the firewall opens **1883 to `0.0.0.0/0`**. Anyone on the internet can
  publish/subscribe — including sending plug `ON`/`OFF` commands and **forging
  telemetry, which feeds billing**. Confidentiality was *intended* to come from
  running MQTT inside the overlay, but the broker is also exposed publicly.
  - [Partial 2026-07-05] `docker-compose.prod.yml` now binds the published port
    via `${MQTT_BIND_IP}` from `.env`. Set `MQTT_BIND_IP=<VM overlay IP>`
    (e.g. `100.87.241.70`) so only overlay peers reach 1883 — the ESP32
    connects over the overlay, and the backend uses the internal compose
    network, so nothing needs the public port. Default is `0.0.0.0` (legacy)
    until flipped. The unused websocket port 9001 is no longer published.
  - Still to do: remove the public GCP firewall rule for 1883
    (`gcloud compute firewall-rules list --filter="allowed[].ports=1883"`,
    then delete/restrict it) and add broker auth (needs a firmware
    credentials field before `allow_anonymous false` can be enabled).
- **CORS is fully open** (`allow_origins=["*"]`). Restrict to the known frontend
  origin(s) before production.
- **`/api/payments/webhook`** is unauthenticated by design but HMAC-gated. It now
  auto-credits coins on `payment.captured`; abuse via replay is mitigated by the
  HMAC signature check plus idempotency on `razorpay_payment_id`, but a leaked
  `RAZORPAY_WEBHOOK_SECRET` would allow forged credits — keep it secret and rotate
  if exposed.

## 4. AuthZ gaps

- [Resolved 2026-07-02] **SSE auth gap.** The frontend now passes the JWT token as a `?token=` query parameter, and the backend verifies the token and session ownership. A potential future hardening is to use a short-lived, single-use ticket instead of the full JWT token in the query parameter.

> RBAC across the `/api/cpo/*` surface is now enforced (`require_role`) — see
> [§7](#7-recently-fixed).

## 5. Data-integrity gaps

- Wallet credit/debit is now **row-locked** (`SELECT ... FOR UPDATE`) in the stop,
  verify, and webhook paths — the previous race is closed. Remaining hardening:
  consider a single atomic `UPDATE ... SET balance = balance + :n` and DB-level
  check constraints to prevent negative balances.

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

Still open:
- [ ] **Rotate** WireGuard keys, DuckDNS token, DB password at the source
      (values remain in git history even though HEAD is clean).
      *(Tapo password rotated 2026-07-05.)*
- [ ] Set a strong `JWT_SECRET_KEY` in every environment (enforced by
      deploy.ps1 as of 2026-07-05; backend falls back to an ephemeral key).
- [ ] Set `MQTT_BIND_IP=<overlay IP>` in the VM `.env` and redeploy; then drop
      the public 1883 firewall rule; longer-term add broker auth.
- [ ] Restrict CORS to the known frontend origin(s).
- [ ] Consider a DB-level non-negative-balance constraint.
- [ ] Unique `razorpay_payment_id` ledger column (the idempotency check is a
      pre-lock SELECT — concurrent /verify + webhook can still double-credit).

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
