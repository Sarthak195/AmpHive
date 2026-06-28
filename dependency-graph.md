# AmpHive — Dependency Graph

> Verified against source on 2026-06-20.

---

## 1. Backend Import Dependency Graph

### Core Application Flow

```
main.py (FastAPI app, all 22 endpoints)
│
├── database/db.py
│   ├── sqlalchemy.ext.asyncio (create_async_engine, async_sessionmaker, AsyncSession)
│   └── database/models.py → Base (for create_all)
│
├── database/models.py
│   ├── sqlalchemy (Column, Integer, String, Float, Boolean, ForeignKey, DateTime, Enum)
│   ├── sqlalchemy.orm (DeclarativeBase, Mapped, mapped_column, relationship)
│   └── enum (Python stdlib)
│
├── services/auth.py
│   ├── jose (jwt) ← python-jose[cryptography]
│   ├── passlib.context (CryptContext) ← passlib[bcrypt]
│   ├── fastapi (Depends, HTTPException)
│   ├── fastapi.security (HTTPBearer, HTTPAuthorizationCredentials)
│   ├── sqlalchemy (select)
│   ├── database/db.py → get_db
│   └── database/models.py → User
│
├── services/mqtt_manager.py
│   ├── paho.mqtt.client ← paho-mqtt
│   ├── json (stdlib)
│   ├── re (stdlib)
│   └── logging (stdlib)
│
├── services/telemetry.py
│   ├── asyncio (stdlib)
│   ├── time (stdlib)
│   └── dataclasses (stdlib)
│
├── services/tapo_direct.py
│   ├── tapo (ApiClient) ← tapo (Rust-backed Python lib)
│   ├── urllib.request (stdlib)
│   └── json (stdlib)
│
├── services/payments.py
│   ├── razorpay ← razorpay
│   ├── hashlib (stdlib)
│   └── hmac (stdlib)
│
└── Third-party imports in main.py:
    ├── fastapi (FastAPI, HTTPException, Depends, Request)
    ├── fastapi.middleware.cors (CORSMiddleware)
    ├── pydantic (BaseModel, EmailStr)
    ├── sqlalchemy (select, or_, and_)
    ├── sse_starlette.sse (EventSourceResponse)
    └── dotenv (load_dotenv)
```

### Python Package Dependencies (`requirements.txt`)

```
fastapi>=0.100.0        → Core web framework
uvicorn>=0.22.0         → ASGI server
paho-mqtt>=2.1.0        → MQTT client
sqlalchemy>=2.0.0       → ORM
asyncpg>=0.28.0         → PostgreSQL async driver
pydantic>=2.0.0         → Request/response validation
python-jose[cryptography]>=3.3.0  → JWT encoding/decoding
passlib[bcrypt]>=1.7.4  → Password hashing
bcrypt==4.0.1           → bcrypt pinned version
python-dotenv>=1.0.0    → .env file loading
razorpay>=1.4.0         → Payment gateway SDK
sse-starlette>=1.6.0    → SSE support for FastAPI
tapo                    → TP-Link Tapo smart plug control
```

---

## 2. Frontend Import Dependency Graph

### Application Entry Chain

```
main.jsx (ReactDOM.createRoot, StrictMode)
│
├── styles/global.css (design system, all CSS)
│
├── contexts/AuthContext.jsx
│   └── api/client.js
│
├── contexts/WalletContext.jsx
│   └── contexts/AuthContext.jsx
│
├── contexts/SessionContext.jsx
│   └── api/client.js
│
└── App.jsx (BrowserRouter, Routes)
    │
    ├── components/Navbar.jsx
    │   ├── contexts/AuthContext.jsx
    │   ├── contexts/WalletContext.jsx
    │   └── react-router-dom (Link, useLocation, useNavigate)
    │
    ├── pages/Home.jsx
    │   ├── components/WalletCard.jsx
    │   │   ├── contexts/WalletContext.jsx
    │   │   ├── contexts/AuthContext.jsx
    │   │   └── react-router-dom (useNavigate)
    │   ├── contexts/AuthContext.jsx
    │   ├── contexts/SessionContext.jsx
    │   ├── api/client.js
    │   └── react-router-dom (useNavigate)
    │
    ├── pages/Login.jsx
    │   ├── contexts/AuthContext.jsx
    │   └── react-router-dom (useNavigate)
    │
    ├── pages/Session.jsx
    │   ├── components/SessionMonitor.jsx
    │   │   └── contexts/SessionContext.jsx
    │   ├── contexts/SessionContext.jsx
    │   └── react-router-dom (useNavigate)
    │
    ├── pages/TopUp.jsx
    │   ├── contexts/WalletContext.jsx
    │   ├── contexts/AuthContext.jsx
    │   ├── api/client.js
    │   └── react-router-dom (useNavigate)
    │
    └── pages/Groups.jsx
        ├── contexts/AuthContext.jsx
        ├── api/client.js
        └── react-router-dom (useNavigate)
```

### NPM Dependencies (`package.json`)

```
Production:
  react ^19.2.6              → UI framework
  react-dom ^19.2.6          → DOM rendering
  react-router-dom ^6.22.3   → Client-side routing

Dev:
  vite ^8.0.12               → Build tool & dev server
  @vitejs/plugin-react ^6.0.1 → Vite React integration
  typescript ~6.0.2          → TypeScript compiler (NOT USED in app code)
  eslint ^10.3.0             → Linting
  eslint-plugin-react-hooks  → React hooks rules
  eslint-plugin-react-refresh → HMR support
```

