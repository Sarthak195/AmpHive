# AmpHive — System Architecture

*Verified against source on 2026-07-02. For per-component status and the gap
between this and the product specs, see [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md).*

AmpHive turns off-the-shelf smart plugs (TP-Link Tapo P110) into a shared,
monetizable EV-charging network. A driver enters a **Plug ID** in a web app,
the backend authorizes and bills against a prepaid coin wallet, and a command
travels to the plug — either through an ESP32 gateway over an encrypted overlay
network, or (today, for dev/test) directly over a WireGuard tunnel.

---

## 1. The four planes

```
┌───────────────┐   REST/JSON    ┌─────────────────────┐   asyncpg    ┌──────────────────┐
│  Driver Web   │ ◄────────────► │   FastAPI backend   │ ◄──────────► │   PostgreSQL 15  │
│  App (React)  │  + Socket.io   │   (Uvicorn, main.py)│              │ (Docker on VM)   │
└───────────────┘                └──────┬──────────────┘              └──────────────────┘
                                        │ MQTT (paho)
                                  ┌─────▼──────────┐
                                  │   Mosquitto    │  topics: amphive/gateways/...
                                  │   MQTT broker  │
                                  └─────┬──────────┘
                                        │  (MQTT runs *inside* the overlay tunnel)
              ┌─────────────────────────┴───────────────────────────┐
              │                                                       │
   ── PATH A: ESP32 gateway ─────────────────          ── PATH B: Direct Mode (dev) ──
              │                                                       │
   ┌──────────▼───────────┐                              ┌───────────▼────────────┐
   │  ESP32-S3 gateway     │  Tailscale overlay          │  Backend calls a relay  │
   │  (microlink client)   │  100.64.0.0/10              │  over WireGuard tunnel  │
   └──────────┬───────────┘                              │  10.10.0.0/24           │
              │ local LAN/VLAN                            └───────────┬────────────┘
   ┌──────────▼───────────┐                              ┌───────────▼────────────┐
   │  Tapo P110 smart plug │                              │  Tapo P110 (home LAN)   │
   └──────────────────────┘                              └─────────────────────────┘
```

The cloud control plane is identical for both paths. The difference is **how the
last hop to the physical plug is reached**.

---

## 2. The two operating modes

AmpHive can drive a plug two ways. As of 2026-07-06 the deployment runs **Path A**
(ESP32 gateway over MQTT); **Path B (Direct Mode over WireGuard) has been retired**
— the WireGuard tunnel is no longer used and `DIRECT_MODE=false`. The Direct-Mode
code (`tapo_direct`, `/direct/*`) remains but is dormant, kept for reference.

### Path A — ESP32 gateway over MQTT (the product design)
1. The ESP32 joins the **Headscale/Tailscale overlay** via the `microlink`
   firmware component and gets a `100.64.x.x` VPN IP.
2. It connects to the MQTT broker at `mqtt://100.64.0.1:1883` (the server's
   overlay IP) and subscribes to its command topic.
3. The backend publishes `ON`/`OFF` commands; the ESP32 drives the local plug
   and publishes telemetry/status back.
4. See [MQTT_CONTRACT.md](MQTT_CONTRACT.md) for exact topics.

**Status:** the firmware control loop, overlay client, and MQTT contract are
implemented and the topic strings match the backend. The backend's inbound
handlers are **live** — telemetry updates the in-memory `TelemetryStore` (feeding
the Socket.io stream) and persists `energy_kwh`/`peak_power_w` to the session row, and
status messages update gateway online/offline state in the DB. The ESP32 now implements a **real KLAP v2** Tapo driver (mbedTLS SHA/AES + esp_http_client), with on-device flash verification pending. See [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md).

### Path B — Direct Mode over WireGuard (retired 2026-07-06 — kept for reference)
1. A WireGuard tunnel links the GCP VM (`10.10.0.1`) to the developer's home PC
   (`10.10.0.2`) on UDP/51820.
2. The backend's `tapo_direct` service calls a small HTTP **relay**
   (`tools/relay_server.py`, port 8000) running on the home PC via
   `TAPO_RELAY_URL`. The relay uses the `tapo` Python library to control the
   real plug on the home LAN.
3. Exposed under `POST/GET /api/direct/plug/*` and gated by `DIRECT_MODE=true`.

This is explicitly a **temporary bypass** until physical ESP32 hardware is in
place, but it is the path that is wired up in the committed environment.

---

## 3. Charging-session flow (cloud control plane)

This is common to both modes. Endpoint details in [API_REFERENCE.md](API_REFERENCE.md).

```
Driver enters Plug ID
        │
        ▼
POST /api/sessions/start            (JWT required)
  • verify plug access (public, ungrouped, or member of its private group)
  • require coin_balance ≥ 50        → 402 if not
  • reject if plug is OCCUPIED        → 409 if so
  • MQTT publish ON to the gateway    → 500 if publish fails (Path A)
  • create charging_sessions(status=active), set plug OCCUPIED
  • telemetry_store.start_session()
        │
        ▼
Socket.io: subscribe_session          (WebSocket)
  • streams telemetry snapshots from the in-memory TelemetryStore
  • the store is fed by inbound MQTT telemetry (Path A), where the ESP32 drives
    real P110 hardware via the local KLAP v2 protocol.
        │
        ▼
POST /api/sessions/stop              (JWT required)
  • MQTT publish OFF (best-effort)
  • read final energy/cost from telemetry_store.get_latest()
  • finalize session, debit wallet, write ledger_transactions(session_debit)
  • set plug AVAILABLE
```

