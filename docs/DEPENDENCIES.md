# AmpHive — Dependency Graph

*Verified against source on 2026-07-26. Import graphs, package manifests,
high-impact files, and known dead code across the backend, frontend, and
firmware.*

---

## 1. Backend (`backend/`)

`main.py` is app assembly only (lifespan, CORS, health, router includes,
Socket.io wrap) — routes live in **9 routers** under `backend/routers/`
(one of which, `cpo`, is itself a sub-package of 12 domain modules plus a
shared `_common.py`), request/response schemas are centralized in
`backend/schemas.py`, and business logic lives in `backend/services/`
(25 top-level modules, plus a `mqtt/` sub-package that splits
`MQTTManager` into 7 mixin modules). The import graph is acyclic: `main.py`
→ `routers/*` → `services/*` → `database/*`.

```
main.py  (app assembly only since 2026-07-07: lifespan, CORS, health, router includes, Socket.io wrap)
│
├── routers/                    9 routers included in main.py
│   ├── auth.py                 register/login/me/password-reset
│   ├── groups.py                charger-group list/join (access codes)
│   ├── plugs.py                 driver-facing plug listing/detail
│   ├── sessions.py               start/stop/history + session limits
│   ├── payments.py               Razorpay create-order/verify/webhook
│   ├── notifications.py          driver notification feed + Web Push subscribe
│   ├── reservations.py           book-ahead charger reservations
│   ├── admin.py                  platform-admin console (tenants/users/payouts/audit)
│   └── cpo/                      operator-portal package (splice-mounted as one router)
│       ├── __init__.py           assembles + re-exports the sub-routers below
│       ├── _common.py            shared tenant-scope guard, cross-tenant tariff loader
│       ├── _gateways.py / _plugs.py / _groups.py   gateway/plug/group CRUD + roster
│       ├── _analytics.py         revenue/energy/telemetry dashboards (date_trunc)
│       ├── _tariffs.py           pricing v2 tariff + slot CRUD
│       ├── _payouts.py           settlement requests (see services/payouts.py)
│       ├── _topups.py            CPO-funded offline (cash) driver top-ups
│       ├── _invoices.py          GST invoice listing/download
│       ├── _disputes.py          session dispute triage
│       ├── _reservations.py      operator-side reservation management
│       ├── _events.py            gateway alarm/event feed (ack)
│       └── _profile.py           tenant profile + GST settings
│
├── schemas.py                  all Pydantic request/response models (cross-router)
├── state.py                    mutable runtime handles (mqtt_manager, telemetry_store, …) set by lifespan
├── logging_config.py           structured JSON logging + correlation ids
├── database/db.py              async engine + async_sessionmaker + get_db + init_db (alembic upgrade head)
│    └── database/models.py → Base
├── database/models.py          24 ORM tables + 9 enums  (source of truth, applied via Alembic)
│    └── sqlalchemy / sqlalchemy.orm, enum
├── database/reset_db.py        destructive dev-only reset (drop_all + create_all; confirmation-gated)
├── migrations/                 Alembic env.py + versions/ (0001_baseline .. 0026_offline_topups + later)
│
├── services/auth.py            JWT (python-jose, HS256, token_version epoch) + bcrypt (pyca, direct) + get_current_user
├── services/rbac.py            require_role(...) dependency factory → guards all /api/cpo/* and /api/admin/* routes
├── services/rate_limit.py      in-process sliding-window rate limiting for auth endpoints
├── services/mqtt_manager.py    MQTTManager — the one class every call site/test targets; composes the mixins below
├── services/mqtt/              MQTTManager's collaborators, split out of one god-object file:
│   ├── connection.py            paho client lifecycle (connect/disconnect/subscribe)
│   ├── router.py                inbound topic parsing/routing (telemetry/status/discovery/alarm regexes)
│   ├── commands.py               outbound publish: retained roster config, ON/OFF/OTA/SET_INTERVAL/SET_LIMITS
│   ├── telemetry.py               telemetry ingestion → TelemetryStore + DB persist + liveness throttle
│   ├── alarms.py                  inbound alarm/event handling (safety cutoffs, OTA notices) → GatewayEvent
│   ├── status.py                  online/offline transitions + reconnect reconciliation (orphan-OFF republish)
│   └── discovery.py               AmpHive Agent plug auto-discovery upsert
├── services/telemetry.py       in-memory TelemetryStore singleton + async stream() (live cost calc)
├── services/telemetry_persistence.py  buffered batch-flush of telemetry into TelemetryReading
├── services/session_start.py   shared begin-session helper (driver POST /start + reaper queued-charge auto-start)
├── services/session_lifecycle.py  gateway_is_live, finalize_charging_session (the one stop/billing path), telemetry-interval control
├── services/session_reaper.py  background sweep: stale-session reaping, duration/energy auto-stop, queued-charge auto-start
├── services/billing.py         segmented (time-of-day) cost math for Pricing v2 mid-session rate crossings
├── services/pricing.py         tariff resolution chain: plug → group → tenant default → global COINS_PER_KWH env fallback
├── services/wallet.py          the only code that mutates User.coin_balance; available_balance() for auth holds
├── services/money.py           Decimal-safe coin/rupee arithmetic (NUMERIC(12,2) columns)
├── services/caps.py            circuit admission control (Σ active-plug current caps ≤ group max_current_a)
├── services/capacity.py        "request capacity" notification fan-out when a circuit frees up
├── services/reservations.py    book-ahead reservation policy (free, no coin hold) + reaper hooks
├── services/plug_watch.py      "notify me when free" plug-availability watch fan-out
├── services/payments.py        Razorpay create-order / verify / webhook (HMAC, idempotent credit)
├── services/payouts.py         CPO settlement earnings math (manual, no bank/UPI integration)
├── services/invoices.py        India GST invoice issuance (combined-rate, intra-state only)
├── services/notifications.py   driver notification feed: DB row + Socket.io emit + Web Push fan-out
├── services/audit.py           CPO/admin action audit trail (AuditLog writer)
├── services/email.py           password-reset email: SMTP (STARTTLS) or console-log fallback
├── services/socketio_manager.py  Socket.io server, auth, session rooms, telemetry broadcasting
│
└── seed.py                     dev/test data seeder (tenants, CPOs, drivers, plugs, sessions)
```

