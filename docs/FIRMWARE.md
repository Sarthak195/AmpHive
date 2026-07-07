# AmpHive — ESP32-S3 Gateway Firmware

> Quick setup? See [ESP32_CONNECTION.md](ESP32_CONNECTION.md) for build/flash/monitor commands and common connection issues.

*Verified against `firmware/` on 2026-07-06.*

The gateway is an ESP-IDF application targeting **ESP32-S3-N16R8** (16 MB flash /
8 MB PSRAM). It joins a Tailscale-style overlay, receives MQTT commands, drives a
local Tapo plug, and enforces edge safety watchdogs. The standout piece is
`microlink` — a substantial, near-complete **from-scratch Tailscale-protocol
client written in C**.

```
firmware/
├── CMakeLists.txt            # ESP-IDF project "amphive-gateway"
├── sdkconfig.defaults        # PSRAM octal, 16MB flash, dual-OTA custom partition
├── main/
│   ├── main.c                # boot, WiFi, captive portal, MQTT loop, watchdogs
│   ├── tapo_protocol.c/.h    # Tapo P110 driver — real KLAP v2 (mbedTLS + esp_http_client)
│   ├── session_nvs.c/.h      # NVS session register — persist active session for crash recovery
│   ├── offline_log.c/.h      # NVS ring buffer — cache telemetry during MQTT outages
│   └── CMakeLists.txt
└── components/
    ├── microlink/            # custom Tailscale client (Noise/ts2021, DERP, DISCO, STUN, WG)
    └── wireguard_lwip/       # vendored WireGuard-over-lwIP (BSD-3, ref-C crypto)
```

---

## 1. Boot & tasks (`main/main.c`)

`app_main`:
1. `wifi_init()` — init NVS, load saved config, connect STA (blocking).
2. If no config / WiFi fails → `start_captive_portal()` and idle until the user
   submits the setup form (which triggers `esp_restart()`).
3. `tapo_init()` (also restores the persisted energy integrator from NVS).
4. Create `telemetry_safety` task (stack 8192, prio 5).
5. Create `microlink_vpn` task (stack 32768, prio 6).

| Task | Stack | Prio | Core |
|------|-------|------|------|
| `telemetry_safety` | 8192 | 5 | floating |
| `microlink_vpn` | 32768 | 6 | floating |
| `ml_coord_poll` (inside microlink) | 8 KB | max-1 | pinned Core 1 |

Hard-coded constants: `SERVER_VPN_IP "100.64.0.1"`, `MQTT_BROKER_URL
"mqtt://100.64.0.1:1883"`, `TARGET_PLUG_ID 1`. SSID, WiFi password, auth key,
device name, gateway id, and target plug IP all come from NVS (namespace
`storage`) populated by the captive portal.

## 2. Captive portal (✅ implemented)

If WiFi config is missing/invalid, the device starts SoftAP `AmpHive_Setup_XXXX`
(open), runs `esp_http_server` on `192.168.4.1`, serves an HTML form, and on POST
`/save` parses **8 fields** (WiFi SSID/password, Tailscale/Headscale auth key,
device name, gateway id, target plug IP, **Tapo account email + password**),
URL-decodes them, writes them to NVS, and reboots. The Tapo credentials are used
by the KLAP driver's auth hash (see §4). This is one of the few fully-delivered
product features.

## 3. MQTT control loop & watchdogs

- Lazy MQTT start once the overlay is up; topics per [MQTT_CONTRACT.md](MQTT_CONTRACT.md).
- Commands parsed with **cJSON** (`"action":"ON"`/`"OFF"`/`"SET_INTERVAL"`, optional
  `max_duration_seconds` / `max_kwh` / `session_id`, `interval_ms`); topic/data
  buffers 256/512 B with an oversized/fragmented-payload guard. Defaults: 14400 s,
  30.0 kWh, 10000 ms.
