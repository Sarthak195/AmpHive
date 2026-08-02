# AmpHive — Security Notes

*Verified against source on 2026-07-20. This is a developer-facing inventory of
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
> adds the **firmware/gateway device attack surface** plus a few backend
> authn/integrity gaps. Statuses re-checked 2026-07-11: the overlay-key +
> anonymous-broker item (§8.3) and JWT revocation (§8.6) were resolved by the
> 2026-07-08…10 work, and the open provisioning portal (§8.1, was CRITICAL)
> was locked down in fw 1.6.0 (WPA2 setup AP + setup code on `/save` + idle
> timeout). The still-open device items (no flash-encryption §8.2, boot-time
> portal fallback §8.4) are the highest-severity *open* gaps — read §8 first.

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
  `deploy.ps1 -NoTls`. **Follow-ups completed 2026-07-11:** tcp:8000 dropped
  from `allow-amphive-ports` (the backend's direct plain-HTTP port; the SPA
  reaches the API via the frontend nginx proxy, so nothing public needed it —
  :8000 stays published for VM-local debugging over SSH), and **HSTS**
  (`Strict-Transport-Security: max-age=31536000`) added to the generated
  Caddyfile's domain block. **[Resolved 2026-07-20]** DuckDNS replaced with a
  real domain — `amphive.app` / `cpo.amphive.app` are live (see §6); the CORS
  / Socket.io allowlists have also been trimmed of the raw-IP origins.
- **MQTT broker** — *closed as of 2026-07-20.* It *used* to be
  anonymous and reachable on **1883 from `0.0.0.0/0`** — anyone could
  publish/subscribe, send plug `ON`/`OFF`, and **forge telemetry that
  feeds billing**. Since then: public exposure closed 2026-07-06, auth
  enforced 2026-07-07, TLS listener added 2026-07-08, and the public **8883
  direct-MQTT path** hardened with per-gateway credentials + topic ACLs
  2026-07-10 (details below). **[Resolved 2026-07-20]** the plaintext 1883
  listener is no longer host-published; direct-MQTT gateways use 8883 only.
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
- [Fixed + deployed 2026-07-06, **domain + allowlist updated 2026-07-20**]
  **CORS** is restricted to an explicit allowlist (localhost, `amphive.app` /
  `cpo.amphive.app`; the raw-IP/DuckDNS origins have been trimmed) with the
  wildcard removed, in `backend/main.py:187`. Verified in prod: an allowed
  origin is echoed, a foreign origin gets no `Access-Control-Allow-Origin`
  header.
- **`/api/payments/webhook`** is unauthenticated by design but HMAC-gated. It now
  auto-credits coins on `payment.captured`; abuse via replay is mitigated by the
  HMAC signature check plus idempotency on `razorpay_payment_id`, but a leaked
  `RAZORPAY_WEBHOOK_SECRET` would allow forged credits — keep it secret and rotate
  if exposed.
