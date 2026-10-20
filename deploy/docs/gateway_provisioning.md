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

## Where a gateway's configuration lives

A gateway's setup is split across three places. Each is changed in its own
way, and none of them is a copy of another:

| Where | What is kept there | How you change it |
|-------|--------------------|-------------------|
| **1. On the device** (its NVS flash, the `storage` settings) | The site's Wi-Fi name + password, the Tapo account (email + password) the gateway uses to talk to the plug, the gateway's broker password, and the setup code (the password of its setup Wi-Fi; printed in the serial log whenever the portal starts, and on the unit's label if it has one) | The setup portal: join the gateway's Wi-Fi **`AmpHive_Setup_XXXX`** and open `http://192.168.4.1`. Or, with the board on a USB cable, the [USB tool below](#provision-over-usb-no-phone) |
| **2. On the server** (the backend database) | The plug roster: which plugs belong to this gateway, their LAN IPs, and any per-plug current cap | The CPO dashboard / plug API. The backend pushes the roster to the gateway as a retained message on `amphive/gateways/<id>/config` whenever a plug is created, edited or deleted, and again every time the gateway (re)connects |
| **3. On the broker** (mosquitto's passwd file on the VM) | The gateway's MQTT account: username = the gateway id (its MAC, e.g. `1cc3abb4fb54`) and a hash of its password | `deploy/scripts/add_gateway_user.ps1` (step B below) |

So the broker password is set in two places (device and broker) and the two
must match. Plugs are only ever managed on the server; the roster it sends
carries plug ids, LAN IPs and caps, nothing else. Wi-Fi and Tapo
credentials are set only on the device (through the portal or the USB tool);
nothing the server sends ever changes them.

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

Do these from a dev workstation that can SSH to the VM (Windows OpenSSH plus
the `~/.ssh/google_compute_engine` key that a first `gcloud compute ssh`
creates). You need the `gateway_id` (the MAC shown on the setup page or the
device sticker).

**A. Create the gateway record** (CPO dashboard, or the API):

```bash
# gateway_id == the MAC shown in the setup portal; vpn_ip is legacy/optional.
curl -X POST https://<backend>/api/cpo/gateways \
  -H "Authorization: Bearer <cpo-token>" -H "Content-Type: application/json" \
  -d '{"gateway_id":"1cc3abb4fb54","name":"Client Site A - Bay 1"}'
```

**B. Mint the per-gateway broker credential** (it goes into the device's MQTT
Password field, or the USB tool's secrets file, and nowhere else):

```powershell
.\deploy\scripts\add_gateway_user.ps1 -GatewayId 1cc3abb4fb54
```

This generates a strong password, creates the broker account `1cc3abb4fb54`
(ACL-scoped to its own subtree), reloads the broker, checks the login by
subscribing as the gateway, and prints the password **once**. It is not
stored anywhere, so hand it straight to the installer. The username is the
`gateway_id`; the installer types only the password. (`-Password <pwd>` uses
your own instead, 16-63 chars of `[A-Za-z0-9_-]`, but leaves it in your shell
history.) Run for an existing account, it **resets** the password.

The script uploads `deploy/scripts/relay_set_gateway_password.sh` and runs it
over plain OpenSSH (default `Sarthak@8.235.4.66`; `-VmUser`/`-VmIp`/`-KeyFile`
override), with the password on stdin only. The two script headers explain
the gotchas: `gcloud compute ssh` on Windows injects a "y" line into remote
stdin; `mosquitto_passwd` rejects blank lines; the passwd file is a
single-file bind mount, so it is rewritten in place and the broker is HUP'd.

**C. Register the plug row(s)** under the gateway (CPO dashboard / plug API), so
telemetry for `plug_id` is attributed and billable.

Then the installer finishes the setup form (step above) and reboots the device.
Within a few seconds it should appear **online** in the dashboard and stream
telemetry.

---

## Verify

The dashboard should show the gateway **online**. On the VM, from
`~/amphive-relay` (service names work whatever the containers are called):

```bash
# broker: the device authenticates as its own gateway_id
sudo docker compose -f docker-compose.relay.yml logs --since 60s mqtt | grep "u'1cc3abb4fb54'"
# backend: messages attributed to the gateway
sudo docker compose -f docker-compose.relay.yml logs --since 60s backend | grep 1cc3abb4fb54
```

On the device serial you should see:
`MQTT: authenticating as '1cc3abb4fb54'` → `MQTT connected to server broker.`

---

## Provision over USB (no phone)

The setup portal needs a phone or laptop on the gateway's setup Wi-Fi. With
the board on a USB cable instead, write the same settings with
[`tools/gateway_provision/provision_nvs.py`](../../tools/gateway_provision/README.md).
It builds the NVS partition from a secrets file kept **outside the repo**,
checks it, and flashes it with esptool. It never prints a secret value.

> **⚠ Writing NVS replaces the WHOLE `nvs` partition, and reboots the
> gateway.** NVS holds more than settings: the crash-recovery record of the
> active charging session, the persisted energy counters and the offline log
> live there too, and the tool writes only the settings, so all of that is
> wiped. esptool also resets the board into its bootloader to write, then
> (by default) hard-resets it, so the gateway drops offline and comes back
> with no session on record.
>
> On 2026-10-20 this cut a live charging session: the rebooted gateway found
> the relay ON with no session and force-OFFed the plug as `UNAUTHORIZED_ON`.
> **Never provision, reset or flash a gateway while a session is active on
> it. Check first** (CPO dashboard: no active session on any of its plugs).
> The tool asks you to type the gateway id before it writes.

1. Get the gateway's broker password (step B) and fill in a secrets file
   outside the repo (format in the
   [tool's README](../../tools/gateway_provision/README.md)): Wi-Fi, Tapo
   account, `mqtt_pwd`, and the unit's existing `setup_code` from its label.
   Without it, the firmware generates a new code and the label goes stale.
2. Activate ESP-IDF v5.3.x in the shell (`. <IDF>\export.ps1`), or pass
   `--idf-python` and `--nvs-tool`.
3. Build and verify. This doesn't touch the device:

   ```powershell
   python tools\gateway_provision\provision_nvs.py --gateway-id 1cc3abb4fb54 `
       --secrets <outside-repo>\gw-1cc3abb4fb54.local.txt --out <outside-repo>\nvs.bin
   ```

4. Confirm no session is active, then re-run with `--write COM<n>` and type
   the gateway id when asked.
5. The gateway reboots, joins Wi-Fi and logs in to the broker; check
   [Verify](#verify). Delete `nvs.bin` afterwards (the secrets are in it in
   plain text).

---

## Re-credentialing a deployed device

To rotate a gateway's broker password, run `add_gateway_user.ps1 -GatewayId
<id>` (it resets the existing account), then give the device the new password
through the setup portal's MQTT Password field or the USB tool above. Between
those two steps the gateway cannot log in, so do it when you can reach the
device, and never during a charging session.

---

## Why no auto claim-code / JWT (decision 2026-07-10)

Considered dynsec and JWT for programmatic credential issuance. Rejected for now:
provisioning is manual and infrequent, so static per-gateway passwd + ACL is the
**most stable** option (no plugin, no control plane, no clock). JWT is
specifically a poor fit — the ESP has no reliable clock (`CONFIG_MBEDTLS_HAVE_TIME_DATE`
is off; it doesn't even validate cert dates), so token `exp`/`iat` can't be
handled without forcing non-expiring tokens, which negates JWT's benefit. Revisit
dynsec only if per-gateway provisioning becomes high-churn.
