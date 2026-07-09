# AmpHive Agent

A Home-Assistant-style **local hub**: discovers smart plugs of many brands on the
LAN and pushes telemetry to the AmpHive cloud over **outbound MQTT/TLS**, speaking
the existing [MQTT contract](../docs/MQTT_CONTRACT.md) as a *software gateway* —
so the backend treats it exactly like an ESP32 gateway (zero backend change for
the data path).

Design: [`docs/AMPHIVE_AGENT.md`](../docs/AMPHIVE_AGENT.md). Where it fits among
the alternatives: [`docs/PLUG_CONNECTIVITY_OPTIONS.md`](../docs/PLUG_CONNECTIVITY_OPTIONS.md).

## Install

```bash
cd agent
pip install -e ".[all]"     # or ".[kasa]", ".[shelly]", or bare for sim-only
```

## Run

Configure via env (see [`.env.example`](.env.example)), then:

```bash
python -m amphive_agent        # or: amphive-agent
```

### Try it with no hardware (sim provider)

```bash
export AMPHIVE_GATEWAY_ID=agent-lab-01
# Production broker: public TLS listener; validate against the AmpHive CA.
export AMPHIVE_BROKER=8.231.81.12 AMPHIVE_USE_TLS=true
export AMPHIVE_CA_FILE=../deploy/config/mqtt-certs/ca.crt
# Per-gateway creds (USER == gateway_id; created by deploy/scripts/add_gateway_user.ps1):
export AMPHIVE_MQTT_USER=agent-lab-01 AMPHIVE_MQTT_PASS=<pass>
export AMPHIVE_PROVIDERS=sim AMPHIVE_SIM_COUNT=2
python -m amphive_agent

# watch it on the broker (needs credentials with rights to this subtree):
mosquitto_sub -t 'amphive/gateways/agent-lab-01/#' -v
```

The agent announces each plug on `.../discovery` and then **waits for the backend
to assign a plug_id** on the retained `.../assign` topic before it publishes
telemetry or accepts commands (plug_id is backend-authoritative — it's the DB
`plugs.id`). With the real backend running this is automatic. To drive the sim
standalone, play the backend yourself — assign `sim:1 → 7`, then command plug 7:

```bash
# assign a plug_id (retained), then ON it and watch session-relative kWh climb
mosquitto_pub -r -t 'amphive/gateways/agent-lab-01/assign' -m '{"sim:1":7}'
mosquitto_pub -t 'amphive/gateways/agent-lab-01/plugs/7/commands' \
  -m '{"action":"ON","session_id":"demo-1"}'
```

### Real plugs

```bash
# Kasa + Tapo (Tapo needs TP-Link creds for local KLAP auth)
export AMPHIVE_PROVIDERS=kasa TPLINK_USER=you@example.com TPLINK_PASS=...

# Shelly Gen2+ (local HTTP RPC)
export AMPHIVE_PROVIDERS=shelly AMPHIVE_SHELLY_HOSTS=192.168.1.50,192.168.1.51

# both at once
export AMPHIVE_PROVIDERS=kasa,shelly
```

## What it speaks (recap)

| Direction | Topic | Payload |
|---|---|---|
| in  | `amphive/gateways/{gw}/plugs/{id}/commands` | `{"action":"ON"\|"OFF"\|"SET_INTERVAL",...}` |
| out | `amphive/gateways/{gw}/telemetry` | `{"plug_id","watts","kwh","voltage","current","status","session_id"}` |
| out | `amphive/gateways/{gw}/status` | `{"status":"online","fw":"agent-0.1"}` / LWT `offline` |
| out | `amphive/gateways/{gw}/discovery` | `{"unique_id","provider","model","alias","capabilities"}` |
| in  | `amphive/gateways/{gw}/assign` | retained `{"<unique_id>": <plug_id>, ...}` |

`plug_id` is **backend-authoritative**: the agent identifies devices by a stable
`unique_id`, announces them on `/discovery`, and adopts the ids the backend hands
back on the retained `/assign` map (persisted locally, so it survives restarts).
`kwh` is **session-relative** (baseline captured at `ON`), matching the firmware
so the backend bills correctly. See [`docs/AMPHIVE_AGENT.md`](../docs/AMPHIVE_AGENT.md).

## Add a provider

Implement `PlugProvider` + `PlugDevice` (see `amphive_agent/model.py`), drop the
module in `amphive_agent/providers/`, and register it in `providers/__init__.py`.
`sim.py` is the smallest example; `shelly.py` shows a real HTTP ecosystem.
