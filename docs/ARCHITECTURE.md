# AmpHive — System Architecture

*Verified against source on 2026-06-20. For per-component status and the gap
between this and the product specs, see [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md).*

AmpHive turns off-the-shelf smart plugs (TP-Link Tapo P110) into a shared,
monetizable EV-charging network. A driver enters a **Plug ID** in a web app,
the backend authorizes and bills against a prepaid coin wallet, and a command
travels to the plug — either through an ESP32 gateway over an encrypted overlay
network, or (today, for dev/test) directly over a WireGuard tunnel.

---

## 1. The four planes

```
┌───────────────┐   REST/JSON    ┌─────────────────────┐   asyncpg    ┌──────────────┐
│  Driver Web   │ ◄────────────► │   FastAPI backend   │ ◄──────────► │  PostgreSQL  │
│  App (React)  │   + SSE live   │   (Uvicorn, main.py)│              │ (Cloud SQL)  │
└───────────────┘                └──────┬──────────────┘              └──────────────┘
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

AmpHive can drive a plug two ways. This is the single most important thing to
understand about the current codebase, because the docs historically describe
Path A as "done" while the committed configuration actually runs Path B.

### Path A — ESP32 gateway over MQTT (the product design)
1. The ESP32 joins the **Headscale/Tailscale overlay** via the `microlink`
   firmware component and gets a `100.64.x.x` VPN IP.
2. It connects to the MQTT broker at `mqtt://100.64.0.1:1883` (the server's
   overlay IP) and subscribes to its command topic.
3. The backend publishes `ON`/`OFF` commands; the ESP32 drives the local plug
   and publishes telemetry/status back.
4. See [MQTT_CONTRACT.md](MQTT_CONTRACT.md) for exact topics.

**Status:** the firmware control loop, overlay client, and MQTT contract are
implemented and the topic strings match the backend — **but** the ESP32's Tapo
driver is a mock (returns simulated telemetry), and the backend's inbound
MQTT handlers are stubs (they only log; they do not persist telemetry or feed
the live SSE stream). So end-to-end billing over Path A does not yet produce
real energy/cost figures. See [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md).

### Path B — Direct Mode over WireGuard (current dev/test reality)
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
GET /api/sessions/live/{id}          (SSE, text/event-stream)
  • streams telemetry snapshots from the in-memory TelemetryStore
  • NOTE: in Path A nothing currently writes live data into the store, so the
    stream emits the "starting"→"completed" placeholder snapshots only.
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
  Passwords hashed with bcrypt. All accounts are created as `driver`; there is
  no role enforcement in code yet (see [SECURITY.md](SECURITY.md)).
- **Wallet:** a single `users.coin_balance` float. Credited by Razorpay
  top-ups (`/api/payments/verify`), debited at session stop. Conversion is
  `COINS_PER_RUPEE` (default 1.0).
- **Telemetry/live data:** held in an **in-memory** `TelemetryStore` singleton
  (`backend/services/telemetry.py`) — there is no time-series database. The
  product spec's "TimescaleDB" is not present.

---

## 4. Frontend

React 19 + Vite SPA in `frontend/`. Served by Nginx, which also reverse-proxies
`/api/` to the backend (so the SPA and API are same-origin in production).

| Route | Page | Access | Purpose |
|-------|------|--------|---------|
| `/` | `Home.jsx` | public (content gated on login) | Wallet card + available chargers + "start by Plug ID" |
| `/login` | `Login.jsx` | public | Combined sign-in / register |
| `/session` | `Session.jsx` | protected | Live session monitor (`SessionMonitor` + SSE) |
| `/topup` | `TopUp.jsx` | protected | Razorpay checkout to buy coins |
| `/groups` | `Groups.jsx` | protected | Join private charger groups by access code |

State lives in three React contexts: `AuthContext` (JWT in `localStorage`,
`/api/auth/me` on load), `SessionContext` (opens a real `EventSource` to the SSE
endpoint), and `WalletContext` (derives balance from the user object). Razorpay
is loaded via a CDN `<script>` and used through `window.Razorpay`. There is **no
map** UI despite the "find nearest plug" product framing.

---

## 5. Networks at a glance

| Network | Range | Purpose | Used by |
|---------|-------|---------|---------|
| Headscale/Tailscale overlay | `100.64.0.0/10` | Secure mesh between server and ESP32 gateways | Path A (firmware `microlink`, K8s `headscale.yaml`) |
| WireGuard Direct-Mode tunnel | `10.10.0.0/24` | Cloud VM ↔ home PC for Direct Mode | Path B (`amphive_tunnel.conf`, `tapo_direct`) |
| Site IoT VLAN (VLAN 20) | site-defined | Physically isolate plugs + gateway from the resident network | Product design (CPO router config) |

These are three independent networks; the `100.64.x` overlay and the `10.10.0.x`
tunnel are unrelated despite both being "VPNs".

---

## 6. Tech stack (as built)

| Layer | Technology |
|-------|------------|
| Frontend | React 19, React Router 6, Vite 8, hand-written CSS (glassmorphism). Razorpay via CDN. |
| Backend | Python 3.11 (Dockerfile), FastAPI, Uvicorn, SQLAlchemy 2.0 (async), Pydantic, paho-mqtt v2, python-jose, passlib/bcrypt, razorpay, tapo. |
| Database | PostgreSQL 15 (Cloud SQL in prod). **No** TimescaleDB. |
| Messaging | Eclipse Mosquitto 2.0 (anonymous, no TLS — secured by the overlay). |
| Overlay VPN | Headscale control plane + the custom `microlink` Tailscale client on the ESP32. |
| Firmware | ESP-IDF (targets ESP32-S3-N16R8), FreeRTOS, `microlink`, vendored `wireguard_lwip`. |
| Infra | Docker / Docker Compose on a GCP Compute Engine VM (`asia-south1`); K8s/K3s manifests also present but not the live deployment. |
</content>
