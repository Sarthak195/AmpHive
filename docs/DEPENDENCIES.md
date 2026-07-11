# AmpHive — Dependency Graph

*Verified against source on 2026-07-02. Import graphs, package manifests,
high-impact files, and known dead code across the backend, frontend, and
firmware.*

---

## 1. Backend (`backend/`)

All routes and Pydantic schemas live in a single `main.py`; everything else is a
thin service or the data layer. The import graph is acyclic
(`main.py` → `services/*` → `database/*`).

```
main.py  (app assembly only since 2026-07-07: lifespan, CORS, health, router includes, Socket.io wrap)
│
├── routers/               7 route modules (auth, groups, plugs, sessions, payments, direct, cpo)
├── schemas.py             all Pydantic request/response models
├── state.py               mutable runtime handles (mqtt_manager, telemetry_store, …) set by lifespan
├── database/db.py         async engine + async_sessionmaker + get_db + init_db (alembic upgrade head)
│    └── database/models.py → Base
├── database/models.py     9 ORM tables + 5 enums  (source of truth, applied via Alembic)
│    └── sqlalchemy / sqlalchemy.orm, enum
├── database/reset_db.py   destructive dev-only reset (drop_all + create_all; confirmation-gated)
├── migrations/            Alembic env.py + versions/ (0001_baseline = frozen full schema)
│
├── services/auth.py       JWT (python-jose, HS256, 7-day) + bcrypt (passlib) + get_current_user
├── services/rbac.py       require_role(...) dependency factory → guards all /api/cpo/* routes
├── services/mqtt_manager.py  paho-mqtt bridge: publishes ON/OFF; ingests telemetry
│                             (updates TelemetryStore + persists energy/peak_power) & status
├── services/telemetry.py  in-memory TelemetryStore singleton + async stream() (COINS pricing)
├── services/socketio_manager.py Socket.io server, auth, session rooms, and telemetry broadcasting
├── services/payments.py   razorpay create-order / verify / webhook (HMAC, idempotent credit)
├── services/tapo_direct.py  Direct-Mode Tapo driver (tapo lib or HTTP relay via TAPO_RELAY_URL)
│
└── seed.py                dev/test data seeder (tenants, CPOs, drivers, plugs, sessions)
```

### Python packages (`backend/requirements.txt`)

| Package | Role |
|---------|------|
| `fastapi`, `uvicorn` | Web framework + ASGI server |
| `sqlalchemy>=2.0`, `asyncpg` | Async ORM + PostgreSQL driver |
| `pydantic` | Request/response validation |
| `paho-mqtt>=2.1` | MQTT client (backend ↔ gateway) |
| `python-jose[cryptography]` | JWT encode/decode |
| `passlib[bcrypt]`, `bcrypt==4.0.1` | Password hashing |
| `python-dotenv` | `.env` loading |
| `razorpay` | Payment gateway SDK |
| `python-socketio`, `python-engineio` | Socket.io server and engine for real-time WebSockets |
| `tapo` | Rust-backed TP-Link Tapo control (Direct Mode) |

---

## 2. Frontend (`frontend/`)

React 19 + Vite SPA. Two route trees: the driver app and the CPO operator portal.
Entry chain `main.jsx` → context providers → `App.jsx` (router).

```
main.jsx  (ReactDOM.createRoot, StrictMode)
├── styles/global.css                  design system (glassmorphic dark theme)
├── contexts/AuthContext.jsx           JWT in localStorage, /api/auth/me on load  → api/client.js
├── contexts/WalletContext.jsx         derives balance from user                  → AuthContext
├── contexts/SessionContext.jsx        Socket.io-client connection & subscription → api/client.js
└── App.jsx  (BrowserRouter, ProtectedRoute + CpoProtectedRoute)
    ├── components/Navbar.jsx
    │
    ├── Driver routes
    │   ├── pages/Home.jsx        → components/WalletCard.jsx, components/MapComponent.jsx (react-leaflet)
    │   ├── pages/Login.jsx
    │   ├── pages/Session.jsx     → components/SessionMonitor.jsx (SessionContext)
    │   ├── pages/TopUp.jsx       → WalletContext, Razorpay CDN checkout
    │   ├── pages/Groups.jsx
    │   └── pages/History.jsx
    │
    └── CPO routes  (CpoProtectedRoute → requires 'cpo' role)
        └── pages/cpo/{CpoSetup,CpoDashboard,CpoPlugs,CpoGroups,CpoSessions}.jsx
            → components/CpoLayout.jsx, recharts (analytics charts)
```