### Auth & wallet
- **Auth:** stateless JWT (HS256, 7-day expiry), `Authorization: Bearer`.
  Passwords hashed with bcrypt. Accounts are created as `driver`; a driver
  self-promotes to `cpo` via `POST /api/cpo/setup` (which also creates their
  tenant). **Role-based access control is enforced** on every `/api/cpo/*` route
  by `require_role("cpo","admin")` (`backend/services/rbac.py`), which checks the
  live DB role rather than trusting the token.
- **Wallet:** a single `users.coin_balance` float. Credited by Razorpay
  top-ups (`/api/payments/verify` and the server-authoritative webhook), debited
  at session stop. Conversion is `COINS_PER_RUPEE` (default 1.0). Credits and
  debits are **row-locked** (`SELECT ... FOR UPDATE`) so concurrent updates
  don't race.
- **Telemetry/live data:** live telemetry is served in real time via **Socket.io** (with legacy SSE endpoint kept as fallback) from an **in-memory**
  `TelemetryStore` singleton (`backend/services/telemetry.py`). Raw samples are
  **also** persisted to the `telemetry_readings` time-series table via a buffered
  background batch-flush (`backend/services/telemetry_persistence.py`), decoupled
  from the live streaming path, and queried by `GET /api/cpo/analytics/telemetry`. This uses
  **plain Postgres** + `date_trunc` aggregation; the product spec's "TimescaleDB"
  is not present (a possible future upgrade).

---

## 4. Frontend

React 19 + Vite SPA in `frontend/`. Served by Nginx, which also reverse-proxies
`/api/` to the backend (so the SPA and API are same-origin in production).

| Route | Page | Access | Purpose |
|-------|------|--------|---------|
| `/` | `Home.jsx` | public (content gated on login) | Wallet card + available chargers + "start by Plug ID" |
| `/login` | `Login.jsx` | public | Combined sign-in / register |
| `/session` | `Session.jsx` | protected | Live session monitor (`SessionMonitor` + Socket.io) |
| `/topup` | `TopUp.jsx` | protected | Razorpay checkout to buy coins |
| `/groups` | `Groups.jsx` | protected | Join private charger groups by access code |

State lives in three React contexts: `AuthContext` (JWT in `localStorage`,
`/api/auth/me` on load), `SessionContext` (manages Socket.io connection and subscription
for live telemetry), and `WalletContext` (derives balance from the user object). Razorpay
is loaded via a CDN `<script>` and used through `window.Razorpay`. Home renders a
**Leaflet/OpenStreetMap** map (`MapComponent`, `react-leaflet`) of available
plugs — though plug **coordinates aren't persisted** in the data model yet, so
markers currently use fallback/random positions near India.

**CPO operator portal** — a second set of pages under `frontend/src/pages/cpo/`
(`CpoSetup`, `CpoDashboard`, `CpoPlugs`, `CpoGroups`, `CpoSessions`) sits behind
a `CpoProtectedRoute` that requires the `cpo` role and drives the `/api/cpo/*`
endpoints (tenant setup, gateway/plug/group CRUD, and analytics).

---

## 5. Networks at a glance

| Network | Range | Purpose | Used by |
|---------|-------|---------|---------|
| Headscale/Tailscale overlay | `100.64.0.0/10` | Secure mesh between server and ESP32 gateways | Path A (firmware `microlink`, K8s `headscale.yaml`) |
| WireGuard Direct-Mode tunnel | `10.10.0.0/24` | Cloud VM ↔ home PC for Direct Mode (**retired 2026-07-06**) | Path B (`amphive_tunnel.conf`, `tapo_direct`) |
| Site IoT VLAN (VLAN 20) | site-defined | Physically isolate plugs + gateway from the resident network | Product design (CPO router config) |

These are three independent networks; the `100.64.x` overlay and the `10.10.0.x`
tunnel are unrelated despite both being "VPNs".

---

## 6. Tech stack (as built)

| Layer | Technology |
|-------|------------|
| Frontend | React 19, React Router 6, Vite 8, hand-written CSS (glassmorphism). Razorpay via CDN. |
| Backend | Python 3.11 (Dockerfile), FastAPI, Uvicorn, SQLAlchemy 2.0 (async), Pydantic, paho-mqtt v2, python-jose, passlib/bcrypt, razorpay, tapo. |
| Database | PostgreSQL 15 (Docker container on the GCP VM — Cloud SQL was decommissioned 2026-06-29). **No** TimescaleDB. |
| Messaging | Eclipse Mosquitto 2.0 (anonymous, no TLS — secured by the overlay). |
| Overlay VPN | Headscale control plane + the custom `microlink` Tailscale client on the ESP32. |
| Firmware | ESP-IDF (targets ESP32-S3-N16R8), FreeRTOS, `microlink`, vendored `wireguard_lwip`. |
| Infra | Docker / Docker Compose on a GCP Compute Engine VM (`asia-south1`); K8s/K3s manifests also present but not the live deployment. |
