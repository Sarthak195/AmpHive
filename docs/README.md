# AmpHive Documentation

This folder is the **technical reference** for the AmpHive platform, and the
**single source of truth** for how the system works today. Unlike the root-level
[`requirements.md`](../requirements.md) and [`features_list.md`](../features_list.md)
— which are *product/aspirational* specifications — the documents here describe
**what the code actually does today**, verified against the source on 2026-07-02.

> The older root-level "map" files (`architecture.md`, `api-map.md`, `routes.md`,
> `database-map.md`, `dependency-graph.md`, `memory.md`) have been **superseded by
> this folder**; they now contain only redirect pointers here. Update the docs in
> this folder — not those — when behaviour changes.

> When a doc here and a product spec disagree, the docs here win for "current
> behaviour"; the product spec wins for "intended direction". The
> [Implementation Status](IMPLEMENTATION_STATUS.md) page reconciles the two.

## Index

| Doc | What it covers |
|-----|----------------|
| [ARCHITECTURE.md](ARCHITECTURE.md) | End-to-end system architecture, the retired overlay/Direct-Mode paths, and how a charging session flows through the stack. |
| [API_REFERENCE.md](API_REFERENCE.md) | Every backend REST endpoint (see that doc for the current count), request/response shapes, and auth requirements. |
| [DATA_MODEL.md](DATA_MODEL.md) | PostgreSQL tables, SQLAlchemy models, enums, relationships, and the schema-vs-ORM drift. |
| [DEPENDENCIES.md](DEPENDENCIES.md) | Backend/frontend/firmware import graphs, package dependencies, high-impact files, and known dead code. |
| [MQTT_CONTRACT.md](MQTT_CONTRACT.md) | The exact MQTT topic/payload contract between the backend and the ESP32 gateway. |
| [FIRMWARE.md](FIRMWARE.md) | ESP32-C3 firmware: boot flow, tasks, watchdogs, and the Tapo KLAP v2 driver (transport is direct MQTT/TLS; the retired `microlink` Tailscale-protocol client is documented as history only). |
| [ESP32_CONNECTION.md](ESP32_CONNECTION.md) | Build, flash, and monitor guide for the ESP32 gateway firmware, including toolchain version pins and other-board notes. |
| [PLUG_CONNECTIVITY_OPTIONS.md](PLUG_CONNECTIVITY_OPTIONS.md) | Survey of every viable plug-to-server connectivity path versus today's ESP32 gateway, and a recommendation for a plug-and-play client. |
| [AMPHIVE_AGENT.md](AMPHIVE_AGENT.md) | A Home-Assistant-style local hub that discovers multi-brand smart plugs on a client's LAN and bridges them into the existing MQTT contract. |
| [DEPLOYMENT.md](DEPLOYMENT.md) | How the system is deployed (GCP VM + Docker Compose), the helper scripts, and the K8s manifests. |
| [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md) | Honest "what works / what's a stub / what's aspirational" matrix and the full list of doc-vs-code discrepancies. |
| [SECURITY.md](SECURITY.md) | Committed secrets, the open MQTT broker, auth gaps, and remediation notes. |
| [MAINTAINER_GUIDE.md](MAINTAINER_GUIDE.md) | Operational "how to work on and run this" guide for whoever owns the repo next. |
| [PRICING_V2_SPEC.md](PRICING_V2_SPEC.md) | Time-of-day tariffs and forward-only segmented billing design spec — all 4 phases shipped and live in prod. |
| [TECH_DEBT.md](TECH_DEBT.md) | Prioritized technical-debt register (cause / impact / effort / priority). |
| [MARKET_GAP_ANALYSIS.md](MARKET_GAP_ANALYSIS.md) | Competitive gap analysis against commercial EV-charging apps — a product view, distinct from the engineering-debt view in TECH_DEBT.md. |
| [TESTING.md](TESTING.md) | Current test coverage across the stack and a prioritized testing roadmap. |
| [TODO.md](TODO.md) | Prioritized improvement roadmap (immediate → long term). |
| [proposals/](proposals/) | Design proposals for individual features (e.g. faster gateway-offline detection, queued charging on an offline plug), each tagged with its own shipped/pending status. |
| [audits/](audits/) | Point-in-time audit reports (e.g. the 2026-07-14 reconciliation & billing-divergence audit) with per-finding resolution status. |

## Project map (one-liner per component)

- **`backend/`** — FastAPI app (REST routes across 9 routers in `routers/`, schemas in `schemas.py`, assembly-only `main.py` — see [API_REFERENCE](API_REFERENCE.md) for the current route count), SQLAlchemy 2.0 + async PostgreSQL with Alembic migrations, authenticated MQTT bridge, Razorpay payments, and Socket.io live telemetry with time-series persistence (90-day retention). See [API_REFERENCE](API_REFERENCE.md) / [DATA_MODEL](DATA_MODEL.md).
- **`frontend/`** — React 19 + Vite SPA (driver web app). Login/register, plug-ID charging, live Socket.io session monitor, Razorpay top-up, charger groups. See [ARCHITECTURE](ARCHITECTURE.md#frontend).
- **`firmware/`** — ESP32-C3 (ESP-IDF) gateway. Direct MQTT/TLS transport + control loop + safety watchdogs, with a **real KLAP v2** Tapo plug driver (the retired `microlink` Tailscale client was removed 2026-08-02). See [FIRMWARE](FIRMWARE.md).
- **`deploy/`** — Docker Compose (dev/prod), GCP deploy scripts, K8s manifests, Mosquitto configs, and runbooks. See [DEPLOYMENT](DEPLOYMENT.md).
- **`tools/`** — Standalone Python helpers for manual Tapo plug on/off testing (`turn_on.py`, `turn_off.py`, `local_tapo_test.py`, `klap_probe.py`).
