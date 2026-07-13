# AmpHive — MQTT Contract

*The exact topic/payload contract between the FastAPI backend
(`backend/services/mqtt_manager.py`) and the ESP32 gateway firmware
(`firmware/main/main.c`). Verified 2026-06-20 — firmware and backend agree.*

- **Broker:** Eclipse Mosquitto 2.0. Two listeners:
  - **`mqtts://8.231.81.12:8883` — PUBLIC, the primary transport** (2026-07-10,
    "direct MQTT"): devices/agents dial **outbound** TLS to the VM's static IP,
    validating the broker cert (SANs carry both IPs) against the embedded
    AmpHive CA. Outbound-only traversal — works behind symmetric NAT/CGNAT with
    no overlay, STUN, or port-forwards.
  - **`mqtt://100.87.241.70:1883` — overlay-only, legacy/transition**: reachable
    only over the WireGuard overlay; also the backend's path (internal Docker
    network). Retire per-device once migrated to 8883.
- **Auth is enforced** on both listeners: `allow_anonymous false` + passwd file,
  plus **topic ACLs** (2026-07-10): every device has its **own** account
  (username == gateway_id, added via `deploy/scripts/add_gateway_user.ps1`),
  confined by `pattern readwrite amphive/gateways/%u/#` to its own subtree; the
  backend account has `amphive/#`. The shared `amphive-gateway` account and its
  broad grant were **retired 2026-07-10**. The firmware authenticates with NVS
  `mqtt_user`/`mqtt_pwd` — see [SECURITY.md §3](SECURITY.md).
- **Backend client id:** `amphive_backend_server` (paho-mqtt v2, `VERSION2`).
- **Gateway broker URL (firmware):** `AMPHIVE_DIRECT_MQTT=1` (default, fw ≥
  1.3.0) hard-codes `mqtts://8.231.81.12:8883`, started right after Wi-Fi.
  The legacy overlay build (`AMPHIVE_DIRECT_MQTT=0`) uses
  `mqtt://100.87.241.70:1883`, started lazily once the overlay reaches
  `CONNECTED`/`MONITORING`.
- **Namespace prefix:** `amphive/`.

---

## Topics

| Direction | Topic | QoS | Retained | Payload |
|-----------|-------|-----|----------|---------|
| backend → gateway | `amphive/gateways/{gateway_id}/plugs/{plug_id}/commands` | 1 | no | `{"action":"ON"\|"OFF","max_duration_seconds":<int>,"max_kwh":<float>,"session_id":"<str>","local_ip":"<str>"}` OR `{"action":"SET_LIMITS","max_kwh":<float>,"max_duration_seconds":<int>,"local_ip":"<str>"}` OR `{"action":"SET_INTERVAL","interval_ms":<int>}` OR `{"action":"OTA","url":"<http(s)>"}` |
| gateway → backend | `amphive/gateways/{gateway_id}/telemetry` | 0 | no | `{"plug_id":<int>,"watts":<f>,"kwh":<f>,"voltage":<f>,"current":<f>,"relay":<bool>,"status":"occupied"\|"available","session_id":"<str>"}` |
| gateway → backend | `amphive/gateways/{gateway_id}/status` | 1 | yes | `{"status":"online","fw":"<ver>"}` (on connect) / `{"status":"offline"}` (LWT) |
| gateway → backend | `amphive/gateways/{gateway_id}/alarms` | 1 | no | `{"error":"THERMAL_CUTOFF"\|"OVERCURRENT_CUTOFF"\|"UNAUTHORIZED_ON","plug_id":<int>}` or `{"event":"OTA_STARTED"\|"OTA_OK_REBOOTING"\|"OTA_FAILED"\|"OTA_REFUSED_SESSION_ACTIVE"\|...}` |
| agent → backend | `amphive/gateways/{gateway_id}/discovery` | 1 | no | `{"unique_id":"<str>","provider":"<str>","model":"<str>","alias":"<str>","capabilities":["switch","power","energy"]}` |
| backend → agent | `amphive/gateways/{gateway_id}/assign` | 1 | yes | `{"<unique_id>":<plug_id:int>, ...}` (full map for the gateway) |

