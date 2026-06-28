# AmpHive — Routes Map

> Verified against source on 2026-06-20.

---

## 1. Frontend Routes (React Router 6 — BrowserRouter)

Defined in `frontend/src/App.jsx`. Protection via `ProtectedRoute` wrapper that checks `useAuth().user`.

| Route | File | Component | Auth Required | Layout | Purpose |
|-------|------|-----------|:------------:|--------|---------|
| `/` | `src/pages/Home.jsx` | `Home` | No (partial*) | Navbar + page-container | Dashboard: wallet card, plug ID entry, available charger list |
| `/login` | `src/pages/Login.jsx` | `Login` | No | Navbar + page-container (440px max) | Combined login/register form, mode toggle |
| `/topup` | `src/pages/TopUp.jsx` | `TopUp` | **Yes** | Navbar + page-container | Wallet top-up: ₹50/100/200/500 grid, Razorpay checkout |
| `/session` | `src/pages/Session.jsx` | `Session` | **Yes** | Navbar + page-container (800px max) | Live session monitor wrapper. Auto-redirect to `/` if no session |
| `/groups` | `src/pages/Groups.jsx` | `Groups` | **Yes** | Navbar + page-container | Join private group (access code), list user's groups |
| `*` | `src/App.jsx` | `Navigate to="/"` | No | — | Catch-all redirect to home |

*\*Home shows a sign-in prompt for unauthenticated users and hides the charger list + start form.*

### Route Hierarchy

```
<BrowserRouter>
  <Navbar />                          ← Always rendered (sticky top)
  <Routes>
    <Route path="/" element={<Home />} />                           ← Public
    <Route path="/login" element={<Login />} />                     ← Public
    <Route path="/topup" element={<ProtectedRoute><TopUp /></ProtectedRoute>} />
    <Route path="/session" element={<ProtectedRoute><Session /></ProtectedRoute>} />
    <Route path="/groups" element={<ProtectedRoute><Groups /></ProtectedRoute>} />
    <Route path="*" element={<Navigate to="/" />} />                ← Catch-all
  </Routes>
</BrowserRouter>
```

---

## 2. Backend API Routes (FastAPI)

All routes defined inline in `backend/main.py`. No router separation.

### Health

| Method | Route | Auth | Request Body | Response | Purpose |
|--------|-------|:----:|-------------|----------|---------|
| `GET` | `/api/health` | No | — | `{status, service, version}` | Service liveness check |

### Authentication

| Method | Route | Auth | Request Body | Response | Purpose |
|--------|-------|:----:|-------------|----------|---------|
| `POST` | `/api/auth/register` | No | `{email, password, full_name}` | `{token, user}` | Create driver account |
| `POST` | `/api/auth/login` | No | `{email, password}` | `{token, user}` | Authenticate, get JWT |
| `GET` | `/api/auth/me` | Yes | — | `{id, email, full_name, role, coin_balance}` | Get current user profile |

### Charger Groups

| Method | Route | Auth | Request Body | Response | Purpose |
|--------|-------|:----:|-------------|----------|---------|
| `POST` | `/api/groups/join` | Yes | `{access_code}` | `{status, group_id, group_name}` | Join private group |
| `GET` | `/api/groups/my` | Yes | — | `[{id, name, is_public, plug_count}]` | List user's groups |

### Plugs

| Method | Route | Auth | Request Body | Response | Purpose |
|--------|-------|:----:|-------------|----------|---------|
| `GET` | `/api/plugs/available` | Yes | — | `[{id, name, status, current_power_w, plug_model, group_name}]` | List accessible plugs |
| `GET` | `/api/plugs/{plug_id}` | Yes | — | `{id, name, status, ...}` | Get single plug (access check) |
| `POST` | `/api/plugs/register` | **No** ⚠ | `{gateway_id, name, local_ip, plug_model, group_id?}` | `{status, plug_id, ...}` | Register plug on gateway |

### Gateways

| Method | Route | Auth | Request Body | Response | Purpose |
|--------|-------|:----:|-------------|----------|---------|
| `POST` | `/api/gateways/register` | **No** ⚠ | `{gateway_id, name, vpn_ip, tenant_id}` | `{status, gateway_id, ...}` | Register ESP32 gateway |

