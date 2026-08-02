# AmpHive — ESP32-C3 Gateway Firmware

> Quick setup? See [ESP32_CONNECTION.md](ESP32_CONNECTION.md) for build/flash/monitor commands and common connection issues.

*Verified against `firmware/` on 2026-07-26 (well past the 2026-07-10
direct-MQTT pivot; current shipped fw is **2.3.0-direct**, per
`firmware/CMakeLists.txt`'s `PROJECT_VER`). Multi-plug refactor (TD#20) shipped
in fw **1.7.1-direct** and **verified on-device 2026-07-12** (single-plug
charging regression on the real gateway — see §3; two-real-plug validation still
needs a second unit).*

The gateway is an ESP-IDF application targeting the real fielded hardware,
**ESP32-C3** (~4 MB flash, **no PSRAM** — see `firmware/sdkconfig.defaults`).
Since fw **1.3.0** it connects **directly to the public broker over
outbound MQTT/TLS** (`AMPHIVE_DIRECT_MQTT=1`, the default — NAT/CGNAT-immune, no
overlay; see [MQTT_CONTRACT.md](MQTT_CONTRACT.md) and [SECURITY.md §3](SECURITY.md)),
receives MQTT commands, drives **one or more** local Tapo plugs (each in its own
per-plug slot — KLAP session + energy meter, TD#20), and enforces edge safety
watchdogs. A legacy overlay transport (`microlink`, a from-scratch
Tailscale-protocol client written in C) shipped alongside it as a rollback path,
but was defeated by symmetric NAT (root-caused 2026-07-09), was never the
default, and was **removed 2026-08-02** (see §5).

```
firmware/
├── CMakeLists.txt            # ESP-IDF project "amphive-gateway"
├── sdkconfig.defaults        # ESP32-C3: 4MB flash, no PSRAM, dual-OTA custom partition
├── main/
│   ├── main.c                # boot, WiFi, captive portal, MQTT loop, per-plug slots + watchdogs
│   ├── tapo_protocol.c/.h    # Tapo P110 driver — real KLAP v2 (mbedTLS + esp_http_client); per-plug context
│   ├── session_nvs.c/.h      # NVS session register — persist all active per-plug sessions for crash recovery
│   ├── offline_log.c/.h      # NVS ring buffer — cache telemetry during MQTT outages
│   └── CMakeLists.txt
└── components/
    └── json/                 # vendored cJSON (ESP-IDF v6 removed it from core)
```

---

## 1. Boot & tasks (`main/main.c`)

`app_main`:
1. `wifi_init()` — init NVS, load saved config, connect STA (blocking).
2. If no config / WiFi fails → `start_captive_portal()` and idle until the user
   submits the setup form (which triggers `esp_restart()`).
3. `tapo_init()` (also restores the persisted energy integrator from NVS).
4. Create `telemetry_safety` task (stack 8192, prio 5).
5. `start_mqtt_client()` immediately (esp-mqtt owns reconnects).

| Task | Stack | Prio | Core |
|------|-------|------|------|
| `telemetry_safety` | 8192 | 5 | floating |

Broker endpoint (fw ≥ 2.3.0): `AMPHIVE_DIRECT_MQTT 1` → default `MQTT_BROKER_URL
"mqtts://mqtt.amphive.app:8883"` — a **DNS name**, so the broker can move
machines by flipping the A record without touching firmware. An optional NVS
key `broker_url` (namespace `storage`, full URI) **overrides** the compiled
default when set; there is no captive-portal field for it (set it manually for
lab brokers). **One-time self-migration:** if the stored `broker_url` contains
a legacy pinned IP (`8.231.81.12` or `100.87.241.70`), boot logs a warning,
erases the key, and uses the DNS default — an OTA alone retargets the fleet.
TLS validation: the broker CA is embedded via `EMBED_TXTFILES
"certs/mqtt_ca.crt"`; esp-tls/mbedTLS verifies the cert chain **and** the URI
host against the cert SANs (DNS SAN `mqtt.amphive.app` for the hostname
default; the cert also keeps the legacy IP SANs so a raw-IP `broker_url` still
validates). No `skip_cert_common_name_check` / `common_name` override is set,
so default hostname verification applies. No date check
(`MBEDTLS_HAVE_TIME_DATE` off — no clock). Fw ≤ 2.2.0 hard-coded
`mqtts://8.231.81.12:8883` and validated the IP SAN. SSID, WiFi password,
device name, gateway id, and MQTT credentials all come from
NVS (namespace `storage`) populated by the captive portal. DNS caveat: the
gateway resolves `mqtt.amphive.app` via the DHCP-provided LAN DNS; if that
resolver is down the connection fails (no compiled-in IP fallback — a raw-IP
`broker_url` in NVS is the manual workaround). Plug IPs are **not**
stored on-device (fw ≥ 2.0.0-direct) — they arrive from the backend's retained
roster (§3).

## 2. Captive portal (✅ implemented; locked down fw 1.6.0; mobile-first wizard 2026-08-02)

If WiFi config is missing or the STA connection fails, the device starts SoftAP
`AmpHive_Setup_XXXX` — **WPA2-protected since fw 1.6.0** (passphrase = the
per-device **setup code**, see below) — and runs `esp_http_server` on
`192.168.4.1` in **AP-only mode** (no STA interface, so the portal is reachable
exclusively via the setup AP).

**Portal UX (2026-08-02 preflashed-unit onboarding rework).** `GET /` serves a
single self-contained page (inline CSS/JS, no external resources — the phone
has no internet on the setup AP) styled as a two-step, mobile-first wizard
(`firmware/main/main.c` `portal_html`, sent via `httpd_resp_send_chunk` so the
one dynamic value — the device's auto-detected ID — is substituted at request
time without re-parsing the rest as a printf format string):
- **Step 1 of 2 — Wi-Fi.** The setup code field, then a **tappable scanned
  network list** (`GET /scan`, see below) with a manual-entry text field as
  the fallback/always-available option (typing directly is never blocked —
  the scan is a convenience, not a requirement). Fields get
  `autocapitalize="none"` — the setup code's alphabet is lowercase and the
  match is case-sensitive, so a phone keyboard's default auto-capitalization
  was a latent "your code is right but it still says wrong" trap this closes.
- **Step 2 of 2 — smart plug.** Tapo account email/password. The per-gateway
  **MQTT password field is optional**, tucked behind an "Installer options"
  toggle — see the preflashed-unit note below.
- Every response (wrong code, Wi-Fi association failure, success) renders
  through a shared `send_status_page()` helper: a big colored heading (green
  for success, red for failure) plus plain-language copy — a visible
  pass/fail state without needing icon glyphs (this file stays pure ASCII;
  see the code comment on why `\u`-escapes in a narrow C string literal
  aren't worth the toolchain gamble).

**Wi-Fi network scan (`GET /scan`, 2026-08-02).** Briefly hops to **AP+STA**
— the same single-radio trade-off as the pre-check below (the AP may drop the
installer's phone for a couple of seconds while the radio visits other
channels) — runs one blocking `esp_wifi_scan_start`, and returns up to 20
networks as JSON (`{"networks":[{"ssid","rssi","secure"}]}`), strongest-first
and deduped by SSID (mesh APs broadcast the same name on multiple BSSIDs). No
STA connection is attempted, so nothing needs tearing down beyond restoring
AP-only mode. Not gated by the setup code: only a client already on the
(WPA2) setup AP can reach it at all, so it adds no exposure beyond joining
the AP already grants (SECURITY.md §8.1).

**Preflashed-unit onboarding (claim-code flow, see docs/API_REFERENCE.md +
deploy/docs/preflashed_unit_runbook.md).** The portal's job stays "get this
device onto Wi-Fi and talking to its smart plug" — binding the gateway to a
CPO's tenant now happens on the backend via a short claim code the CPO types
into the CPO portal (`POST /api/cpo/gateways/claim`), not by an operator
hand-registering the device before shipping. The one field this changes on
the device side is **MQTT password**: a preflashed unit can have its
per-gateway broker password written into NVS at manufacturing time (part of
the runbook's "flash → mint inventory row → create broker account" sequence),
so the portal no longer *requires* it — `save_config_to_nvs` only overwrites
the stored `mqtt_pwd` when the submitted value is non-empty, so an ordinary
buyer who never opens "Installer options" doesn't blank out the
pre-provisioned password.

The form still submits the setup code plus the same 5 config fields as
before (WiFi SSID/password, Tapo account email + password, per-gateway MQTT
password — now optional); `gateway_id`, `device_name`, and `mqtt_user` are
derived from the STA MAC, not typed, and plug IPs come from the backend's
retained roster (§3), not the portal. POST `/save` verifies the setup code
(constant-time compare; wrong code → 1 s throttle + 403, nothing written),
URL-decodes the fields, **pre-checks the Wi-Fi credentials** (see below),
writes them to NVS, and reboots. The Tapo credentials are used by the KLAP
driver's auth hash (see §4).

- **Setup code:** 10 chars from an unambiguous alphabet, generated via
  `esp_random()` the first time the portal runs, persisted in NVS
  (`setup_code`), and printed over serial at **every** portal start — copy it
  onto the unit label. One secret serves as both the AP's WPA2 passphrase and
  the `/save` token (defense-in-depth against other clients on the setup AP).
  After a full NVS erase a new code is generated — re-label the unit.
- **Wi-Fi pre-check (TD#31, fw ≥ 2.1.0-direct):** before committing + rebooting,
  `/save` briefly flips to **AP+STA** and tries to associate to the submitted
  SSID/password (`portal_precheck_wifi`, ≤ 20 s wait), so a typo'd network name
  or password is caught at provisioning instead of at charge time. **Fail-open:**
  only a *definite* association failure (retry-exhausted `WIFI_FAIL_BIT`) blocks
  the save with an error page; success, timeout, or an inconclusive result
  proceeds — the check can never brick provisioning. Limitation: the single
  radio may briefly hop to the STA's channel during the test, dropping the
  installer's phone off the AP for a few seconds. The old "plug IP
  reachability" half of TD#31 is moot — the portal no longer collects plug IPs
  (retained roster, §3).
- **Idle timeout:** the portal reboots the device after **10 min** without HTTP
  activity, so the boot-time Wi-Fi-loss fallback (SECURITY.md §8.4) no longer
  leaves an AP up indefinitely — a provisioned gateway retries its STA link on
  the way back up.

## 3. MQTT control loop & watchdogs

- MQTT starts right after Wi-Fi (TLS to the public broker). Topics per
  [MQTT_CONTRACT.md](MQTT_CONTRACT.md).
- **Per-plug slots (multi-plug, TD#20).** A gateway can drive several plugs. Each
  plug the backend addresses gets a slot in a `MAX_PLUGS`-wide table (guarded by
  `plugs_mutex`): its DB `plug_id` (from the command topic), LAN IP, a **per-plug
  KLAP driver context** (`tapo_plug_t` — its own handshake/session + energy meter,
  so plug B's command can never actuate plug A), and its own session/watchdog
  state. The slot table is built from the backend's **retained plug roster**
  (`/config`, fw ≥ 2.0.0-direct — see [MQTT_CONTRACT.md](MQTT_CONTRACT.md));
  `ON`/`OFF` also carry the target `local_ip` as a live refresh/fallback. A slot
  is **freed** only when the roster drops the plug: `handle_plug_roster` flags it
  and the telemetry task — the sole owner of per-plug KLAP I/O — frees the context
  (`tapo_plug_destroy`) at the top of its next sweep, so a handle is never freed
  mid-call and an **active session is never reaped**.
- **Idle telemetry from boot via the retained roster (fw ≥ 2.0.0-direct).** The
  gateway subscribes to `amphive/gateways/{gw}/config` on connect and builds its
  slot table from the retained roster, so **idle telemetry flows for every plug
  from boot** — keeping the backend's session-start liveness gate fresh before any
  command arrives. This **replaced** the fw-1.7.1 boot-time "provisional slot"
  (which pre-registered a single provisioned `target_plug_ip` under id `1` to fix
  a 1.7.0 liveness regression): with no provisioned plug IP on-device anymore, the
  retained roster does that job for all plugs at once. Crash-recovered sessions
  still repopulate their own slots.
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
  - If MQTT is disconnected: buffers the reading in `offline_log` (NVS ring
    buffer, session-relative `kwh`). Since fw 2.1.0-direct (TD#24) each entry
    also stamps the **owning session id** (compact `uint32_t`) + occupied state
    at capture time, echoed on resync so the backend attributes replayed
    readings to the exact session, not whatever is ACTIVE at reconnect.
  - While a session is active, enforces edge cutoffs in both online and offline modes:
    - **Duration:** elapsed ≥ `max_duration_s` → local OFF + NVS clear. Counts
      **total** elapsed across reboots (TD#23): elapsed-so-far is persisted with
      the session (30 s throttle in the telemetry task + on every state change)
      and recovery back-dates `start_time_s` by it, so a crash can't restart the
      time cap from zero (worst-case overrun ≈ one persist interval).
    - **Energy:** consumed ≥ `max_kwh` → local OFF + NVS clear.
    - **Thermal:** plug reports `overheat_status != "normal"` → local OFF + NVS clear
      + publish `THERMAL_CUTOFF` alarm (if online). (The P110 has no temperature
      sensor, so this uses the plug's own overheat flag rather than a °C threshold.)
    - **Over-current:** plug reports `overcurrent_status != "normal"` → local OFF +
      NVS clear + publish `OVERCURRENT_CUTOFF` alarm. (The plug does the sensing;
      this replaces the previously-unimplemented 13 A/5-min rule.)
    - **Per-plug current cap (REC-03):** measured `current_a` above the session's
      `max_current_a` (+0.5 A margin, 3-poll debounce against inrush) → local OFF
      + `OVERCURRENT_CAP` alarm. The cap arrives on `ON`/`SET_LIMITS`/the roster
      (default `DEFAULT_PLUG_CAP_A` 16 A, NVS-overridable via `plug_cap_a`) and —
      fw ≥ 2.2.0-direct — is persisted in the session blob so crash recovery
      re-arms the session's own cap, not the gateway default.
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
`max_duration_s`, `max_kwh` and `start_energy_kwh` as milliWh integers), plus
`elapsed_s` — seconds elapsed as of the save, re-persisted on a 30 s throttle so
the duration watchdog survives reboots (TD#23, fw 2.1.0) — and `max_current_ma`,
the session's current cap, so recovery re-arms `OVERCURRENT_CAP` at the
session's own threshold (fw 2.2.0). Carrying
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

## 5. Historical: microlink (removed 2026-08-02)

> This section used to document `microlink` — a from-scratch Tailscale-protocol
> client in C (~13.5k LOC, Noise ts2021 handshake, DERP relay, DISCO/STUN NAT
> traversal, a unified magicsock UDP port) plus the vendored `wireguard_lwip`
> WireGuard-over-lwIP component it wrapped. It was the gateway's original
> transport: an overlay VPN to a self-hosted Headscale control plane. It was
> retired by the 2026-07-10 direct-MQTT pivot (defeated by symmetric NAT,
> root-caused 2026-07-09) and kept compilable-but-unused for rollback until
> `firmware/components/microlink/` and `firmware/components/wireguard_lwip/`
> were deleted outright on 2026-08-02 — the linker map showed zero objects
> pulled from either archive under the shipped `AMPHIVE_DIRECT_MQTT=1` build.
> See git history from before that date for the implementation.

## 6. Build config (`sdkconfig.defaults`)

- **Target hardware: ESP32-C3, no PSRAM.** `sdkconfig.defaults` sets
  `CONFIG_ESPTOOLPY_FLASHSIZE_4MB` and has **no `CONFIG_SPIRAM*` lines** — the
  real fielded gateways have no PSRAM to configure. (An earlier revision of
  this doc described an ESP32-S3-N16R8 target with octal PSRAM and 16 MB
  flash; that was never what's in `sdkconfig.defaults` and has been corrected
  here.)
- **Partition table:** `CONFIG_PARTITION_TABLE_CUSTOM` → `partitions_ota.csv`
  (2026-07-07) — **dual OTA app slots** (`ota_0`/`ota_1`, 1920 KB each) +
  `otadata`, with `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE`. NVS keeps its
  pre-OTA offset (`0x9000`) so provisioning survives the one-time migration
  reflash. The ~1.1 MB image uses ~55% of a slot. (Was
  `SINGLE_APP_LARGE` — no OTA — before this.)
- mbedTLS TLS 1.2 + full cert bundle (for MQTT-broker TLS and HTTPS OTA). lwIP IPv4-only.
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
- Main task stack 32768.

## 7. Build & flash

```bash
cd firmware
idf.py set-target esp32c3
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

A working **demo/prototype**, not production firmware: `microlink` was deep and
mostly functional (with unified magicsock NAT traversal fully operational) before
being **retired by the 2026-07-10 direct-MQTT pivot and removed from the tree
2026-08-02** (see §5), the captive portal and watchdogs work,
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

**fw `1.5.0-direct` (historical milestone) — OTA'd + verified on the real gateway
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

**Current: fw `2.3.0-direct`** (`firmware/CMakeLists.txt` `PROJECT_VER`). Fw has
advanced well past 1.5.0 through many small, individually-verified jumps — DNS-based
broker addressing with legacy-IP self-migration (§1), the multi-plug refactor
(TD#20) with a backend-pushed retained plug roster replacing captive-portal plug
IPs, per-plug current caps (REC-03) persisted across crash recovery, a Wi-Fi
pre-check at provisioning (TD#31), and WARN/ERROR log forwarding over MQTT
(TD#28) — none of which change the overall shape described in §§1–4 above. See
[IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md) for the version-by-version
matrix and on-device verification history.
