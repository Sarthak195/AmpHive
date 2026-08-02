# Provisioning a new AmpHive gateway (direct-MQTT)

> **Preparing a batch of preflashed units for retail/self-serve buyers
> instead of a single custom site install?** See
> [preflashed_unit_runbook.md](preflashed_unit_runbook.md) (claim-code
> onboarding, 2026-08-02) — same captive portal and `gateways` table, but the
> operator mints an unclaimed inventory row + claim code instead of doing
> step A below, and the buyer binds it to their own tenant later via the CPO
> portal. This doc (custom site install, operator does every step) is still
> the right flow for a one-off/lab gateway.

How to bring a new gateway online for a client site. Applies to the **ESP32
gateway** (firmware ≥ 1.3.2) and, with the credential steps only, to the
**AmpHive Agent** software gateway.

Design context: the transport is outbound MQTT/TLS to the public broker
(no overlay) and auth is a **per-gateway broker account** (username ==
`gateway_id` == the device MAC) scoped by ACL to `amphive/gateways/<id>/#`.
See [../../docs/SECURITY.md §3](../../docs/SECURITY.md) and
[../../docs/MQTT_CONTRACT.md](../../docs/MQTT_CONTRACT.md).

> Provisioning is **manual and infrequent** by design — no auto claim-code /
> dynsec / JWT machinery (a poor fit for the clockless ESP; see the decision
> note at the bottom). The steps below are the whole flow.

---

## What the installer needs on site

Nothing pre-computed. The device tells you its own `gateway_id`.

1. Flash stock firmware (`idf.py -p <PORT> flash`, or ship pre-flashed).
2. Power on. With no config it starts a Wi-Fi AP **`AmpHive_Setup_XXXX`**.
3. Join that AP, open `http://192.168.4.1`. The page shows the
   **auto-detected Gateway ID** (the device MAC, e.g. `1cc3abb4fb54`) — this is
   the value the operator needs. The firmware derives it from the hardware, so
   it is never typed.

The setup form now asks for only:

| Field | Notes |
|-------|-------|
| Wi-Fi SSID / Password | the site network |
| Tapo Account Email / Password | for the local KLAP handshake to the plug |
| **MQTT Password** | the per-gateway broker password from step B below |

`gateway_id`, `device_name`, and the MQTT **username** are all derived from the
MAC — not entered. **Plug IPs are no longer entered here** (fw ≥ 2.0.0-direct):
the operator registers each plug (with its LAN IP) in the backend (CPO dashboard
/ `POST /api/cpo/plugs`), and the gateway receives the full plug roster over MQTT
(retained `amphive/gateways/{gw}/config` topic). (Tailscale/overlay fields are
gone.)

---

## Operator steps (once per gateway)

Do these from a dev workstation with `gcloud` access. You need the `gateway_id`
(the MAC shown on the setup page or the device sticker).

**A. Create the gateway record** (CPO dashboard, or the API):

```bash
# gateway_id == the MAC shown in the setup portal; vpn_ip is legacy/optional.
curl -X POST https://<backend>/api/cpo/gateways \
  -H "Authorization: Bearer <cpo-token>" -H "Content-Type: application/json" \
  -d '{"gateway_id":"1cc3abb4fb54","name":"Client Site A - Bay 1"}'
```

**B. Mint the per-gateway broker credential** (choose a strong password; it goes
into the device's MQTT Password field and nowhere else):

```powershell
$pw = python -c "import secrets; print(secrets.token_urlsafe(21))"
.\deploy\scripts\add_gateway_user.ps1 -GatewayId 1cc3abb4fb54 -Password $pw
Write-Host "MQTT password for 1cc3abb4fb54: $pw"   # give this to the installer
```

This creates the broker account `1cc3abb4fb54` (ACL-scoped to its own subtree)
and reloads the broker. The username is the `gateway_id`; the installer types
only the password.

**C. Register the plug row(s)** under the gateway (CPO dashboard / plug API), so
telemetry for `plug_id` is attributed and billable.

Then the installer finishes the setup form (step above) and reboots the device.
Within a few seconds it should appear **online** in the dashboard and stream
telemetry.

---

## Verify

```bash
# broker: the device authenticates as its own gateway_id
sudo docker logs amphive-mqtt --since 60s | grep "u'1cc3abb4fb54'"
# backend: telemetry attributed to the gateway
sudo docker logs amphive-backend --since 60s | grep "gw=1cc3abb4fb54"
```

On the device serial you should see:
`MQTT: authenticating as '1cc3abb4fb54'` → `MQTT connected to server broker.`

---

## Re-credentialing a deployed device

If a gateway's broker password must be rotated without a site visit, the NVS can
be rewritten over USB (nvs region only, OTA slots preserved) — see the procedure
recorded in the `direct-mqtt-architecture` note. The captive portal is the
normal path for a fresh device.

---

## Why no auto claim-code / JWT (decision 2026-07-10)

Considered dynsec and JWT for programmatic credential issuance. Rejected for now:
provisioning is manual and infrequent, so static per-gateway passwd + ACL is the
**most stable** option (no plugin, no control plane, no clock). JWT is
specifically a poor fit — the ESP has no reliable clock (`CONFIG_MBEDTLS_HAVE_TIME_DATE`
is off; it doesn't even validate cert dates), so token `exp`/`iat` can't be
handled without forcing non-expiring tokens, which negates JWT's benefit. Revisit
dynsec only if per-gateway provisioning becomes high-churn.
