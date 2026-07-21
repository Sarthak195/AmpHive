# AmpHive — System Architecture

*Verified against source on 2026-07-20. For per-component status and the gap
between this and the product specs, see [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md).*

AmpHive turns off-the-shelf smart plugs (TP-Link Tapo P110) into a shared,
monetizable EV-charging network. A driver enters a **Plug ID** in a web app,
the backend authorizes and bills against a prepaid coin wallet, and a command
travels to the plug through an ESP32-C3 gateway that dials **outbound direct
MQTT** to the public broker over the open internet — no VPN/overlay hop.

---

## 1. The four planes

```
┌───────────────┐   REST/JSON    ┌─────────────────────┐   asyncpg    ┌──────────────────┐
│  Driver Web   │ ◄────────────► │   FastAPI backend   │ ◄──────────► │   PostgreSQL 15  │
│  App (React)  │  + Socket.io   │   (Uvicorn, main.py)│              │ (Docker on VM)   │
│  (Caddy TLS)  │                │   :8000, VM-local    │              │ (Docker on VM)   │
└───────────────┘                └──────┬──────────────┘              └──────────────────┘
                                        │ MQTT (paho)
                                  ┌─────▼──────────┐
                                  │   Mosquitto    │  topics: amphive/gateways/...
                                  │   MQTT broker  │  TLS on :8883 (mqtt.amphive.app)
                                  └─────┬──────────┘  per-gateway user/pass + ACLs
                                        │  outbound mqtts:// over the public internet
                             ┌──────────▼───────────┐
                             │  ESP32-C3 gateway     │
                             │  (AMPHIVE_DIRECT_MQTT)│
                             └──────────┬───────────┘
                                        │ local LAN/VLAN
                             ┌──────────▼───────────┐
                             │  Tapo P110 smart plug │
                             └──────────────────────┘
```

Caddy terminates TLS for the web/API surface on `amphive.app` (driver) and
`cpo.amphive.app` (operator portal); the FastAPI backend on `:8000` is
VM-local only, not published to the internet directly. The MQTT broker
terminates TLS on `mqtt.amphive.app:8883`; each gateway authenticates with a
per-gateway username/password and is restricted by topic ACL to its own
`amphive/gateways/<id>/#` namespace. There is no VPN/overlay between the
gateway and the broker — the connection is a plain outbound TLS session, same
as any MQTT client on the public internet.

---

## 2. The gateway path — direct MQTT (the product design)

Since 2026-07-10 the deployment runs a single live path: the ESP32-C3 gateway
over **direct MQTT**, built with `AMPHIVE_DIRECT_MQTT=1`. Two earlier paths are
retired and kept only for history — see below.

### Direct MQTT gateway (live)
1. The ESP32-C3 dials **outbound** `mqtts://mqtt.amphive.app:8883` over the
   public internet (TLS + per-gateway username/password) and subscribes to its
   command topic. No VPN/overlay hop is involved.
2. The backend publishes `ON`/`OFF` commands; the gateway drives the local plug
   and publishes telemetry/status back, restricted by broker ACL to its own
   `amphive/gateways/<id>/#` topic namespace.
3. See [MQTT_CONTRACT.md](MQTT_CONTRACT.md) for exact topics.

**Status:** the firmware control loop and MQTT contract are implemented and the
topic strings match the backend. The backend's inbound handlers are **live** —
telemetry updates the in-memory `TelemetryStore` (feeding the Socket.io stream)
and persists `energy_kwh`/`peak_power_w` to the session row, and status messages
update gateway online/offline state in the DB. The ESP32 runs a **real KLAP v2**
Tapo driver (mbedTLS SHA/AES + esp_http_client) and has been run **end-to-end on
physical hardware** — a real ESP32-C3 + P110 drove billed sessions with correct
session-relative energy delivery. See
[IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md).

### Retired — ESP32 gateway over the Headscale/Tailscale overlay (retired 2026-07-10)
Gateways formerly joined a Headscale/Tailscale overlay (`100.64.x.x` VPN IPs,
`microlink` firmware component) and reached the broker over that tunnel at
`mqtt://100.64.0.1:1883`. The direct-MQTT pivot removed this hop entirely; the
overlay control plane is decommissioned and the `microlink` firmware task is
compiled out.

### Retired — Direct Mode over WireGuard (retired 2026-07-06 — kept for reference)
1. A WireGuard tunnel linked the GCP VM (`10.10.0.1`) to the developer's home PC
   (`10.10.0.2`) on UDP/51820.
2. The backend's `tapo_direct` service called a small HTTP **relay**
   (`tools/relay_server.py`, port 8000) running on the home PC via
   `TAPO_RELAY_URL`. The relay used the `tapo` Python library to control the
   real plug on the home LAN.
3. Exposed under `POST/GET /api/direct/plug/*` and gated by `DIRECT_MODE=true`.

This was a **temporary bypass** used before physical ESP32 hardware was
available; the code (`tapo_direct`, `/direct/*`) remains but is dormant.

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
- **Wallet:** a single `users.coin_balance` `NUMERIC(12,2)` (Decimal — moved off
  `float` to kill rounding drift; all wallet math goes through
  `services/money.to_money`). Credited by Razorpay top-ups
  (`/api/payments/verify` and the server-authoritative webhook), debited at
  session stop (energy × `COINS_PER_KWH`). Conversion is `COINS_PER_RUPEE`
  (default 1.0). Credits and debits are **row-locked** (`SELECT ... FOR UPDATE`)
  so concurrent updates don't race.
