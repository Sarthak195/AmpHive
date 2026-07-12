# AmpHive — ESP32-S3 Gateway Firmware

> Quick setup? See [ESP32_CONNECTION.md](ESP32_CONNECTION.md) for build/flash/monitor commands and common connection issues.

*Verified against `firmware/` on 2026-07-06; multi-plug refactor (TD#20)
code-complete + builds clean 2026-07-12, on-device verification pending.*

The gateway is an ESP-IDF application targeting **ESP32-S3-N16R8** (16 MB flash /
8 MB PSRAM). Since fw **1.3.0** it connects **directly to the public broker over
outbound MQTT/TLS** (`AMPHIVE_DIRECT_MQTT=1`, the default — NAT/CGNAT-immune, no
overlay; see [MQTT_CONTRACT.md](MQTT_CONTRACT.md) and [SECURITY.md §3](SECURITY.md)),
receives MQTT commands, drives **one or more** local Tapo plugs (each in its own
per-plug slot — KLAP session + energy meter, TD#20), and enforces edge safety
watchdogs. The legacy transport (`AMPHIVE_DIRECT_MQTT=0`) joins a Tailscale-style
overlay via `microlink` — a substantial, near-complete **from-scratch
Tailscale-protocol client written in C** — kept compilable for rollback, but
defeated by symmetric NAT (root-caused 2026-07-09) and no longer the default.

```
firmware/
├── CMakeLists.txt            # ESP-IDF project "amphive-gateway"
├── sdkconfig.defaults        # PSRAM octal, 16MB flash, dual-OTA custom partition
├── main/
│   ├── main.c                # boot, WiFi, captive portal, MQTT loop, per-plug slots + watchdogs
│   ├── tapo_protocol.c/.h    # Tapo P110 driver — real KLAP v2 (mbedTLS + esp_http_client); per-plug context
│   ├── session_nvs.c/.h      # NVS session register — persist all active per-plug sessions for crash recovery
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
5. Direct build (default): `start_mqtt_client()` immediately (esp-mqtt owns
   reconnects). Legacy overlay build: create `microlink_vpn` task (stack 32768,
   prio 6) which starts MQTT lazily once the overlay is up.

| Task | Stack | Prio | Core |
|------|-------|------|------|
| `telemetry_safety` | 8192 | 5 | floating |
| `microlink_vpn` (legacy build only) | 32768 | 6 | floating |
| `ml_coord_poll` (inside microlink, legacy build only) | 8 KB | max-1 | pinned Core 1 |

Hard-coded constants: `AMPHIVE_DIRECT_MQTT 1` → `MQTT_BROKER_URL
"mqtts://8.231.81.12:8883"` (the VM's static public IP; the broker CA is
embedded via `EMBED_TXTFILES "certs/mqtt_ca.crt"` and validated by mbedTLS:
chain + IP SAN, no date check). The legacy build (`AMPHIVE_DIRECT_MQTT 0`) keeps
`SERVER_VPN_IP "100.87.241.70"` + plaintext `mqtt://100.87.241.70:1883` inside
the WireGuard tunnel. `TARGET_PLUG_ID 1`. SSID, WiFi password, auth key,
device name, gateway id, target plug IP, and MQTT credentials all come from
NVS (namespace `storage`) populated by the captive portal.

## 2. Captive portal (✅ implemented; locked down fw 1.6.0)

If WiFi config is missing or the STA connection fails, the device starts SoftAP
`AmpHive_Setup_XXXX` — **WPA2-protected since fw 1.6.0** (passphrase = the
per-device **setup code**, see below) — and runs `esp_http_server` on
`192.168.4.1` in **AP-only mode** (no STA interface, so the portal is reachable
exclusively via the setup AP). The form collects the setup code plus **6 config
fields** (WiFi SSID/password, target plug IP, **Tapo account email + password**,
per-gateway MQTT password); `gateway_id`, `device_name`, and `mqtt_user` are
derived from the STA MAC, not typed. POST `/save` verifies the setup code
(constant-time compare; wrong code → 1 s throttle + 403, nothing written),
URL-decodes the fields, writes them to NVS, and reboots. The Tapo credentials
are used by the KLAP driver's auth hash (see §4).

- **Setup code:** 10 chars from an unambiguous alphabet, generated via
  `esp_random()` the first time the portal runs, persisted in NVS
  (`setup_code`), and printed over serial at **every** portal start — copy it
  onto the unit label. One secret serves as both the AP's WPA2 passphrase and
  the `/save` token (defense-in-depth against other clients on the setup AP).
  After a full NVS erase a new code is generated — re-label the unit.
- **Idle timeout:** the portal reboots the device after **10 min** without HTTP
  activity, so the boot-time Wi-Fi-loss fallback (SECURITY.md §8.4) no longer
  leaves an AP up indefinitely — a provisioned gateway retries its STA link on
  the way back up.

## 3. MQTT control loop & watchdogs

- Direct build: MQTT starts right after Wi-Fi (TLS to the public broker). Legacy
  build: lazy MQTT start once the overlay is up. Topics per
  [MQTT_CONTRACT.md](MQTT_CONTRACT.md).
- **Per-plug slots (multi-plug, TD#20).** A gateway can drive several plugs. Each
  plug the backend addresses gets a slot in a `MAX_PLUGS`-wide table (guarded by
  `plugs_mutex`): its DB `plug_id` (from the command topic), LAN IP, a **per-plug
  KLAP driver context** (`tapo_plug_t` — its own handshake/session + energy meter,
  so plug B's command can never actuate plug A), and its own session/watchdog
  state. `ON`/`OFF` carry the target `local_ip` (see [MQTT_CONTRACT.md](MQTT_CONTRACT.md)),
  so the gateway learns and drives the right plug without a static on-device
  roster; an empty `local_ip` falls back to the one provisioned `target_plug_ip`
  (single-plug back-compat). Slots are added, never freed at runtime, so the
  telemetry task can read them without holding the lock across a KLAP call.
- Commands parsed with **cJSON** (`"action":"ON"`/`"OFF"`/`"SET_INTERVAL"`, optional
  `max_duration_seconds` / `max_kwh` / `session_id` / `local_ip`, `interval_ms`);
  topic/data buffers 256/512 B with an oversized/fragmented-payload guard.
  Defaults: 14400 s, 30.0 kWh, 10000 ms. `SET_INTERVAL` is gateway-wide (one poll
  cadence for all plugs); `OTA` is refused if **any** plug is mid-session.
- `telemetry_safety` runs at a **configurable interval** (`telemetry_interval_ms`, default **10 s** / 10000 ms) and, each sweep, **polls every known plug regardless of MQTT connectivity** (with many plugs at a fast session cadence the sweep may exceed one interval, which just stretches the effective cadence — safety still runs every sweep):
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
  - **Unauthorized physical-on guard (fw 1.5.0):** when there is **no** active
    session, the loop checks the plug's real `device_on`; if the relay is ON
    (physical button, Tapo app, or NVS crash-recovery resuming a stale session)
    it commands OFF every cycle and publishes
    `{"error":"UNAUTHORIZED_ON","plug_id":N}` once per episode (rising edge).
  - Since fw 1.5.0 the telemetry payload also includes `"relay":<bool>` — the
    actual `device_on` state, distinct from `"status"` (session state).
- On MQTT reconnect, buffered offline telemetry entries are drained and published
  with `"offline":true` and `"offline_ts"` so the backend can distinguish replayed data.

### 3a. NVS Session Register (`main/session_nvs.c`)

The active per-plug sessions are persisted to a dedicated NVS namespace
`"session"` (separate from WiFi config in `"storage"`). Since the multi-plug
refactor (TD#20) the whole set is stored as **one blob** (`session_nvs_save_all`
/ `session_nvs_load_all`) so a save is atomic; each record adds `plug_id` and
`local_ip` to the prior fields (`active`, `session_id`, `start_time_s`,
`max_duration_s`, `max_kwh` and `start_energy_kwh` as milliWh integers). Carrying
`local_ip` is what lets crash recovery re-create the plug's KLAP context and keep
driving it — the backend ON command that first taught the IP is gone after a
reboot. On boot, `session_nvs_load_all()` restores **every** recovered session's
watchdog state so limits are enforced immediately on all plugs. (The blob format
supersedes the pre-multi-plug single-session keys; after an OTA from that firmware
`load_all` finds no blob and recovers nothing — safe, because OTA is refused while
any session is active, so there is never a live session to lose across the
upgrade.)

### 3b. Offline Telemetry Buffer (`main/offline_log.c`)

A 64-entry NVS-backed ring buffer (namespace `"offlog"`) that stores telemetry
snapshots as packed blobs (~20 bytes each) while MQTT is down. Metadata (head,
tail, count) is persisted atomically. The buffer overwrites the oldest entry when
full (~16 minutes of buffering at 15 s intervals).

## 4. Tapo driver — real KLAP v2 (`main/tapo_protocol.c`)

The mock is **replaced by a real KLAP v2 driver** (SHA1/SHA256/AES-128-CBC via
mbedTLS + `esp_http_client`). The exact protocol was validated against a real
P110 (fw 1.1.3) before porting — see `tools/klap_probe.py`.

Since the multi-plug refactor (TD#20) the driver is **per-plug**: the Tapo
*account* (`auth_hash`) and nominal voltage stay module-global (one account owns
every plug), while the KLAP session and energy integrator live in an opaque
`tapo_plug_t` handle. `main.c` creates one per plug via `tapo_plug_create(plug_id,
local_ip)` and calls the `tapo_plug_*` operations on it; each handle has its own
mutex, so a plug's set/get can't race another's, and plug A's session keys can
never be reused to act on plug B.

- **Auth (shared):** `tapo_init(email, password, nominal_v)` derives
  `auth_hash = SHA256(SHA1(email) || SHA1(password))` once. The Tapo account
  email+password are collected by the captive portal and stored in NVS (see §2).
- **Handshake (per plug):** `POST /app/handshake1` (verify server hash), `POST
  /app/handshake2`, then per-request AES-CBC with an incrementing signed seq and a
  SHA256 signature (`POST /app/request?seq=N`). Each plug's session is cached under
  its own mutex; re-handshakes on HTTP 403.
- **`tapo_plug_set_power(plug, on)`** → `set_device_info {device_on}`.
- **`tapo_plug_get_telemetry(plug, out)`** → `get_energy_usage` (real
  `current_power`, **milliwatts**) + `get_device_info` (`device_on`,
  `overheat_status`, `overcurrent_status`).
- **`tapo_plug_set_ip(plug, ip)`** rebinds a plug's IP (new DHCP lease) and
  invalidates its session so the next call re-handshakes.

**P110 telemetry reality** — the plug exposes power and energy, *not* voltage,
current, or temperature. So the `tapo_telemetry_t` fields map as:

| Field | Source |
|-------|--------|
| `power_w` | **real** — `current_power` (mW) ÷ 1000 |
| `energy_kwh` | **real** — driver-side monotonic **lifetime** Wh integrator (robust vs the plug's daily `today_energy` reset); **per-plug**, persisted to NVS (key `wh_<plug_id>`) across reboots and updated under that plug's mutex. Since fw 1.5.0 it integrates with the **trapezoidal rule** (average of consecutive power samples × dt) instead of left-rectangle, reducing error on ramping loads at the 10 s poll cadence; the previous sample is held per-plug in the `tapo_plug_t` context |
| `device_on` / `overheated` / `overcurrent` | **real** — from `get_device_info` status strings |
| `voltage_v` | **nominal** (configured, default 230 V) |
| `current_a` | **derived** — `power_w / voltage_v` |
| `temperature_c` | **nominal** — the P110 has no temperature sensor |

> **Lifetime meter vs billed energy:** `tapo_telemetry_t.energy_kwh` is the
> *lifetime* integrator. `main.c` subtracts the session baseline before publishing,
> so the MQTT `kwh` the backend bills is session-relative (see §3 and
> [MQTT_CONTRACT.md](MQTT_CONTRACT.md)). Each plug's integrator is protected by
> that plug's own mutex because the telemetry task and the `ON`-handler's baseline
> read can call `tapo_plug_get_telemetry` for the same plug concurrently.

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
- **Signed OTA (2026-07-10):** `CONFIG_SECURE_SIGNED_APPS_NO_SECURE_BOOT` +
  ECDSA scheme (v1) + `CONFIG_SECURE_SIGNED_ON_UPDATE_NO_SECURE_BOOT` — every
  OTA image must carry a valid ECDSA signature or `esp_https_ota_finish`
  rejects it. Software-only verification (no eFuses burned, no boot-time
  check — reversible). The build signs automatically
  (`CONFIG_SECURE_BOOT_BUILD_SIGNED_BINARIES`) with
  `firmware/secure_boot_signing_key.pem` — **gitignored, back it up**: losing
  it means devices only accept a USB reflash. Plain-http OTA is gone
  (`CONFIG_ESP_HTTPS_OTA_ALLOW_HTTP` removed; `ota_update_start` also refuses
  non-`https://` URLs).
- Main task stack 32768. `CONFIG_MICROLINK_DISCO_PORT=51821` (cosmetic, actual port is hardcoded to `41641` to match standard magicsock).

## 7. Build & flash

```bash
cd firmware
idf.py set-target esp32
idf.py -p COM5 flash monitor
```

Building requires the OTA signing key at `firmware/secure_boot_signing_key.pem`
(gitignored; generate once with
`python -m espsecure generate_signing_key --version 1 secure_boot_signing_key.pem`
and back it up — see §6). The build output `build/amphive-gateway.bin` is
already signed; `build/amphive-gateway-unsigned.bin` is the pre-signature
artifact (68 bytes smaller) and must never be shipped.

### Publishing an OTA image

Images are served from the **public-read GCS bucket `gs://amphive-fw`**
(`https://storage.googleapis.com/amphive-fw/...`) — a valid public-CA TLS
host the firmware's Mozilla bundle validates. Upload + trigger:

```bash
gcloud storage cp firmware/build/amphive-gateway.bin \
    gs://amphive-fw/amphive-gateway-<version>.bin
# then POST /api/cpo/gateways/{gateway_id}/ota with that https URL
```

or run `deploy/scripts/publish_firmware.ps1`, which reads the version from
`firmware/CMakeLists.txt`, uploads, and prints the OTA-trigger call. Full
runbook (including the one-time bucket setup that was run 2026-07-10):
[deploy/docs/ota_image_publishing.md](../deploy/docs/ota_image_publishing.md).

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
**OTA is now implemented and verified end-to-end** (2026-07-08): dual-slot
`esp_https_ota` with bootloader rollback, triggered by an `OTA` MQTT command
(`ota_update.c`/`.h`); the new image only cancels rollback once it re-reaches
the broker. A live `1.1.0 → 1.1.1` push downloaded into `ota_1`, rebooted,
and committed (`marking image valid`) on real hardware.

**OTA over the direct-MQTT path — verified 2026-07-10.** A `1.3.0 → 1.3.1`
push, image hosted at a **public** URL (`http://8.231.81.12/...`) and triggered
over the public broker, downloaded (1 MB in ~20 s), swapped `ota_0 → ota_1`,
rebooted into `1.3.1-direct`, reconnected, and `marking image valid` — no
overlay anywhere. Because direct devices fetch images across the public
internet, `ota_update.c` attaches the **Mozilla CA bundle**
(`esp_crt_bundle_attach`, fw ≥ 1.3.1); the 1.3.0→1.3.1 jump itself used plain
http only because the *old* running image predated the cert bundle.

**OTA hardening — signed + https-only (2026-07-10, fw ≥ 1.4.0, rolled
out).** Both follow-ups from the direct-MQTT pivot are implemented:
images are hosted on the public HTTPS bucket `gs://amphive-fw` (see §7), and
every update must carry a valid **ECDSA app signature** (§6) — a
valid-but-malicious image from a MITM'd or compromised host is now rejected
by the device itself. Plain `http://` is refused in the firmware
(`ALLOW_HTTP` removed + explicit scheme check) *and* by the backend
(`CpoGatewayOtaRequest` requires `https://`). **Verified on-device
2026-07-10:** the real gateway `1cc3abb4fb54` was OTA'd `1.3.2 → 1.5.0` with
a signed image over https, and the backend `^https://` validation is
deployed. The pre-1.4.0 running image accepted the jump (it doesn't check
signatures; the signature is a trailer it ignores); from 1.4.0 on, only
signed images install. See
[IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md) for the full matrix.

**fw `1.5.0-direct` (current) — OTA'd + verified on the real gateway
`1cc3abb4fb54` 2026-07-10** (`1.3.2 → 1.5.0`; the new `relay` field seen on
the wire). Three changes:
- **Unauthorized physical-on guard** — with no active session, a relay found
  ON (physical button, Tapo app, or NVS crash-recovery resuming a stale
  session) is commanded OFF every telemetry cycle and
  `{"error":"UNAUTHORIZED_ON","plug_id":N}` is published once per episode
  (rising edge). See §3.
- **Trapezoidal energy integration** — `tapo_protocol.c` integrates energy
  with the trapezoidal rule (average of consecutive power samples × dt)
  instead of left-rectangle, reducing error on ramping loads at the 10 s poll
  cadence (`s_energy_last_power_w` holds the previous sample). See §4.
- **Telemetry `relay` field** — telemetry now includes `"relay":<bool>` (the
  actual `device_on`), distinct from `"status"` (session state). See §3.
