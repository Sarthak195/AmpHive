# AmpHive — MQTT Contract

*The exact topic/payload contract between the FastAPI backend
(`backend/services/mqtt_manager.py`) and the ESP32 gateway firmware
(`firmware/main/main.c`). Verified 2026-06-20 — firmware and backend agree.*

- **Broker:** Eclipse Mosquitto 2.0, plain MQTT (no TLS). Confidentiality comes
  from the overlay tunnel, not from MQTT. **Auth is enforced** (2026-07-07):
  `allow_anonymous false` + passwd file (backend + gateway accounts, generated
  by `deploy.ps1`); the firmware authenticates with NVS `mqtt_user`/`mqtt_pwd`
  — see [SECURITY.md §3](SECURITY.md).
- **Backend client id:** `amphive_backend_server` (paho-mqtt v2, `VERSION2`).
- **Gateway broker URL (firmware):** hard-coded `mqtt://100.64.0.1:1883`
  (the server's overlay IP). The MQTT client is started lazily once the overlay
  reaches `CONNECTED`/`MONITORING`.
- **Namespace prefix:** `amphive/`.

---

## Topics

| Direction | Topic | QoS | Retained | Payload |
|-----------|-------|-----|----------|---------|
| backend → gateway | `amphive/gateways/{gateway_id}/plugs/{plug_id}/commands` | 1 | no | `{"action":"ON"\|"OFF","max_duration_seconds":<int>,"max_kwh":<float>,"session_id":"<str>"}` OR `{"action":"SET_INTERVAL","interval_ms":<int>}` |
| gateway → backend | `amphive/gateways/{gateway_id}/telemetry` | 0 | no | `{"plug_id":<int>,"watts":<f>,"kwh":<f>,"voltage":<f>,"current":<f>,"status":"occupied"\|"available","session_id":"<str>"}` |
| gateway → backend | `amphive/gateways/{gateway_id}/status` | 1 | yes | `{"status":"online"}` (on connect) / `{"status":"offline"}` (LWT) |
| gateway → backend | `amphive/gateways/{gateway_id}/alarms` | 1 | no | `{"error":"THERMAL_CUTOFF"}` |

The backend subscribes with wildcards: `amphive/gateways/+/telemetry` (QoS 0)
and `amphive/gateways/+/status` (QoS 1). It does **not** currently subscribe to
`/alarms`.

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

> **Note on topic shape:** telemetry is published **per-gateway**
> (`.../{gateway_id}/telemetry`) with `plug_id` inside the JSON body — *not* the
> per-plug `.../plugs/{plug_id}/telemetry` shape some product docs describe.
> Firmware and backend both use the per-gateway form, so they are consistent;
> only `requirements.md` is out of date.

---

## Command publishing (backend)

- `MQTTManager.send_plug_command(gateway_id, plug_id, action, max_duration=14400, max_kwh=30.0, session_id=None)`
  publishes to the command topic at QoS 1 and `wait_for_publish(timeout=3.0)`,
  returning `is_published()`. `/api/sessions/start` passes `session_id=session.id`
  on `ON` and returns HTTP 500 if the publish fails; `/api/sessions/stop` omits
  `session_id` and ignores the result (best-effort OFF).
- `MQTTManager.send_plug_interval(gateway_id, plug_id, interval_ms)`
  publishes the `SET_INTERVAL` command at QoS 1 to configure the gateway's telemetry reporting interval.

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

Status messages update the gateway's `status`/`last_seen_at` in the DB. The
backend does **not** subscribe to `/alarms`.

There is **no Last Will & Testament configured on the backend client**; the
LWT/`offline` message is published by the *gateway* firmware.

## Firmware side (summary)

The ESP32 publishes `online` status (retained) + subscribes to its commands on
connect; runs a dynamically adjustable telemetry/watchdog loop (default 10 seconds,
updated via `SET_INTERVAL` between 500ms and 60000ms); parses commands with
**cJSON** (topic/data buffers 256/512 B, oversized/fragmented payloads dropped);
and enforces local safety cutoffs (duration, energy, thermal/over-current via the
plug's status flags → publishes the alarm). See [FIRMWARE.md](FIRMWARE.md).

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
