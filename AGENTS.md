# AGENTS.md — working in the AmpHive repo

Guidance for AI agents and developers. **Read [`docs/`](docs/) first** — it is the
single source of truth for how the system works today, verified against source.
Start with [docs/IMPLEMENTATION_STATUS.md](docs/IMPLEMENTATION_STATUS.md) (what
actually works vs. stub/aspirational) and [docs/SECURITY.md](docs/SECURITY.md)
(open gaps).

## What AmpHive is

A shared EV-charging PaaS that turns off-the-shelf TP-Link Tapo P110 smart plugs
into a monetizable charging network: a FastAPI backend + React SPA in the cloud,
ESP32-C3 gateways at each site, connected by **direct MQTT** — gateways dial
outbound to `mqtts://mqtt.amphive.app:8883` (public), authenticated by TLS plus
per-gateway username/password and topic ACLs. Firmware builds with
`AMPHIVE_DIRECT_MQTT=1`; there is no VPN/overlay hop. (A Headscale/WireGuard
overlay and a separate "Direct Mode" WireGuard-relay path both existed earlier
in the project and are retired; the `tapo_direct` / `/direct/*` backend code
remains but is dormant.) See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Where things live

| Area | Path | Reference |
|------|------|-----------|
| Backend (FastAPI, routes in `backend/routers/*.py`) | `backend/` | [API_REFERENCE](docs/API_REFERENCE.md) · [DATA_MODEL](docs/DATA_MODEL.md) |
| Frontend (React 19 + Vite, driver + CPO portal) | `frontend/` | [ARCHITECTURE](docs/ARCHITECTURE.md#4-frontend) |
| Firmware (ESP-IDF, ESP32-C3) | `firmware/` | [FIRMWARE](docs/FIRMWARE.md) |
| Deploy (compose, K8s, configs, runbooks) | `deploy/` | [DEPLOYMENT](docs/DEPLOYMENT.md) |
| Ops helper scripts (VM start/stop, remote logs) | `scripts/` | [DEPLOYMENT](docs/DEPLOYMENT.md#helper-scripts-scripts) |
| Direct-Mode Tapo helpers (run on home PC) | `tools/` | [DEPLOYMENT](docs/DEPLOYMENT.md) |
| Import graphs / high-impact files | — | [DEPENDENCIES](docs/DEPENDENCIES.md) |

## Hard rules

1. **Don't run the app stack or a database on this Windows box.** It's a dev
   workstation — no local `docker compose up` of the full stack, no local
   Postgres, no DB migrations or seeds against a local database. *Operating the
   GCP VM from this box is allowed* (updated 2026-07-05): `deploy/scripts/deploy.ps1`,
   `gcloud compute ssh amphive-vm-in`, and secret-rotation commands act on the
   VM/providers, not the local machine, and are fine. **Confirm before any
   destructive or irreversible action** — DB wipe/migration on the live VM,
   `git push --force`, or deleting a firewall rule.
2. **Deploy via the script.** Backend/frontend changes ship through
   `deploy/scripts/deploy.ps1`, which uploads code and rebuilds containers on the
   VM. See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).
3. *(Historical, no longer applies)* ESP32 stack sizing for the `microlink` VPN
   task's large PSRAM-backed stack — that overlay client is retired and
   compiled out (the esp32c3 target has no external PSRAM anyway).
4. *(Historical, no longer applies)* Headscale config validation — the
   Headscale/WireGuard overlay is retired; there is no Headscale config left
   to validate.
5. **Secrets.** Do not commit new secrets; several already-committed ones need
   rotation — see [docs/SECURITY.md](docs/SECURITY.md). App `.env` files are
   gitignored.

## Documentation policy

`docs/` is the source of truth — **keep it, not scattered copies, up to date.**

- When behaviour changes, update the relevant file in `docs/` and record any
  works/stub/aspirational change in [docs/IMPLEMENTATION_STATUS.md](docs/IMPLEMENTATION_STATUS.md).
- Log real infrastructure commands run against the VM in the appropriate
  `deploy/docs/` runbook.
- Do **not** create parallel top-level "map" documents. The old root files
  (`architecture.md`, `api-map.md`, `routes.md`, `database-map.md`,
  `dependency-graph.md`, `memory.md`) are superseded and now only redirect into
  `docs/`; that duplication is what previously caused the docs to drift.
- Product vision lives in [requirements.md](requirements.md) and
  [features_list.md](features_list.md); the gap between vision and code lives in
  [docs/IMPLEMENTATION_STATUS.md](docs/IMPLEMENTATION_STATUS.md).
