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
| Gateway reachability in the driver plug API | ✅ | `GET /api/plugs/available` and `/api/plugs/{id}` now return `gateway_online` (via `gateway_is_live`: ONLINE + `last_seen_at` within the liveness window), so the driver UI can flag an unreachable charger before a start attempt rather than only via a 409. **Verified in prod** (live gateways → true, never-connected gateway → false). |
| Gateway firmware-version tracking | ✅ | The `fw` field on the gateway's `online` status is now persisted to `gateways.firmware_version` (Alembic `0006_gateway_firmware_version`; LWT/offline never clobbers it), exposed by `GET /api/cpo/gateways`, and shown in the CPO plugs table (online dot + `fw <ver>`) so an operator can see which gateways need an OTA. |
| Live-stream staleness flag | ✅ | The Socket.io `telemetry` payload now carries `is_stale`/`age_sec` (age since the last MQTT write vs `TELEMETRY_STALE_AFTER_SEC`, default 15 s), plus `relay_on`/`voltage_v`, so the frontend shows a "reconnecting" banner instead of freezing on stale values. |
| Live telemetry / Socket.io | ✅ | Streams real telemetry from TelemetryStore via Socket.io (sole transport — the legacy SSE endpoint + `sse-starlette` dep were retired 2026-07-07). Automatically triggers the plug to report telemetry at 1s intervals when there are active listeners or an active session. **Correction 2026-07-05:** the stream task called a non-existent `await sio.get_participants(room=...)` API, which raised on every iteration and killed each stream before the first emit — the earlier "fully functional/verified" claim was wrong. Fixed (room-manager membership check) with a regression test in `backend/tests/test_socketio.py`. |
| Time-series telemetry persistence | ✅ | Persistent `telemetry_readings` table fed by a buffered background batch-flush from the MQTT handler (`services/telemetry_persistence.py`); queried by `GET /api/cpo/analytics/telemetry` via `date_trunc`. Plain Postgres (no TimescaleDB) — hypertables/continuous-aggregates noted as a future upgrade; row retention is env-driven (`TELEMETRY_RETENTION_DAYS=90` in prod as of 2026-07-07). Live Socket.io still uses the in-memory TelemetryStore. |
| Razorpay create-order + verify | ✅ | HMAC-verified; credits coins + ledger. Supports decimal INR amounts and coin balances (money columns are `Numeric(12,2)`/Decimal as of 2026-07-06). **2026-07-05:** `/verify` now credits the **Razorpay-confirmed** amount fetched server-side (the client-sent `amount_inr` is deprecated/ignored — it was previously trusted, allowing arbitrary wallet inflation). |
| Razorpay webhook auto-credit | ✅ | Credits coins on `payment.captured`; atomic + idempotent vs. `/verify` (dedupes on the UNIQUE `razorpay_payment_id` via `IntegrityError`). Money columns are `Numeric(12,2)`/Decimal (2026-07-06). |
| Wallet debit on stop + ledger | ✅ | Row-locked (`SELECT ... FOR UPDATE`) in stop/verify/webhook paths |
| Prepaid protection: auto-stop on balance exhaustion | ✅ | On each telemetry write, if the accrued energy cost (`kwh × COINS_PER_KWH`) reaches the driver's wallet balance, the session is auto-stopped via the shared `finalize_charging_session` path (own txn, row-locked, race-safe with a user stop / the reaper). Caps free charging past a drained wallet to ≤ one telemetry interval. Env-toggle `AUTO_STOP_ON_BALANCE_EXHAUSTED` (default on). Tests in `test_mqtt_manager.py`. |
| Direct Mode Tapo endpoints | ✅ | Gated by `DIRECT_MODE`; lib or relay mode |

