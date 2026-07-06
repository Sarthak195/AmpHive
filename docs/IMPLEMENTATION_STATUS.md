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
| REST API (auth, groups, plugs, sessions, payments, direct, CPO portal) | ✅ | 37 endpoints — see [API_REFERENCE.md](API_REFERENCE.md) |
| JWT auth + bcrypt | ✅ | 7-day token, loaded fresh per request |
| Role-based access control | ✅ | Enforced via `services/rbac.py` `require_role(...)` on all `/api/cpo/*` routes (checks the DB role, not just the token) |
| MQTT command publish (ON/OFF) | ✅ | QoS 1, 3 s wait |
| MQTT inbound telemetry/status handling | ✅ | Telemetry updates TelemetryStore and session DB. Status updates gateway state in DB. |
| Live telemetry / Socket.io & SSE | ✅ | Streams real telemetry from TelemetryStore via Socket.io (with SSE as legacy/fallback). Automatically triggers the plug to report telemetry at 1s intervals when there are active listeners or an active session. **Correction 2026-07-05:** the stream task called a non-existent `await sio.get_participants(room=...)` API, which raised on every iteration and killed each stream before the first emit — the earlier "fully functional/verified" claim was wrong. Fixed (room-manager membership check) with a regression test in `backend/tests/test_socketio.py`. |
| Time-series telemetry persistence | ✅ | Persistent `telemetry_readings` table fed by a buffered background batch-flush from the MQTT handler (`services/telemetry_persistence.py`); queried by `GET /api/cpo/analytics/telemetry` via `date_trunc`. Plain Postgres (no TimescaleDB) — hypertables/retention/continuous-aggregates noted as a future upgrade. Live Socket.io/SSE still uses the in-memory TelemetryStore. |
| Razorpay create-order + verify | ✅ | HMAC-verified; credits coins + ledger. Supports decimal INR amounts and coin balances (money columns are `Numeric(12,2)`/Decimal as of 2026-07-06). **2026-07-05:** `/verify` now credits the **Razorpay-confirmed** amount fetched server-side (the client-sent `amount_inr` is deprecated/ignored — it was previously trusted, allowing arbitrary wallet inflation). |
| Razorpay webhook auto-credit | ✅ | Credits coins on `payment.captured`; atomic + idempotent vs. `/verify` (dedupes on the UNIQUE `razorpay_payment_id` via `IntegrityError`). Money columns are `Numeric(12,2)`/Decimal (2026-07-06). |
| Wallet debit on stop + ledger | ✅ | Row-locked (`SELECT ... FOR UPDATE`) in stop/verify/webhook paths |
| Direct Mode Tapo endpoints | ✅ | Gated by `DIRECT_MODE`; lib or relay mode |