### NPM packages (`frontend/package.json`)

| Package | Role |
|---------|------|
| `react` / `react-dom` ^19 | UI framework |
| `react-router-dom` ^6 | Client-side routing |
| `leaflet` / `react-leaflet` | OpenStreetMap map on Home (`MapComponent`) |
| `recharts` | CPO analytics charts |
| `vite` ^8, `@vitejs/plugin-react` | Build tool / dev server |
| `eslint` (+ react-hooks / react-refresh plugins) | Linting |
| `typescript`, `@types/*` | Toolchain present, but **app code is `.jsx`/`.js`** |

### External CDN / runtime

- Razorpay Checkout SDK — `<script src="https://checkout.razorpay.com/v1/checkout.js">` in `index.html`.
- Google Fonts (`Inter`) — `@import` in `global.css`.
- OpenStreetMap tiles — `tile.openstreetmap.org` (via Leaflet).

---

## 3. Firmware (`firmware/`, ESP-IDF)

```
main/main.c  (app_main)
├── FreeRTOS (task.h, event_groups.h)
├── ESP-IDF (esp_wifi, esp_event, esp_netif, esp_http_server, nvs_flash, mqtt_client, esp_http_client, mbedtls)
├── components/microlink/         from-scratch Tailscale client (Noise/ts2021, DERP, DISCO, STUN, WG)
├── components/wireguard_lwip/     vendored WireGuard-over-lwIP
├── main/tapo_protocol.c           Real KLAP v2 Tapo P110 driver (mbedTLS SHA/AES + esp_http_client)
├── main/session_nvs.c             NVS active session persistence for crash recovery
└── main/offline_log.c             NVS telemetry ring buffer for offline resync

main/CMakeLists.txt → SRCS main.c tapo_protocol.c session_nvs.c offline_log.c
                      REQUIRES esp_wifi mqtt esp_http_server nvs_flash microlink esp_http_client mbedtls
```

---

## 4. High-impact files (modify with care)

| File | Why it's critical |
|------|-------------------|
| `backend/main.py` | App assembly only (lifespan, CORS, health, router includes, Socket.io wrap); routes live in `backend/routers/`, schemas in `backend/schemas.py` |
| `backend/database/models.py` | All ORM models — changes alter the runtime DB schema |
| `backend/services/auth.py` | JWT secret, password hashing, auth dependency for most routes |
| `backend/services/rbac.py` | Role enforcement for the entire CPO surface |
| `backend/database/db.py` | Connection pool, `init_db` |
| `.env` | All secrets and configuration |
| `frontend/src/api/client.js` | Every API call + JWT handling routes through here |
| `frontend/src/contexts/AuthContext.jsx` | Auth state consumed by every page/context |
| `frontend/src/styles/global.css` | Entire design system |
| `firmware/main/main.c` | Physical safety watchdogs (duration/energy/thermal) |
| `deploy/scripts/deploy.ps1` | Production deploy pipeline |
| `frontend/nginx.conf` | `/api/` proxy + SPA fallback |

---

## 5. Dead / redundant code

| File | Status | Recommendation |
|------|--------|----------------|
| ~~`backend/database/init_db.py`~~ | **Renamed 2026-07-09** to `reset_db.py` (TD#8) — the destructive drop-all reset no longer shares a name with `db.py:init_db()` | — |
| ~~`backend/database/schema.sql` / `schema_v2.sql`~~ | **Deleted 2026-07-07** — replaced by Alembic (`backend/migrations/`, see [DATA_MODEL.md §4](DATA_MODEL.md#4-migrations-alembic-since-2026-07-07)) | — |
| `frontend/README.md` | Stock Vite template, not project docs | Replace with project-specific notes |

> `frontend/src/api/mockSse.js` (a former Phase-1 leftover) has already been
> **deleted**; it is listed here only so older references stop looking for it.

---

## 6. No circular dependencies

The graph is acyclic in every layer:
- **Backend:** `main.py` → `services/*` → `database/*` (one direction).
- **Frontend:** `App` → `pages/*` → `components/*` → `contexts/*` → `api/client`.
  `WalletContext` depends on `AuthContext` but not vice-versa.