The backend subscribes with wildcards: `amphive/gateways/+/telemetry` (QoS 0),
`amphive/gateways/+/status` (QoS 1), `amphive/gateways/+/discovery` (QoS 1),
and `amphive/gateways/+/alarms` (QoS 1). Alarm/event messages are persisted as
`gateway_events` rows (tenant resolved from the gateway) and broadcast to
clients via the `gateway_alarm` Socket.io event; a CPO reads them through
`GET /api/cpo/events` and clears them with `POST /api/cpo/events/{id}/ack`.

The `relay` field (firmware ≥ 1.5.0) is the plug's **actual** reported relay
state (`device_on`), distinct from `status` (the gateway's own session state).
It lets the backend/UI show the physical relay and underpins the firmware's
`UNAUTHORIZED_ON` guard: a relay ON with no active session (physical button /
Tapo app / stale NVS resume) is forced OFF locally and alarmed.

> **`/discovery` + `/assign` (software gateways only).** These two topics belong
> to the **AmpHive Agent** ([AMPHIVE_AGENT.md](AMPHIVE_AGENT.md)) — a software
> gateway that adopts non-AmpHive plugs (Kasa/Tapo, Shelly, …). The **ESP32
> firmware does not use them.** plug_id is **backend-authoritative**: the MQTT
> `plug_id` in every command/telemetry payload *is* the global DB `plugs.id`
> (`_persist_telemetry` looks up `Plug.id == plug_id`), so an agent must never
> invent local ids. Instead it announces each discovered device by a stable
> `unique_id` (brand-scoped, MAC-derived — stable across reboots/IP changes) on
> `/discovery`; the backend upserts a `Plug` keyed by `(gateway_id, unique_id)`,
> letting the DB assign `plugs.id`, then publishes the **retained** full
> `{unique_id: plug_id}` map on `/assign`. The agent adopts those ids and only
> then starts publishing telemetry under them. Retained so a restarted/late agent
> re-learns its ids immediately. Discovery for an **unclaimed** gateway (no
> `gateways` row) is dropped — claim the gateway first.

> **`kwh` is session-relative.** The telemetry `kwh` field is energy consumed
> **this session** (`meter − session_baseline` on the firmware), **not** the
> plug's lifetime meter. The backend bills it directly (`kwh × COINS_PER_KWH`),
> so the firmware subtracts the baseline captured at `ON`; publishing the raw
> lifetime integrator re-bills the plug's whole history every session (fixed
> 2026-07-06 in `firmware/main/main.c`, **verified on-device the same day** —
> consecutive sessions each started at `kwh: 0.0000` on the wire). Idle (no
> active session) reports `0`.

> **`session_id` round-trip.** On `ON` the backend includes the DB session id as
> a **string** (`send_plug_command(..., session_id=…)`); the firmware persists it
> for crash recovery and echoes it in every telemetry payload (empty string when
> idle). The backend attributes a reading to that exact session — guarded so it
> must still be `ACTIVE` and on the same plug — falling back to "the active
> session on this plug" when the id is empty/absent (e.g. pre-`session_id`
> firmware). The **offline-resync** path (`resync_offline_logs`) does *not* carry
> `session_id` (the compact NVS ring-buffer entry has no room), so replayed
> readings still attribute by `plug_id`.