### Frontend
| Capability | Status | Notes |
|------------|:------:|-------|
| Login/register, protected routes | ✅ | |
| Plug-ID start + available-plugs list | ✅ | |
| Live session monitor (Socket.io) | ✅ | Real Socket.io client with token-auth. **2026-07-10:** client-side ticking elapsed clock (no longer freezes between frames), a "reconnecting" staleness banner (server `is_stale` OR no frame for 15 s), voltage + actual-relay secondary line, and a per-plug `gateway_alarm` warning banner (e.g. unauthorized-on). Tests in `SessionMonitor.test.jsx`. |
| Driver plug list: gateway-offline UX | ✅ | Home marks a plug whose gateway is unreachable (`gateway_online === false`) as "charger offline", dims it, and disables start — no more blind 409s at session start. |
| CPO alert feed | ✅ | `CpoAlerts` on the dashboard fetches `GET /api/cpo/events?unacknowledged_only=true`, merges live `gateway_alarm` broadcasts, and dismisses via the ack endpoint. Severity-styled (critical/warning/info). |
| CPO session CSV export | ✅ | `GET /api/cpo/analytics/sessions.csv` (same filters as the JSON endpoint) → downloadable `text/csv`; "Export CSV" button on the Sessions page (authenticated fetch → blob). |
| CPO load analytics: current (amps) | ✅ | `/api/cpo/analytics/telemetry` now also aggregates `avg_current_a`/`max_current_a` per bucket; the dashboard load chart header shows peak W **and** A. |
| Razorpay top-up flow | ✅ | CDN script + `window.Razorpay`; key comes from backend order. Formats and displays decimal coin balances. |
| Pricing clarity | ✅ | `GET /api/config` (public) feeds a `ConfigProvider`; Home shows the tariff (`coins_per_kwh`) + what the driver's balance covers (≈ kWh) with a top-up nudge below the minimum, and the session monitor reads the rate from config instead of hardcoding it. The session-start minimum is now env-driven (`MIN_START_BALANCE_COINS`) and the 402 message matches the displayed number (2026-07-10). |
| Low-balance live warning | ✅ | The session monitor warns (with remaining coins ≈ kWh) as accrued cost nears the wallet balance, pairing with the backend auto-stop so the driver sees it coming. Tests in `SessionMonitor.test.jsx`. |
| Post-session receipt | ✅ | `finalize_charging_session` now returns a full receipt (plug name, energy, peak power, duration, coins charged + any forgiven shortfall, balance before → after, timestamps, stop reason); the Session page shows a `SessionReceipt` card on stop (with an auto-stop notice when applicable) and refreshes the wallet. **Verified live end-to-end 2026-07-10** — a real billed session on the fake plug (0.101 kWh → 0.51 coins, balance 499.47 → 498.96, reconciled in the ledger). Tests in `SessionReceipt.test.jsx`. |
| Charger groups (join/list) | ✅ | |
| CPO operator portal (setup, dashboard, plugs, groups, sessions) | ✅ | `pages/cpo/*` behind `CpoProtectedRoute`; charts via `recharts` |
| Map of available plugs | ✅ | Leaflet/OpenStreetMap `MapComponent` on Home. Plug geolocation is now persisted (`Plug.latitude`/`longitude`, falling back to the gateway's coords); markers use real coordinates and plugs without a known location are omitted — the old `Math.random()` fallback (which also jittered markers on every re-render) is gone. |
| "View History" button (WalletCard) | ✅ | `WalletCard` button → `/history` route → `History.jsx`, now **tabbed**: "Charging Sessions" (`GET /api/sessions/history`) and "Wallet Ledger" (`GET /api/wallet/ledger`) — the latter is the unified money trail (top-up credits **and** session debits, signed amount + running `balance_after`), closing the old "debits-only" gap (2026-07-10). |
| CPO gateways management + OTA-from-UI | ✅ | New `/cpo/gateways` page (sidebar link) lists each gateway's status, reported firmware, last-seen, and plug count, with an "Update Firmware" action that POSTs `/api/cpo/gateways/{id}/ota` (https image URL; button disabled unless the gateway is online with ≥1 plug). Completes the OTA loop the fw-tracking surfaced (2026-07-10). |
| TypeScript usage | — | **Decided against 2026-07-07** (TD#14): toolchain removed; all app code is plain `.jsx`/`.js` by policy. ESLint now actually lints `js/jsx` (the old config matched only `ts,tsx` — zero files). |
| Frontend tests (Vitest + RTL) | ✅ | 20 tests: AuthContext, ProtectedRoute/CpoProtectedRoute, TopUp payment handler (no client amount on `/verify`), multi-session SessionContext. `npm test` runs in CI. |

### Firmware
| Capability | Status | Notes |
|------------|:------:|-------|
| **Direct MQTT transport (fw ≥ 1.3.0, default)** | ✅ | `AMPHIVE_DIRECT_MQTT=1`: outbound `mqtts://8.231.81.12:8883` right after Wi-Fi — no overlay, NAT/CGNAT-immune, esp-mqtt owns reconnects. **Verified on-device 2026-07-10** (~3.3 s power-on→connected through a symmetric-NAT router; telemetry at 10 s cadence). Binary shrank ~50% (microlink linked out). |
| **Unauthorized physical-on guard (fw ≥ 1.5.0)** | ✅ | The relay ON with no active session (physical button / Tapo app / stale NVS resume) is forced OFF locally every poll and alarmed once per episode (`UNAUTHORIZED_ON`, rising-edge). Uses the plug's real `device_on` (previously read but discarded). **Live on the real gateway 2026-07-10** (OTA'd to `1.5.0-direct`); the backend ingests the alarm end-to-end (verified in prod). The remote out-of-band physical-press trigger itself is unit-tested + by-construction (no LAN path to press the button remotely). |
| **Richer telemetry: relay state + trapezoidal energy (fw ≥ 1.5.0)** | ✅ | Telemetry now carries the actual `relay` (device_on) state alongside derived `current`/nominal `voltage`; the driver-side kWh integrator switched from left-rectangle to the **trapezoidal rule** (averages consecutive power samples) for lower error on ramping loads at the 10 s cadence. **Verified on the wire 2026-07-10** (real gateway telemetry shows `"relay":false`). |
| `microlink` Tailscale client (Noise/ts2021, DERP, DISCO, STUN, WG) | 🟡 | **Demoted to legacy transport** (`AMPHIVE_DIRECT_MQTT=0`): works for full-cone NATs, but symmetric NAT defeats DISCO hole-punching (root-caused 2026-07-09) — the reason for the direct-MQTT pivot. Kept compilable for rollback/comparison. |
| MQTT control loop + topic contract | 🟡 | Matches backend topics. **2026-07-06 audit: single-plug only** — `main.c` has one `target_plug_ip`/`active_session` and `tapo_protocol.c` one global KLAP session + energy integrator, so on a gateway with >1 plug a command for plug B toggles plug A and telemetry is misattributed. Multi-plug needs a per-plug state table + instance-based KLAP driver. (The software **AmpHive Agent** already handles multiple plugs per gateway — this limit is ESP32-firmware-only.) (§3.50, TD#20, SEC §8.5) |
| Captive portal provisioning | ✅ | `AmpHive_Setup_XXXX` → NVS → reboot |
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
| Docker Compose on GCP VM | ✅ | Live/canonical |
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
50. **[Open] ESP32 firmware is single-plug.** `main.c` (`target_plug_ip`,
    `active_session`, `active_plug_id`, `telemetry_interval_ms`) and
    `tapo_protocol.c` (global `s_sess`, `s_energy_wh`) are single-instance:
    commands for a second plug on the same gateway actuate the first, and
    telemetry is published under the last-commanded id. The data model allows
    many plugs per gateway, and the software AmpHive Agent already drives
    multiple plugs — this is firmware-only. (TD#20, SEC §8.5)
51. **[Resolved 2026-07-11] Sessions startable on OFFLINE/MAINTENANCE plugs.**
    `start_charging_session` now 409s on any non-`AVAILABLE` status (OCCUPIED
    keeps its "in use" message; OFFLINE/MAINTENANCE get "out of service").
    Behavior change: new plugs default to OFFLINE, so a freshly registered
    plug must be set AVAILABLE in the CPO portal before its first session
    (finalize already resets used plugs to AVAILABLE). (TD#22)
52. **[Open] Crash-recovery resets the duration watchdog.** On reboot the
    recovered session's `start_time_s` is reset to "now" (`main.c`, tick-based,
    no wall clock), so the time cap restarts from zero each reboot (the energy
    cap still holds). (TD#23)
53. **[Open, narrowed] Offline-resync telemetry can bill the wrong session.**
    Live readings are now attributed by the firmware-echoed `session_id`
    (fw ≥ 1.4.x), which closes the plug-reused-while-online case. But
    `offline_log` ring-buffer entries still carry no `session_id`, so readings
    buffered across an MQTT outage are attributed to the plug's *current*
    ACTIVE session on resync — the stale-overwrite window remains for the
    buffered path. (TD#24)
54. **[Open, reduced scope] Device / provisioning security.** Still open: open
    setup AP + unauthenticated `/save`, no flash-encryption (plaintext NVS
    secrets — Wi-Fi, Tapo account, per-gateway MQTT creds), and the boot-time
    fallback into the open portal. Since the audit: OTA images are **signed**
    (ECDSA verify-on-update) and HTTPS-only, and the reusable-overlay-key +
    anonymous-broker item is **gone** (devices left the overlay for direct
    MQTT with per-gateway credentials + topic ACLs + TLS, 2026-07-10).
    (SEC §8)
55. **[Partially resolved] Observability / onboarding polish.** Fixed since the
    audit: unified wallet ledger (endpoint + History tab, 2026-07-10 — TD#29),
    gateway staleness (read-time `gateway_is_live` + `gateway_online` in the
    driver API + session reaper — TD#27), and the shared-`Event` latency nit
    (closed by retiring SSE 2026-07-07 — TD#32). Still open: unstructured
    stdout-only logging (no correlation ids — TD#28), no CPO admin audit log
    (TD#26), registration skips `EmailStr`/password rules (TD#30), and the
    captive-portal inputs render narrow (`width:100%%` — TD#31). (TD#26–31)
