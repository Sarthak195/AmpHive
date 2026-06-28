# AmpHive — API Map

> Verified against `backend/main.py` on 2026-06-20. All 22 endpoints documented.

---

## Endpoint Summary

| # | Method | Route | Auth | Used By | Status |
|---|--------|-------|:----:|---------|:------:|
| 1 | `GET` | `/api/health` | No | Monitoring, Docker health checks | ✅ |
| 2 | `POST` | `/api/auth/register` | No | Login.jsx (register mode) | ✅ |
| 3 | `POST` | `/api/auth/login` | No | Login.jsx (login mode) | ✅ |
| 4 | `GET` | `/api/auth/me` | JWT | AuthContext.jsx (session restore, refreshUser) | ✅ |
| 5 | `POST` | `/api/groups/join` | JWT | Groups.jsx | ✅ |
| 6 | `GET` | `/api/groups/my` | JWT | Groups.jsx | ✅ |
| 7 | `GET` | `/api/plugs/available` | JWT | Home.jsx | ✅ |
| 8 | `GET` | `/api/plugs/{plug_id}` | JWT | Home.jsx (plug ID entry) | ✅ |
| 9 | `POST` | `/api/gateways/register` | **None** | ESP32 / admin scripts | ✅ |
| 10 | `POST` | `/api/plugs/register` | **None** | Admin scripts | ✅ |
| 11 | `POST` | `/api/sessions/start` | JWT | SessionContext.jsx | ✅ |
| 12 | `POST` | `/api/sessions/stop` | JWT | SessionContext.jsx | ✅ |
| 13 | `GET` | `/api/sessions/live/{id}` | JWT* | SessionContext.jsx (EventSource SSE) | 🟡 |
| 14 | `GET` | `/api/sessions/history` | JWT | **No frontend consumer** (endpoint exists) | 🟡 |
| 15 | `POST` | `/api/payments/create-order` | JWT | TopUp.jsx | ✅ |
| 16 | `POST` | `/api/payments/verify` | JWT | TopUp.jsx | ✅ |
| 17 | `POST` | `/api/payments/webhook` | HMAC | Razorpay server callback | 🟦 |
| 18 | `POST` | `/api/direct/plug/on` | JWT | **No frontend consumer** (API-only) | ✅ |
| 19 | `POST` | `/api/direct/plug/off` | JWT | **No frontend consumer** (API-only) | ✅ |
| 20 | `GET` | `/api/direct/plug/info` | JWT | **No frontend consumer** (API-only) | ✅ |
| 21 | `GET` | `/api/direct/plug/energy` | JWT | **No frontend consumer** (API-only) | ✅ |
| 22 | `GET` | `/api/direct/plug/health` | JWT | **No frontend consumer** (API-only) | ✅ |

Legend: ✅ Working · 🟡 Partial · 🟦 Stub

---

## Detailed Endpoint Reference

### 1. `GET /api/health`

**No auth.** Returns service health.

```json
// Response 200
{ "status": "healthy", "service": "amphive-backend", "version": "2.0.0" }
```

---

### 2. `POST /api/auth/register`

**No auth.** Creates a new driver account.

```json
// Request
{ "email": "user@example.com", "password": "secret123", "full_name": "John Doe" }

// Response 200
{
  "token": "eyJ...",
  "user": { "id": 1, "email": "user@example.com", "full_name": "John Doe", "role": "driver", "coin_balance": 0.0 }
}

// Error 400: "An account with this email already exists."
```

**Side effects:** Creates `User` row (role=driver, balance=0). Hashes password with bcrypt.

---

### 3. `POST /api/auth/login`

**No auth.** Authenticates user and returns JWT.

```json
// Request
{ "email": "user@example.com", "password": "secret123" }

// Response 200
{
  "token": "eyJ...",
  "user": { "id": 1, "email": "...", "full_name": "...", "role": "driver", "coin_balance": 150.0 }
}

// Error 401: "Invalid email or password."
```

---

### 4. `GET /api/auth/me`

**JWT required.** Returns current user profile with fresh DB data.

```json
// Response 200
{ "id": 1, "email": "user@example.com", "full_name": "John Doe", "role": "driver", "coin_balance": 150.0 }
```

---

### 5. `POST /api/groups/join`

**JWT required.** Joins a private charger group using an access code.

```json
// Request
{ "access_code": "SUNRISE2024" }

// Response 200
{ "status": "joined", "group_id": 3, "group_name": "Sunrise Apartments" }

// Error 404: "Invalid access code. No group found."
// Error 400: "This group is public. No access code needed."
// Error 400: "You are already a member of this group."
```

**Side effects:** Creates `GroupMembership` row.

---

### 6. `GET /api/groups/my`

**JWT required.** Lists all groups the user can access (public + joined private).

```json
// Response 200
[
  { "id": 1, "name": "Public Network", "is_public": true, "plug_count": 5 },
  { "id": 3, "name": "Sunrise Apartments", "is_public": false, "plug_count": 2 }
]
```

---

### 7. `GET /api/plugs/available`

**JWT required.** Lists all plugs the user can access.

Access rules: ungrouped (NULL group_id) + public groups + joined private groups.

```json
// Response 200
[
  { "id": 1, "name": "Home Charger", "status": "available", "current_power_w": 0.0, "plug_model": "tapo_p110", "group_name": "Sunrise Apartments" }
]
```

