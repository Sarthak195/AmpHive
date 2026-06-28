# AmpHive — System Architecture

> Verified against source on 2026-06-20.

---

## 1. High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    GOOGLE CLOUD PLATFORM (asia-south1)                      │
│                                                                             │
│  ┌─────────────────┐    ┌──────────────────────┐    ┌───────────────────┐  │
│  │  Frontend        │    │  FastAPI Backend      │    │  Cloud SQL        │  │
│  │  (React/Vite)    │───►│  (Uvicorn, Python)    │───►│  (PostgreSQL 15)  │  │
│  │  Nginx :80       │REST│  :8000                │SQL │  db-f1-micro      │  │
│  └─────────────────┘    └──────────┬────────────┘    └───────────────────┘  │
│                                     │                                        │
│                                     │ MQTT (paho-mqtt)                       │
│                                     │                                        │
│                              ┌──────▼──────────┐                             │
│                              │  Mosquitto 2.0  │                             │
│                              │  MQTT Broker    │                             │
│                              │  :1883          │                             │
│                              └──────┬──────────┘                             │
│                                     │                                        │
│  ┌───────────────────┐              │ WireGuard/Tailscale VPN Tunnel         │
│  │  Headscale         │              │                                        │
│  │  Control Server    │              │                                        │
│  └───────────────────┘              │                                        │
└─────────────────────────────────────┼────────────────────────────────────────┘
                                      │
                    ┌─────────────────▼──────────────────┐
                    │  ESP32-S3 Edge Gateway              │
                    │  ┌─────────────────────────────┐   │
                    │  │ MicroLink Tailscale Client   │   │
                    │  │ MQTT Client                  │   │
                    │  │ Tapo P110 Protocol Driver    │   │
                    │  │ Safety Watchdog (FreeRTOS)   │   │
                    │  └─────────────────────────────┘   │
                    └─────────────────┬──────────────────┘
                                      │ VLAN 20 (Home LAN)
                    ┌─────────────────▼──────────────────┐
                    │  TP-Link Tapo P110 Smart Plug       │
                    │  (3.5 kW max, HTTP API)             │
                    └────────────────────────────────────┘
```

---

## 2. Two Operating Modes

### Path A — ESP32 + MQTT (Production Target)

```
Driver App → REST API → MQTT Broker → WireGuard → ESP32 → Smart Plug
                                                    ↑
                                               Telemetry
                                                    ↓
            SSE ← REST API ← MQTT Broker ← WireGuard ← ESP32

