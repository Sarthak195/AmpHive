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
| [ARCHITECTURE.md](ARCHITECTURE.md) | End-to-end system architecture, the two operating modes (ESP32/MQTT vs Direct Mode), and how a charging session flows through the stack. |
| [API_REFERENCE.md](API_REFERENCE.md) | Every backend REST endpoint (all 22), request/response shapes, and auth requirements. |
| [DATA_MODEL.md](DATA_MODEL.md) | PostgreSQL tables, SQLAlchemy models, enums, relationships, and the schema-vs-ORM drift. |
| [DEPENDENCIES.md](DEPENDENCIES.md) | Backend/frontend/firmware import graphs, package dependencies, high-impact files, and known dead code. |
| [MQTT_CONTRACT.md](MQTT_CONTRACT.md) | The exact MQTT topic/payload contract between the backend and the ESP32 gateway. |
| [FIRMWARE.md](FIRMWARE.md) | ESP32-S3 firmware: boot flow, tasks, watchdogs, the Tapo KLAP v2 driver, and the `microlink` Tailscale-protocol client. |
| [DEPLOYMENT.md](DEPLOYMENT.md) | How the system is deployed (GCP VM + Docker Compose), the helper scripts, and the K8s manifests. |
| [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md) | Honest "what works / what's a stub / what's aspirational" matrix and the full list of doc-vs-code discrepancies. |
| [SECURITY.md](SECURITY.md) | Committed secrets, the open MQTT broker, auth gaps, and remediation notes. |
| [reference/](reference/) | Background notes on the upstream projects AmpHive builds on (ESP32-Tailscale-WoL firmware, Headscale control server). |

## Project map (one-liner per component)

- **`backend/`** — FastAPI app (single `main.py`, 22 REST routes), SQLAlchemy 2.0 + async PostgreSQL, MQTT bridge, Razorpay payments, in-memory telemetry/SSE, and a "Direct Mode" Tapo driver. See [API_REFERENCE](API_REFERENCE.md) / [DATA_MODEL](DATA_MODEL.md).
- **`frontend/`** — React 19 + Vite SPA (driver web app). Login/register, plug-ID charging, live SSE session monitor, Razorpay top-up, charger groups. See [ARCHITECTURE](ARCHITECTURE.md#frontend).
- **`firmware/`** — ESP32-S3 (ESP-IDF) gateway. A near-complete from-scratch Tailscale client (`microlink`) + MQTT control loop + safety watchdogs, with a **real KLAP v2** Tapo plug driver. See [FIRMWARE](FIRMWARE.md).
- **`deploy/`** — Docker Compose (dev/prod), GCP deploy scripts, K8s manifests, Mosquitto/WireGuard configs, and runbooks. See [DEPLOYMENT](DEPLOYMENT.md).
- **`tools/`** — Standalone Python helpers for the Direct-Mode Tapo relay and manual on/off testing.
</content>
</invoke>