---

### 8. `GET /api/plugs/{plug_id}`

**JWT required.** Single plug lookup with access check.

```json
// Response 200
{ "id": 1, "name": "Home Charger", "status": "available", ... }

// Error 404: "Plug with ID X not found."
// Error 403: "This plug belongs to a private group. Join the group first using an access code."
```

---

### 9. `POST /api/gateways/register`

**⚠ No auth.** Registers a new ESP32 gateway.

```json
// Request
{ "gateway_id": "AA:BB:CC:DD:EE:FF", "name": "Gateway Alpha", "vpn_ip": "100.64.0.5", "tenant_id": 1 }

// Response 200
{ "status": "registered", "gateway_id": "AA:BB:CC:DD:EE:FF", "name": "Gateway Alpha", "vpn_ip": "100.64.0.5" }
```

---

### 10. `POST /api/plugs/register`

**⚠ No auth.** Registers a smart plug on a gateway.

```json
// Request
{ "gateway_id": "AA:BB:CC:DD:EE:FF", "name": "Home Charger", "local_ip": "192.168.20.10", "plug_model": "tapo_p110", "group_id": 1 }

// Response 200
{ "status": "registered", "plug_id": 1, ... }
```

---

### 11. `POST /api/sessions/start`

**JWT required.** Starts a charging session.

```json
// Request
{ "plug_id": 1, "max_duration_seconds": 14400, "max_kwh": 30.0 }

// Response 200
{ "status": "started", "session_id": 42, "plug_id": 1, "plug_name": "Home Charger", "message": "Charging started on Home Charger." }

// Error 404: "Plug X not found."
// Error 403: "You don't have access to this plug."
// Error 402: "Insufficient balance. You have X coins. Minimum 50 required."
// Error 409: "This plug is currently in use."
// Error 500: "Failed to publish start command to the gateway."
```

**Side effects:** MQTT ON command, creates `ChargingSession` (ACTIVE), sets `Plug.status=OCCUPIED`, initializes `TelemetryStore`.

---

### 12. `POST /api/sessions/stop`

**JWT required.** Stops an active session, debits wallet.

```json
// Request
{ "session_id": 42 }

// Response 200
{ "status": "completed", "session_id": 42, "energy_kwh": 1.234, "coins_spent": 6.17, "balance_remaining": 93.83 }
```

**Side effects:** MQTT OFF command (best-effort), session COMPLETED, wallet debited, `LedgerTransaction` created, plug status AVAILABLE.

---

### 13. `GET /api/sessions/live/{session_id}`

**JWT required.** Server-Sent Events (SSE) telemetry stream.

```
event: telemetry
data: {"plug_id":1,"power_w":7200.0,"current_a":31.3,"voltage_v":230.0,"energy_kwh":0.456,"duration_sec":228,"cost_coins":2.28,"status":"charging","updated_at":1719878400.0}
```

Yields every ~1 second. Stream ends when `status` becomes `"completed"`.

---

### 14. `GET /api/sessions/history`

**JWT required.** Past sessions, most recent first, limit 50.

```json
// Response 200
[
  { "id": 42, "plug_id": 1, "started_at": "2026-06-20T10:00:00+00:00", "ended_at": "2026-06-20T11:30:00+00:00", "energy_kwh": 1.234, "coins_spent": 6.17, "status": "completed" }
]
```

---

### 15. `POST /api/payments/create-order`

**JWT required.** Creates Razorpay order for wallet top-up.

```json
// Request
{ "amount_inr": 100 }

// Response 200
{ "order_id": "order_NxGQi...", "amount": 10000, "currency": "INR", "key_id": "rzp_test_..." }

// Error 400: "Minimum top-up amount is ₹10." / "Maximum top-up amount is ₹10,000."
// Error 503: "Payment service is not configured."
```

---

### 16. `POST /api/payments/verify`

**JWT required.** Verifies Razorpay payment signature and credits coins.

```json
// Request
{ "razorpay_order_id": "order_NxGQi...", "razorpay_payment_id": "pay_...", "razorpay_signature": "abc123...", "amount_inr": 100 }

// Response 200
{ "status": "success", "coins_credited": 100, "new_balance": 250.0 }

// Error 400: "Payment verification failed. Invalid signature."
```

**Side effects:** `coin_balance += coins`, creates `LedgerTransaction` (TOPUP).

---

### 17. `POST /api/payments/webhook`

**No JWT auth** (HMAC gated). Razorpay server-to-server callback. Currently only logs — no auto-credit.

```json
// Response 200
{ "status": "ok" }
```

---

### 18–22. Direct Mode Endpoints (`/api/direct/plug/*`)

All require JWT + `DIRECT_MODE=true`. Return 503 if disabled.

| Endpoint | Tapo Action | Error on failure |
|----------|-------------|-----------------|
| `POST /api/direct/plug/on` | `device.on()` | 502 |
| `POST /api/direct/plug/off` | `device.off()` | 502 |
| `GET /api/direct/plug/info` | `device.get_device_info()` | 502 |
| `GET /api/direct/plug/energy` | `device.get_energy_usage()` | 502 |
| `GET /api/direct/plug/health` | `device.get_device_info()` (connectivity test) | 200 with `reachable: false` |

Plug IP resolution priority: request body `plug_ip` > `TAPO_PLUG_IP` env var.