Status: WIRED BUT NOT FULLY FUNCTIONAL
- MQTT topic contract matches between backend and firmware
- Firmware Tapo driver is MOCKED (returns simulated data)
- Backend inbound telemetry handlers only LOG (don't process)
- Sessions over this path record 0 kWh / 0 coins
```

### Path B — Direct Mode (Currently Active)

```
Driver App → REST API → WireGuard Tunnel → Dev PC → Home LAN → Smart Plug
                         (relay_server.py :8000)

Status: WORKING (controls real hardware)
- Enabled via DIRECT_MODE=true in .env
- Uses `tapo` Python library or HTTP relay
- Does NOT feed session/telemetry pipeline
- Separate on/off/info surface from the session flow
```

---

## 3. Docker Container Topology

### Development (docker-compose.yml)

```
┌── Docker Network ─────────────────────────────────────────────┐
│                                                                │
│  amphive-frontend-dev  (:80)                                   │
│       │  nginx: /api/* → proxy_pass backend:8000               │
│       │         /*     → try_files → index.html (SPA)          │
│       ▼                                                        │
│  amphive-backend-dev   (:8000)                                 │
│       │  DATABASE_URL → db:5432                                │
│       │  MQTT_BROKER_HOST → mqtt                               │
│       ▼                     ▼                                  │
│  amphive-db-dev (:5432)    amphive-mqtt-dev (:1883, :9001)     │
│  postgres:15-alpine        eclipse-mosquitto:2.0               │
│  Volume: pg_dev_data       Volume: mosquitto.conf              │
│                                                                │
└────────────────────────────────────────────────────────────────┘
```

### Production (deploy/docker/docker-compose.prod.yml)

Same topology but:
- `DATABASE_URL` points to Cloud SQL (external, not containerized)
- No local PostgreSQL container
- Env vars injected from `.env` on the VM

---

## 4. Communication Protocols

| From → To | Protocol | Port | Auth | TLS |
|-----------|----------|------|------|-----|
| Browser → Frontend | HTTP | 80 | None | No (TODO) |
| Frontend → Backend | HTTP (via nginx proxy) | 8000 | JWT Bearer | No |
| Backend → PostgreSQL | TCP (asyncpg) | 5432 | Password | Cloud SQL proxy |
| Backend → MQTT Broker | MQTT | 1883 | Anonymous | **No** |
| MQTT Broker → ESP32 | MQTT | 1883 | Anonymous | **No** (secured by overlay) |
| ESP32 → VPN Control | HTTPS | 443 | Tailscale auth key | Yes |
| ESP32 → Smart Plug | HTTP | 80 | Tapo KLAP/AES | No (LAN only) |
| Backend → Razorpay | HTTPS | 443 | API Key + Secret | Yes |
| Backend → Smart Plug (Direct) | HTTP | 80/8000 | Tapo creds / relay | Via WireGuard |

---

## 5. Data Persistence

| Data Type | Storage | Persistence | Location |
|-----------|---------|:-----------:|----------|
| User accounts | PostgreSQL | ✅ Persistent | Cloud SQL |
| Charging sessions | PostgreSQL | ✅ Persistent | Cloud SQL |
| Wallet balances | PostgreSQL | ✅ Persistent | Cloud SQL |
| Ledger transactions | PostgreSQL | ✅ Persistent | Cloud SQL |
| Charger groups | PostgreSQL | ✅ Persistent | Cloud SQL |
| Live telemetry | In-memory dict | ❌ Lost on restart | Backend RAM |
| MQTT messages | Mosquitto | Partial (persistence=true) | Broker disk |
| Gateway config | NVS flash | ✅ Persistent | ESP32 flash |
| Active session state | RAM struct | ❌ Lost on restart | ESP32 RAM |

---

## 6. Security Perimeter

```
┌─ PUBLIC INTERNET ──────────────────────────────────────────┐
│                                                             │
│  :80   Frontend (Nginx)     ← Anyone                       │
│  :8000 Backend API          ← Anyone (JWT for most routes)  │
│  :1883 MQTT Broker          ← ANYONE (anonymous, no TLS!) ⚠│
│  :9001 MQTT WebSocket       ← Published but not configured  │
│                                                             │
└─────────────────────────────────────────────────────────────┘

┌─ WireGuard OVERLAY ────────────────────────────────────────┐
│  100.64.0.1  Server VPN IP                                  │
│  10.10.0.2   Developer PC (Direct Mode)                     │
│  100.64.x.x  ESP32 Gateway VPN IP (dynamic from Headscale)  │
└─────────────────────────────────────────────────────────────┘
```

---

## 7. Firmware Architecture (ESP32-S3)

```
app_main()
    │
    ├── wifi_init()
    │     ├── load_config_from_nvs()
    │     ├── If no config: start_captive_portal() → block
    │     └── Connect to WiFi STA
    │
    ├── tapo_init()
    │
    ├── xTaskCreate(telemetry_task)    [priority 5, 4KB stack]
    │     └── Every 15s:
    │           ├── Read plug telemetry
    │           ├── Publish to MQTT
    │           └── Watchdog checks:
    │                 ├── Max duration exceeded?
    │                 ├── Max energy exceeded?
    │                 └── Temperature > 75°C?
    │
    └── xTaskCreate(microlink_task)    [priority 6, 32KB stack]
          └── MicroLink VPN lifecycle:
                ├── Initialize config (auth_key, device_name)
                ├── Connect to Headscale/Tailscale
                └── On CONNECTED: start_mqtt_client()
                      ├── Configure LWT (offline status)
                      ├── Subscribe to commands
                      └── Handle ON/OFF commands
```
