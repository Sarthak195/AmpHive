# AmpHive — MQTT Contract

*The exact topic/payload contract between the FastAPI backend
(`backend/services/mqtt_manager.py`) and the ESP32 gateway firmware
(`firmware/main/main.c`). Verified 2026-06-20 — firmware and backend agree.*

- **Broker:** Eclipse Mosquitto 2.0, plain MQTT (no TLS). Confidentiality comes
  from the overlay tunnel, not from MQTT. The broker is **anonymous** today.
- **Backend client id:** `amphive_backend_server` (paho-mqtt v2, `VERSION2`).
- **Gateway broker URL (firmware):** hard-coded `mqtt://100.64.0.1:1883`
  (the server's overlay IP). The MQTT client is started lazily once the overlay
  reaches `CONNECTED`/`MONITORING`.
- **Namespace prefix:** `amphive/`.

---

## Topics

| Direction | Topic | QoS | Retained | Payload |
|-----------|-------|-----|----------|---------|
| backend → gateway | `amphive/gateways/{gateway_id}/plugs/{plug_id}/commands` | 1 | no | `{"action":"ON"\|"OFF","max_duration_seconds":<int>,"max_kwh":<float>}` OR `{"action":"SET_INTERVAL","interval_ms":<int>}` |
| gateway → backend | `amphive/gateways/{gateway_id}/telemetry` | 0 | no | `{"plug_id":<int>,"watts":<f>,"kwh":<f>,"voltage":<f>,"current":<f>,"status":"occupied"\|"available"}` |
| gateway → backend | `amphive/gateways/{gateway_id}/status` | 1 | yes | `{"status":"online"}` (on connect) / `{"status":"offline"}` (LWT) |
| gateway → backend | `amphive/gateways/{gateway_id}/alarms` | 1 | no | `{"error":"THERMAL_CUTOFF"}` |

The backend subscribes with wildcards: `amphive/gateways/+/telemetry` (QoS 0)
and `amphive/gateways/+/status` (QoS 1). It does **not** currently subscribe to
`/alarms`.

> **Note on topic shape:** telemetry is published **per-gateway**
> (`.../{gateway_id}/telemetry`) with `plug_id` inside the JSON body — *not* the
> per-plug `.../plugs/{plug_id}/telemetry` shape some product docs describe.
> Firmware and backend both use the per-gateway form, so they are consistent;
> only `requirements.md` is out of date.

---

## Command publishing (backend)

- `MQTTManager.send_plug_command(gateway_id, plug_id, action, max_duration=14400, max_kwh=30.0)`
  publishes to the command topic at QoS 1 and `wait_for_publish(timeout=3.0)`,
  returning `is_published()`. `/api/sessions/start` returns HTTP 500 if this fails;
  `/api/sessions/stop` ignores the result (best-effort OFF).
- `MQTTManager.send_plug_interval(gateway_id, plug_id, interval_ms)`
  publishes the `SET_INTERVAL` command at QoS 1 to configure the gateway's telemetry reporting interval.

## Inbound handling (backend) — ⚠️ stubbed

`_handle_gateway_telemetry` and `_handle_gateway_status` currently **only log**.
They do **not** write to the database or feed the `TelemetryStore`. The DB-update
code is commented out, and `MQTTManager` is never given a `db_session_factory`.

Consequences:
- The live SSE stream (`/api/sessions/live/{id}`) never receives real power/energy
  values over the ESP32/MQTT path — it emits only the `starting`/`completed`
  placeholder snapshots created by session start/stop.
- `/api/sessions/stop` reads `telemetry_store.get_latest()`, which is the
  `starting` snapshot (0 kWh / 0 coins) in the pure MQTT path, so completed
  sessions record 0 energy/cost unless another path populated the store.

There is **no Last Will & Testament configured on the backend client**; the
LWT/`offline` message is published by the *gateway* firmware.

## Firmware side (summary)

The ESP32 publishes `online` status (retained) + subscribes to its commands on
connect; runs a dynamically adjustable telemetry/watchdog loop (default 10 seconds,
updated via `SET_INTERVAL` between 500ms and 60000ms); parses commands with
`strstr`/`sscanf` (not a JSON parser); and enforces local safety cutoffs
(duration, energy, 75 °C thermal → publishes the thermal alarm). See
[FIRMWARE.md](FIRMWARE.md).

## To make Path A fully functional

1. Implement `_handle_gateway_telemetry` to parse the payload and call
   `telemetry_store.update(...)` (and optionally persist to the DB).
2. Pass a `db_session_factory` into `MQTTManager` if DB persistence is wanted.
3. ~~Replace the firmware Tapo driver stub with a real KLAP implementation~~ **Done**
   — the driver is now real KLAP v2, so `watts` is the plug's real `current_power`
   and `kwh` is a driver-side energy integrator (voltage/current are nominal/derived;
   the P110 reports neither). On-device flash verification pending. See
   [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md) and
   [FIRMWARE.md §4](FIRMWARE.md#4-tapo-driver--real-klap-v2-maintapo_protocolc).
</content>
