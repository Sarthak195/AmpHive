# Preparing a batch of preflashed AmpHive gateways (claim-code onboarding)

How an operator turns a stack of bare ESP32-C3 boards into shippable,
plug-and-play AmpHive gateways — units a non-technical buyer can set up
themselves with only Wi-Fi credentials and a claim code, no operator visit
per install. This is the **manufacturing/batch** flow; for a one-off custom
site install (operator on-site, choosing the plug/gateway names as they go)
see [gateway_provisioning.md](gateway_provisioning.md) instead — both flows
converge on the same captive portal and the same `gateways` table, just in a
different order and for a different audience.

See also: [docs/FIRMWARE.md §2](../../docs/FIRMWARE.md#2-captive-portal--implemented-locked-down-fw-160-mobile-first-wizard-2026-08-02)
(the portal itself), [docs/API_REFERENCE.md](../../docs/API_REFERENCE.md)
(the claim endpoints), and the printable
[buyer_setup_card.md](buyer_setup_card.md) (what ships in the box).

---

## Why claim codes (design summary)

Today, adding a gateway means an operator hand-registers a DB row
(`POST /api/cpo/gateways`) and provisions per-gateway MQTT broker credentials
(`add_gateway_user.ps1`) for **every unit, per customer**. That doesn't scale
to a retail/self-serve product. The claim-code flow splits the work:

- **At manufacturing time** (this runbook): the operator flashes a batch of
  units, mints each as **unclaimed inventory** in the backend (`tenant_id`
  NULL, a fresh human-typable claim code), and still creates the MQTT broker
  account per unit — that part stays manual (see "Out of scope" below).
- **At unboxing time** (buyer, no operator involved): the buyer joins the
  device's setup Wi-Fi, fills in the two-step captive-portal wizard, then
  opens `cpo.amphive.app` and types the claim code from the label to bind
  the gateway to their own tenant. No backend registration step for them.

The claim code is a separate concept from the MQTT username/password: it
only ever touches the backend's `gateways.tenant_id`/`claimed_at` columns
(who owns this row), never the broker's `passwd`/ACL file. See
`backend/database/models.py` `Gateway` and Alembic `0034_gateway_claim_code`.

---

## Batch steps (per unit)

Do these from a dev workstation with `gcloud` access and an admin AmpHive
account. Repeat per unit; steps B–D can be scripted/batched if you're doing
more than a handful.

### A. Flash

```bash
cd firmware
idf.py -p <PORT> flash monitor
```

Read the device's **gateway ID** (the STA MAC, e.g. `1cc3abb4fb54`) and its
**setup code** off the serial log (`SETUP CODE: ...` — see
[docs/FIRMWARE.md §2](../../docs/FIRMWARE.md)). Both are needed below and
both go on the physical label.

### B. Mint the inventory row + claim code (admin API)

```bash
curl -X POST https://<backend>/api/admin/gateways/inventory \
  -H "Authorization: Bearer <admin-token>" -H "Content-Type: application/json" \
  -d '{"gateway_id":"1cc3abb4fb54","name":"Batch 2026-08 unit 014"}'
# -> {"status":"minted","gateway_id":"1cc3abb4fb54","name":"...","claim_code":"H4KX9Q2PFW"}
```

Or via the admin console: **Admin → Gateways → Mint inventory gateway**
(shows the claim code once, with a copy button — write it down immediately,
it is not retrievable in plaintext again... actually it IS: `GET
/api/admin/gateways/inventory` lists claim codes for admins for reprint, but
the console only shows it inline at mint time — check that endpoint if a
label is lost/damaged before the unit ships).

The row is created **unclaimed** (`tenant_id` NULL) — it will not appear in
any CPO's `GET /api/cpo/gateways` fleet view, nor in the admin fleet view
(`GET /api/admin/gateways`, which is tenant-scoped) until claimed. Use `GET
/api/admin/gateways/inventory?claimed=false` to audit unshipped stock.

### C. Create the per-gateway MQTT broker account

**Unchanged from the existing flow** — this is deliberately still manual
(see "Out of scope" below):

```powershell
$pw = python -c "import secrets; print(secrets.token_urlsafe(21))"
.\deploy\scripts\add_gateway_user.ps1 -GatewayId 1cc3abb4fb54 -Password $pw `
  -VmName amphive-relay -VmZone us-west1-a -RemoteDir ~/amphive-relay
```

> The script's built-in defaults (`-VmName amphive-vm-in -VmZone
> asia-south1-a -RemoteDir /home/Sarthak/amphive`) are **stale** — prod moved
> to the free-tier `amphive-relay` VM (`us-west1-a`, `~/amphive-relay`) in the
> 2026-07-27 consolidation. Always pass the three flags above explicitly
> until the script's defaults are updated.

Because the buyer never opens "Installer options" in the portal (see below),
**burn this password into the unit's NVS now**, over the same USB/serial
connection from step A, rather than relying on the buyer to type it — the
portal's MQTT password field is optional precisely so a preflashed unit can
skip it. The straightforward way (no extra tooling needed) is a one-off
`idf.py monitor` + a tiny NVS-write helper, matching the technique already
documented for rotating a deployed device's Tapo password (see
[docs/ESP32_CONNECTION.md §9](../../docs/ESP32_CONNECTION.md) troubleshooting
table, "KLAP handshake1 auth mismatch" row): `nvs_set_str(handle, "mqtt_pwd",
pw)` in namespace `"storage"`, called once before the unit leaves the bench.
(If you'd rather skip this step for a given batch, that's fine too — the
buyer's portal still has "Installer options" as a fallback; it's just an
extra step for them.)

### D. Print the label + pack the box

Print (or write) onto the unit/box:

- **Setup code** (from step A) — needed once, during the buyer's Wi-Fi step.
- **Claim code** (from step B) — needed once, on the CPO portal.
- A copy of [buyer_setup_card.md](buyer_setup_card.md) (or point to the
  hosted version of it) in the box.

The gateway ID (MAC) itself does **not** need to go on the label — it's only
useful to you (the operator/admin), and it's what `GET
/api/admin/gateways/inventory` keys off if you ever need to look a unit up
by its claim code or vice versa.

---

## Verify a claimed unit

Same as [gateway_provisioning.md](gateway_provisioning.md)'s verify section:
watch the broker/backend logs for the gateway's own id, and its serial
console for `MQTT connected to server broker.`. To confirm the claim itself
landed, check `GET /api/cpo/gateways` (as the claiming tenant) or `GET
/api/admin/gateways/inventory?claimed=true` (as admin) for the row's
`tenant_id`/`claimed_at`.

---

## Out of scope (by design)

**Automatic Mosquitto broker-credential provisioning is not built.** Minting
a claim code and creating the broker account (step C) are two independent
admin actions today — claiming a gateway (the buyer's side) only ever
touches the backend's `gateways` row, never the VM's `mosquitto_passwd` file.
Automating step C would mean either giving the backend shell access to the
relay VM or standing up an MQTT dynsec/control-plane, both bigger changes
than this batch-onboarding pass — see
[gateway_provisioning.md "Why no auto claim-code / JWT"](gateway_provisioning.md#why-no-auto-claim-code--jwt-decision-2026-07-10)
for the prior reasoning on why per-gateway static passwd + ACL was chosen
over a control plane for the *broker* side specifically (that decision is
about MQTT credentials, not the claim-code system this doc introduces, which
is purely a backend/CPO-portal tenant-assignment convenience).
