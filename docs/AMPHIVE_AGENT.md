# AmpHive Agent — a Home-Assistant-style local hub

A lightweight local service that discovers smart plugs of **many brands** on a
client's LAN, normalizes them, and pushes telemetry to the AmpHive cloud over
**outbound MQTT/TLS** — using the *existing* [MQTT contract](MQTT_CONTRACT.md).

It's the Home Assistant pattern (local hub + a plugin per ecosystem) minus HA's
weight, and it reuses the **same underlying libraries HA uses** so we don't
re-implement vendor protocols. See [PLUG_CONNECTIVITY_OPTIONS.md](PLUG_CONNECTIVITY_OPTIONS.md)
(Route A) for where this sits among the alternatives.

## Why this shape
- **Gets all the sensors** — one plugin per ecosystem (Kasa/Tapo, Shelly, Tuya,
  Matter), each mapping devices into one common model. Add a brand = add a plugin.
- **Solves the Tapo-energy gap** — local `python-kasa` reads **P110 energy**,
  which the unofficial *cloud* API can't (see PLUG_CONNECTIVITY_OPTIONS.md).
- **Solves the NAT/overlay pain** — the hub dials **outbound** to the broker
  (exactly how HA's Nabu Casa remote access works). No overlay, no port-forward.
- **Reuses the core path** — the agent registers as a *software gateway* and
  speaks the existing telemetry/status/command contract, so the backend treats it
  like any ESP32 gateway. It adds two topics (`/discovery` + `/assign`) for
  brand-agnostic plug adoption; the backend now consumes them
  ([implemented](MQTT_CONTRACT.md)).

> **Status (2026-07-09):** implemented as a runnable package under
> [`agent/`](../agent) (providers: `kasa`, `shelly`, `sim`) and verified
> end-to-end against a real broker — discovery → backend assign → adopt →
> session-relative telemetry. The backend side (`_persist_plug_discovery` +
> `/assign`) is wired and unit-tested (`backend/tests/test_mqtt_manager.py`).

## Relationship to the ESP32 gateway
The ESP32 gateway is a single-brand hub (Tapo, in C). The Agent is the same idea
generalized: a **multi-brand software gateway** on a Pi-class box (Raspberry Pi,
mini-PC, NAS, or a Docker container on any always-on machine the client has). Both
present to the backend as a `gateway_id` with `plug_id`s underneath. A client can
run either; the ESP stays the cheap single-brand option.

```
   Client LAN                                        AmpHive Cloud
 ┌───────────────────────────────────────┐        ┌────────────────────┐
 │  AmpHive Agent (Python)                │        │                    │
 │                                        │        │                    │
 │  providers/                            │  MQTT  │   Mosquitto        │
 │   ├─ kasa   (python-kasa)  ┐           │  TLS   │   broker (8883)    │
 │   ├─ shelly (aioshelly)    ├─► core ───┼───────►│        │           │
 │   ├─ tuya   (tinytuya)     │   ├ discovery      │        ▼           │
 │   └─ matter (matter-server)┘   ├ poll   │        │   FastAPI backend  │
 │                                ├ command│◄───────┤  (unchanged for    │
 │  plug_id ↔ device map (persisted)      │        │   telemetry/status)│
 └───────────────────────────────────────┘        └────────────────────┘
      local protocols, on-LAN            outbound-only, survives any NAT
```

---

## Provider (plugin) interface

Each ecosystem is a plugin implementing two small protocols. The core knows
nothing about vendors — only these shapes.

```python
from typing import Protocol, runtime_checkable

class PlugState:
    on: bool
    watts: float            # instantaneous power
    energy_kwh: float       # lifetime/cumulative kWh (0.0 if unsupported)
    voltage: float          # 0.0 if unsupported
    current: float          # 0.0 if unsupported

@runtime_checkable
class PlugDevice(Protocol):
    unique_id: str          # STABLE, brand-scoped, e.g. "kasa:AA:BB:CC:DD:EE:FF"
    model: str
    alias: str
    capabilities: set[str]  # subset of {"switch", "power", "energy"}
    async def get_state(self) -> PlugState: ...
    async def set_power(self, on: bool) -> None: ...

@runtime_checkable
class PlugProvider(Protocol):
    name: str               # "kasa" | "shelly" | "tuya" | "matter"
    async def discover(self) -> list[PlugDevice]: ...
```

Design rules:
- `unique_id` must be **stable across reboots and IP changes** (MAC-based), so a
  plug keeps its `plug_id` mapping. Never key off IP.
- Providers are **fail-isolated**: one plugin throwing (or a brand's cloud dep
  being down) must not stop the others.
- Capabilities drive the payload: a plug without `energy` reports `kwh: 0` and
  `status` still works.

---

## Discovery & plug_id assignment (backend-authoritative)

**plug_id is the backend's to assign.** The MQTT `plug_id` in every command and
telemetry payload *is* the global DB `plugs.id` (`_persist_telemetry` looks up
`Plug.id == plug_id`). An agent therefore **cannot invent local ids** — it would
collide with real DB rows and mis-bill. Instead it identifies devices by a stable
`unique_id` and lets the backend hand back the authoritative id:

1. **LAN discovery** (finding devices) — each provider does its own: `python-kasa`
   uses UDP broadcast + KLAP, `aioshelly`/Matter use mDNS/zeroconf, `tinytuya`
   scans + local keys. The core runs `provider.discover()` on an interval.
2. **Announce** — for each newly-seen `unique_id` **without** a stored assignment,
   the agent publishes a (non-retained) announcement:

   ```
   amphive/gateways/{gateway_id}/discovery            (QoS 1)
   {"unique_id":"kasa:AA:BB:..","provider":"kasa","model":"KP115",
    "alias":"Bay 3 Charger","capabilities":["switch","power","energy"]}
   ```
3. **Assign** — the backend (`_persist_plug_discovery`) upserts a `Plug` keyed by
   `(gateway_id, unique_id)` — the DB assigns `plugs.id` — then publishes the
   **retained** full `{unique_id: plug_id}` map for that gateway:

   ```
   amphive/gateways/{gateway_id}/assign               (retained, QoS 1)
   {"kasa:AA:BB:..": 42, "shelly:..": 43}
   ```
   Discovery for an **unclaimed** gateway (no `gateways` row) is dropped — the
   gateway must be claimed first, exactly as an ESP gateway is.
4. **Adopt** — the agent stores each `unique_id → plug_id` locally (JSON, so it
   survives restarts), moves the device from `pending` to active under the
   assigned id, and only **then** starts publishing telemetry for it. On restart
   it adopts persisted ids immediately (and re-learns from the retained `/assign`
   if the local store is lost).

This keeps the whole system single-authority for `plug_id` while letting the
agent discover any brand. The operator UI still surfaces the auto-populated plug
to bind pricing/records — but the id itself is never guessed by the edge.

---

## MQTT protocol

The agent reuses the [existing contract](MQTT_CONTRACT.md) **verbatim** for the
data path, and adds a discovery/assign handshake.

| Direction | Topic | QoS | Ret. | Payload | Status |
|---|---|---|---|---|---|
| backend → agent | `amphive/gateways/{gw}/plugs/{plug_id}/commands` | 1 | no | `{"action":"ON"\|"OFF",...}` / `SET_LIMITS` / `SET_INTERVAL` / `OTA` | **existing** |
| agent → backend | `amphive/gateways/{gw}/telemetry` | 0 | no | `{"plug_id","watts","kwh","voltage","current","status","session_id"}` | **existing** |
| agent → backend | `amphive/gateways/{gw}/status` | 1 | yes | `{"status":"online","fw":"agent-x"}` / LWT `{"status":"offline"}` | **existing** |
| agent → backend | `amphive/gateways/{gw}/discovery` | 1 | no | `{"unique_id","provider","model","alias","capabilities"}` | **new** |
| backend → agent | `amphive/gateways/{gw}/assign` | 1 | yes | `{"<unique_id>": <plug_id>, ...}` | **new** |

**Energy semantics — must match the firmware.** Per the contract, telemetry `kwh`
is **session-relative** (energy *this session*, billed directly), not the plug's
lifetime meter. So the agent, exactly like the ESP:
- on `ON`: capture `baseline_kwh = device.energy_kwh` and store the backend's
  `session_id` **and the local watchdog limits** (`max_kwh` /
  `max_duration_seconds` from the payload; persisted, so a restart mid-session
  keeps them); mark the plug `occupied`.
- each poll: publish `kwh = max(0, device.energy_kwh − baseline_kwh)`, echo
  `session_id`, `status:"occupied"` — **and run the local watchdog**: at
  `session kwh ≥ max_kwh` or elapsed `≥ max_duration_seconds` the agent cuts the
  plug OFF itself (LAN-local `set_power(False)`, so it works with the broker
  unreachable — no unbilled offline tail beyond the limit). The trip frame goes
  out pre-watchdog (occupied + final kwh, like the firmware), then a QoS-1
  `{"event":"LOCAL_LIMIT_CUTOFF","reason":"ENERGY_LIMIT"|"DURATION_LIMIT","plug_id"}`
  alarm is queued on `/alarms` (paho delivers it on reconnect). A plug with no
  cumulative meter still gets an energy limit via a watts×dt integration
  fallback.
- `SET_LIMITS` re-caps a **running** session's `max_kwh`/`max_duration_s`
  without re-baselining (no-op when idle) — same semantics as the firmware
  (MQTT_CONTRACT.md).
- on `OFF` / idle: `kwh: 0`, `status:"available"`, `session_id:""`.

Tests: `agent/test_local_limits.py` (stub device + stub broker, incl. the
broker-down cutoff) and the `python -m amphive_agent.core` self-checks.

`OTA` is a no-op for the agent (it self-updates via its own package channel);
reply on `/alarms` with `OTA_REFUSED` or just ignore. `SET_INTERVAL` adjusts the
poll period (clamped, like the firmware's 500 ms–60 s).

---

## The runnable module — [`agent/`](../agent)

The single-file PoC is now a package. Layout:

```
agent/
  pyproject.toml            # amphive-agent, console entry point
  README.md, .env.example
  amphive_agent/
    __main__.py             # `python -m amphive_agent`
    config.py               # Config.from_env()
    model.py                # PlugState + PlugDevice/PlugProvider protocols
    core.py                 # AmpHiveAgent: MQTT + discover/poll/command loops
    store.py                # persisted unique_id→plug_id + session baselines (JSON, atomic)
    providers/
      __init__.py           # build_providers() — lazy per-provider imports
      kasa.py               # python-kasa (Kasa + Tapo P110 energy, local)
      shelly.py             # aioshelly (Gen2 RPC)
      sim.py                # hardware-free fake plugs (integrates energy over real time)
```

`core.AmpHiveAgent` implements the flow above: `_discover_loop` announces on
`/discovery` (or adopts a persisted assignment); `_handle_assignment` consumes the
retained `/assign` map and promotes `pending → devices` under the backend id;
`_poll_loop` publishes **session-relative** telemetry only for adopted devices.

Run it (the `sim` provider needs no hardware):
```bash
pip install -e agent            # or: pip install "python-kasa>=0.7" "paho-mqtt>=2.0"
export AMPHIVE_GATEWAY_ID=agent-lab-01
export AMPHIVE_BROKER=8.231.81.12                       # public TLS listener
export AMPHIVE_CA_FILE=deploy/config/mqtt-certs/ca.crt  # AmpHive CA (self-signed broker)
export AMPHIVE_PROVIDERS=kasa    # or: sim  (comma-separate to combine)
# Per-gateway creds — USER must equal the gateway_id (broker ACLs scope each
# account to its own subtree). Created by deploy/scripts/add_gateway_user.ps1.
export AMPHIVE_MQTT_USER=agent-lab-01 AMPHIVE_MQTT_PASS=...
export TPLINK_USER=you@example.com TPLINK_PASS=...   # needed for Tapo local KLAP
python -m amphive_agent
# watch it land on the broker:
# mosquitto_sub -t 'amphive/gateways/agent-lab-01/#' -v
```

**Verified** end-to-end against a real broker with `AMPHIVE_PROVIDERS=sim`:
online → `/discovery` → backend `/assign {sim:1: 7}` → adopt → `available`
telemetry on plug_id 7 → `ON` (occupied, session id echoed, climbing session
kWh) → `OFF` (available) → offline LWT; the `unique_id→plug_id` assignment
persisted across the run.

---

## From module to product
- **More providers:** add `providers/tuya.py` (`tinytuya`, needs local keys),
  `providers/matter.py` (`python-matter-server`) — each implementing
  `PlugProvider`; register in `providers/__init__.build_providers()`.
- **Credentials:** TP-Link creds are used **locally** (KLAP), not sent to their
  cloud — better than the cloud API, and energy works. Store encrypted at rest.
- **Packaging:** a [`Dockerfile`](../agent/Dockerfile) ships it as a container
  (`docker build -t amphive-agent --build-arg EXTRAS=all agent/`); `pip install`
  / `pipx` also work (console script `amphive-agent`). State is a `/state`
  volume; use `--network host` on Linux so LAN discovery (kasa/shelly) can
  broadcast and receive replies. Auto-reconnect is built in (paho backoff).
- **Provisioning:** the agent needs its `gateway_id` + broker creds — same
  manual flow as an ESP gateway (`deploy/docs/gateway_provisioning.md`): the
  operator picks a `gateway_id`, mints a per-gateway broker account with
  `add_gateway_user.ps1`, and sets `AMPHIVE_MQTT_USER/PASS` + `AMPHIVE_CA_FILE`.
  Plugs then auto-populate via `/discovery` → `/assign`.

## Provider roadmap

| Provider | Library | Brands | Energy | Status |
|---|---|---|---|---|
| `kasa` | `python-kasa` | Kasa + **Tapo (P110)** | ✓ local | **implemented** (Tapo needs TP-Link creds for local KLAP) |
| `shelly` | `aioshelly` | Shelly | ✓ local | **implemented** (Gen2 RPC) |
| `sim` | — | fake plugs | ✓ synthetic | **implemented** (hardware-free testing) |
| `tuya` | `tinytuya`/LocalTuya | Tuya (many brands) | ✓ local | planned — needs per-device local key extraction |
| `matter` | `python-matter-server` | any Matter plug | ✓ (1.3) | planned — needs a Matter controller/border router |

## Open decisions
1. ~~**`plug_id` authority**~~ **Resolved: backend-authoritative.** The MQTT
   `plug_id` *is* the DB `plugs.id`, so the agent announces `unique_id` on
   `/discovery` and adopts the backend's `/assign` map. The backend consumes the
   discovery topic (`_persist_plug_discovery`).
2. **Agent vs ESP overlap** — **Resolved: operational separation + future
   auto-dedupe.** The ESP gateway and the Agent are *alternative* gateways for a
   given plug; a client uses one or the other per physical device. Running both
   against the **same** plug is unsupported — it double-represents the device
   (the ESP drives it by its `plugs.local_ip` with no stored MAC; the Agent
   discovers it as `unique_id "kasa:<MAC>"` — different `plugs` rows), so it
   could double-bill. Automatic dedupe isn't possible today because ESP-driven
   plug rows don't record the device MAC. Future enhancement: store the plug MAC
   on ESP-driven plugs and have `_persist_plug_discovery` skip/merge a discovery
   whose MAC matches an existing plug. Until then, the operator must not register
   the same physical plug under both.
3. ~~**Backend discovery support**~~ **Resolved: implemented.** The backend
   subscribes `amphive/gateways/+/discovery`, upserts by `(gateway_id, unique_id)`,
   and publishes the retained `/assign` map.