### Python packages (`backend/requirements.txt`, pinned in `backend/requirements.lock.txt`)

| Package | Role |
|---------|------|
| `fastapi`, `uvicorn[standard]` | Web framework + ASGI server |
| `sqlalchemy>=2.0`, `asyncpg` | Async ORM + PostgreSQL driver |
| `alembic` | Schema migrations (`backend/migrations/`) |
| `pydantic`, `email-validator` | Request/response validation + `EmailStr` syntax checking |
| `paho-mqtt>=2.1` | MQTT client (backend ↔ gateway, direct `mqtts://` broker) |
| `python-jose[cryptography]` | JWT encode/decode (HS256) |
| `bcrypt>=4.1` | Password hashing — pyca/bcrypt called directly (passlib dropped 2026-07-21: abandoned upstream, was pinning bcrypt to 4.0.1) |
| `python-dotenv` | `.env` loading |
| `razorpay` | Payment gateway SDK (top-ups) |
| `python-socketio`, `python-engineio` | Socket.io server + engine for real-time WebSockets |
| `pywebpush` | Web Push delivery for driver notifications (VAPID) |
| `tapo` (`<1.0`) | Rust-backed TP-Link Tapo control — standalone tools bench scripts only (`tools/turn_on.py`, `turn_off.py`, `local_tapo_test.py`), not imported by the backend runtime |

Dev/CI-only (`backend/requirements-dev.txt`): `pytest`, `pytest-asyncio`,
`pytest-cov`, `ruff`, `mypy` — see [TESTING.md](TESTING.md).

---

## 2. Frontend (`frontend/`)