> **`SET_LIMITS` re-caps a running session without re-baselining.** To change a
> live session's watchdog thresholds (energy + duration caps) mid-charge, the
> backend sends `SET_LIMITS` — **not** a second `ON`. The firmware's `ON` handler
> re-reads the meter baseline on *every* `ON`, so re-sending `ON` mid-session
> would reset `start_energy_kwh` and re-bill from zero. `SET_LIMITS` updates
> **only** `max_kwh` and `max_duration_s` (then re-persists to NVS) and leaves
> `start_energy_kwh`, `start_time_s`, `session_active`, and `session_id`
> untouched, so accumulated billing is unaffected. It is a **no-op when no
> session is active** on the addressed plug (logged and ignored). `local_ip`
> targets the physical plug on a multi-plug gateway (TD#20), exactly as ON/OFF.

> **`local_ip` targets the plug (multi-plug, TD#20).** One ESP32 gateway can
> drive several P110s, so ON/OFF carry the target plug's LAN IP (the backend
> stores it as `plugs.local_ip` and now ships it on every ON/OFF —
> `send_plug_command(..., local_ip=…)`). The firmware keeps a **per-plug** slot
> (its own KLAP session + energy meter) keyed by the topic's `plug_id`, binding
> that slot's IP from the payload — so a command for plug B can no longer actuate
> plug A, and telemetry is published under each plug's own id. The device learns
> a plug it hasn't seen (e.g. after a reboot) from the command itself, so no
> static plug roster is shipped on-device (SECURITY.md §8.5). `local_ip` is
> **absent** on `SET_INTERVAL` (the poll cadence is gateway-wide) and `OTA`
> (gateway-scoped). Older single-plug firmware ignores the field and falls back
> to its one provisioned target plug, so adding it is backward-safe. On gateway
> reconnect the backend re-sends OFF (now with `local_ip`) to every idle plug,
> which is also how a freshly-booted gateway re-learns all its plugs.

> **Note on topic shape:** telemetry is published **per-gateway**
> (`.../{gateway_id}/telemetry`) with `plug_id` inside the JSON body — *not* the
> per-plug `.../plugs/{plug_id}/telemetry` shape some product docs describe.
> Firmware and backend both use the per-gateway form, so they are consistent;
> only `requirements.md` is out of date.

---

## Command publishing (backend)

- `MQTTManager.send_plug_command(gateway_id, plug_id, action, max_duration=14400, max_kwh=30.0, session_id=None, local_ip=None, wait=True)`
  publishes to the command topic at QoS 1 and `wait_for_publish(timeout=3.0)`,
  returning `is_published()`. `/api/sessions/start` passes `session_id=session.id`
  and `local_ip=plug.local_ip` on `ON` and returns HTTP 500 if the publish fails;
  `/api/sessions/stop` (via `finalize_charging_session`) omits `session_id`,
  passes `local_ip=plug.local_ip`, and ignores the result (best-effort OFF).
  `wait=False` (event-loop callers, e.g. the reconnect OFF republish) skips the
  blocking broker-ack wait.
- `MQTTManager.send_plug_interval(gateway_id, plug_id, interval_ms)`
  publishes the `SET_INTERVAL` command at QoS 1 to configure the gateway's telemetry reporting interval.
- `MQTTManager.send_plug_limits(gateway_id, plug_id, max_kwh, max_duration_seconds, local_ip=None)`
  publishes the `SET_LIMITS` command at QoS 1 (blocking `wait_for_publish(timeout=3.0)`,
  returns `is_published()`) to re-cap a **running** session's energy/duration
  watchdog thresholds without re-baselining (see the `SET_LIMITS` note above).
  Intended to be wired **best-effort** into `PATCH /api/sessions/{id}/limits`.
- `MQTTManager.send_gateway_ota(gateway_id, plug_id, firmware_url)`
  publishes an `OTA` command at QoS 1. Triggered by
  `POST /api/cpo/gateways/{id}/ota` (RBAC + tenant-scoped; requires the
  gateway `ONLINE` — the status flag, not telemetry freshness, so a gateway
  whose plug is unreachable can still be updated — and to have ≥1 plug, since
  the firmware only subscribes to the per-plug command topic, so the OTA
  command rides one of the gateway's plug topics and the firmware ignores the
  plug_id for OTA). The gateway
  downloads the image into its passive OTA slot and reboots
  (rollback-protected); it refuses the update while a session is active.
  The `url` must be reachable by the **gateway**: for direct-MQTT devices
  (fw ≥ 1.3.0) that means a **public** URL, and fw ≥ 1.3.1 validates
  `https://` hosts against the built-in Mozilla CA bundle — prefer HTTPS
  (plain HTTP is accepted but MITM-able on the public internet). Verified
  over the direct path 2026-07-10 (`1.3.0 → 1.3.1`, public host, slot swap +
  rollback-cancel).

## Inbound handling (backend) — live

`_handle_gateway_telemetry` and `_handle_gateway_status` are **wired to the app**
(`lifespan` in `main.py` gives `MQTTManager` a `telemetry_store`,
`db_session_factory`, `event_loop`, and `telemetry_persistence`). On each
telemetry message the handler:
1. Feeds the in-memory `TelemetryStore` (drives the live Socket.io stream and
   the running cost). The update is **marshaled onto the event loop** via
   `loop.call_soon_threadsafe` — the handler runs on the paho network thread and
   `asyncio.Event.set()` (used to wake stream consumers) is not thread-safe
   cross-thread (fixed 2026-07-06; regression test in `tests/test_mqtt_manager.py`).
2. Enqueues a raw sample for the `telemetry_readings` time-series table
   (buffered batch-flush in `services/telemetry_persistence.py`).
3. Persists authoritative session totals (`energy_kwh`, `peak_power_w`) to the
   active `charging_sessions` row and `current_power_w` to the plug.

Status messages update the gateway's `status`/`last_seen_at` in the DB. Alarm
messages (`/alarms`) are ingested by `_handle_gateway_alarm` → persisted as
`gateway_events` and broadcast as the `gateway_alarm` Socket.io event
(2026-07-10).

There is **no Last Will & Testament configured on the backend client**; the
LWT/`offline` message is published by the *gateway* firmware.

## Firmware side (summary)

The ESP32 connects **directly over TLS to the public broker**
(`mqtts://8.231.81.12:8883`, firmware ≥ 1.3.0, `AMPHIVE_DIRECT_MQTT=1`) as
soon as Wi-Fi is up — no overlay; esp-mqtt owns reconnection — validating the
broker cert against the embedded self-signed CA (chain + IP SAN; dates
unchecked, no clock needed). (The legacy `AMPHIVE_DIRECT_MQTT=0` build keeps
the microlink overlay + plaintext 1883 for comparison/rollback.) It publishes
`online` status (retained, with its `fw` version) + subscribes to its commands
on connect; runs a dynamically adjustable
telemetry/watchdog loop (default 10 seconds, updated via `SET_INTERVAL`
between 500ms and 60000ms); parses commands with **cJSON** (topic/data
buffers 256/512 B, oversized/fragmented payloads dropped); enforces local
safety cutoffs (duration, energy, thermal/over-current via the plug's status
flags → publishes the alarm); and applies `OTA` updates via `esp_https_ota`
into a dual app-slot layout with bootloader rollback (refuses mid-session;
cancels rollback once it re-reaches the broker). See [FIRMWARE.md](FIRMWARE.md).

## Path A status

Path A has been exercised **end-to-end on physical hardware**: a real ESP32 +
P110 ran a billed session over MQTT with real telemetry. The remaining items are
verification-and-polish, not missing plumbing:

1. ~~Implement `_handle_gateway_telemetry`~~ **Done** — feeds `TelemetryStore`,
   persists session totals, and enqueues time-series samples (see above).
2. ~~Pass a `db_session_factory` into `MQTTManager`~~ **Done** (via `lifespan`).
3. ~~Replace the firmware Tapo driver stub with a real KLAP implementation~~ **Done**
   — the driver is now real KLAP v2, so `watts` is the plug's real `current_power`
   and `kwh` is a driver-side energy integrator (voltage/current are nominal/derived;
   the P110 reports neither).
4. ~~Firmware reported the raw lifetime energy integrator, so every session after
   the first overbilled~~ **Fixed + verified on-device 2026-07-06** — telemetry
   reports session-relative `kwh` (see the `kwh` note above); `mosquitto_sub` on
   the live broker showed `"kwh":0.0000` at each session start with the
   `session_id` echoed, and consecutive billed sessions (#77–79) each billed only
   their own energy.

See [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md) and
[FIRMWARE.md §4](FIRMWARE.md#4-tapo-driver--real-klap-v2-maintapo_protocolc).