- **Telemetry/live data:** live telemetry is served in real time via **Socket.io** (sole transport since 2026-07-07; the legacy SSE endpoint is retired) from an **in-memory**
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
plugs — plug **coordinates are persisted** (`Plug.latitude`/`longitude`, falling
back to the gateway's coords); markers use real coordinates and plugs without a
known location are omitted (the old `Math.random()` fallback is gone). Markers are
color-coded by availability (Available/In use/Offline, `utils/plugAvailability.js`)
with a legend + live counts, and availability/group-name filters narrow both the
list and the map together. Visiting `/?plug=<id>` (e.g. from a QR code printed via
`/cpo/plugs`) prefills and focuses the Plug ID start input — still fully
auth-gated: `ProtectedRoute`/`CpoProtectedRoute` and this deep-link guard all stash
the origin location as router `state.from` so `Login.jsx` returns the driver to it
(query string included) after signing in, instead of always landing on `/`.

**CPO operator portal** — a second set of pages under `frontend/src/pages/cpo/`
(`CpoSetup`, `CpoDashboard`, `CpoPlugs`, `CpoGroups`, `CpoSessions`) sits behind
a `CpoProtectedRoute` that requires the `cpo` role and drives the `/api/cpo/*`
endpoints (tenant setup, gateway/plug/group CRUD, and analytics).

**Hostname partition (2026-07-20)** — one bundle, two hostnames. The portal
is served on `cpo.amphive.app` (the real `amphive.app` domain went live
2026-07-20; the earlier `amphive.duckdns.org` DuckDNS hostnames are retired —
no DuckDNS updater cron remains). `CADDY_CPO_DOMAIN` in `.env` makes
`deploy.ps1` emit a second, identical Caddy site block.
`frontend/src/utils/appHost.js` (`isCpoHost()`,
with a `VITE_FORCE_CPO_HOST` dev/test override, plus `cpoOrigin()` /
`driverOrigin()`) drives the split in `App.jsx` and `Navbar.jsx`:

- **Driver host** — driver routes only; `/cpo/*` hard-redirects to the CPO
  origin (`components/HostRouting.jsx` `ExternalRedirect`). The navbar's
  in-app CPO links are gone; signed-in users get a modest external
  "Apply to host chargers" link to `<cpo-origin>/cpo`.
- **CPO host** — operator portal only. `/` role-routes (`CpoLanding`):
  anonymous → `/login` (same `Login.jsx`), `cpo`/`admin` → `/cpo/dashboard`,
  a driver-role login gets a "not an operator account" notice linking to the
  driver origin and to the `/cpo` become-a-host flow. Driver routes
  (`/map`, `/topup`, `/session`, `/groups`, `/history`) hard-redirect to the
  driver origin. Both hostnames are in the backend CORS/Socket.io allowlists.

---

## 5. Networks at a glance

| Network | Range | Purpose | Used by |
|---------|-------|---------|---------|
| Public internet (TLS) | n/a | Gateway ↔ broker direct MQTT (`mqtt.amphive.app:8883`), TLS + per-gateway creds/ACLs | Live gateway path (`AMPHIVE_DIRECT_MQTT`) |
| Site IoT VLAN (VLAN 20) | site-defined | Physically isolate plugs + gateway from the resident network | Product design (CPO router config) |
| ~~Headscale/Tailscale overlay~~ | `100.64.0.0/10` | ~~Secure mesh between server and ESP32 gateways~~ — **retired 2026-07-10**, superseded by direct MQTT | Historical (firmware `microlink`, K8s `deploy/k8s/headscale.yaml`) |
| ~~WireGuard Direct-Mode tunnel~~ | `10.10.0.0/24` | ~~Cloud VM ↔ home PC for Direct Mode~~ — **retired 2026-07-06** | Historical (`amphive_tunnel.conf`, `tapo_direct`) |

The overlay and the WireGuard Direct-Mode tunnel were two independent,
now-retired VPNs; gateways today reach the broker directly over TLS with no
VPN hop.

---

## 6. Tech stack (as built)

| Layer | Technology |
|-------|------------|
| Frontend | React 19, React Router 6, Vite 8, hand-written CSS (glassmorphism); Inter (`@fontsource`) + Leaflet CSS self-hosted/bundled — no style/font CDNs. Razorpay via CDN (the one allowed script origin). |
| Backend | Python 3.11 (Dockerfile), FastAPI, Uvicorn, SQLAlchemy 2.0 (async), Pydantic, paho-mqtt v2, python-jose, bcrypt (pyca, direct — passlib dropped 2026-07-21), razorpay, tapo. |
| Database | PostgreSQL 15 (Docker container on the GCP VM — Cloud SQL was decommissioned 2026-06-29). **No** TimescaleDB. |
| Messaging | Eclipse Mosquitto 2.0, TLS on `:8883` (`mqtt.amphive.app`), per-gateway username/password + topic ACLs. Plaintext `:1883` is not host-published. |
| Firmware | ESP-IDF (targets ESP32-C3), FreeRTOS, direct MQTT client (`AMPHIVE_DIRECT_MQTT`). |
| Infra | Docker / Docker Compose on a GCP Compute Engine VM (`asia-south1`), Caddy for TLS termination (`amphive.app` / `cpo.amphive.app`); K8s/K3s manifests also present but not the live deployment. |

**Retired:** Headscale control plane + the custom `microlink` Tailscale client
on the ESP32, and vendored `wireguard_lwip` — both superseded by the direct-MQTT
pivot (2026-07-10); no longer part of the live stack.