### Frontend
| Capability | Status | Notes |
|------------|:------:|-------|
| Login/register, protected routes | ✅ | |
| Plug-ID start + available-plugs list | ✅ | |
| Live session monitor (Socket.io) | ✅ | Uses real Socket.io client with token-based connection authentication |
| Razorpay top-up flow | ✅ | CDN script + `window.Razorpay`; key comes from backend order. Formats and displays decimal coin balances. |
| Charger groups (join/list) | ✅ | |
| CPO operator portal (setup, dashboard, plugs, groups, sessions) | ✅ | `pages/cpo/*` behind `CpoProtectedRoute`; charts via `recharts` |
| Map of available plugs | ✅ | Leaflet/OpenStreetMap `MapComponent` on Home. Plug geolocation is now persisted (`Plug.latitude`/`longitude`, falling back to the gateway's coords); markers use real coordinates and plugs without a known location are omitted — the old `Math.random()` fallback (which also jittered markers on every re-render) is gone. |
| "View History" button (WalletCard) | ✅ | `WalletCard` button → `/history` route (`App.jsx`) → `History.jsx`, which fetches `GET /api/sessions/history`. Shows charging-session debits; does not yet show a unified ledger with top-up credits. |
| TypeScript usage | ❌ | TS toolchain + `@types/*` present, but all app code is `.jsx`/`.js` |

### Firmware
| Capability | Status | Notes |
|------------|:------:|-------|
| `microlink` Tailscale client (Noise/ts2021, DERP, DISCO, STUN, WG) | ✅ | Fully operational; NAT traversal and direct connections work using the unified magicsock port (41641). |
| MQTT control loop + topic contract | ✅ | Matches backend topics |
| Captive portal provisioning | ✅ | `AmpHive_Setup_XXXX` → NVS → reboot |
| Edge watchdogs (duration/energy/thermal/over-current) | ✅ | Thermal + over-current now use the plug's `overheat_status`/`overcurrent_status` flags (the P110 has no °C sensor) |
| Over-current cutoff | ✅ | Enforced via the plug's `overcurrent_status` flag → local OFF + `OVERCURRENT_CUTOFF` alarm |
| Tapo P110 driver (KLAP/AES) | ✅ | **Real KLAP v2** (mbedTLS SHA/AES + `esp_http_client`); fully verified on-device; builds on **ESP-IDF v5.3** (not v6) |
| Session persistence in NVS / offline resync | ✅ | `session_nvs` module persists active session to NVS; `offline_log` ring buffer (64 entries) caches telemetry during MQTT outages; resync on reconnect |
| OTA updates | ❌ | Single-app partition table; no `esp_https_ota` |
| Headscale (vs Tailscale defaults) | 🟡 | `/key` fetch supports it, but default host constants point at Tailscale |

### Infra / deploy
| Capability | Status | Notes |
|------------|:------:|-------|
| Docker Compose on GCP VM | ✅ | Live/canonical |
| `deploy.ps1` + `scripts/*.bat` helpers | ✅ | VM start/stop + remote compose/logs (in `scripts/`) |
| K8s/K3s manifests | 🟡 | Complete but not the live deployment; stale images, missing env |
| Mosquitto broker | 🟡 | Works, but anonymous + no TLS + publicly reachable |

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
3. **LWT offline alerts on the backend** (README description) is inaccurate (LWT is published by the *firmware*, and the backend has no LWT, though gateway status is now persisted on the backend when received).
4. **Python version:** README says 3.12; Dockerfile uses **3.11**.
5. **`schema.sql`/`schema_v2.sql` are not executed** — the ORM `create_all` is the
   real schema, and it omits unique constraints + indexes the SQL files define.
6. [Resolved 2026-07-02] RBAC is enforced. Self-registration still creates a
   `driver`, but a driver self-promotes to `cpo` via `POST /api/cpo/setup`, and
   `require_role("cpo","admin")` (`backend/services/rbac.py`) gates all `/api/cpo/*`
   routes against the live DB role.
7. [Resolved 2026-07-02] The unauthenticated `gateways/register` /
   `plugs/register` endpoints were removed; provisioning now goes through the
   RBAC-gated, tenant-scoped `POST /api/cpo/gateways` / `POST /api/cpo/plugs`.
8. **Direct Mode is documented as a temporary dev bypass** but is the
   actually-enabled path in the committed config.
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
15. **No OTA** and the **single-app partition table** precludes the spec'd
    dual-partition rollback without a partition change.
16. [Resolved 2026-07-02] **NVS session register / offline telemetry resync** now
    implemented via `session_nvs.c` (persists active session params) and
    `offline_log.c` (64-entry NVS ring buffer). Watchdogs enforce limits locally
    even when MQTT is down; buffered telemetry is resynced on reconnect.
17. [Resolved 2026-07-03] **Over-current cutoff** is now enforced on the firmware via
    the plug's `overcurrent_status` flag (local OFF + `OVERCURRENT_CUTOFF` alarm).
18. **Telemetry topic shape** in `requirements.md` (`.../plugs/{id}/telemetry`)
    doesn't match the implemented per-gateway topic — but firmware & backend agree.
19. **MQTT "Noise-encrypted TCP"** (spec) → plain `mqtt://`, secured only by the
    overlay.
20. **K8s vs VM divergence:** in-cluster Postgres vs Cloud SQL, Docker Hub images
    vs source builds, missing backend secrets in K8s.
21. **Committed VM public IP differs across docs** (`35.200.131.98` vs
    `34.100.200.152` vs others) — the IP is ephemeral; see [SECURITY.md](SECURITY.md).
22. **Stale "EC2" wording** survives in `deployment_checklist.md` though the
    platform is fully on GCP.
23. **mosquitto 9001 (websockets)** port is published but not served (no listener).
24. **Relay port mismatch:** `wireguard_tunnel_setup.md` says `:80`,
    `relay_server.py` listens on `:8000`.
25. **`frontend/README.md`** is the stock Vite template (not project docs).
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
    stay `Float`. A DB-level non-negative-balance CHECK is still not present.
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
    open gaps: sessions can start against offline gateways and there is no
    backend session reaper — see [TODO.md](TODO.md).
