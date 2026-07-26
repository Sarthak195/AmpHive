# Plug → Server Connectivity Options

> **Status (2026-07-26): the recommendation below to drop the overlay for direct
> outbound MQTT/TLS already SHIPPED** — the direct-MQTT pivot landed 2026-07-10
> (`AMPHIVE_DIRECT_MQTT=1`, the default; see [FIRMWARE.md](FIRMWARE.md) and
> [MQTT_CONTRACT.md](MQTT_CONTRACT.md)). The Tailscale/WireGuard overlay
> (`microlink`) referenced throughout this doc is retired and compiled out of
> the default build. The rest of this document — the Tuya/Shelly/Matter/Kasa
> comparison research and the option catalog — remains useful reference for
> future connectivity/plug-brand decisions; only the "current state" framing
> below (§ context, §4, §recommendation) is now historical.

How can a smart plug's data (state + energy) reach the AmpHive server? This
surveys every viable path, documents the two vendor-API repos we pulled into
`research/`, and gives a recommendation for a **plug-and-play** client install.

> Context: today AmpHive uses an **ESP32 gateway** that talks to **Tapo** plugs
> locally (KLAP handshake) and pushes telemetry over MQTT to the cloud broker.
> The gateway↔cloud link rides a custom Tailscale/WireGuard overlay, which is
> where the reliability pain lives (symmetric-NAT traversal — see
> `docs/` overlay notes). The question here: are there simpler ways to get plug
> data to the server, ideally with **no hardware from us and no gateway**?

---

## Two decision axes

Every option is a combination of **where the integration code runs** and **how
it reaches the plug**:

| Where it runs | How it reaches the plug |
|---|---|
| On the plug's firmware (native) | Plug speaks a protocol directly (MQTT / Matter) |
| On a **local hub** on the client LAN (Pi / mini-PC / the ESP32) | Local LAN protocol (Kasa/Tapo local, Tuya local) |
| In **our cloud backend** (no client hardware) | Vendor **cloud API** (poll/push) |

"Plug-and-play with no hardware" pushes you toward **cloud APIs** or
**self-reporting plugs** (Matter/Shelly-MQTT). "Local, private, offline-capable"
pushes you toward a **hub** or the **ESP32 gateway**.

---

## The two repos you found

### `research/tuyapi` — local Tuya control (Node.js)

The well-known [`codetheweb/tuyapi`](https://github.com/codetheweb/tuyapi). Talks
to **Tuya-ecosystem** plugs (sold under hundreds of brands) **over the LAN**
(TCP port 6668), not the cloud.

- **API:** `new TuyAPI({id, key})` → `find()` → `connect()` → `get()/set()` +
  `data`/`dp-refresh` events. State is a `dps` map (e.g. `dps['1']` = on/off).
- **What it needs:** the device `id` **and a per-device local `key`** that must be
  extracted from the Tuya cloud/app (and changes every time the plug is re-paired).
- **Constraints:** one TCP connection at a time (conflicts with the phone app);
  no sensor support; must be on the **same LAN** as the plug.
- **AmpHive verdict:** only useful if we want to support **cheap Tuya-brand
  plugs**, and only via a **local hub** running Node on the client's network. The
  local-key extraction is a real onboarding wall — **not plug-and-play by itself**.
  There's a ready-made [`tuya-mqtt`](https://github.com/TheAgentK/tuya-mqtt) bridge
  that turns tuyapi into MQTT, which could feed our broker from a hub.

### `research/tplink-cloud-api` — cloud Kasa/Tapo control (Python)

Controls TP-Link **Kasa + Tapo** devices through **TP-Link's V2 cloud API** — so
**our backend can reach the plugs from anywhere, no LAN presence, no gateway**.

- **API:** `TPLinkDeviceManager(user, password)` (dual-logs-in to Kasa + Tapo
  clouds, HMAC-SHA1 signed) → `get_devices()` / `find_device(alias)` →
  `power_on()/power_off()/toggle()`, schedules, and — for **Kasa e-meter plugs** —
  `get_power_usage_realtime()`. Handles MFA and refresh tokens.
- **⚠️ Energy nuance (verified in the code):** energy monitoring is implemented
  **only for Kasa e-meter devices** — `HS110`, `KP115`, `KP125`, `HS300` outlets
  (`emeter_device.py`). **Tapo devices (incl. P110) get on/off only** through the
  generic class — there is no `p110.py`. So this library gives us **Tapo on/off
  but not Tapo energy over the cloud.**
- **Constraints:** it's a **reverse-engineered** API (not an official TP-Link
  partner API) — subject to breakage and ToS; requires storing the client's
  **TP-Link account credentials**; cloud-poll latency + undocumented rate limits.