### External CDN Dependencies

```
index.html:
  Razorpay Checkout SDK → <script src="https://checkout.razorpay.com/v1/checkout.js">

global.css:
  Google Fonts → @import url('https://fonts.googleapis.com/css2?family=Inter:...')
```

---

## 3. Firmware Dependency Graph

### Source Dependencies

```
main.c (app_main entry point)
│
├── FreeRTOS (task management, event groups)
│   ├── freertos/FreeRTOS.h
│   ├── freertos/task.h
│   └── freertos/event_groups.h
│
├── ESP-IDF System
│   ├── esp_wifi.h (WiFi STA/AP)
│   ├── esp_event.h (event loop)
│   ├── esp_log.h (logging)
│   ├── esp_system.h (restart)
│   ├── esp_netif.h (network interface)
│   └── esp_http_server.h (captive portal)
│
├── NVS Flash (config persistence)
│   ├── nvs_flash.h
│   └── nvs.h
│
├── MQTT Client
│   └── mqtt_client.h (esp-mqtt)
│
├── MicroLink (Tailscale/WireGuard VPN)
│   └── microlink.h (components/microlink)
│
└── Tapo Protocol Driver
    ├── tapo_protocol.h
    └── tapo_protocol.c (MOCK implementation)

tapo_protocol.c
├── esp_http_client.h (HTTP requests to plug)
├── esp_log.h
└── cJSON.h (JSON parsing)
```

### Component Dependencies (CMake)

```
firmware/
├── CMakeLists.txt → project(amphive_gateway)
├── components/
│   └── microlink/ (git submodule) → Tailscale-compatible VPN client
│       ├── WireGuard implementation
│       ├── DERP relay support
│       ├── DISCO/STUN NAT traversal
│       └── Noise/ts2021 key exchange
└── main/
    └── CMakeLists.txt → SRCS main.c tapo_protocol.c
                         REQUIRES esp_wifi mqtt esp_http_server nvs_flash microlink
```

---

## 4. Infrastructure Dependencies

```
docker-compose.yml
│
├── frontend (Dockerfile)
│   ├── Build: node:22-alpine → npm install → vite build
│   └── Serve: nginx:alpine → nginx.conf
│       └── Depends: backend (proxy target)
│
├── backend (Dockerfile)
│   ├── python:3.11-slim → pip install -r requirements.txt
│   ├── Depends: db (PostgreSQL)
│   └── Depends: mqtt (Mosquitto)
│
├── db
│   └── postgres:15-alpine
│       └── Volume: pg_dev_data
│
└── mqtt
    └── eclipse-mosquitto:2.0
        └── Volume: deploy/config/mosquitto.conf
```

---

## 5. Critical Files (High Impact — Modify With Care)

| File | Impact Score | Reason |
|------|:-----------:|--------|
| `backend/main.py` | 🔴 10/10 | ALL 22 endpoints, ALL business logic, ALL Pydantic schemas |
| `backend/database/models.py` | 🔴 10/10 | ALL ORM models — changes alter DB schema |
| `backend/services/auth.py` | 🔴 9/10 | JWT secret, password hashing, auth dependency for most routes |
| `backend/database/db.py` | 🟠 8/10 | DB connection, pool config, init_db |
| `.env` | 🔴 9/10 | All secrets and configuration |
| `frontend/src/api/client.js` | 🟠 8/10 | All API calls route through here; JWT handling |
| `frontend/src/contexts/AuthContext.jsx` | 🟠 8/10 | All auth state, used by every page and context |
| `frontend/src/styles/global.css` | 🟠 7/10 | Entire design system, every component depends on it |
| `firmware/main/main.c` | 🔴 9/10 | Physical hardware safety (watchdogs, thermal cutoff) |
| `deploy/scripts/deploy.ps1` | 🟠 7/10 | Production deploy pipeline |
| `frontend/nginx.conf` | 🟠 7/10 | API proxy routing, SPA fallback |
| `docker-compose.yml` | 🟡 6/10 | Container orchestration |

---

## 6. Dead Code / Unused Files

| File | Status | Safe to Remove? |
|------|--------|:--------------:|
| `frontend/src/api/mockSse.js` | Never imported anywhere. Phase 1 leftover. Event shape doesn't match consumer. | ✅ Yes |
| `backend/database/init_db.py` | Standalone helper. Functionality duplicated in `db.py:init_db()`. | ✅ Yes |
| `backend/database/schema.sql` | Reference only. Not executed by app. May confuse new engineers. | 🟡 Keep as reference, add disclaimer |
| `backend/database/schema_v2.sql` | Migration delta reference. Not executed by app. | 🟡 Keep as reference, add disclaimer |
| `frontend/README.md` | Stock Vite template. Not project documentation. | ✅ Replace |

---

## 7. Circular Dependency Check

**No circular dependencies found.** The import graph is acyclic:
- Backend: `main.py` → `services/*` → `database/*` (one direction)
- Frontend: `App` → `pages/*` → `components/*` → `contexts/*` → `api/client` (one direction)
- `WalletContext` depends on `AuthContext` (not circular — `AuthContext` doesn't import `WalletContext`)
