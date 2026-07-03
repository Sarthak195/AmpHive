# AmpHive — Implementation Status & Discrepancies

*Verified against source on 2026-07-02. This page reconciles the aspirational
product specs ([requirements.md](../requirements.md), [features_list.md](../features_list.md))
with what the code actually does. Legend: ✅ works · 🟡 partial · 🟦 stub/mock ·
❌ not implemented.*

---

## 1. Status matrix

### Backend
| Capability | Status | Notes |
|------------|:------:|-------|
| REST API (auth, groups, plugs, sessions, payments, direct, CPO portal) | ✅ | 35 endpoints — see [API_REFERENCE.md](API_REFERENCE.md) |
| JWT auth + bcrypt | ✅ | 7-day token, loaded fresh per request |
| Role-based access control | ✅ | Enforced via `services/rbac.py` `require_role(...)` on all `/api/cpo/*` routes (checks the DB role, not just the token) |
| MQTT command publish (ON/OFF) | ✅ | QoS 1, 3 s wait |
| MQTT inbound telemetry/status handling | ✅ | Telemetry updates TelemetryStore and session DB. Status updates gateway state in DB. |
| Live telemetry / SSE | ✅ | Fully functional, streams real telemetry from TelemetryStore |
| Time-series telemetry persistence | ✅ | Persistent `telemetry_readings` table fed by a buffered background batch-flush from the MQTT handler (`services/telemetry_persistence.py`); queried by `GET /api/cpo/analytics/telemetry` via `date_trunc`. Plain Postgres (no TimescaleDB) — hypertables/retention/continuous-aggregates noted as a future upgrade. Live SSE still uses the in-memory TelemetryStore. |
| Razorpay create-order + verify | ✅ | HMAC-verified; credits coins + ledger |
| Razorpay webhook auto-credit | ✅ | Credits coins on `payment.captured`; atomic + idempotent vs. `/verify` (dedupes on `razorpay_payment_id`) |
| Wallet debit on stop + ledger | ✅ | Row-locked (`SELECT ... FOR UPDATE`) in stop/verify/webhook paths |
| Direct Mode Tapo endpoints | ✅ | Gated by `DIRECT_MODE`; lib or relay mode |

### Frontend
| Capability | Status | Notes |
|------------|:------:|-------|
| Login/register, protected routes | ✅ | |
| Plug-ID start + available-plugs list | ✅ | |
| Live session monitor (SSE) | ✅ | Uses real `EventSource` with token query parameter |
| Razorpay top-up flow | ✅ | CDN script + `window.Razorpay`; key comes from backend order |
| Charger groups (join/list) | ✅ | |
| CPO operator portal (setup, dashboard, plugs, groups, sessions) | ✅ | `pages/cpo/*` behind `CpoProtectedRoute`; charts via `recharts` |
| Map of available plugs | 🟡 | Leaflet/OpenStreetMap `MapComponent` on Home; plug coordinates not persisted, so markers use fallback/random positions |
| "View History" button (WalletCard) | ✅ | `WalletCard` button → `/history` route (`App.jsx`) → `History.jsx`, which fetches `GET /api/sessions/history`. Shows charging-session debits; does not yet show a unified ledger with top-up credits. |
| TypeScript usage | ❌ | TS toolchain + `@types/*` present, but all app code is `.jsx`/`.js` |

### Firmware
| Capability | Status | Notes |
|------------|:------:|-------|
| `microlink` Tailscale client (Noise/ts2021, DERP, DISCO, STUN, WG) | 🟡 | Substantial & mostly working; TODOs in WG send/pubkey, no IPv6, zerocopy excluded |
| MQTT control loop + topic contract | ✅ | Matches backend topics |
| Captive portal provisioning | ✅ | `AmpHive_Setup_XXXX` → NVS → reboot |
| Edge watchdogs (duration/energy/thermal 75 °C) | ✅ | |
| Over-current cutoff (13 A / 5 min) | ❌ | Current published but never compared |
| Tapo P110 driver (KLAP/AES) | 🟦 | Mock — returns simulated telemetry, cleartext POST |
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

- **Path A (ESP32 + MQTT)** is functional on the backend: telemetry ingestion,
  TelemetryStore updates, DB persistence, and gateway status updates are fully
  implemented. However, the ESP32 firmware plug driver is currently mocked (returns
  simulated telemetry), so the values are not yet from a physical smart plug.
- **Path B (Direct Mode + WireGuard relay)** is the path that actually controls a
  physical plug today, and it's what the committed env enables. It does not feed
  the session/telemetry pipeline either — it's a separate on/off/info surface.
- A fully working billed session with a real plug over Path A requires finishing
  the firmware Tapo driver (real Tapo driver instead of the mock).

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
11. **Frontend SSE auth gap:** comment says pass `?token=`, code doesn't.
12. [Partly resolved 2026-07-02] A Leaflet/OpenStreetMap map (`MapComponent`) is
    now on Home, but plug **geolocation isn't in the data model**, so markers use
    fallback/random coordinates rather than real plug locations.
13. [Resolved 2026-07-02] The dead `frontend/src/api/mockSse.js` leftover has been
    deleted.
14. **Firmware Tapo driver is a mock** — no KLAP, no AES, simulated readings.
15. **No OTA** and the **single-app partition table** precludes the spec'd
    dual-partition rollback without a partition change.
16. [Resolved 2026-07-02] **NVS session register / offline telemetry resync** now
    implemented via `session_nvs.c` (persists active session params) and
    `offline_log.c` (64-entry NVS ring buffer). Watchdogs enforce limits locally
    even when MQTT is down; buffered telemetry is resynced on reconnect.
17. **No over-current cutoff** on the firmware.
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
</content>
