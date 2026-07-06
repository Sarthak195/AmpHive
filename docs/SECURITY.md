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

> **2026-07-06 follow-up audit:** [§8](#8-firmware--gateway-device-security--backend-follow-up-gaps-2026-07-06-audit)
> adds the **firmware/gateway device attack surface** (open provisioning AP, no
> flash-encryption, reusable overlay key + anonymous broker) plus a few
> backend authn/integrity gaps. These are the highest-severity *open* items —
> read §8 first.

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

## 3. Open / unauthenticated surfaces

- **MQTT broker is anonymous + no TLS** (`mosquitto.conf`: `allow_anonymous true`).
  It *used* to be reachable on **1883 from `0.0.0.0/0`** — anyone could
  publish/subscribe, send plug `ON`/`OFF`, and **forge telemetry that feeds
  billing**. The public exposure was closed 2026-07-06 (see below); broker
  **auth** is still missing, so any overlay peer can still publish.
  - [Done 2026-07-06] MQTT now binds to the VM overlay IP `100.87.241.70`
    (`MQTT_BIND_IP` in `.env`), and the GCP firewall rule was restricted to
    tcp:80 + tcp:8000 — **1883 is no longer publicly reachable**. The ESP32
    connects over the overlay and the backend uses the internal compose network,
    so nothing needs the public port. The unused websocket port 9001 is no longer
    published.
  - Still to do: add broker **auth** (needs a firmware credentials field before
    `allow_anonymous false` can be enabled) so overlay peers can't publish
    anonymously.
- [Fixed + deployed 2026-07-06] **CORS** is restricted to an explicit allowlist
  (localhost, `amphive.duckdns.org`, VM IP; http+https) with the wildcard removed,
  in `backend/main.py:187`. Verified in prod: an allowed origin is echoed, a
  foreign origin gets no `Access-Control-Allow-Origin` header.
- **`/api/payments/webhook`** is unauthenticated by design but HMAC-gated. It now
  auto-credits coins on `payment.captured`; abuse via replay is mitigated by the
  HMAC signature check plus idempotency on `razorpay_payment_id`, but a leaked
  `RAZORPAY_WEBHOOK_SECRET` would allow forged credits — keep it secret and rotate
  if exposed.
- [2026-07-06 audit] **Backend trusts the payload `plug_id`.**
  `_handle_gateway_telemetry` bills whatever `plug_id` the telemetry body claims
  without checking it belongs to the topic's `gateway_id`. Even once broker ACLs
  land, add a `plug.gateway_id == <topic gateway>` check so a compromised or
  spoofing gateway can't attribute energy/billing to another tenant's plug.
  (TECH_DEBT — related to TD#24/§8.)
- [2026-07-06 audit] **Firmware safety alarms are never consumed.** The firmware
  publishes `THERMAL_CUTOFF`/`OVERCURRENT_CUTOFF` to `amphive/gateways/{id}/alarms`,
  but the backend subscribes only to `+/telemetry` + `+/status`, so cutoffs are
  dropped — no record, no alert (accountability gap; TECH_DEBT #21).

## 4. AuthZ gaps

- [Resolved 2026-07-02] **SSE auth gap.** The frontend now passes the JWT token as a `?token=` query parameter, and the backend verifies the token and session ownership. A potential future hardening is to use a short-lived, single-use ticket instead of the full JWT token in the query parameter.

> RBAC across the `/api/cpo/*` surface is now enforced (`require_role`) — see
> [§7](#7-recently-fixed).

## 5. Data-integrity gaps

- Wallet credit/debit is now **row-locked** (`SELECT ... FOR UPDATE`) in the stop,
  verify, and webhook paths — the previous race is closed. Remaining hardening:
  consider a single atomic `UPDATE ... SET balance = balance + :n` and DB-level
  check constraints to prevent negative balances.
- [2026-07-06] Money columns migrated from `Float` to `Numeric(12,2)` (Decimal),
  and all wallet math goes through `services/money.to_money` — float rounding
  drift is closed. A DB-level non-negative-balance CHECK is still not in place.
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

## 8. Firmware / gateway device security & backend follow-up gaps (2026-07-06 audit)

*New in the 2026-07-06 follow-up audit. These are **open** and, for the device
items, the highest-severity gaps in the project — an attacker with brief Wi-Fi
proximity or physical access to a gateway can take it over or harvest the owner's
Tapo account. Ordered by severity.*

### 8.1 Provisioning portal is an open, unauthenticated door — **CRITICAL**

- The setup Access Point is `WIFI_AUTH_OPEN` with an empty password
  (`firmware/main/main.c:245-247`), and the `/save` handler requires **no
  authentication** (`main.c:191`).
- Anyone within Wi-Fi range during provisioning can (a) **sniff** the submitted
  Tapo password, Wi-Fi password, and Headscale auth key over the open air, and
  (b) **POST** arbitrary config to overwrite all of them → full device takeover /
  redirect to attacker infrastructure.
- **Fix:** WPA2 on the setup AP with a per-device password (derive from the MAC,
  print on the unit label), a setup PIN/token gating `/save`, and a portal
  timeout. Serve the portal only on the AP interface.

### 8.2 No flash encryption / secure boot — secrets are extractable — **HIGH**

- `firmware/sdkconfig`: `CONFIG_SECURE_FLASH_ENC_ENABLED` and `CONFIG_SECURE_BOOT`
  are both **unset**. Wi-Fi password, the full **Tapo account email + password**,
  and the overlay auth key sit in **plaintext NVS**, readable with
  `esptool read_flash` given brief physical access.
- That yields the victim's entire Tapo account plus a working overlay credential.
- **Fix:** enable flash encryption + Secure Boot v2 for production units; at
  minimum use NVS encryption.

### 8.3 Reusable overlay key + anonymous broker = forge anything — **HIGH**

- The Headscale/Tailscale auth key stored on-device is typically reusable.
  Extracted (§8.2) or sniffed (§8.1), an attacker joins the overlay. The MQTT
  broker is `allow_anonymous true`, no ACLs, no TLS (`deploy/config/mosquitto.conf`),
  so **any** overlay node can publish forged telemetry for **any** `plug_id`
  (drives billing) and send `ON`/`OFF` to **any** gateway's command topic.
- This is the "bad actor on the overlay" threat and is currently unmitigated
  beyond "you must be on the overlay" (which §8.1/§8.2 make cheap to breach).
- **Fix:** (a) ephemeral, single-use, **tagged** auth keys with Headscale ACLs
  restricting each gateway to just the broker; (b) broker **auth** (per-gateway
  credentials or client-cert) + **ACLs** limiting a gateway to
  `amphive/gateways/<own-id>/#` publish and its own command topic subscribe;
  (c) **TLS** on 1883. (Extends §3's open-broker note; TD#3 auth item.)

### 8.4 Boot-time fallback into the open portal — **MEDIUM**

- On boot with Wi-Fi down, the device drops into the open portal
  (`main.c:701-708`). An attacker who deauths/jams the STA link and forces a
  reboot lands the gateway in §8.1's unauthenticated portal.
- **Fix:** require a physical button-hold to enter provisioning instead of
  auto-opening it on transient Wi-Fi loss; keep retrying STA otherwise.

### 8.5 Multi-plug refactor must stay security-safe

The single-plug → multi-plug refactor (TECH_DEBT #20) touches this surface, so
capture it here:

- `tapo_protocol.c` holds **one** global KLAP session (`s_sess`) and **one**
  energy integrator (`s_energy_wh`); multi-plug needs a **per-plug** KLAP session
  + meter so plug A's crypto/session can't be reused to act on plug B.
- Prefer carrying the target `local_ip` in the backend `ON` command payload (the
  backend already stores `plugs.local_ip`) over shipping a plug roster to the
  device — fewer secrets on-device.
- Broker ACLs stay **per-gateway**, not per-plug: one gateway legitimately drives
  several plugs under `amphive/gateways/<id>/plugs/+/…`.
- Backend must validate `plug.gateway_id == <topic gateway>` before billing (see
  §3) so a gateway can only report for plugs it actually owns.

### 8.6 Backend authn hardening — **LOW/MEDIUM**

- **JWT: 7-day expiry, no revocation/blacklist** (`backend/services/auth.py`). A
  stolen token is valid for a week. Consider short-lived access tokens + refresh,
  or a revocation list — especially for CPO/admin.
- **No rate limiting** on `/api/auth/login` and `/api/auth/register` —
  brute-force / account-enumeration open. Add rate limiting (login already returns
  a generic "Invalid email or password").
- **Registration input** isn't validated: `email` is a bare `str` (not the
  imported `EmailStr`) and there's no password-strength rule (`backend/main.py:210`).

---

## Quick remediation checklist

Status — open items and recently closed:
- [x] **Rotate** WireGuard keys, DuckDNS token, Tapo & DB passwords at the source
      (2026-07-06). Dead old values remain in git history — *optional* scrub.
- [x] **Commit + deploy** the CORS allowlist (2026-07-06) — live in prod.
- [ ] Add MQTT broker **auth** (firmware credentials field needed).
- [ ] Set a strong `JWT_SECRET_KEY` in every environment (enforced by
      deploy.ps1 as of 2026-07-05; backend falls back to an ephemeral key).
- [x] MQTT bound to the overlay IP + public 1883 firewall rule dropped (2026-07-06).
- [x] CORS restricted to an allowlist in `backend/main.py` (2026-07-06, deployed).
- [ ] Consider a DB-level non-negative-balance constraint (money is now
      `Numeric(12,2)`, but no CHECK enforces `coin_balance >= 0` yet).
- [x] Unique `razorpay_payment_id` ledger column (2026-07-06) —
      `uq_ledger_razorpay_payment_id` + `IntegrityError` handling in
      `_credit_topup` closes the concurrent /verify + webhook double-credit race.

Device security — open (2026-07-06 audit, see [§8](#8-firmware--gateway-device-security--backend-follow-up-gaps-2026-07-06-audit)):
- [ ] **Lock down the provisioning portal** — WPA2 setup AP + PIN/token on
      `/save` + timeout (§8.1, CRITICAL).
- [ ] **Enable flash encryption + Secure Boot v2** so NVS secrets (Tapo account,
      overlay key) aren't extractable (§8.2, HIGH).
- [ ] **Ephemeral/tagged overlay keys + broker auth/ACL/TLS** so an overlay peer
      can't forge telemetry or ON/OFF for arbitrary plugs (§8.3, HIGH).
- [ ] **Require a button-hold for provisioning** instead of auto-opening the open
      portal on Wi-Fi loss (§8.4, MEDIUM).
- [ ] **Validate `plug.gateway_id` against the topic gateway** before billing
      telemetry (§3, §8.5).
- [ ] **Consume `+/alarms`** so THERMAL/OVERCURRENT cutoffs are recorded/alerted
      (§3, TD#21).
- [ ] **Shorter-lived JWTs / revocation + auth rate limiting** (§8.6).

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
