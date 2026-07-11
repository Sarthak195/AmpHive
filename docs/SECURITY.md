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

- [Resolved 2026-07-11, **deployed + verified in prod**] **Web tier served
  plain HTTP.** The SPA, API, and Socket.io were served http-only on `:80` —
  logins, JWTs, and the Razorpay checkout traveled cleartext. `deploy.ps1`
  now ships a **Caddy TLS front door by default** (`docker-compose.tls.yml`):
  auto-renewed Let's Encrypt cert for `CADDY_DOMAIN` (`.env`; Caddyfile
  generated on the VM), HTTP→HTTPS redirect for the domain, and the frontend
  container no longer publishes a host port (Caddy is the only public web
  entrypoint). Bare-IP/unknown-Host requests are **served** (plain http)
  rather than redirected, so a DNS outage can't take the site down. CORS /
  Socket.io allowlists already carried the https origins; firewall rule
  `allow-amphive-https` (tcp:443) added. **Verified live:** `https://` 200
  with a validated LE cert (CN `amphive.duckdns.org`, expires 2026-10-09,
  auto-renew), domain http→https 308, `/api` + Socket.io over https, CPO
  login; broker + both gateways unaffected. The rollout rode out a real
  **DuckDNS authoritative-nameserver outage** (~1 h; Caddy auto-retried the
  cert in — incident log: `deploy/docs/web_tls_rollout.md`). Rollback:
  `deploy.ps1 -NoTls`. **Remaining follow-ups:** drop tcp:8000 from
  `allow-amphive-ports` (the backend's direct plain-HTTP port — the SPA
  reaches the API via the frontend nginx proxy, so nothing public needs it),
  add HSTS + flip bare-IP back to a redirect, and replace DuckDNS with a
  real domain (this outage makes it a proven SPOF — see §6).
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
    (`mosquitto.conf`): a self-signed CA + server cert
    (`deploy/config/gen_mqtt_certs.sh`); the gateway firmware embeds the CA and
    validates the broker cert (chain + IP SAN — dates aren't checked, no clock
    needed). Server-auth only; clients still present username/password.
    Rollout is staged for safety: the plaintext **1883** listener stays up
    during the transition (backend on the internal Docker network; OTA-
    rollback target for gateways), and is bound internal-only once every
    gateway is confirmed on 8883.
  - [Done 2026-07-10] **8883 is deliberately PUBLIC — the direct-MQTT path.**
    The overlay proved fragile for devices behind symmetric NAT (DISCO
    hole-punching fails; see §7), so the transport is now a plain **outbound**
    MQTT/TLS connection: devices/agents dial `mqtts://8.231.81.12:8883` (the
    VM's reserved static IP; GCP rule `allow-amphive-mqtts`), which traverses
    any NAT/CGNAT without STUN/DERP/port-forwards. The trust the tailnet used
    to provide implicitly is now explicit:
    * **TLS**: server cert (SANs `100.87.241.70` + `8.231.81.12`) chained to
      the AmpHive CA; CA regenerated 2026-07-10 with proper
      `basicConstraints`/`keyUsage` extensions (Python 3.13 strict validators —
      i.e. the AmpHive Agent — reject a CA without them). Firmware embeds the
      new CA from 1.3.0.
    * **AuthN**: `allow_anonymous false` + passwd file. **Per-gateway accounts**
      (username == gateway_id) via `deploy/scripts/add_gateway_user.ps1`;
      the passwd file survives redeploys (no more `-c` truncation). The shared
      `amphive-gateway` account was **retired 2026-07-10** — every device has
      its own account (real ESP `1cc3abb4fb54`, fake plug `fakeplug-gw-01`),
      and `deploy.ps1` no longer provisions a shared one.
    * **AuthZ**: mosquitto **topic ACLs** (`mosquitto_acl`, generated on the VM
      by `deploy.ps1`): backend → `amphive/#` + `$SYS` read; per-gateway
      accounts → `pattern readwrite amphive/gateways/%u/#` (a gateway can only
      touch its own subtree — no cross-site forgery). The old shared broad
      grant (`amphive/gateways/#`) is **gone**. Verified 2026-07-10: an account
      subscribed to another gateway's telemetry receives nothing.
      *Ops note:* the ACL/passwd files are **bind-mounted**, so edit them
      **in place** (mosquitto_passwd / `tee`) — replacing via `mv` swaps the
      inode and the running broker keeps the old file until restarted.
    **Verified in prod 2026-07-10:** TLS 1.3 handshake from the public internet
    validates against the CA (strict mode); anonymous and bogus credentials
    both get `not authorised`; backend, fake plug, and the real ESP32 all
    stayed connected under the ACLs. The overlay 1883 listener is unchanged
    (legacy/transition + backend-internal).
  - [Hardened + rolled out 2026-07-10] **OTA image
    transport + signing.** Both halves of the earlier TODO are implemented
    (fw ≥ 1.4.0):
    * **HTTPS-only images**: hosted on the public-read GCS bucket
      `gs://amphive-fw` (`https://storage.googleapis.com/amphive-fw/...`,
      public-CA cert the firmware's Mozilla bundle validates; public-read
      user-approved 2026-07-10 — images hold no secrets and are signed).
      Plain `http://` is refused by the firmware
      (`CONFIG_ESP_HTTPS_OTA_ALLOW_HTTP` removed + an explicit scheme check
      in `ota_update_start`) *and* by the backend
      (`CpoGatewayOtaRequest` now requires `https://`).
    * **Signed OTA** (signed-app verification *without* secure boot, ECDSA
      scheme v1, `CONFIG_SECURE_SIGNED_ON_UPDATE_NO_SECURE_BOOT`): the device
      rejects any update lacking a valid signature from
      `firmware/secure_boot_signing_key.pem` (gitignored; **back it up** —
      losing it strands fielded devices on USB reflash). A
      valid-but-malicious image from a MITM or a compromised bucket no longer
      installs. No eFuses burned; boot-time verification stays off (full
      secure boot remains a possible future step).
    **Rolled out 2026-07-10:** the real gateway was OTA'd to the signed
    `1.5.0-direct` image over https, and the backend `^https://` validation
    is deployed (see `deploy/docs/ota_image_publishing.md`). Pre-1.4.0
    firmware ignores the signature trailer, so the migration jump installed
    cleanly; from 1.4.0 on, only signed images install.
- [Added 2026-07-10, fw 1.5.0] **Unauthorized-use safety control.** The plug's
  physical button and the Tapo app are control paths that bypass AmpHive
  entirely — a relay could be energized with no authorized session (free,
  unmetered power). The firmware now enforces this locally: with no active
  session, a relay found ON is forced OFF every telemetry cycle and raised
  (once per episode) as a **critical `UNAUTHORIZED_ON` event** surfaced to the
  operator (`gateway_events` table → `GET /api/cpo/events`) — a defense
  against out-of-band plug activation.
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

- [Resolved 2026-07-02] **SSE auth gap.** The frontend now passes the JWT token as a `?token=` query parameter, and the backend verifies the token and session ownership. A potential future hardening is to use a short-lived, single-use ticket instead of the full JWT token in the query parameter.

> RBAC across the `/api/cpo/*` surface is now enforced (`require_role`) — see
> [§7](#7-recently-fixed).

## 5. Data-integrity gaps

- Wallet credit/debit is now **row-locked** (`SELECT ... FOR UPDATE`) in the stop,
  verify, and webhook paths — the previous race is closed. Remaining hardening:
  consider a single atomic `UPDATE ... SET balance = balance + :n`.
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
- [x] **Web HTTPS front door deployed + verified in prod** (2026-07-11):
      Caddy on 80/443, validated Let's Encrypt cert, http→https redirect.
      See §3 and `deploy/docs/web_tls_rollout.md`.
- [ ] Drop tcp:8000 from `allow-amphive-ports` (nothing public needs the
      backend's direct port now that Caddy fronts the web tier); add HSTS;
      replace DuckDNS with a real domain (proven SPOF — §3/§6).
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