React 19 + Vite SPA, plain `.jsx`/`.js` (no TypeScript, by decision — TD#14).
**Three route trees share one build**: the driver app, the CPO operator
portal, and a platform-admin console, partitioned onto two hostnames
(`amphive.app` = driver, `cpo.amphive.app` = operator + admin) by
`utils/appHost.js` + `components/HostRouting.jsx` — a route that belongs to
the other host renders `ExternalRedirect` (hard-navigates across origins)
instead of 404ing. Provider nesting is fixed in `main.jsx`; `App.jsx` owns
routing only.

```
main.jsx  (ReactDOM.createRoot, StrictMode)
├── styles/{tokens,base,primitives,layouts}.css   design system v3 (self-hosted fonts: Inter/Bricolage Grotesque/JetBrains Mono)
├── components/ErrorBoundary.jsx
└── ToastProvider → ConfigProvider → AuthProvider → WalletProvider → SessionProvider → App.jsx
    ├── contexts/ConfigContext.jsx     GET /api/config once (coins_per_kwh, min balance) → api/client.js
    ├── contexts/AuthContext.jsx       JWT in localStorage, /api/auth/me on load  → api/client.js
    ├── contexts/WalletContext.jsx     balance/available_balance derived from the user object → AuthContext
    └── contexts/SessionContext.jsx    Socket.io-client connection & multi-session subscription → api/client.js

App.jsx  (BrowserRouter; host-partitioned route trees via utils/appHost.js)
├── components/HostRouting.jsx        ExternalRedirect (cross-host hard nav), CpoLanding ("/" on the cpo host)
├── components/ProtectedRoutes.jsx    ProtectedRoute / CpoProtectedRoute / AdminProtectedRoute
│
├── Driver routes  (AppBar/MobileTabBar/BootSplash shell)
│   ├── pages/MapPage.jsx      → components/MapComponent.jsx (react-leaflet), components/PlugCard.jsx
│   ├── pages/Login.jsx, Signup.jsx, ForgotPassword.jsx, ResetPassword.jsx
│   ├── pages/Terms.jsx        public /terms page (Razorpay-compliance copy, added PR #79)
│   ├── pages/Session.jsx      → components/SessionMonitor.jsx, ChargeRing.jsx, ChargeSetupModal.jsx
│   ├── pages/Wallet.jsx       served at /credit ("charging credit" reframe; /wallet + /topup redirect here)
│   ├── pages/Activity.jsx     session history → components/SessionReceipt.jsx, DisputeModal.jsx
│   ├── pages/Groups.jsx, pages/Account.jsx
│   └── pages/Dashboard.jsx    driver home
│
├── CPO routes  (CpoProtectedRoute → requires 'cpo' role; components/CpoLayout.jsx shell)
│   ├── contexts/TenantContext.jsx   mounted by CpoLayout: shared /api/cpo/profile + badge counts (polled)
│   └── pages/cpo/{CpoSetup,CpoDashboard,CpoGateways,CpoChargers,CpoGroups,CpoSessions,
│                  CpoReservations,CpoHealth,CpoEarnings,CpoPricing,CpoInvoices,
│                  CpoDisputes,CpoSettings}.jsx
│       → recharts (analytics), qrcode.react (per-plug QR)
│
└── Admin routes  (AdminProtectedRoute → requires 'admin' role; components/AdminLayout.jsx shell)
    └── pages/admin/{AdminOverview,AdminTenants,AdminTenantDetail,AdminUsers,
                     AdminPayouts,AdminGateways,AdminDisputes,AdminAudit}.jsx

Shared: components/ui/  (DataTable, Modal, Toast, Tabs, ConfirmDialog, Money, Skeleton, EmptyState/ErrorState, StatusDot, PageHeader)
        utils/  (appHost, money, plugAvailability, razorpay, reservationTime, safePath, statusCopy)
```

### NPM packages (`frontend/package.json`)

| Package | Role |
|---------|------|
| `react` / `react-dom` ^19 | UI framework |
| `react-router-dom` ^6 | Client-side routing |
| `socket.io-client` | Real-time telemetry/notification stream → `SessionContext` |
| `leaflet` / `react-leaflet` | OpenStreetMap map on `MapPage` (`MapComponent`), color-coded markers via `utils/plugAvailability.js` |
| `@fontsource/inter`, `@fontsource/bricolage-grotesque`, `@fontsource/jetbrains-mono` | Self-hosted fonts (UI text / display headings / money-telemetry mono) — no Google Fonts CDN, so the CSP carries no font origin |
| `lucide-react` | Icon set used across the v3 redesign |
| `qrcode.react` | Per-plug QR code (CPO Chargers page → deep-links to `/?plug=<id>`) |
| `recharts` | CPO analytics charts |
| `vite` ^8, `@vitejs/plugin-react` | Build tool / dev server |
| `eslint` ^10 (+ react-hooks / react-refresh plugins) | Linting (`npm run lint`) |
| `vitest` ^4, `@testing-library/react` / `jest-dom` / `user-event`, `jsdom` | Component tests (`npm test`) — see [TESTING.md](TESTING.md) |

### External CDN / runtime

- Razorpay Checkout SDK — lazy-loaded on demand by `utils/razorpay.js`
  (`loadRazorpay()`) right before the Wallet/`/credit` page opens checkout,
  not a blocking `<script>` in `index.html`.
- OpenStreetMap tiles — `tile.openstreetmap.org` (via Leaflet).
- No Google Fonts / other font CDN — retired in favor of the self-hosted
  `@fontsource/*` packages above (tighter CSP).

---

## 3. Firmware (`firmware/`, ESP-IDF)

Target hardware is the **ESP32-C3** (~4 MB flash, no PSRAM — see
`sdkconfig.defaults`'s `CONFIG_ESPTOOLPY_FLASHSIZE_4MB`), **not** an ESP32-S3.
Current shipped version is `2.3.0-direct` (`firmware/CMakeLists.txt`
`PROJECT_VER`). Transport is direct MQTT over TLS to the public broker
(`mqtts://`, per-gateway creds + ACLs) — there is no VPN overlay in the
active code path.

```
main/main.c  (app_main)
├── FreeRTOS (task.h, event_groups.h)
├── ESP-IDF (esp_wifi, esp_event, esp_netif, esp_http_server, nvs_flash, mqtt_client, esp_http_client, mbedtls,
│            esp_https_ota, app_update, esp_app_format — signed OTA)
├── #define AMPHIVE_DIRECT_MQTT 1   the only build mode shipped to the field: TLS MQTT straight to the broker
├── components/json/                vendored cJSON (ESP-IDF v6 removed it from core; kept local instead of
│                                    fetching it from the component registry)
├── main/tapo_protocol.c            Real KLAP v2 Tapo P110 driver (mbedTLS SHA/AES + esp_http_client)
├── main/session_nvs.c              NVS active session persistence for crash recovery
├── main/offline_log.c              NVS telemetry ring buffer for offline resync
└── main/ota_update.c               Signed OTA-over-MQTT apply path (esp_https_ota + app_update)

main/CMakeLists.txt → SRCS main.c tapo_protocol.c session_nvs.c offline_log.c ota_update.c
                      REQUIRES mqtt nvs_flash esp_wifi esp_http_server esp_http_client
                               mbedtls json esp_https_ota app_update esp_app_format
main/idf_component.yml → espressif/mqtt ^1.0.0 (ESP-IDF v6 moved esp-mqtt out of core into the registry)
```

---

## 4. High-impact files (modify with care)

| File | Why it's critical |
|------|-------------------|
| `backend/main.py` | App assembly only (lifespan, CORS, health, router includes, Socket.io wrap); routes live in `backend/routers/`, schemas in `backend/schemas.py` |
| `backend/database/models.py` | All ORM models (24 tables, 9 enums) — changes alter the runtime DB schema |
| `backend/services/auth.py` | JWT secret, password hashing, auth dependency for most routes |
| `backend/services/rbac.py` | Role enforcement for the entire CPO + admin surface |
| `backend/services/mqtt_manager.py` + `services/mqtt/*` | The one gateway↔backend transport; a mixin regression breaks telemetry/commands for every fielded gateway |
| `backend/services/wallet.py` | The only code allowed to mutate `User.coin_balance` |
| `backend/database/db.py` | Connection pool, `init_db` |
| `.env` | All secrets and configuration |
| `frontend/src/api/client.js` | Every API call + JWT handling routes through here |
| `frontend/src/utils/appHost.js` + `components/HostRouting.jsx` | Decides which of the three route trees (driver/CPO/admin) renders per hostname |
| `frontend/src/contexts/AuthContext.jsx` | Auth state consumed by every page/context |
| `frontend/src/styles/tokens.css` | Design system v3 tokens (colors/spacing/type) consumed by every page |
| `firmware/main/main.c` | Physical safety watchdogs (duration/energy/thermal) + the MQTT transport mode switch (`AMPHIVE_DIRECT_MQTT`) |
| `deploy/scripts/deploy.ps1` | Production deploy pipeline |
| `frontend/nginx.conf` | `/api/` + `/socket.io/` proxy and SPA fallback |

---

## 5. Dead / redundant code

| File | Status | Recommendation |
|------|--------|----------------|
| ~~`firmware/components/microlink/`, `firmware/components/wireguard_lwip/`~~ | **Removed 2026-08-02** — legacy VPN-overlay transport, dead since the 2026-07-10 direct-MQTT pivot; the linker map showed zero objects pulled from either archive | — |
| `frontend/README.md` | Stock Vite template, not project docs | Replace with project-specific notes |
| ~~`backend/database/init_db.py`~~ | **Renamed 2026-07-09** to `reset_db.py` (TD#8) | — |
| ~~`backend/database/schema.sql` / `schema_v2.sql`~~ | **Deleted 2026-07-07** — replaced by Alembic (`backend/migrations/`) | — |

> `frontend/src/api/mockSse.js` (a former Phase-1 leftover) has already been
> **deleted**; it is listed here only so older references stop looking for it.

---

## 6. No circular dependencies

The graph is acyclic in every layer:
- **Backend:** `main.py` → `routers/*` → `services/*` → `database/*` (one direction). Within `services/`, `mqtt_manager.py` composes the `services/mqtt/*` mixins but nothing in `mqtt/*` imports back out to `mqtt_manager.py`.
- **Frontend:** `App` → `pages/*` → `components/*` → `contexts/*` → `api/client`.
  `WalletContext` depends on `AuthContext` but not vice-versa; `TenantContext`
  (CPO-only) depends on `api/client` directly and is mounted by `CpoLayout`,
  not the app-wide provider stack in `main.jsx`.