### Sessions

| Method | Route | Auth | Request Body | Response | Purpose |
|--------|-------|:----:|-------------|----------|---------|
| `POST` | `/api/sessions/start` | Yes | `{plug_id, max_duration_seconds?, max_kwh?}` | `{status, session_id, plug_name}` | Start charging (MQTT ON) |
| `POST` | `/api/sessions/stop` | Yes | `{session_id}` | `{status, energy_kwh, coins_spent, balance_remaining}` | Stop session (MQTT OFF, debit) |
| `GET` | `/api/sessions/live/{session_id}` | Yes* | — | SSE stream: `{plug_id, power_w, current_a, energy_kwh, duration_sec, cost_coins, status}` | Real-time telemetry (SSE) |
| `GET` | `/api/sessions/history` | Yes | — | `[{id, plug_id, started_at, ended_at, energy_kwh, coins_spent, status}]` | Past sessions (last 50) |

*\*SSE route requires JWT in theory, but EventSource can't send auth headers. See auth gap in SECURITY.md.*

### Payments (Razorpay)

| Method | Route | Auth | Request Body | Response | Purpose |
|--------|-------|:----:|-------------|----------|---------|
| `POST` | `/api/payments/create-order` | Yes | `{amount_inr}` | `{order_id, amount, currency, key_id}` | Create Razorpay order |
| `POST` | `/api/payments/verify` | Yes | `{razorpay_order_id, payment_id, signature, amount_inr}` | `{status, coins_credited, new_balance}` | Verify payment, credit coins |
| `POST` | `/api/payments/webhook` | No | Razorpay event JSON | `{status: "ok"}` | Server-to-server webhook (stub) |

### Direct Mode (Tapo P110)

| Method | Route | Auth | Request Body | Response | Purpose |
|--------|-------|:----:|-------------|----------|---------|
| `POST` | `/api/direct/plug/on` | Yes | `{plug_ip?}` | `{status, plug_ip, message, mode}` | Turn plug ON |
| `POST` | `/api/direct/plug/off` | Yes | `{plug_ip?}` | `{status, plug_ip, message, mode}` | Turn plug OFF |
| `GET` | `/api/direct/plug/info` | Yes | `?plug_ip=` | `{plug_ip, device_info, mode}` | Get device info |
| `GET` | `/api/direct/plug/energy` | Yes | `?plug_ip=` | `{plug_ip, energy_usage, mode}` | Get energy usage |
| `GET` | `/api/direct/plug/health` | Yes | `?plug_ip=` | `{plug_ip, health, mode}` | Health check |

---

## 3. MQTT Topics

| Direction | Topic Pattern | QoS | Retained | Payload Schema |
|-----------|--------------|-----|----------|----------------|
| Backend → Gateway | `amphive/gateways/{gw_id}/plugs/{plug_id}/commands` | 1 | No | `{"action":"ON"|"OFF","max_duration_seconds":<int>,"max_kwh":<float>}` |
| Gateway → Backend | `amphive/gateways/{gw_id}/telemetry` | 0 | No | `{"plug_id":<int>,"watts":<f>,"kwh":<f>,"voltage":<f>,"current":<f>,"status":"occupied"|"available"}` |
| Gateway → Backend | `amphive/gateways/{gw_id}/status` | 1 | Yes | `{"status":"online"}` / `{"status":"offline"}` (LWT) |
| Gateway → Backend | `amphive/gateways/{gw_id}/alarms` | 1 | No | `{"error":"THERMAL_CUTOFF"}` |

Backend subscribes: `amphive/gateways/+/telemetry` (QoS 0), `amphive/gateways/+/status` (QoS 1).
Backend does NOT subscribe to `/alarms`.

---

## 4. Firmware HTTP Routes (Captive Portal)

When the ESP32 has no saved config, it starts a WiFi AP (`AmpHive_Setup_XXXX`) and serves:

| Method | Route | Purpose |
|--------|-------|---------|
| `GET` | `/` | Serve HTML config form (WiFi SSID, password, auth key, device name, gateway ID, plug IP) |
| `POST` | `/save` | Save config to NVS flash, respond with "Saved!", reboot after 2 seconds |
