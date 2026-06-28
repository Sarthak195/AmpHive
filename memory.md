# AmpHive — Codebase Memory (Complete Project Intelligence)

> **Generated:** 2026-06-20 · **Source of truth:** Verified against all source files in the repository.
> This document is the permanent brain of the project. A new engineer reading this
> should be able to understand what AmpHive does, why it exists, how every system
> works, and where every file lives — without reading a single line of code first.

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Business Purpose](#2-business-purpose)
3. [Tech Stack](#3-tech-stack)
4. [Repository Structure](#4-repository-structure)
5. [System Architecture](#5-system-architecture)
6. [Routing Map](#6-routing-map)
7. [Frontend Architecture](#7-frontend-architecture)
8. [Backend Architecture](#8-backend-architecture)
9. [Database Architecture](#9-database-architecture)
10. [Authentication Flow](#10-authentication-flow)
11. [API Inventory](#11-api-inventory)
12. [Data Flow Diagrams](#12-data-flow-diagrams)
13. [Environment Variables](#13-environment-variables)
14. [Third-Party Integrations](#14-third-party-integrations)
15. [Feature Inventory](#15-feature-inventory)
16. [Dependency Graph](#16-dependency-graph)
17. [Important Files](#17-important-files)
18. [Performance Notes](#18-performance-notes)
19. [Technical Debt](#19-technical-debt)
20. [Development Workflow](#20-development-workflow)
21. [Deployment Process](#21-deployment-process)
22. [Known Risks](#22-known-risks)
23. [Future Recommendations](#23-future-recommendations)

---

## 1. Project Overview

**AmpHive** is an enterprise-grade **SaaS / PaaS platform** that transforms budget, off-the-shelf TP-Link Tapo P110 smart plugs into a secure, monetizable, shared EV (Electric Vehicle) charging network.

The system has **four pillars**:

| Pillar | Role |
|--------|------|
| **FastAPI Backend** | Central REST API: auth, wallets, sessions, MQTT routing, payments |
| **React + Vite Frontend** | Driver-facing SPA: plug-ID entry, live session monitor, wallet top-up |
| **ESP32-S3 Edge Gateway** (firmware) | Microcontroller at the charging site: receives MQTT commands, controls physical smart plugs over local VLAN, runs safety watchdogs |
| **Headscale/WireGuard VPN Overlay** | Encrypted WireGuard tunnels between the cloud server and edge gateways, bypassing NAT/firewalls |

### Two Operating Modes

1. **Path A — ESP32 + MQTT (production target):** Backend → MQTT Broker → WireGuard Tunnel → ESP32 → Smart Plug. This path is *wired but not fully functional* — the firmware Tapo driver is mocked, and backend inbound telemetry handlers are stubs.

2. **Path B — Direct Mode (current working path):** Backend → WireGuard Tunnel → Developer PC → Home LAN → Smart Plug. This is the path that actually controls a physical plug today, enabled via `DIRECT_MODE=true`. It does **not** feed the session/telemetry pipeline.

---

## 2. Business Purpose

### Problem Solved
In India and emerging markets, installing commercial EV chargers (ChargePoint, ABB, etc.) costs ₹1–5 lakhs per unit. Residential and small commercial properties can't justify this. Meanwhile, a ₹1,500 TP-Link Tapo P110 smart plug can deliver 3.5 kW (sufficient for overnight Level 1/slow charging of most EVs and 2-wheelers). AmpHive turns these cheap plugs into a metered, billed, access-controlled charging network.

### Target Users
| Role | Description |
|------|-------------|
| **Driver** | EV owner who arrives at a charging location, enters a Plug ID, pays from a prepaid wallet, and charges |
| **CPO** (Charge Point Operator) | Property owner/manager who deploys plugs, creates charger groups (public or access-code-gated private), and earns revenue |
| **Admin** | Platform operator (not yet implemented in code) |

### Major User Workflow
1. Driver registers/logs in on the web app
2. Tops up prepaid wallet via Razorpay (UPI, cards, wallets, net banking)
3. Arrives at a charging location, enters the Plug ID printed on the outlet
4. System verifies wallet balance (≥₹50), group access, plug availability
5. Backend sends ON command via MQTT to the ESP32 gateway
6. Live session telemetry streams to the driver's phone via SSE
7. Driver stops charging → system calculates kWh consumed, debits wallet, creates ledger entry

### Primary Entities
Tenant (CPO) → Gateway → Plug → ChargerGroup → User → ChargingSession → LedgerTransaction

---

## 3. Tech Stack

| Layer | Technology | Version / Details |
|-------|------------|-------------------|
| **Frontend Framework** | React | 19.2.6 |
| **Build Tool** | Vite | 8.0.12 |
| **Routing** | React Router | 6.22.3 (BrowserRouter, client-side) |
| **Styling** | Vanilla CSS | Glassmorphic dark theme, Inter font, CSS custom properties |
| **Charts** | Recharts | React-native SVG charting library (~45kB gzipped) |
| **State Management** | React Context API | 3 providers: AuthContext, SessionContext, WalletContext |
| **Backend Framework** | FastAPI | ≥0.100 (Python 3.11) |
| **ASGI Server** | Uvicorn | ≥0.22 |
| **ORM** | SQLAlchemy 2.0 (async) | `DeclarativeBase`, `mapped_column`, asyncpg driver |
| **Database** | PostgreSQL 15 | Cloud SQL (prod), Docker (dev) |
| **Authentication** | JWT (HS256) + bcrypt | `python-jose` + `passlib` |
| **Payments** | Razorpay | India-specific; UPI, cards, wallets, net banking |
| **Real-time** | SSE (Server-Sent Events) | `sse-starlette` library |
| **Messaging** | MQTT (Eclipse Mosquitto 2.0) | `paho-mqtt` (backend), ESP-IDF mqtt_client (firmware) |
| **VPN Overlay** | Headscale + WireGuard | MicroLink Tailscale-compatible client on ESP32 |
| **Firmware** | ESP-IDF v5.x (C) | FreeRTOS, ESP32-S3 target |
| **Containers** | Docker + Docker Compose | Multi-stage builds, Nginx for frontend serving |
| **Cloud** | Google Cloud Platform | Compute Engine (e2-highcpu-4), Cloud SQL, asia-south1 |
| **Orchestration** | Kubernetes (K3s) | Manifests available but not the live deployment |
| **API Client** | Native `fetch` | No Axios — custom wrapper with JWT auto-attach |

### What is NOT used (despite TS toolchain being present)
- **TypeScript**: Config files (`tsconfig.json`, `tsconfig.app.json`, `tsconfig.node.json`) are present but ALL application code is `.jsx`/`.js`. TypeScript is configured but not actively used.
- **Redux / Zustand**: Not used — state management is purely React Context.
- **Tailwind CSS**: Not used — all styling is vanilla CSS.
- **Redis**: Not used — telemetry is in-memory dict (noted as a scaling concern).

---

## 4. Repository Structure

```
AmpHive/
├── .env                          # Root environment variables (gitignored in practice, but committed — SECURITY RISK)
├── .gitmodules                   # 3 submodules: ChargeHub, headscale, ESP32-Tailscale-WoL
├── .gitignore                    # Standard ignores: .env, node_modules, .venv, dist, etc.
├── docker-compose.yml            # Local dev shortcut (mirrors deploy/docker/docker-compose.dev.yml)
├── README.md                     # Main project README (product overview + architecture)
├── agent.md                      # AI-agent context, file map & progress log
├── features_list.md              # Detailed features roadmap & specifications
├── requirements.md               # Product Requirements Document (PRD) & Design Spec
├── eim_config.toml               # External tool config
│
├── backend/                      # ═══ FastAPI Backend Server ═══
│   ├── main.py                   # ★ FastAPI app, lifespan, ALL 36 REST endpoints (~1980 lines)
│   ├── Dockerfile                # python:3.11-slim → pip install → uvicorn
│   ├── requirements.txt          # 11 Python dependencies
│   ├── database/
│   │   ├── db.py                 # Async engine, session factory, get_db dependency, init_db
│   │   ├── init_db.py            # Standalone DB-init helper
│   │   ├── models.py             # ★ All 8 SQLAlchemy ORM models + 5 enums (source of truth)
│   │   ├── schema.sql            # Reference SQL v1 (NOT executed by app)
│   │   └── schema_v2.sql         # Migration delta: charger_groups + memberships
│   └── services/
│       ├── auth.py               # JWT (HS256) + bcrypt + get_current_user FastAPI dependency
│       ├── rbac.py               # ★ Role-based access control: require_role() dependency factory
│       ├── mqtt_manager.py       # paho-mqtt singleton: publish commands, stub inbound handlers
│       ├── payments.py           # Razorpay: create order, verify HMAC signature, webhook
│       ├── tapo_direct.py        # Direct Mode Tapo P110 driver (lib or HTTP relay)
│       └── telemetry.py          # In-memory TelemetryStore + async SSE generator
│
├── frontend/                     # ═══ React + Vite Driver & CPO Web App ═══
│   ├── index.html                # Vite HTML entry (includes Razorpay CDN script)
│   ├── package.json              # React 19, Router 6, Vite 8, Recharts
│   ├── vite.config.ts            # Vite config (React plugin)
│   ├── Dockerfile                # Multi-stage: Node build → Nginx serve
│   ├── nginx.conf                # SPA fallback + /api/ proxy → backend:8000
│   ├── .env                      # Frontend env (VITE_API_URL, VITE_RAZORPAY_KEY_ID)
│   └── src/
│       ├── main.jsx              # React DOM mount + Context providers (Auth → Wallet → Session)
│       ├── App.jsx               # BrowserRouter, 10 routes + ProtectedRoute + CpoProtectedRoute
│       ├── api/
│       │   ├── client.js         # ★ Fetch wrapper: JWT auto-attach, 401 redirect, convenience methods
│       │   └── mockSse.js        # DEAD CODE — Phase 1 leftover, never imported
│       ├── contexts/
│       │   ├── AuthContext.jsx   # Login/register/logout/refreshUser, JWT in localStorage
│       │   ├── SessionContext.jsx # Start/stop session, SSE EventSource telemetry stream
│       │   └── WalletContext.jsx # Balance derived from AuthContext user, refreshBalance
│       ├── components/
│       │   ├── Navbar.jsx        # Glassmorphic sticky navbar: logo, links, CPO Portal, balance
│       │   ├── CpoLayout.jsx    # ★ CPO sidebar + content wrapper with purple accent branding
│       │   ├── SessionMonitor.jsx # Real-time stat grid: power, energy, current, cost, timer
│       │   └── WalletCard.jsx    # Balance display with decorative glow blobs, top-up button
│       ├── pages/
│       │   ├── Home.jsx          # Dashboard: wallet card, plug ID entry, available chargers list
│       │   ├── Login.jsx         # Combined login/register form with mode toggle
│       │   ├── Session.jsx       # Wrapper page for SessionMonitor, redirect if no active session
│       │   ├── TopUp.jsx         # ₹50/100/200/500 grid, Razorpay checkout flow
│       │   ├── Groups.jsx        # Join private group by access code, list public+joined groups
│       │   └── cpo/             # ═══ CPO Admin Dashboard Pages ═══
│       │       ├── CpoSetup.jsx     # Onboarding: create tenant, promote to CPO role
│       │       ├── CpoDashboard.jsx # ★ Analytics overview: stat cards, Recharts, session table
│       │       ├── CpoPlugs.jsx     # Plug CRUD: table, filters, add/edit modals
│       │       ├── CpoGroups.jsx    # Group CRUD: cards, access codes, delete confirmation
│       │       └── CpoSessions.jsx  # Session history: filters, summary stats, full table
│       └── styles/
│           └── global.css        # ★ Design system + CPO sidebar/tables/modals/stat cards
│
├── firmware/                     # ═══ ESP32-S3 Gateway Firmware (ESP-IDF v5.x) ═══
│   ├── CMakeLists.txt            # Top-level CMake project
│   ├── sdkconfig.defaults        # FreeRTOS + PSRAM config
│   ├── components/               # External submodules (MicroLink Tailscale client)
│   └── main/
│       ├── CMakeLists.txt        # Component sources
│       ├── main.c                # ★ Entry point: WiFi → Captive Portal → Tapo → Telemetry → VPN
│       ├── tapo_protocol.c       # Tapo P110 driver (MOCK — simulated telemetry)
│       └── tapo_protocol.h       # Telemetry struct + function declarations
│
├── deploy/                       # ═══ Deployment Configs & Scripts ═══
│   ├── scripts/
│   │   ├── deploy.ps1            # ★ Main GCP VM deploy: wait SQL → create DB → gen .env → SCP → compose up
│   │   └── startup.sh            # One-time VM bootstrap (Docker install)
│   ├── docker/
│   │   ├── docker-compose.prod.yml   # Production compose (backend + frontend + MQTT)
│   │   └── docker-compose.dev.yml    # Local dev compose (includes local Postgres)
│   ├── config/
│   │   ├── mosquitto.conf            # MQTT broker config (anonymous, no TLS)
│   │   └── .env.template             # Environment variable template
│   ├── docs/                         # Deployment guides & runbooks (6 files)
│   └── k8s/                          # K3s manifests (namespace, backend, frontend, mqtt, headscale, postgres)
│
├── tools/                        # ═══ Direct-Mode Helpers (run on developer PC) ═══
│   ├── relay_server.py           # HTTP relay for backend Tapo calls
│   ├── local_tapo_test.py        # Tapo connection self-test
│   ├── turn_on.py                # Manual plug ON
│   └── turn_off.py               # Manual plug OFF
│
├── docs/                         # ═══ Technical Reference Documentation ═══
│   ├── ARCHITECTURE.md           # System architecture & operating modes
│   ├── API_REFERENCE.md          # All 22 REST endpoints (detailed)
│   ├── DATA_MODEL.md             # DB tables, models, enums, schema drift
│   ├── MQTT_CONTRACT.md          # Backend↔gateway MQTT topic/payload contract
│   ├── FIRMWARE.md               # ESP32 firmware + MicroLink Tailscale client
│   ├── DEPLOYMENT.md             # Compose, deploy scripts, K8s
│   ├── IMPLEMENTATION_STATUS.md  # What works / stub / aspirational + 25 discrepancies
│   ├── SECURITY.md               # Committed secrets, open broker, auth gaps
│   └── reference/                # Additional reference docs
│
├── context_repos/                # ═══ Git Submodules (reference projects) ═══
│   ├── ChargeHub/                # Open-source EV charging protocol reference
│   ├── headscale/                # Self-hosted Tailscale control server
│   └── ESP32-Tailscale-WoL/     # ESP32 Tailscale WireGuard client reference
│
├── *.bat                         # Windows helper scripts (start-vm, stop-vm, logs, restart)
└── setup_duckdns.sh              # DuckDNS dynamic DNS updater
```

---

## 5. System Architecture

### High-Level Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        GOOGLE CLOUD PLATFORM                           │
│                        (asia-south1 / Mumbai)                          │
│                                                                         │
│  ┌───────────────────┐     ┌───────────────────┐    ┌──────────────┐  │
│  │  Frontend (Nginx)  │     │  FastAPI Backend   │    │  Cloud SQL   │  │
│  │  React SPA         │────►│  Uvicorn           │───►│  PostgreSQL  │  │
│  │  Port :80          │ API │  Port :8000        │SQL │  15          │  │
│  └─────────┬─────────┘     └────────┬───────────┘    └──────────────┘  │
│            │                        │                                    │
│            │ nginx proxy            │ MQTT                               │
│            │ /api/ → :8000          │                                    │
│            │                   ┌────▼──────────┐                         │
│            │                   │  Mosquitto    │                         │
│            │                   │  MQTT Broker  │                         │
│            │                   │  Port :1883   │                         │
│            │                   └────┬──────────┘                         │
│  ┌─────────────────┐               │                                    │
│  │  Headscale       │               │ WireGuard VPN Tunnel               │
│  │  Control Server  │               │                                    │
│  └─────────────────┘               │                                    │
└─────────────────────────────────────┼────────────────────────────────────┘
                                      │
                         ┌────────────▼────────────────┐
                         │  ESP32-S3 Edge Gateway       │
                         │  - MicroLink/Tailscale VPN   │
                         │  - MQTT Client               │
                         │  - Tapo P110 Driver          │
                         │  - Safety Watchdogs          │
                         └────────────┬────────────────┘
                                      │ VLAN 20 / Home LAN
                         ┌────────────▼────────────────┐
                         │  TP-Link Tapo P110          │
                         │  Smart Plug (3.5 kW max)    │
                         └─────────────────────────────┘
```

### Direct Mode Architecture (Currently Active)

```
Cloud Backend (GCP VM)
    → WireGuard Tunnel (UDP 51820)
        → Developer's PC (WireGuard client + relay_server.py on :8000)
            → Home LAN
                → Tapo P110 Smart Plug (HTTP port 80)
```

### Container Topology (Docker Compose)

```
┌─ Docker Network ─────────────────────────────────────────┐
│                                                           │
│  amphive-frontend-dev (:80)                               │
│       │ nginx proxy_pass /api/ → backend:8000             │
│       ▼                                                   │
│  amphive-backend-dev (:8000)                              │
│       │ SQL → db:5432                                     │
│       │ MQTT → mqtt:1883                                  │
│       ▼                  ▼                                │
│  amphive-db-dev (:5432)   amphive-mqtt-dev (:1883, :9001) │
│                                                           │
└───────────────────────────────────────────────────────────┘
```

---

## 6. Routing Map

### Frontend Routes (React Router 6)

| Route | File | Component | Auth Required | Purpose |
|-------|------|-----------|:------------:|---------|
| `/` | `pages/Home.jsx` | `Home` | No (partial) | Dashboard: wallet card, plug ID entry, charger list. Shows sign-in prompt if not logged in |
| `/login` | `pages/Login.jsx` | `Login` | No | Combined login/register form with mode toggle |
| `/topup` | `pages/TopUp.jsx` | `TopUp` | **Yes** | Wallet top-up with ₹50/100/200/500 Razorpay checkout |
| `/session` | `pages/Session.jsx` | `Session` | **Yes** | Live charging session monitor. Redirects to `/` if no active session |
| `/groups` | `pages/Groups.jsx` | `Groups` | **Yes** | Join private groups by access code, list user's groups |
| `/cpo` | `pages/cpo/CpoSetup.jsx` | `CpoSetup` | **Yes** | CPO onboarding: create org, promote to CPO role |
| `/cpo/dashboard` | `pages/cpo/CpoDashboard.jsx` | `CpoDashboard` | **CPO** | Analytics: stat cards, revenue/energy charts, sessions |
| `/cpo/plugs` | `pages/cpo/CpoPlugs.jsx` | `CpoPlugs` | **CPO** | Plug CRUD: table, filters, add/edit modals |
| `/cpo/groups` | `pages/cpo/CpoGroups.jsx` | `CpoGroups` | **CPO** | Group CRUD: cards, access codes, delete confirm |
| `/cpo/sessions` | `pages/cpo/CpoSessions.jsx` | `CpoSessions` | **CPO** | Session history: filters, summary, full table |
| `*` (catch-all) | `App.jsx` | `Navigate` | No | Redirects to `/` |

### Backend API Routes (FastAPI)

See [API Inventory (§11)](#11-api-inventory) for the complete 36-endpoint table.

### MQTT Topics

| Direction | Topic Pattern | Purpose |
|-----------|--------------|---------|
| Backend → Gateway | `amphive/gateways/{gw_id}/plugs/{plug_id}/commands` | ON/OFF commands |
| Gateway → Backend | `amphive/gateways/{gw_id}/telemetry` | Power/energy telemetry |
| Gateway → Backend | `amphive/gateways/{gw_id}/status` | Online/offline status (LWT) |
| Gateway → Backend | `amphive/gateways/{gw_id}/alarms` | Safety alerts (thermal cutoff) |

---

## 7. Frontend Architecture

### Context Provider Hierarchy

```
<React.StrictMode>
  <AuthProvider>          ← JWT auth state, login/register/logout/refreshUser
    <WalletProvider>      ← coin_balance derived from AuthContext user
      <SessionProvider>   ← active session, SSE stream, start/stop
        <App />           ← BrowserRouter + routes
      </SessionProvider>
    </WalletProvider>
  </AuthProvider>
</React.StrictMode>
```

### Component Hierarchy

```
App
├── Navbar                  (always visible, sticky top)
│   ├── Logo link (/)
│   ├── Nav links: Home, Top Up, Groups
│   ├── CPO Portal link (role=cpo only) / Become a Host link (role=driver)
│   ├── Coin balance display
│   └── Sign In / Sign Out button
│
├── Home (/)
│   ├── WalletCard          (balance + top-up/history buttons)
│   ├── Start Charging form (plug ID input + start button)
│   └── Available Chargers list (glass cards, clickable to start)
│
├── Login (/login)
│   └── Combined login/register form
│
├── Session (/session) [Protected]
│   └── SessionMonitor
│       ├── Status indicator (active/completed pulse dot)
│       ├── Elapsed timer (HH:MM:SS)
│       ├── StatBox grid (power kW, energy kWh, current A, cost coins)
│       └── Stop Charging button
│
├── TopUp (/topup) [Protected]
│   ├── Balance display
│   ├── Amount selection grid (₹50, ₹100, ₹200, ₹500)
│   └── Pay button → Razorpay Checkout modal
│
├── Groups (/groups) [Protected]
│   ├── Join group form (access code input)
│   └── Groups list (public/private badges, plug count)
│
├── CpoSetup (/cpo) [Protected]
│   └── Tenant creation form + feature list
│
└── CpoLayout wrapper (sidebar + content)
    ├── CpoDashboard (/cpo/dashboard) [CPO Protected]
    │   ├── Stat cards (plugs, sessions, energy, revenue)
    │   ├── All-time summary bar
    │   ├── Revenue area chart (Recharts, 30-day)
    │   ├── Energy bar chart (Recharts, 30-day)
    │   └── Recent sessions table
    │
    ├── CpoPlugs (/cpo/plugs) [CPO Protected]
    │   ├── Status filter bar
    │   ├── Plugs data table
    │   ├── Add Plug modal
    │   ├── Edit Plug modal
    │   └── Inline maintenance toggle
    │
    ├── CpoGroups (/cpo/groups) [CPO Protected]
    │   ├── Group cards with plug/member counts
    │   ├── Access code display with copy-to-clipboard
    │   ├── Create/Edit/Delete modals
    │   └── Access code regeneration
    │
    └── CpoSessions (/cpo/sessions) [CPO Protected]
        ├── Filter bar (date range, plug, status)
        ├── Summary stats bar
        └── Full sessions data table
```

### Design System (global.css)

- **Theme:** Premium dark glassmorphism — deep navy background (`hsl(220, 20%, 7%)`), frosted glass panels with `backdrop-filter: blur(20px)`
- **Colors:** Electric cyan primary (`hsl(200, 85%, 55%)`), warm amber accent for coins (`hsl(42, 95%, 55%)`), semantic danger/success/warning
- **Typography:** Inter font from Google Fonts, `-0.02em` letter-spacing on headings
- **Animations:** `fadeIn`, `slideUp`, `pulse`, `shimmer` (skeleton loader), `glow`
- **Responsive:** Mobile breakpoints at 768px and 480px; navbar hides desktop links on mobile
- **Utility classes:** Flex, gap, margin, text-center, etc. (Tailwind-inspired but vanilla CSS)

### API Client (`api/client.js`)

- **Wrapper:** Native `fetch` with automatic JWT attachment from `localStorage`
- **Key:** `amphive_token` in localStorage
- **401 handling:** Clears token and redirects to `/login`
- **Base URL:** `import.meta.env.VITE_API_URL || ''` (same-origin in Docker)
- **Methods:** `api.get()`, `api.post()`, `api.put()`, `api.delete()`

### State Flow

```
AuthContext:
  localStorage('amphive_token') ←→ JWT token
  localStorage('amphive_user')  ←→ user object
  On mount: GET /api/auth/me to verify token validity
  login/register: POST → save token + user → setUser()
  logout: clear localStorage → setUser(null)

WalletContext:
  balance = user?.coin_balance ?? 0 (from AuthContext)
  refreshBalance() = refreshUser() from AuthContext

SessionContext:
  startSession(plugId):
    1. POST /api/sessions/start → get session_id
    2. Open EventSource to /api/sessions/live/{session_id}
    3. Listen for 'telemetry' events → setSessionData()
  stopSession():
    1. Close EventSource
    2. POST /api/sessions/stop
    3. Mark session as completed
```

---

## 8. Backend Architecture

### Application Structure

The entire backend lives in a **single main.py** (~1980 lines) plus 6 service modules and 2 database modules. There is no controller/router separation — all 37 endpoints are defined inline in `main.py`.

```
backend/
├── main.py                    # FastAPI app + all 37 routes (monolithic)
├── database/
│   ├── db.py                  # Engine, session factory, get_db, init_db
│   └── models.py              # 8 ORM models + 5 enums
└── services/
    ├── auth.py                # Password hashing, JWT, get_current_user
    ├── rbac.py                # ★ require_role() factory: role-based access control
    ├── mqtt_manager.py        # MQTT client singleton (paho-mqtt)
    ├── payments.py            # Razorpay integration
    ├── tapo_direct.py         # Direct Mode plug control
    └── telemetry.py           # In-memory telemetry store + SSE
```

### Lifespan Management

The FastAPI `lifespan` context manager:
1. **Startup:** `init_db()` → `MQTTManager.start()` → (optional) `TapoDirectDriver` init
2. **Shutdown:** `MQTTManager.stop()`

### Global Singletons

| Singleton | Type | Scope |
|-----------|------|-------|
| `mqtt_manager` | `MQTTManager` | Module-level, initialized in lifespan |
| `tapo_driver` | `TapoDirectDriver` | Module-level, conditional on `DIRECT_MODE` |
| `telemetry_store` | `TelemetryStore` | Module-level, `__new__` singleton pattern |

### Middleware

- **CORS:** `allow_origins=["*"]`, all methods/headers/credentials allowed. **TODO: restrict in production.**

### Error Handling Pattern

All endpoints use `HTTPException` with specific status codes:
- 400: Validation errors (bad input, already member, etc.)
- 401: Auth failures (invalid/expired token)
- 402: Insufficient wallet balance
- 403: Access denied (private group, not a member)
- 404: Resource not found
- 409: Conflict (plug occupied)
- 500: MQTT publish failure
- 502: Direct Mode plug unreachable
- 503: Service not configured (payments, direct mode)

---

## 9. Database Architecture

### Engine & Connection Pool

- **Driver:** `asyncpg` (non-blocking PostgreSQL)
- **Pool:** `pool_size=5`, `max_overflow=10`, `pool_pre_ping=True`
- **URL format:** `postgresql+asyncpg://user:pass@host:5432/amphive`
- **Session:** `expire_on_commit=False` to prevent lazy-load errors in response serialization
- **Schema creation:** `Base.metadata.create_all` on app startup (auto-creates tables)

### Entity-Relationship Diagram

```
tenants (CPO/Organization)
  │
  ├──< users (drivers, CPOs)
  │     │
  │     ├──< charging_sessions
  │     │        │
  │     │        └──< ledger_transactions
  │     │
  │     └──< group_memberships ──> charger_groups
  │
  ├──< gateways (ESP32 devices)
  │     │
  │     └──< plugs ──> charger_groups (optional)
  │            │
  │            └──< charging_sessions
  │
  ├──< charging_sessions
  │
  └──< charger_groups
        │
        ├──< plugs
        └──< group_memberships
```

### Tables Detail

| Table | PK | Key Fields | Relationships |
|-------|----|------------|---------------|
| `tenants` | `id` (auto) | `name` (unique) | → users, gateways, sessions, charger_groups |
| `users` | `id` (auto) | `email` (unique), `hashed_password`, `role`, `coin_balance` | → tenant, sessions, transactions, group_memberships |
| `gateways` | `id` (VARCHAR, caller-supplied MAC/UUID) | `vpn_ip` (unique), `status`, `tenant_id` | → tenant, plugs |
| `plugs` | `id` (auto) | `local_ip`, `plug_model`, `status`, `group_id` (nullable) | → gateway, sessions, charger_group |
| `charging_sessions` | `id` (auto) | `user_id`, `plug_id`, `energy_kwh`, `coins_spent`, `status` | → tenant, user, plug, ledger_transactions |
| `ledger_transactions` | `id` (auto) | `amount` (signed), `transaction_type`, `balance_after` | → user, session (nullable) |
| `charger_groups` | `id` (auto) | `name`, `is_public`, `access_code` (unique, nullable) | → tenant, plugs, memberships |
| `group_memberships` | `id` (auto) | `user_id`, `group_id` | → user, charger_group |

### Enums

| Enum | DB Type | Values |
|------|---------|--------|
| `UserRole` | `user_role` | `admin`, `cpo`, `driver` |
| `GatewayStatus` | `gateway_status` | `online`, `offline` |
| `PlugStatus` | `plug_status` | `available`, `occupied`, `offline`, `maintenance` |
| `SessionStatus` | `session_status` | `active`, `completed`, `paid`, `cancelled` |
| `TransactionType` | `tx_type` | `topup`, `session_debit`, `refund` |

### Schema Drift Warning

The SQL files (`schema.sql`, `schema_v2.sql`) define constraints and indexes that the ORM does **not** create:
- `UNIQUE (gateway_id, local_ip)` on `plugs` — **missing in ORM**
- `UNIQUE (user_id, group_id)` on `group_memberships` — **missing in ORM** (dedup only in app logic)
- All performance `CREATE INDEX` statements — **missing in ORM**

---

## 10. Authentication Flow

### Registration Flow
```
Frontend                         Backend
   │                                │
   │ POST /api/auth/register        │
   │ {email, password, full_name}   │
   │ ──────────────────────────────►│
   │                                │ 1. Check email uniqueness
   │                                │ 2. bcrypt hash password
   │                                │ 3. Create User (role=driver, balance=0)
   │                                │ 4. Generate JWT (HS256, 7-day expiry)
   │ ◄──────────────────────────────│
   │ {token, user}                  │
   │                                │
   │ Save to localStorage:          │
   │   amphive_token = token        │
   │   amphive_user = user JSON     │
```

### Login Flow
```
Frontend                         Backend
   │                                │
   │ POST /api/auth/login           │
   │ {email, password}              │
   │ ──────────────────────────────►│
   │                                │ 1. Find user by email
   │                                │ 2. bcrypt verify password
   │                                │ 3. Generate JWT
   │ ◄──────────────────────────────│
   │ {token, user}                  │
```

### Session Restoration (App Load)
```
Frontend                         Backend
   │                                │
   │ Check localStorage for token   │
   │                                │
   │ GET /api/auth/me               │
   │ Authorization: Bearer <token>  │
   │ ──────────────────────────────►│
   │                                │ 1. Decode JWT
   │                                │ 2. Load User from DB (fresh data)
   │ ◄──────────────────────────────│
   │ {id, email, full_name,         │
   │  role, coin_balance}           │
```

### JWT Token Structure
```json
{
  "sub": "42",          // user ID (string)
  "role": "driver",     // user role
  "email": "user@example.com",
  "iat": 1719878400,    // issued at
  "exp": 1720483200     // expires (7 days)
}
```

### Auth Middleware

`get_current_user` is a FastAPI `Depends()` dependency that:
1. Extracts `Bearer <token>` from `Authorization` header via `HTTPBearer`
2. Decodes JWT using `JWT_SECRET_KEY`
3. Loads fresh `User` from database by `user_id`
4. Returns `User` ORM instance or raises 401

### Known Auth Gaps
- ~~**No role enforcement:**~~ **RESOLVED (Phase 2.5)** — `require_role()` in `services/rbac.py` enforces CPO/admin roles on all `/api/cpo/*` endpoints. Returns 403 if role doesn't match.
- **SSE auth gap:** `EventSource` cannot send `Authorization` headers. The code comments say to pass `?token=`, but this is not implemented.
- **Gateway/plug registration is unauthenticated:** Legacy `POST /api/gateways/register` and `POST /api/plugs/register` require no JWT. CPOs should use the authenticated `POST /api/cpo/gateways` and `POST /api/cpo/plugs` instead.

---

## 11. API Inventory

| # | Method | Route | Auth | Purpose | Status |
|---|--------|-------|:----:|---------|:------:|
| 1 | `GET` | `/api/health` | No | Service health check | ✅ |
| 2 | `POST` | `/api/auth/register` | No | Create driver account, return JWT | ✅ |
| 3 | `POST` | `/api/auth/login` | No | Authenticate user, return JWT | ✅ |
| 4 | `GET` | `/api/auth/me` | Yes | Get current user profile (verify token) | ✅ |
| 5 | `POST` | `/api/groups/join` | Yes | Join private group by access code | ✅ |
| 6 | `GET` | `/api/groups/my` | Yes | List user's groups (public + joined private) | ✅ |
| 7 | `GET` | `/api/plugs/available` | Yes | List accessible plugs (public + joined groups + ungrouped) | ✅ |
| 8 | `GET` | `/api/plugs/{plug_id}` | Yes | Get single plug (with access check) | ✅ |
| 9 | `POST` | `/api/gateways/register` | **No** | Register ESP32 gateway (legacy) | ✅ |
| 10 | `POST` | `/api/plugs/register` | **No** | Register smart plug on gateway (legacy) | ✅ |
| 11 | `POST` | `/api/sessions/start` | Yes | Start charging session (MQTT ON) | ✅ |
| 12 | `POST` | `/api/sessions/stop` | Yes | Stop session (MQTT OFF, debit wallet, ledger) | ✅ |
| 13 | `GET` | `/api/sessions/live/{id}` | Yes | SSE real-time telemetry stream | 🟡 |
| 14 | `GET` | `/api/sessions/history` | Yes | Past sessions (last 50, most recent first) | ✅ |
| 15 | `POST` | `/api/payments/create-order` | Yes | Create Razorpay order for wallet top-up | ✅ |
| 16 | `POST` | `/api/payments/verify` | Yes | Verify Razorpay payment signature, credit coins | ✅ |
| 17 | `POST` | `/api/payments/webhook` | No | Razorpay server-to-server webhook (backup, log-only) | 🟦 |
| 18 | `POST` | `/api/direct/plug/on` | Yes | [Direct Mode] Turn plug ON | ✅ |
| 19 | `POST` | `/api/direct/plug/off` | Yes | [Direct Mode] Turn plug OFF | ✅ |
| 20 | `GET` | `/api/direct/plug/info` | Yes | [Direct Mode] Get device info | ✅ |
| 21 | `GET` | `/api/direct/plug/energy` | Yes | [Direct Mode] Get energy usage | ✅ |
| 22 | `GET` | `/api/direct/plug/health` | Yes | [Direct Mode] Health check | ✅ |
| | | | | **CPO Admin Dashboard (Phase 2.5)** | |
| 23 | `POST` | `/api/cpo/setup` | Yes | Create tenant, promote user to CPO role | ✅ |
| 24 | `GET` | `/api/cpo/profile` | CPO | Get CPO profile + tenant info + counts | ✅ |
| 25 | `GET` | `/api/cpo/gateways` | CPO | List CPO's gateways | ✅ |
| 26 | `POST` | `/api/cpo/gateways` | CPO | Register new gateway (authenticated) | ✅ |
| 27 | `GET` | `/api/cpo/plugs` | CPO | List all plugs with status + group info | ✅ |
| 28 | `POST` | `/api/cpo/plugs` | CPO | Register new plug on a CPO gateway | ✅ |
| 29 | `PUT` | `/api/cpo/plugs/{id}` | CPO | Update plug name/group/status | ✅ |
| 30 | `GET` | `/api/cpo/groups` | CPO | List CPO's charger groups | ✅ |
| 31 | `POST` | `/api/cpo/groups` | CPO | Create group (auto-generates access code) | ✅ |
| 32 | `PUT` | `/api/cpo/groups/{id}` | CPO | Update group, regenerate access code | ✅ |
| 33 | `DELETE` | `/api/cpo/groups/{id}` | CPO | Delete group, unlink plugs | ✅ |
| 34 | `GET` | `/api/cpo/analytics/overview` | CPO | Dashboard summary: plugs, sessions, energy, revenue | ✅ |
| 35 | `GET` | `/api/cpo/analytics/sessions` | CPO | Session history with filters | ✅ |
| 36 | `GET` | `/api/cpo/analytics/revenue` | CPO | Daily revenue breakdown for charts | ✅ |
| 37 | `GET` | `/api/cpo/analytics/energy` | CPO | Daily energy breakdown for charts | ✅ |

Legend: ✅ Working · 🟡 Partial (SSE endpoint exists but no live data source) · 🟦 Stub (log only) · CPO = Requires `cpo` or `admin` role

---

## 12. Data Flow Diagrams

### Flow 1: Start Charging Session

```
Driver taps "Start" on Plug ID 1
    │
    ▼
Frontend: POST /api/sessions/start { plug_id: 1 }
    │ Authorization: Bearer <JWT>
    ▼
Backend:
    1. Verify JWT → load User from DB
    2. Load Plug (id=1) from DB
    3. Access check: if plug.group_id → check group.is_public or membership
    4. Balance check: user.coin_balance ≥ 50
    5. Availability check: plug.status != OCCUPIED
    6. MQTT publish: amphive/gateways/{gw_id}/plugs/1/commands
       Payload: {"action":"ON","max_duration_seconds":14400,"max_kwh":30.0}
    7. Wait for publish (3s timeout, QoS 1)
    8. Create ChargingSession (status=ACTIVE)
    9. Set plug.status = OCCUPIED
    10. telemetry_store.start_session(plug_id=1)
    │
    ▼ Response: { session_id: 42, plug_name: "Home Charger" }
    │
    ▼
Frontend:
    1. Open EventSource → GET /api/sessions/live/42
    2. Navigate to /session
    3. Listen for 'telemetry' events → update SessionMonitor stats
    │
    ▼
ESP32 Gateway (via MQTT):
    1. Receive ON command
    2. Call tapo_set_power_state(plug_ip, true)
    3. Initialize active_session watchdog
    4. Every 15s: read telemetry → publish to amphive/gateways/{gw}/telemetry
    5. Watchdog checks: duration limit, energy limit, thermal limit (75°C)
```

### Flow 2: Wallet Top-Up (Razorpay)

```
Driver selects ₹100 and taps "Pay"
    │
    ▼
Frontend: POST /api/payments/create-order { amount_inr: 100 }
    │
    ▼
Backend:
    1. Validate amount (₹10 – ₹10,000)
    2. Razorpay API: create order (100 × 100 = 10000 paise)
    3. Return { order_id, amount, currency: "INR", key_id }
    │
    ▼
Frontend:
    1. Open window.Razorpay checkout modal with order_id
    2. User pays via UPI/card/wallet
    3. Razorpay returns: payment_id, order_id, signature
    │
    ▼
Frontend: POST /api/payments/verify { razorpay_order_id, payment_id, signature, amount_inr }
    │
    ▼
Backend:
    1. HMAC SHA256 verify signature (order_id|payment_id vs. Key Secret)
    2. Calculate coins: 100 × COINS_PER_RUPEE = 100 coins
    3. user.coin_balance += 100
    4. Create LedgerTransaction (amount=100, type=TOPUP)
    5. Return { coins_credited: 100, new_balance: 250 }
    │
    ▼
Frontend:
    1. refreshBalance() → GET /api/auth/me → update user state
    2. Show success toast
```

### Flow 3: Stop Charging Session

```
Driver taps "Stop Charging"
    │
    ▼
Frontend:
    1. Close EventSource (SSE connection)
    2. POST /api/sessions/stop { session_id: 42 }
    │
    ▼
Backend:
    1. Load session (verify user_id ownership)
    2. Load plug
    3. MQTT publish OFF command (best-effort, ignores failure)
    4. Get final telemetry from TelemetryStore
    5. session.status = COMPLETED, set ended_at, energy_kwh, coins_spent
    6. user.coin_balance -= cost
    7. Create LedgerTransaction (amount=-cost, type=SESSION_DEBIT)
    8. plug.status = AVAILABLE
    9. telemetry_store.end_session(plug_id)
    │
    ▼ Response: { energy_kwh: 1.234, coins_spent: 6.17, balance_remaining: 93.83 }
```

---

## 13. Environment Variables

### Backend (`.env` at project root)

| Variable | Purpose | Default | Sensitive? |
|----------|---------|---------|:----------:|
| `DATABASE_URL` | PostgreSQL connection string | `postgresql+asyncpg://postgres:amphive_dev@localhost:5432/amphive` | Yes |
| `MQTT_BROKER_HOST` | MQTT broker hostname | `localhost` | No |
| `MQTT_BROKER_PORT` | MQTT broker port | `1883` | No |
| `MQTT_USERNAME` | MQTT auth username | `None` | Yes |
| `MQTT_PASSWORD` | MQTT auth password | `None` | Yes |
| `JWT_SECRET_KEY` | JWT signing secret (HS256) | `amphive-dev-secret-change-in-production` | **Critical** |
| `RAZORPAY_KEY_ID` | Razorpay public key (test mode) | `""` | Semi |
| `RAZORPAY_KEY_SECRET` | Razorpay secret key | `""` | **Critical** |
| `RAZORPAY_WEBHOOK_SECRET` | Razorpay webhook HMAC secret | `""` | Yes |
| `COINS_PER_RUPEE` | Coin conversion rate | `1.0` | No |
| `DIRECT_MODE` | Enable direct Tapo control | `false` | No |
| `TAPO_USERNAME` | TP-Link/Tapo account email | `""` | Yes |
| `TAPO_PASSWORD` | TP-Link/Tapo account password | `""` | **Critical** |
| `TAPO_PLUG_IP` | Plug IP (WireGuard tunnel) | `""` | No |
| `TAPO_RELAY_URL` | HTTP relay URL for Tapo commands | `None` | No |

### Frontend (`.env` in frontend/)

| Variable | Purpose | Default |
|----------|---------|---------|
| `VITE_API_URL` | Backend API base URL | `""` (same origin) |
| `VITE_RAZORPAY_KEY_ID` | Razorpay public key for checkout | `""` |

> ⚠️ **SECURITY WARNING:** The root `.env` file is committed to git with real credentials (Razorpay test keys, Tapo account credentials). While `.env` is in `.gitignore`, the file exists in the repo. See [SECURITY.md](docs/SECURITY.md) for full details on committed secrets.

---

## 14. Third-Party Integrations

| Service | Purpose | SDK / Protocol | Notes |
|---------|---------|---------------|-------|
| **Razorpay** | Payment gateway (India) | `razorpay` Python SDK | UPI, cards, wallets, net banking. Test mode keys committed. |
| **Razorpay Checkout** | Frontend payment modal | CDN `<script>` in `index.html` | Opens native Razorpay UI; returns signature for verification |
| **Eclipse Mosquitto** | MQTT message broker | `paho-mqtt` (backend), ESP-IDF (firmware) | v2.0, anonymous, no TLS, QoS 0/1 |
| **Google Cloud SQL** | Managed PostgreSQL | asyncpg driver | PostgreSQL 15, `db-f1-micro`, asia-south1 |
| **Google Compute Engine** | VM hosting | Docker Compose on VM | `e2-highcpu-4`, 4 vCPU, 4GB RAM, 50GB disk |
| **Headscale** | Self-hosted Tailscale control | MicroLink C library on ESP32 | WireGuard overlay for NAT traversal |
| **TP-Link Tapo** | Smart plug control | `tapo` Python library / HTTP relay | P110 model, KLAP protocol (Python lib), mock in firmware |
| **DuckDNS** | Dynamic DNS | `setup_duckdns.sh` | Maps dynamic VM IP to `amphive.duckdns.org` |
| **Google Fonts** | Typography | CSS `@import` | Inter font family |

---

## 15. Feature Inventory

| # | Feature | Frontend Files | Backend Files | DB Tables | External | Status |
|---|---------|---------------|---------------|-----------|----------|:------:|
| 1 | **User Registration** | `Login.jsx`, `AuthContext.jsx` | `main.py:register`, `auth.py` | `users` | — | ✅ |
| 2 | **User Login** | `Login.jsx`, `AuthContext.jsx` | `main.py:login`, `auth.py` | `users` | — | ✅ |
| 3 | **Session Restore** | `AuthContext.jsx` | `main.py:get_me`, `auth.py` | `users` | — | ✅ |
| 4 | **Wallet Balance Display** | `WalletCard.jsx`, `WalletContext.jsx` | `main.py:get_me` | `users` | — | ✅ |
| 5 | **Wallet Top-Up** | `TopUp.jsx`, `WalletContext.jsx` | `main.py:create_payment_order/verify_payment`, `payments.py` | `users`, `ledger_transactions` | Razorpay | ✅ |
| 6 | **Plug ID Entry** | `Home.jsx` | `main.py:get_plug` | `plugs`, `charger_groups` | — | ✅ |
| 7 | **Available Plugs List** | `Home.jsx` | `main.py:get_available_plugs` | `plugs`, `charger_groups`, `group_memberships` | — | ✅ |
| 8 | **Start Charging** | `Home.jsx`, `SessionContext.jsx` | `main.py:start_charging_session`, `mqtt_manager.py`, `telemetry.py` | `charging_sessions`, `plugs`, `gateways` | MQTT | ✅ |
| 9 | **Live Session Monitor** | `SessionMonitor.jsx`, `SessionContext.jsx` | `main.py:live_session_telemetry`, `telemetry.py` | `charging_sessions` | SSE | 🟡 |
| 10 | **Stop Charging** | `SessionMonitor.jsx`, `SessionContext.jsx` | `main.py:stop_charging_session`, `mqtt_manager.py` | `charging_sessions`, `ledger_transactions`, `users` | MQTT | ✅ |
| 11 | **Session History** | — (endpoint exists, no UI) | `main.py:get_session_history` | `charging_sessions` | — | 🟡 |
| 12 | **Join Charger Group** | `Groups.jsx` | `main.py:join_group` | `charger_groups`, `group_memberships` | — | ✅ |
| 13 | **List My Groups** | `Groups.jsx` | `main.py:get_my_groups` | `charger_groups`, `group_memberships` | — | ✅ |
| 14 | **Gateway Registration** | — (API only) | `main.py:register_gateway` | `gateways` | — | ✅ |
| 15 | **Plug Registration** | — (API only) | `main.py:register_plug` | `plugs` | — | ✅ |
| 16 | **Direct Mode Control** | — (API only) | `main.py:direct_plug_*`, `tapo_direct.py` | — | Tapo, WireGuard | ✅ |
| 17 | **Razorpay Webhook** | — | `main.py:razorpay_webhook`, `payments.py` | — | Razorpay | 🟦 |
| 18 | **Captive Portal (Firmware)** | — | — | — | ESP32 HTTP server | ✅ |
| 19 | **VPN Tunnel (Firmware)** | — | — | — | MicroLink/Tailscale | 🟡 |
| 20 | **Edge Watchdogs (Firmware)** | — | — | — | ESP32 FreeRTOS | ✅ |
| 21 | **Map / Find Nearest** | — | — | — | — | ❌ |
| 22 | **CPO Admin Portal** | — | — | — | — | ❌ |
| 23 | **View Transaction History** | `WalletCard.jsx` (button exists, no handler) | — | — | — | ❌ |
| 24 | **OTA Firmware Updates** | — | — | — | — | ❌ |

---

## 16. Dependency Graph

### Backend Import Chain

```
main.py
  ├── database.db          → get_db, init_db
  │     └── database.models → Base (for create_all)
  ├── database.models      → User, Plug, Gateway, ChargingSession, etc.
  ├── services.auth        → hash_password, verify_password, create_access_token, get_current_user
  │     └── database.db    → get_db
  │     └── database.models → User
  ├── services.mqtt_manager → MQTTManager
  ├── services.telemetry   → TelemetryStore
  ├── services.tapo_direct → TapoDirectDriver
  └── services.payments    → create_order, verify_payment_signature, etc.
```

### Frontend Import Chain

```
main.jsx
  ├── App.jsx
  │     ├── components/Navbar.jsx
  │     │     ├── contexts/AuthContext.jsx → api/client.js
  │     │     └── contexts/WalletContext.jsx → contexts/AuthContext.jsx
  │     ├── pages/Home.jsx
  │     │     ├── components/WalletCard.jsx → contexts/WalletContext, AuthContext
  │     │     ├── contexts/AuthContext.jsx
  │     │     ├── contexts/SessionContext.jsx → api/client.js
  │     │     └── api/client.js
  │     ├── pages/Login.jsx → contexts/AuthContext.jsx
  │     ├── pages/Session.jsx → components/SessionMonitor.jsx, contexts/SessionContext.jsx
  │     ├── pages/TopUp.jsx → contexts/WalletContext, AuthContext, api/client.js
  │     └── pages/Groups.jsx → contexts/AuthContext.jsx, api/client.js
  ├── contexts/AuthContext.jsx → api/client.js
  ├── contexts/WalletContext.jsx → contexts/AuthContext.jsx
  ├── contexts/SessionContext.jsx → api/client.js
  └── styles/global.css
```

### Critical / High-Impact Files

| File | Impact | Why |
|------|--------|-----|
| `backend/main.py` | **Critical** | All 22 endpoints, all business logic, all Pydantic schemas |
| `backend/database/models.py` | **Critical** | All ORM models — any change here affects the DB schema |
| `backend/services/auth.py` | **Critical** | JWT secret, password hashing, auth dependency used by most routes |
| `backend/database/db.py` | **High** | Database connection; pool config; `init_db()` |
| `frontend/src/api/client.js` | **High** | All API calls flow through this; JWT handling |
| `frontend/src/contexts/AuthContext.jsx` | **High** | All auth state; used by every page |
| `frontend/src/styles/global.css` | **High** | Entire design system; any change affects all UI |
| `firmware/main/main.c` | **Critical** | All ESP32 logic: WiFi, VPN, MQTT, safety watchdogs |
| `deploy/scripts/deploy.ps1` | **High** | Production deployment pipeline |
| `.env` | **Critical** | All secrets and configuration |

---

## 17. Important Files

### Files You Should NOT Modify Lightly

| File | Reason |
|------|--------|
| `backend/database/models.py` | Database schema changes require migration strategy |
| `backend/services/auth.py` | Changing JWT config can break all existing sessions |
| `backend/database/db.py` | Pool/engine changes affect DB connection reliability |
| `deploy/scripts/deploy.ps1` | Production deploy pipeline — test in dev first |
| `firmware/main/main.c` | Physical hardware safety (watchdogs, thermal cutoff) |
| `.env` | Secrets — never commit real credentials |

### Dead Code

| File | Status |
|------|--------|
| `frontend/src/api/mockSse.js` | Never imported, Phase 1 leftover, event shape doesn't match consumer |
| `backend/database/init_db.py` | Standalone helper; redundant with `db.py:init_db()` |
| `backend/database/schema.sql` / `schema_v2.sql` | Reference only; not executed by the app |

---

## 18. Performance Notes

| Area | Issue | Severity |
|------|-------|:--------:|
| **Wallet updates not atomic** | `coin_balance += X` on ORM object, no row lock. Concurrent top-ups/debits can race. | 🔴 High |
| **In-memory telemetry** | `TelemetryStore` is a dict in RAM. Lost on restart; doesn't scale to multi-replica. | 🟡 Medium |
| **N+1 in plug listing** | `get_available_plugs()` loads plugs, then queries `ChargerGroup.name` per plug in a loop. | 🟡 Medium |
| **N+1 in group listing** | `get_my_groups()` queries plug count per group individually. | 🟡 Medium |
| **No DB indexes** | ORM `create_all` doesn't create the indexes defined in `schema.sql`. | 🟡 Medium |
| **No pagination** | Session history capped at 50 but no cursor/offset pagination support. | 🟢 Low |
| **Single-file backend** | 1128-line monolithic `main.py` — no router separation. Not a perf issue but a maintainability issue. | 🟢 Low |
| **SSE per-connection state** | Each SSE connection holds a reference to an `asyncio.Event`. At scale, memory grows linearly. | 🟢 Low |

---

## 19. Technical Debt

### Critical (Fix Before Production)

1. **Committed secrets in git history:** WireGuard private key, DuckDNS token, Tapo credentials, DB password. Must rotate and scrub.
2. **Default JWT secret** (`amphive-dev-secret-change-in-production`): All JWTs forgeable if not changed.
3. **MQTT broker is anonymous + public** (port 1883 on 0.0.0.0): Anyone on the internet can send plug ON/OFF commands.
4. **Wallet balance race condition:** Not row-locked; concurrent operations can corrupt balance.
5. **Gateway/plug registration unauthenticated:** Anyone can register fake gateways/plugs.
6. **CORS fully open** (`allow_origins=["*"]`).

### High (Fix Before Scaling)

7. **No RBAC:** `role` exists but is never enforced.
8. **SSE auth gap:** EventSource can't send Authorization headers; `?token=` workaround not implemented.
9. **MQTT inbound handlers are stubs:** Telemetry from gateways is only logged, not processed.
10. **No telemetry persistence:** In-memory only; no TimescaleDB despite specs.
11. **Firmware Tapo driver is mocked:** Returns simulated readings, no KLAP/AES protocol.
12. **TypeScript configured but unused:** All code is `.jsx`/`.js`.
13. **`mockSse.js` is dead code:** Should be removed.
14. **`charging_sessions.peak_power_w`** defined but never populated.

### Medium (Clean Up)

15. **Monolithic `main.py`:** Split into router modules (auth, sessions, plugs, payments, direct).
16. **Missing DB unique constraints/indexes:** `schema.sql` defines them, ORM doesn't create them.
17. **Stale documentation:** README API table, Python version, IP addresses, EC2 references.
18. **K8s manifests stale:** Not the live deployment; missing env vars, outdated images.
19. **`schema.sql`/`schema_v2.sql`** not used by app — confusing for new engineers.

---

## 20. Development Workflow

### Local Development

```bash
# 1. Clone + submodules
git clone https://github.com/Sarthak195/AmpHive.git
cd AmpHive
git submodule update --init --recursive

# 2. Full stack via Docker Compose
docker compose up --build

# 3. Access services
# Frontend:     http://localhost
# Backend API:  http://localhost:8000/docs
# MQTT Broker:  localhost:1883
# PostgreSQL:   localhost:5432 (postgres/amphive_dev)
```

### Frontend-only Development

```bash
cd frontend
npm install
npm run dev    # Vite dev server at http://localhost:5173
```

Requires `VITE_API_URL` pointing to a running backend.

### Backend-only Development

```bash
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r backend/requirements.txt
uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
```

Requires PostgreSQL and optionally MQTT broker running.

### Firmware Development

Requires ESP-IDF v5.x toolchain:
```bash
cd firmware
idf.py set-target esp32s3
idf.py build
idf.py -p COMX flash monitor
```

---

## 21. Deployment Process

### Production Target
- **Cloud:** Google Cloud Platform, `asia-south1` (Mumbai, India)
- **VM:** `amphive-vm-in`, `e2-highcpu-4` (4 vCPU, 4GB RAM, 50GB pd-balanced)
- **Database:** `amphive-db-in`, Cloud SQL PostgreSQL 15, `db-f1-micro`
- **Live IP:** `35.200.131.98` (ephemeral — may change)

### Deployment Pipeline (`deploy.ps1`)

```
Step 1: Wait for Cloud SQL → RUNNABLE
Step 2: Ensure 'amphive' database exists
Step 3: Fetch Cloud SQL IP → update .env with DATABASE_URL
Step 4: tar backend + frontend → SCP to VM + config files
Step 5: SSH → extract → docker-compose up -d --build
```

### Docker Images (Production)

| Service | Base Image | Port |
|---------|-----------|------|
| Backend | `python:3.11-slim` → uvicorn | 8000 |
| Frontend | `node:22-alpine` (build) → `nginx:alpine` (serve) | 80 |
| MQTT | `eclipse-mosquitto:2.0` | 1883 |
| PostgreSQL | Cloud SQL (external) | 5432 |

### Nginx Reverse Proxy

Frontend Nginx config:
- `/api/*` → proxy to `backend:8000`
- `/*` → serve SPA with `try_files $uri $uri/ /index.html`
- 404 → fallback to `index.html` (client-side routing)

---

## 22. Known Risks

| # | Risk | Likelihood | Impact | Mitigation |
|---|------|:----------:|:------:|------------|
| 1 | MQTT broker hijacked (anonymous, public) | High | Critical | Lock down broker: require auth, bind to overlay, remove public firewall rule |
| 2 | JWT secrets forgeable (default key) | High | Critical | Set strong `JWT_SECRET_KEY` in all environments |
| 3 | Wallet balance corruption (race condition) | Medium | High | Use atomic SQL UPDATE: `SET balance = balance + :n` with row lock |
| 4 | Committed secrets in git history | Already happened | High | Rotate all credentials, scrub history |
| 5 | No telemetry → sessions record 0 kWh/0 coins | High (Path A) | High | Wire MQTT inbound handlers to TelemetryStore |
| 6 | VM IP changes → WireGuard tunnel breaks | Medium | Medium | Use DuckDNS hostname or reserve static IP |
| 7 | Single backend instance → single point of failure | High | Medium | K8s deployment or managed service with replicas |
| 8 | No database backups configured | High | Critical | Enable Cloud SQL automated backups |

---

## 23. Future Recommendations

### Immediate (Before MVP Launch)

1. **Fix critical security issues** — see [SECURITY.md](docs/SECURITY.md) remediation checklist
2. **Wire MQTT inbound telemetry** to `TelemetryStore.update()` for real SSE data
3. **Make wallet operations atomic** with `UPDATE users SET coin_balance = coin_balance + :n WHERE id = :id`
4. **Implement RBAC** — check `role` in auth middleware
5. **Add auth to gateway/plug registration** endpoints
6. **Fix SSE auth** — pass short-lived signed token in query string

### Short Term (First Month)

7. **Split `main.py`** into `routers/` modules (auth, sessions, plugs, payments, groups, direct)
8. **Add TypeScript** — config is already in place, just rename files
9. **Add database indexes** matching `schema.sql` definitions
10. **Implement real firmware Tapo driver** (KLAP protocol)
11. **Add "View History" UI** — backend endpoint already exists
12. **Remove dead code** (`mockSse.js`, potentially `init_db.py`)

### Medium Term (3-6 Months)

13. **Time-series telemetry storage** — TimescaleDB or Cloud SQL + hypertable
14. **CPO Admin Portal** — separate frontend for charger operators
15. **Map UI** — OpenStreetMap/Leaflet for "find nearest plug"
16. **OTA firmware updates** — dual-partition scheme on ESP32
17. **Rate limiting** on auth endpoints
18. **Monitoring & alerting** — structured logging, Prometheus metrics

### Long Term (6+ Months)

19. **Multi-plug support per gateway** (currently 1:1 hardcoded)
20. **Dynamic load balancing** across multiple plugs at a site
21. **OCPP compliance** for commercial charger interop
22. **Kubernetes migration** (manifests exist but need updating)
23. **Redis for telemetry** (multi-replica support)
24. **Mobile native app** (React Native / Flutter)