- [Resolved 2026-07-11] ~~**Backend trusts the payload `plug_id`.**~~
  `_persist_telemetry` now verifies the claimed plug belongs to the topic's
  gateway (`plug.gateway_id == <topic gateway>`) and drops foreign/unknown
  claims with a warning; the raw `telemetry_readings` enqueue sits behind the
  same check, so neither billing totals nor history can be written across
  gateways. Ingestion casts are also guarded now (int `plug_id`, try/except
  floats, NaN/inf rejected — TD#25). Residual: the in-memory live-stream
  store is fed before the DB check (UI display only, no billing effect).
  (Found by the 2026-07-06 audit; TECH_DEBT #25, §8.5.)
- [Resolved 2026-07-10] ~~**Firmware safety alarms are never consumed.**~~ The
  backend now subscribes `amphive/gateways/+/alarms`; alarms persist as
  `gateway_events` rows and feed the CPO events API (verified live in prod).
  (Found by the 2026-07-06 audit; TECH_DEBT #21.)

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
- [Closed 2026-07-12] **Forgiven-overage leak closed for held sessions
  (MARKET_GAP_ANALYSIS.md §3 "Authorization hold").** The item above meant a
  session could start on a flat `MIN_START_BALANCE_COINS` floor and then bill
  past whatever the wallet actually held, with the shortfall simply forgiven
  — a real revenue leak, not just a display nit. `POST /api/sessions/start`
  now sizes a session-scoped authorization hold up front
  (`ChargingSession.hold_coins`, Alembic `0013_auth_holds`):
  `min(available_balance, max_kwh × rate)`, where `available_balance`
  (`services/wallet.py`) is `coin_balance` minus what the driver's OTHER
  ACTIVE sessions already hold — computed under the same user-row lock the
  start path already takes, so two concurrent starts can't double-reserve
  the same coins. `finalize_charging_session` then debits
  `min(final_cost, hold_coins)`, so a held session's bill can never exceed
  what it reserved; any unspent remainder is released with no money
  movement (the hold was always logical, never a real debit — `coin_balance`
  and its non-negative CHECK are untouched by holds). The mqtt_manager
  balance-exhaustion auto-stop also switched from the driver's whole wallet
  balance to the session's own hold, so one session can't be kept alive past
  its reservation by a sibling session's unspent balance. **Residual:**
  legacy sessions predating this migration (`hold_coins IS NULL`) keep the
  old floor-only/forgiven-overage behavior — the leak is closed going
  forward, not retroactively. Tests: `backend/tests/test_auth_holds.py`.

## 6. Operational notes

- The **VM public IP is ephemeral** and is recorded inconsistently across docs
  (`35.200.131.98`, `34.100.200.152`, and others). The committed
  `amphive_tunnel.conf` endpoint will break whenever the VM IP changes. Prefer a
  stable hostname (`amphive.app`) or a static IP, and always re-check with `gcloud`.
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

*Found by the 2026-07-06 follow-up audit; statuses re-checked 2026-07-11 when
the audit merged (the 2026-07-08…11 work resolved §8.3 and half of §8.6). The
still-open device items remain the highest-severity gaps in the project — an
attacker with brief Wi-Fi proximity or physical access to a gateway can take it
over or harvest the owner's Tapo account. Ordered by severity.*

### 8.1 ~~Provisioning portal is an open, unauthenticated door~~ — **RESOLVED (fw 1.6.0)**

- Was: the setup Access Point was `WIFI_AUTH_OPEN` and `/save` required no
  authentication — anyone in Wi-Fi range during provisioning could sniff the
  submitted Tapo/Wi-Fi/MQTT secrets over open air or POST arbitrary config
  (full device takeover).
- **Fixed in fw 1.6.0** (`firmware/main/main.c`): a per-device **setup code**
  (10 chars, `esp_random()`, persisted in NVS, printed over serial at every
  portal start for the unit label) is now (a) the **WPA2 passphrase** of the
  setup AP — submitted secrets are never on open air — and (b) a required,
  constant-time-checked token on **`/save`** (wrong code → 1 s throttle + 403,
  nothing written), guarding against other clients already on the setup AP.
  The portal runs **AP-only** (no STA interface — reachable exclusively via
  the setup AP) and the device **reboots after 10 min of portal inactivity**.
  Note the MAC-derived password suggested in the original fix was rejected:
  the softAP BSSID broadcasts the MAC, so a random persisted code is used
  instead.
- Deployment note: fleet units OTA'd to ≥ 1.6.0 generate their code the first
  time the portal next runs; it is read over serial (`idf.py monitor`) —
  label units at reflash/RMA time.
- Residual: the code is printed on serial and stored in plaintext NVS — both
  require physical access, which is §8.2's flash-encryption territory.

### 8.2 No flash encryption — NVS secrets are extractable — **HIGH, config prepared (not burned)**

- `CONFIG_SECURE_FLASH_ENC_ENABLED` and full `CONFIG_SECURE_BOOT` are **unset on
  every device.** The Wi-Fi password, the full **Tapo account email + password**,
  and the gateway's MQTT credentials sit in **plaintext NVS**, readable with
  `esptool read_flash` given brief physical access — the victim's entire Tapo
  account plus a credential that impersonates this gateway (scoped to its own
  topic subtree by the broker ACLs, see §8.3).
- Improved since the audit: **OTA images are signed** (ECDSA
  verify-on-update, `sdkconfig.defaults`, fw ≥ 1.4.0) and HTTPS-only, so flash
  extraction doesn't enable a malicious-update path; the overlay auth key no
  longer exists on-device (direct MQTT, 2026-07-10).
- **Config prepared 2026-07-16 (not yet burned).** An opt-in encrypted-build
  recipe now exists in-repo — `firmware/sdkconfig.flashenc`
  (`CONFIG_SECURE_FLASH_ENC_ENABLED` Development mode + `CONFIG_NVS_ENCRYPTION`)
  and `firmware/partitions_ota_enc.csv` (adds the `nvs_keys` partition). It is
  **not** wired into the default build, so the routine OTA path is unchanged.
  **Key point:** on ESP32, flash encryption alone does NOT encrypt `nvs` — NVS
  encryption + the `nvs_keys` partition is what actually protects the secrets;
  both are enabled together by the fragment. Burning is per-device, **serial-only
  (not OTA-deliverable), and irreversible** — Development mode is chosen to keep a
  serial-reflash escape hatch; Release mode only after validation. Full procedure,
  irreversibility caveats, and the signing-key-loss brick risk:
  [deploy/docs/firmware_flash_encryption.md](../deploy/docs/firmware_flash_encryption.md).
- **Remaining:** an operator burn on a sacrificial dev unit, then rollout; Secure
  Boot v2 (boot-time verification) is a separate later step.

### 8.3 ~~Reusable overlay key + anonymous broker = forge anything~~ — **RESOLVED 2026-07-10**

- Was: an extracted/sniffed overlay key joined the attacker to the tailnet,
  where the broker was anonymous with no ACLs — forged telemetry or `ON`/`OFF`
  for **any** plug/gateway.
- The fix shipped as part of the direct-MQTT pivot (see §3): devices no longer
  hold overlay keys at all; the broker enforces **per-gateway credentials**
  (username == gateway_id, shared account retired), **topic ACLs**
  (`pattern readwrite amphive/gateways/%u/#` — a gateway can only touch its own
  subtree), and **TLS** on the public 8883. Verified in prod 2026-07-10.
- Residual risk: a credential extracted via §8.1/§8.2 still impersonates *that
  one gateway* within its own subtree — which is why the backend-side
  `plug.gateway_id` check mattered (shipped 2026-07-11, see §3).

### 8.4 Boot-time fallback into the portal — **MEDIUM→LOW, open (exposure reduced fw 1.6.0)**

- On boot with Wi-Fi down, the device drops into the provisioning portal
  (`firmware/main/main.c`). An attacker who deauths/jams the STA link and
  forces a reboot lands the gateway in the portal.
- **Reduced since fw 1.6.0** (§8.1 fix): the portal an attacker lands the
  device in is now WPA2-locked + setup-code-gated (nothing to sniff or POST
  without the label code), and the device **reboots and retries STA after
  10 min of portal inactivity** instead of sitting in the portal forever —
  transient Wi-Fi loss now self-heals. Residual: a jamming attacker can still
  keep the gateway offline (that's inherent to radio) and the setup AP
  beacons during each 10-min window.
- **Remaining fix:** require a physical button-hold to enter provisioning
  instead of auto-opening it on Wi-Fi loss; keep retrying STA otherwise.

### 8.5 Multi-plug refactor — **DONE 2026-07-12, stayed security-safe** (code-complete + builds clean, on-device verify pending); backend-pushed roster added 2026-07-15 (fw 2.0.0-direct)

The single-plug → multi-plug refactor (TECH_DEBT #20) touched this surface. Each
guardrail below was honored — recorded here so the invariants are auditable:

- **Per-plug KLAP session + meter (done).** `tapo_protocol.c` no longer holds a
  global `s_sess` / `s_energy_wh`; each plug gets an opaque `tapo_plug_t` with its
  own KLAP handshake/session (keys, cookie, seq) and its own energy integrator +
  mutex, so plug A's crypto/session can't be reused to act on plug B. The shared
  Tapo *account* `auth_hash` stays global (one account owns every plug — that is
  not a per-plug secret).
- **Backend-pushed retained roster, no secrets/static roster on device
  (fw 2.0.0-direct, 2026-07-15).** The gateway learns each plug's IP from the
  backend's **retained** plug roster on `amphive/gateways/{gw}/config`
  (`{plug_id, local_ip, max_current_a}` — no plug name, no secrets), delivered
  live on subscribe; the ON/OFF `local_ip` is kept as a refresh/back-compat
  fallback. Nothing plug-specific is baked into the device at provisioning time
  — the old captive-portal "Target Plug IP" field, its `target_plug` NVS key,
  and the boot-time provisional slot were **removed**. The roster rides the
  gateway's own already-authorized subtree, so it is no different, security-wise,
  from the commands the gateway already receives.
- **Broker ACLs stay per-gateway (unchanged).** Still
  `pattern readwrite amphive/gateways/%u/#` (per-gateway, live since 2026-07-10);
  one gateway legitimately drives several plugs under its own subtree, so no ACL
  change was needed or made — the retained `.../config` roster topic falls inside
  the same grant.
- **Backend `plug.gateway_id == <topic gateway>` check preserved.** The
  2026-07-11 ownership check (§3) is untouched: multi-plug telemetry already
  carries a per-plug `plug_id` and is validated against the topic gateway, so a
  gateway still can only report for plugs it owns. `_persist_telemetry` was not
  weakened.

### 8.6 Backend authn hardening — **RESOLVED**

- ~~**JWT: 7-day expiry, no revocation/blacklist**~~ **Resolved 2026-07-08**:
  tokens carry a per-user `token_version` epoch re-checked each request;
  logout revokes server-side, and expiry is env-configurable
  (`JWT_EXPIRY_DAYS`) — see §2.
- ~~**No rate limiting** on `/api/auth/login` and `/api/auth/register`~~
  **Resolved 2026-07-11**: both endpoints enforce an in-process
  sliding-window limit per client IP (`backend/services/rate_limit.py`;
  429 + `Retry-After`). Defaults 10/min (login) and 10/hour (register),
  env-tunable via `LOGIN_RATE_LIMIT` / `REGISTER_RATE_LIMIT`. Client IP is
  the first `X-Forwarded-For` entry — trustworthy behind Caddy with the
  public :8000 port closed; in the `-NoTls` rollback stack it is forgeable,
  which only lets an attacker shard their own limit. Single-process by
  design (one uvicorn container); counters reset on restart. Distributed
  (multi-IP) attacks are out of scope for this tier. Tests:
  `backend/tests/test_rate_limiting.py`.
- ~~**No per-account rate limiting**~~ **Resolved 2026-08-02**: the per-IP
  limiters above leave one gap — a single account rotating source IPs is
  invisible to a limiter keyed on IP alone. `account_rate_limit_dependency`
  (keyed by `user.id`, via `get_current_user`) and
  `login_account_rate_limit_dependency` (keyed by normalized email, since
  `/login` has no authenticated user yet) layer a second, account-scoped
  limiter ON TOP of the existing per-IP ones — neither replaces it — on
  session start/stop, payment order creation, CPO offline top-up creation,
  and login. Same 429 + `Retry-After` shape; the login variant's copy is
  byte-identical to the per-IP login limiter's so it adds no
  account-enumeration oracle. Env-tunable via `*_ACCOUNT_RATE_LIMIT`
  (`backend/services/rate_limit.py`, `deploy/config/.env.template`).
- ~~**No blanket rate limit on the rest of the API**~~ **Resolved
  2026-08-02**: the dedicated rules above only cover auth and the money
  paths — every other endpoint could be hammered freely (scraping, probing,
  DB exhaustion). `api_rate_limit_middleware` now applies a per-IP sliding
  window to EVERY `/api` route (default 300/60 s, env `API_RATE_LIMIT`,
  `off` disables) as a floor UNDER the per-route limiters — a request that
  passes it still hits its route's own tighter rule. Exempts only
  `/api/health` (uptime probes must never 429). Middleware rather than a
  dependency, so it runs before routing (404 probe floods spend budget too)
  and is registered inside `CORSMiddleware` (preflights never spend budget;
  a 429 still carries CORS headers). Same 429 + `Retry-After` shape as the
  per-route rules. Tests: `backend/tests/test_rate_limiting.py`.
- ~~**Registration input isn't validated**~~ **Resolved 2026-07-11**:
  `RegisterRequest` uses `EmailStr` and an 8-72 char password rule (72 =
  bcrypt truncation boundary). Login is intentionally unvalidated so accounts
  created before the rule can still sign in (`backend/schemas.py`; TD#30).

### 8.7 Claim-code onboarding attack surface (2026-08-02, feat/easy-provisioning) — considered, mitigated

`POST /api/cpo/gateways/claim` lets any authenticated cpo/admin account bind
an admin-minted gateway to their tenant given only a short code — a new
"guess a secret, get something" surface, mitigated the same way the auth
endpoints above are:
- **Keyspace:** 10 characters from a 32-symbol unambiguous alphabet
  (`23456789ABCDEFGHJKMNPQRSTUVWXYZ`) — 32^10 ≈ 1.1 × 10^15 possibilities.
- **No enumeration oracle:** an unknown code, an already-claimed code, and a
  malformed code all return the identical `404
  {"detail":"Claim code not found or already used."}` — a caller can never
  learn which case they hit (same reasoning as `forgot_password`'s generic
  200). The DB lookup always runs (even for an empty/malformed input) so the
  three cases aren't trivially distinguishable by response timing either.
- **Rate-limited:** `account_rate_limit_dependency` with
  `cpo_gateway_claim_account_rate_limiter` (default 10/60s per account,
  `CPO_GATEWAY_CLAIM_ACCOUNT_RATE_LIMIT`) bounds brute-force attempts from
  any single authenticated account — the endpoint requires auth in the first
  place (`require_role("cpo", "admin")`), so it's not open to anonymous
  scanning at all.
- **Blast radius on a successful guess:** binds one gateway's `tenant_id` to
  the guesser's own tenant — no money movement, no credential exposure (the
  MQTT broker password is never touched by this flow, see
  `deploy/docs/preflashed_unit_runbook.md`'s "Out of scope"). An operator can
  detect/undo a wrongly-claimed unit via `GET
  /api/admin/gateways/inventory` (admin-only) and re-assign it manually if
  needed — no self-service "release a claim" endpoint exists yet.
- **On the firmware side**, `GET /scan` (the captive-portal Wi-Fi network
  list) is unauthenticated but adds no exposure beyond joining the WPA2
  setup AP itself already grants (§8.1) — it's reachable exclusively via
  that AP, same as `GET /`.

Tests: `backend/tests/test_gateway_claim.py` (mint/claim/double-claim/
foreign-tenant/bad-code, mocked-DB + DB-gated), `backend/tests/test_rate_limiting.py`
(wiring).

---

## Quick remediation checklist

Status — open items and recently closed:
- [x] **Web HTTPS front door deployed + verified in prod** (2026-07-11):
      Caddy on 80/443, validated Let's Encrypt cert, http→https redirect.
      See §3 and `deploy/docs/web_tls_rollout.md`.
- [x] ~~Drop tcp:8000 from `allow-amphive-ports`; add HSTS~~ **Done
      2026-07-11** (`allow-amphive-ports` is tcp:80-only now; HSTS
      max-age=31536000 in the generated Caddyfile).
- [x] Replace DuckDNS with a real domain (2026-07-20) — `amphive.app` /
      `cpo.amphive.app` are live; DuckDNS retired. Bare-IP serve-mode is
      DELIBERATELY kept (not flipped to a redirect) — it is the DNS-outage
      escape hatch; see the TODO.md cutover entry's DECISION note.
- [x] **Rotate** WireGuard keys, DuckDNS token, Tapo & DB passwords at the source
      (2026-07-06). Dead old values remain in git history — *optional* scrub.
- [x] **Commit + deploy** the CORS allowlist (2026-07-06) — live in prod.
- [x] Add MQTT broker **auth** (2026-07-07) — `allow_anonymous false` + passwd
      file; backend, healthcheck, and gateway firmware (NVS creds) all
      authenticate; per-gateway accounts + topic ACLs + TLS on public 8883 as
      of 2026-07-10 (see §3, §8.3).
- [x] Set a strong `JWT_SECRET_KEY` in every environment — deploy.ps1 aborts on
      a missing/short/default secret (2026-07-05) and prod deploys since prove a
      strong key is set; the backend falls back to an ephemeral key elsewhere.
- [x] MQTT bound to the overlay IP + public 1883 firewall rule dropped (2026-07-06).
- [x] CORS restricted to an allowlist in `backend/main.py` (2026-07-06, deployed).
- [x] MQTT broker **TLS** rollout completion (2026-07-20): all gateways on
      direct-MQTT/8883; plaintext 1883 no longer host-published (see §3).
- [x] DB-level non-negative-balance CHECK (2026-07-07) — Alembic
      `0002_wallet_non_negative` adds `ck_users_coin_balance_non_negative`
      (see §5).
- [x] Unique `razorpay_payment_id` ledger column (2026-07-06) —
      `uq_ledger_razorpay_payment_id` + `IntegrityError` handling in
      `_credit_topup` closes the concurrent /verify + webhook double-credit race.

Device security (2026-07-06 audit, statuses as of 2026-07-11 — see
[§8](#8-firmware--gateway-device-security--backend-follow-up-gaps-2026-07-06-audit)):
- [x] ~~Lock down the provisioning portal~~ **Resolved fw 1.6.0** — WPA2 setup
      AP + per-device setup code gating `/save` + 10-min idle timeout +
      AP-only interface (§8.1).
- [ ] **Enable flash encryption + Secure Boot v2** so NVS secrets (Tapo account,
      MQTT credentials, setup code) aren't extractable (§8.2, HIGH).
- [x] ~~Ephemeral/tagged overlay keys + broker auth/ACL/TLS~~ **Resolved
      2026-07-10** — devices left the overlay; per-gateway broker credentials +
      topic ACLs + TLS are live (§8.3).
- [ ] **Require a button-hold for provisioning** instead of auto-opening the
      portal on Wi-Fi loss (§8.4 — now LOW: the portal is locked + idle-times-out
      since fw 1.6.0).
- [x] **Validate `plug.gateway_id` against the topic gateway** before billing
      telemetry (2026-07-11; §3, §8.5) — plus guarded ingestion casts (TD#25)
      and registration validation (TD#30) in the same change.
- [x] ~~Consume `+/alarms`~~ **Resolved 2026-07-10** — alarms persist as
      `gateway_events` + CPO events feed (§3, TD#21).
- [x] ~~Auth rate limiting on login/register~~ **Resolved 2026-07-11** —
      per-IP sliding window, 429 + Retry-After (§8.6); JWT revocation itself
      shipped 2026-07-08 (§2).

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
