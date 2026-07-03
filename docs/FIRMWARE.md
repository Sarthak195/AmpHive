# AmpHive — ESP32-S3 Gateway Firmware

> Quick setup? See [ESP32_CONNECTION.md](ESP32_CONNECTION.md) for build/flash/monitor commands and common connection issues.

*Verified against `firmware/` on 2026-06-20.*

The gateway is an ESP-IDF application targeting **ESP32-S3-N16R8** (16 MB flash /
8 MB PSRAM). It joins a Tailscale-style overlay, receives MQTT commands, drives a
local Tapo plug, and enforces edge safety watchdogs. The standout piece is
`microlink` — a substantial, near-complete **from-scratch Tailscale-protocol
client written in C**.

```
firmware/
├── CMakeLists.txt            # ESP-IDF project "amphive-gateway"
├── sdkconfig.defaults        # PSRAM octal, 16MB flash, single-app-large partition
├── main/
│   ├── main.c                # boot, WiFi, captive portal, MQTT loop, watchdogs
│   ├── tapo_protocol.c/.h    # Tapo P110 driver — ⚠️ MOCK/STUB (no real protocol)
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
3. `tapo_init()`.
4. Create `telemetry_safety` task (stack 4096, prio 5).
5. Create `microlink_vpn` task (stack 32768, prio 6).

| Task | Stack | Prio | Core |
|------|-------|------|------|
| `telemetry_safety` | 4096 | 5 | floating |
| `microlink_vpn` | 32768 | 6 | floating |
| `ml_coord_poll` (inside microlink) | 8 KB | max-1 | pinned Core 1 |

Hard-coded constants: `SERVER_VPN_IP "100.64.0.1"`, `MQTT_BROKER_URL
"mqtt://100.64.0.1:1883"`, `TARGET_PLUG_ID 1`. SSID, WiFi password, auth key,
device name, gateway id, and target plug IP all come from NVS (namespace
`storage`) populated by the captive portal.

## 2. Captive portal (✅ implemented)

If WiFi config is missing/invalid, the device starts SoftAP `AmpHive_Setup_XXXX`
(open), runs `esp_http_server` on `192.168.4.1`, serves an HTML form, and on POST
`/save` parses 6 fields (WiFi SSID/password, Tailscale auth key, device name,
gateway id, target plug IP), writes them to NVS, and reboots. This is one of the
few fully-delivered product features.

## 3. MQTT control loop & watchdogs

- Lazy MQTT start once the overlay is up; topics per [MQTT_CONTRACT.md](MQTT_CONTRACT.md).
- Commands parsed with `strstr`/`sscanf` (looks for `"action":"ON"`/`"OFF"`,
  optional `max_duration_seconds` / `max_kwh` / `session_id`). Defaults: 14400 s, 30.0 kWh.
- `telemetry_safety` runs every **15 s** and **always polls the plug regardless
  of MQTT connectivity**:
  - If MQTT is connected: publishes telemetry normally.
  - If MQTT is disconnected: buffers the reading in `offline_log` (NVS ring buffer).
  - While a session is active, enforces edge cutoffs in both online and offline modes:
    - **Duration:** elapsed ≥ `max_duration_s` → local OFF + NVS clear.
    - **Energy:** consumed ≥ `max_kwh` → local OFF + NVS clear.
    - **Thermal:** temperature > 75 °C → local OFF + NVS clear + publish `alarms` (if online).
    - **No over-current cutoff** (the 13 A/5-min rule in `requirements.md` is not
      implemented; `current` is published but never compared).
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

## 4. Tapo driver — ⚠️ mock (`main/tapo_protocol.c`)

This is a **stub, not a working driver**:
- No KLAP, no legacy passthrough, no AES — despite the comments/specs describing
  the real handshake.
- `tapo_set_power_state` opens a raw TCP socket to port 80 and sends an
  **unencrypted** `set_device_info` POST (which real Tapo firmware rejects); on
  socket failure it returns `ESP_OK` ("simulating").
- `tapo_get_telemetry` returns **hard-coded simulated EV telemetry**
  (~230 V, ~13 A, ~3000 W, an incrementing kWh, ~42.5 °C) and ignores any real
  response.

Replacing this with a real KLAP implementation (or reusing the `tapo` library
logic from `tools/`) is the key task to make Path A produce real readings.

## 5. `microlink` — the Tailscale client (✅ substantial, some TODOs)

A genuine ts2021 client in C (~13.5k LOC). Public API in
`components/microlink/include/microlink.h`. Subsystems:

| Subsystem | File | Role |
|-----------|------|------|
| coordination | `microlink_coordination.c` (~5k LOC) | Control-plane client: Noise ts2021 handshake, HTTP/2-over-Noise, `/machine/register` + MapRequest/MapResponse long-poll. Fetches server key from `/key` → works against **Headscale/Ionscale**, not just Tailscale. Dedicated Core-1 poll task w/ PSRAM buffer. |
| connection | `microlink_connection.c` | State machine: `IDLE → REGISTERING → FETCHING_PEERS → CONFIGURING_WG → CONNECTED → MONITORING` (heartbeats, reconnect/backoff, key rotation). |
| derp | `microlink_derp.c` | DERP relay over mbedTLS (defaults to `derp9d.tailscale.com` region 9; dynamic DERPMap supported). |
| disco | `microlink_disco.c` | Path discovery: ping/pong, CallMeMaybe, direct↔DERP upgrade. (`microlink_disco_zerocopy.c` is **excluded from the build**.) |
| stun | `microlink_stun.c` | Public IP/port discovery before advertising endpoints. |
| wireguard | `microlink_wireguard.c` | Wraps the vendored `wireguard_lwip` netif. **Has TODOs:** payload send relies on lwIP routing (does not push bytes itself), pubkey extraction is a TODO, IPv6 WG endpoints unsupported. |
| udp | `microlink_udp.c` | Overlay UDP socket abstraction ("nc -u over Tailscale"). |
| peer_registry | `microlink_peer_registry.c` | NVS-backed registry (up to 1024 peers). **Compiled but never referenced** — currently dead/aspirational. |

Vendored crypto: `nacl_box.c`, `x25519.c` (in microlink) and the full
`wireguard_lwip` ref-C crypto (BLAKE2S, ChaCha20-Poly1305, Poly1305, X25519).

**Headscale caveat:** the default host constants point at Tailscale's public
servers (`controlplane.tailscale.com`, `derp9d.tailscale.com`). To use
self-hosted Headscale, those constants must be overridden — the portal collects
an auth key but not a control-plane host.

## 6. Build config (`sdkconfig.defaults`)

- PSRAM: `CONFIG_SPIRAM=y`, octal 80 MHz; stacks allowed in external RAM; 32 KB
  internal reserved. 16 MB flash.
- **Partition table:** `CONFIG_PARTITION_TABLE_SINGLE_APP_LARGE` — **single app,
  no OTA partitions**, so the spec'd OTA dual-partition rollback is not possible
  without changing this.
- mbedTLS TLS 1.2 + full cert bundle (for DERP/coordination TLS). lwIP IPv4-only.
- Main task stack 32768. `CONFIG_MICROLINK_DISCO_PORT=51821`.

## 7. Build & flash

```bash
cd firmware
idf.py set-target esp32s3
idf.py -p COMx flash monitor
```

## 8. Maturity summary

A working **demo/prototype**, not production firmware: `microlink` is deep and
mostly functional (with noted TODOs), the captive portal and watchdogs work,
**session state is persisted in NVS with offline telemetry buffering**, but the
**Tapo driver is mocked**, command parsing is fragile, and there is no OTA.
See [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md) for the full matrix.
</content>