- `telemetry_safety` runs at a **configurable interval** (`telemetry_interval_ms`, default **10 s** / 10000 ms) and **always polls the plug regardless of MQTT connectivity**:
  - If a `"SET_INTERVAL"` command with `"interval_ms"` is received, the interval is updated (clamped between 500 ms and 60000 ms).
  - The published telemetry `kwh` is **session-relative** (`meter − start_energy_kwh`,
    clamped ≥ 0; 0 when idle), and the active `session_id` is echoed back — see
    [MQTT_CONTRACT.md](MQTT_CONTRACT.md). The raw lifetime meter is never billed.
  - If MQTT is connected: publishes telemetry normally.
  - If MQTT is disconnected: buffers the reading in `offline_log` (NVS ring buffer,
    session-relative `kwh`, no `session_id` field).
  - While a session is active, enforces edge cutoffs in both online and offline modes:
    - **Duration:** elapsed ≥ `max_duration_s` → local OFF + NVS clear.
    - **Energy:** consumed ≥ `max_kwh` → local OFF + NVS clear.
    - **Thermal:** plug reports `overheat_status != "normal"` → local OFF + NVS clear
      + publish `THERMAL_CUTOFF` alarm (if online). (The P110 has no temperature
      sensor, so this uses the plug's own overheat flag rather than a °C threshold.)
    - **Over-current:** plug reports `overcurrent_status != "normal"` → local OFF +
      NVS clear + publish `OVERCURRENT_CUTOFF` alarm. (The plug does the sensing;
      this replaces the previously-unimplemented 13 A/5-min rule.)
- On MQTT reconnect, buffered offline telemetry entries are drained and published
  with `"offline":true` and `"offline_ts"` so the backend can distinguish replayed data.

### 3a. NVS Session Register (`main/session_nvs.c`)

Active session parameters are persisted to a dedicated NVS namespace `"session"`
(separate from WiFi config in `"storage"`). Stored fields: `active`, `session_id`,
`start_time_s`, `max_duration_s`, `max_kwh` (as milliWh integer), `start_energy_kwh`
(as milliWh integer). On boot, `session_nvs_load()` checks for a crash-recovered
session and restores the watchdog state so limits are enforced immediately.

### 3b. Offline Telemetry Buffer (`main/offline_log.c`)

A 64-entry NVS-backed ring buffer (namespace `"offlog"`) that stores telemetry
snapshots as packed blobs (~20 bytes each) while MQTT is down. Metadata (head,
tail, count) is persisted atomically. The buffer overwrites the oldest entry when
full (~16 minutes of buffering at 15 s intervals).

## 4. Tapo driver — real KLAP v2 (`main/tapo_protocol.c`)

The mock is **replaced by a real KLAP v2 driver** (SHA1/SHA256/AES-128-CBC via
mbedTLS + `esp_http_client`). The exact protocol was validated against a real
P110 (fw 1.1.3) before porting — see `tools/klap_probe.py`.

- **Auth:** `auth_hash = SHA256(SHA1(email) || SHA1(password))`. The Tapo account
  email+password are collected by the captive portal and stored in NVS (see §2).
- **Handshake:** `POST /app/handshake1` (verify server hash), `POST /app/handshake2`,
  then per-request AES-CBC with an incrementing signed seq and a SHA256 signature
  (`POST /app/request?seq=N`). Session cached with a mutex; re-handshakes on HTTP 403.
- **`tapo_set_power_state`** → `set_device_info {device_on}`.
- **`tapo_get_telemetry`** → `get_energy_usage` (real `current_power`, **milliwatts**)
  + `get_device_info` (`device_on`, `overheat_status`, `overcurrent_status`).

**P110 telemetry reality** — the plug exposes power and energy, *not* voltage,
current, or temperature. So the `tapo_telemetry_t` fields map as:

| Field | Source |
|-------|--------|
| `power_w` | **real** — `current_power` (mW) ÷ 1000 |
| `energy_kwh` | **real** — driver-side monotonic **lifetime** Wh integrator (robust vs the plug's daily `today_energy` reset); persisted to NVS across reboots and updated under the driver mutex |
| `device_on` / `overheated` / `overcurrent` | **real** — from `get_device_info` status strings |
| `voltage_v` | **nominal** (configured, default 230 V) |
| `current_a` | **derived** — `power_w / voltage_v` |
| `temperature_c` | **nominal** — the P110 has no temperature sensor |

> **Lifetime meter vs billed energy:** `tapo_telemetry_t.energy_kwh` is the
> *lifetime* integrator. `main.c` subtracts the session baseline before publishing,
> so the MQTT `kwh` the backend bills is session-relative (see §3 and
> [MQTT_CONTRACT.md](MQTT_CONTRACT.md)). The integrator is mutex-protected because
> the telemetry task and the `ON`-handler's baseline read can call
> `tapo_get_telemetry` concurrently.

Because there is no real temperature, the thermal watchdog now trips on the plug's
`overheat_status` flag rather than a 75 °C compare (see §3).

> **Credentials caveat:** the Tapo email/password are stored in NVS in plaintext
> (acceptable for the prototype; a future hardening item).

## 5. `microlink` — the Tailscale client (✅ substantial, some TODOs)

A genuine ts2021 client in C (~13.5k LOC). Public API in
`components/microlink/include/microlink.h`. Subsystems:

| Subsystem | File | Role |
|-----------|------|------|
| coordination | `microlink_coordination.c` (~5k LOC) | Control-plane client: Noise ts2021 handshake, HTTP/2-over-Noise, `/machine/register` + MapRequest/MapResponse long-poll. Fetches server key from `/key` → works against **Headscale/Ionscale**, not just Tailscale. Dedicated Core-1 poll task w/ PSRAM buffer. |
| connection | `microlink_connection.c` | State machine: `IDLE → REGISTERING → FETCHING_PEERS → CONFIGURING_WG → CONNECTED → MONITORING` (heartbeats, reconnect/backoff, key rotation). |
| derp | `microlink_derp.c` | DERP relay over mbedTLS (defaults to `derp9d.tailscale.com` region 9; dynamic DERPMap supported). |
| disco | `microlink_disco.c` | Path discovery: ping/pong, CallMeMaybe, direct↔DERP upgrade. Bound to Tailscale's standard **port 41641** as a unified "magicsock" shared socket (exposing `ml->disco.sock_fd`). |
| stun | `microlink_stun.c` | Public IP/port discovery before advertising endpoints. Runs probes **through the shared DISCO socket** so that the discovered NAT mapping matches the port where traffic is received. Uses `select()` for non-blocking timeout polling. |
| wireguard | `microlink_wireguard.c` | Wraps the vendored `wireguard_lwip` netif. **Has TODOs:** payload send relies on lwIP routing (does not push bytes itself), pubkey extraction is a TODO, IPv6 WG endpoints unsupported. |
| udp | `microlink_udp.c` | Overlay UDP socket abstraction ("nc -u over Tailscale"). |
| peer_registry | `microlink_peer_registry.c` | NVS-backed registry (up to 1024 peers). **Compiled but never referenced** — currently dead/aspirational. |

**Unified Magicsock Port (NAT Traversal Fix):**
Previously, STUN used an ephemeral socket, DISCO bound to `51821`, and MapRequest advertised `51820`. This port mismatch caused the VM to send direct traffic to ports where the ESP32 wasn't listening, failing NAT traversal. The unified architecture uses the DISCO socket (port `41641`) for all UDP communication:
1. STUN probes are sent/received through the DISCO socket (`ml->disco.sock_fd`).
2. The discovered STUN public port is advertised via MapRequest.
3. The local endpoint port is advertised as `ml->disco.local_port` (`41641`).
4. Incoming WireGuard packets arriving on port `41641` are intercepted by the DISCO task and injected into the WireGuard handler via `microlink_wireguard_inject_packet`.

Vendored crypto: `nacl_box.c`, `x25519.c` (in microlink) and the full
`wireguard_lwip` ref-C crypto (BLAKE2S, ChaCha20-Poly1305, Poly1305, X25519).

**Headscale caveat:** the default host constants point at Tailscale's public
servers (`controlplane.tailscale.com`, `derp9d.tailscale.com`). To use
self-hosted Headscale, those constants must be overridden — the portal collects
an auth key but not a control-plane host.

## 6. Build config (`sdkconfig.defaults`)

- PSRAM: `CONFIG_SPIRAM=y`, octal 80 MHz; stacks allowed in external RAM; 32 KB
  internal reserved. 16 MB flash.
- **Partition table:** `CONFIG_PARTITION_TABLE_CUSTOM` → `partitions_ota.csv`
  (2026-07-07) — **dual OTA app slots** (`ota_0`/`ota_1`, 1920 KB each) +
  `otadata`, with `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE`. NVS keeps its
  pre-OTA offset (`0x9000`) so provisioning survives the one-time migration
  reflash. The ~1.1 MB image uses ~55% of a slot. (Was
  `SINGLE_APP_LARGE` — no OTA — before this.)
- mbedTLS TLS 1.2 + full cert bundle (for DERP/coordination TLS). lwIP IPv4-only.
- Main task stack 32768. `CONFIG_MICROLINK_DISCO_PORT=51821` (cosmetic, actual port is hardcoded to `41641` to match standard magicsock).

## 7. Build & flash

```bash
cd firmware
idf.py set-target esp32
idf.py -p COM5 flash monitor
```

## 8. Maturity summary

A working **demo/prototype**, not production firmware: `microlink` is deep and
mostly functional (with unified magicsock NAT traversal now fully operational), the captive portal and watchdogs work,
**session state is persisted in NVS with offline telemetry buffering**, and the
**Tapo driver is now a real KLAP v2 implementation** (protocol-validated against a
real P110 via `tools/klap_probe.py`; builds on **ESP-IDF v5.3**, not v6).
Path A has now been run end-to-end on real hardware (a billed session with correct
energy delivery); the firmware-side billing fix (session-relative `kwh`) and the
`session_id` echo were **reflashed and verified on-device 2026-07-06** (raw MQTT
payloads show `kwh` starting at 0 per session and the echoed `session_id`).
**OTA is now implemented** (2026-07-07): dual-slot `esp_https_ota` with
bootloader rollback, triggered by an `OTA` MQTT command
(`ota_update.c`/`.h`); the new image only cancels rollback once it re-reaches
the broker. Remaining gap: the control-plane host constants still default to
Tailscale (Headscale retarget pending). See
[IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md) for the full matrix.