- **AmpHive verdict:** **high value for onboarding.** A client who already uses
  Tapo/Kasa can link their account and we read/actuate their plugs with **zero
  hardware and zero firmware** — the ultimate plug-and-play trial. But because it
  **doesn't expose Tapo energy**, for our energy-monitoring core we'd either (a)
  steer clients to **Kasa KP115/KP125** (cloud energy works), or (b) keep the
  **local** path (ESP32 or python-kasa) for Tapo energy. Credential storage and
  API-fragility make it better as an **onboarding/fallback tier** than the sole
  data path.

---

## The full option catalog

### 1. Vendor cloud API — reverse-engineered (backend-only, no hardware)
`tplink-cloud-api` (Kasa/Tapo), `tuyapi` cloud / [`tinytuya`](https://github.com/jasonacox/tinytuya) cloud.
- ✅ No client hardware, works from our cloud, fast to ship, great for onboarding.
- ❌ Unofficial (can break), stores user creds, poll-based, per-vendor, ToS risk.

### 2. Vendor cloud API — **official** (backend-only, sanctioned)
**Tuya IoT Development Platform**: a real developer program with REST APIs **and a
real-time message push** (Pulsar / MQTT) for device status + energy. Requires a
Tuya cloud project and the user linking their Tuya app account to it.
- ✅ Sanctioned, real-time push (not just polling), energy metering documented.
- ❌ Tuya-only; onboarding requires account-linking; setup overhead.
- Note: **TP-Link Kasa/Tapo has no official public API** — which is exactly why
  `tplink-cloud-api` has to reverse-engineer it.

### 3. Local LAN via a hub (client-side hardware, no vendor cloud)
[`python-kasa`](https://github.com/python-kasa/python-kasa) (Kasa **and Tapo**,
**incl. P110 energy** via `get_energy_data`), `tinytuya`/`tuyapi`/
[`LocalTuya`](https://github.com/rospogrigio/localtuya) (Tuya).
- ✅ Local, private, offline-capable, low latency, **real Tapo energy**.
- ❌ Needs a hub (Raspberry Pi / mini-PC / the ESP32) on each client LAN; per-vendor.
- This is essentially **what the ESP32 gateway already does** for Tapo — a Pi
  running python-kasa + an MQTT publisher is the "software hub" version of it.
- **This is the Home-Assistant-style path — see [AMPHIVE_AGENT.md](AMPHIVE_AGENT.md)**
  for a concrete multi-brand agent design (plugin interface, MQTT discovery
  schema, and a `python-kasa`→MQTT proof of concept).

### 4. Our ESP32 gateway (current)
ESP32 → local Tapo KLAP → MQTT → cloud.
- ✅ Full control, our hardware/brand, works with Tapo energy, we own the stack.
- ❌ The connectivity work (drop the overlay, outbound MQTT/TLS — see
  the plug-and-play firmware recommendation) + per-unit cost + provisioning.

### 5. Matter / Thread plugs (self-reporting, local-first)
Matter **1.3** added electrical-energy-measurement clusters; plugs like **Kasa
KP125M**, **Meross MSS315**, **Eve Energy** report power/kWh **locally** through a
Matter controller — no vendor cloud.
- ✅ Standard (not per-vendor), local, future-proof, no account/creds.
- ❌ Needs a **Matter controller/border router** on the client side; energy-cluster
  support is still **uneven** across plugs/controllers in 2026.

### 6. Shelly (official local **MQTT** + HTTP + cloud) — the "product" sweet spot
Shelly plugs (e.g. **Plus Plug S**, **Plus 1PM**) have a **first-class, documented
local API**: they can be told to **publish directly to *our* MQTT broker** (plus a
local HTTP/RPC API and an official cloud). Energy monitoring built in.
- ✅ **The plug talks MQTT straight to our server — no gateway, no reverse-
  engineering, no vendor cloud, local + real-time.** Closest thing to true
  plug-and-play for an energy product.
- ❌ Different plug brand than Tapo; client sets the broker/creds during setup
  (can be templated/QR-guided).

---

## Comparison

| Option | Client HW | Vendor cloud dep. | Real-time | Energy data | Official | Plug-and-play |
|---|---|---|---|---|---|---|
| `tplink-cloud-api` (Kasa/Tapo cloud) | none | **yes** | poll | Kasa only | ✗ | ★★★★ |
| Official Tuya Cloud (push) | none | yes | **push** | ✓ | ✓ | ★★★ |
| `tuyapi`/`python-kasa` on a hub | hub | no | local | ✓ (incl. Tapo) | ✗ | ★★ |
| ESP32 gateway (current) | our HW | no* | local | ✓ | n/a | ★★ |
| Matter/Thread plug | controller | no | local | ✓ (uneven) | ✓ | ★★★ |
| **Shelly → our MQTT** | plug only | no | **push** | ✓ | ✓ | ★★★★ |

\* the ESP gateway needs *some* path to our cloud; that's the overlay/outbound-MQTT work.

---

## Recommendation for AmpHive plug-and-play

Think in **tiers**, not one winner:

1. **Fastest onboarding / "no box" trial → `tplink-cloud-api`.** Let a client link
   their existing Kasa/Tapo account; our backend reads/controls their plugs from
   the cloud. Zero hardware, zero firmware. Caveats: steer energy users to **Kasa
   KP115/KP125** (cloud energy works; Tapo energy does **not** via this lib), store
   creds carefully (MFA + encrypted-at-rest + refresh-token, never the raw
   password), and treat it as a tier that can break with TP-Link changes.

2. **Cleanest hardware plug-and-play → Shelly plugs publishing MQTT to our
   broker.** No gateway, no reverse-engineering, official + local + real-time
   energy. If we're willing to bless a plug brand, this is the strongest product
   path and reuses the broker we already run.

3. **If we stay on Tapo + our ESP32 gateway** (full control, our brand): the
   connectivity fix is the earlier recommendation — **drop the overlay, connect
   the ESP outbound over MQTT/TLS to a public broker** (flip `MQTT_USE_TLS` back
   on). That removes the NAT/overlay fragility entirely.

4. **Broadest device coverage later:** add an **official Tuya Cloud** integration
   (real-time push) for Tuya-brand plugs, and/or a **Matter** path as energy
   clusters mature — both are sanctioned and reduce per-vendor reverse-engineering.

A pragmatic build order: ship **(1)** as a soft-launch onboarding tier now (it's
just a backend service around `tplink-cloud-api`), pilot **(2) Shelly-MQTT** as
the reference hardware, and keep **(3)** as the owned-hardware track once the
outbound-MQTT firmware change lands.

---

## Sources

- [tuyapi (codetheweb)](https://github.com/codetheweb/tuyapi) · [tuya-mqtt bridge](https://github.com/TheAgentK/tuya-mqtt) · [tinytuya](https://github.com/jasonacox/tinytuya) · [LocalTuya](https://github.com/rospogrigio/localtuya)
- [tplink-cloud-api (PyPI)](https://pypi.org/project/tplink-cloud-api/) · [python-kasa](https://github.com/python-kasa/python-kasa) · [python-kasa docs](https://python-kasa.readthedocs.io/en/latest/)
- [Tuya IoT developer platform](https://developer.tuya.com/en/docs/iot/introduction-to-tuya-iot-platform?id=Ka6vijvqb3uhn) · [Tuya energy metering](https://developer.tuya.com/en/docs/iot/smart-plug-with-energy-monitor?id=Kaiuz8hje2r6j) · [Tuya cloud capabilities](https://developer.tuya.com/en/cloud-capabilities)
- [Matter smart plugs that report energy](https://matter-smarthome.de/en/practice/which-matter-smart-plugs-report-energy-data/) · [Matter plugs for Home Assistant 2026](https://jpk.io/home-automation/matter-smart-plugs-home-assistant-2026/) · [Best energy-monitoring smart plugs 2026](https://www.logix4u.net/best-smart-plugs-with-energy-monitoring/)
