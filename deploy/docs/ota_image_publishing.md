# OTA image publishing — signed firmware on the public HTTPS bucket

*Runbook for shipping gateway firmware over the direct-MQTT path. Canonical
background: [docs/FIRMWARE.md](../../docs/FIRMWARE.md) §6–§8,
[docs/SECURITY.md](../../docs/SECURITY.md) §3.*

Since the 2026-07-10 hardening, an OTA image must be:

1. **Signed** — the build appends an ECDSA (scheme v1) signature made with
   `firmware/secure_boot_signing_key.pem`. Firmware ≥ 1.4.0 verifies it in
   `esp_https_ota_finish` and refuses unsigned/forged images.
2. **Served over HTTPS from a public-CA host** — firmware ≥ 1.4.0 refuses
   `http://` URLs (and the backend rejects them in `CpoGatewayOtaRequest`),
   and validates the host cert against the built-in Mozilla bundle.

## The signing key

- Lives at `firmware/secure_boot_signing_key.pem` — **gitignored, never
  commit**. Generated 2026-07-10 on the dev box with
  `python -m espsecure generate_signing_key --version 1 secure_boot_signing_key.pem`
  (espsecure from the IDF v5.3.3 python env).
- **Back it up somewhere safe.** Every fielded device with fw ≥ 1.4.0 only
  accepts images signed with this exact key; if it is lost, the only recovery
  path is a physical USB reflash of every gateway.
- The build fails if the key file is missing; the public half is extracted at
  build time and embedded in the app (`signature_verification_key.bin`).

## One-time bucket setup (done 2026-07-10)

Commands actually run (project `project-7ee69f02-c0cf-4f51-952`):

```bash
gcloud storage buckets create gs://amphive-fw \
    --location=asia-south1 --uniform-bucket-level-access
gcloud storage buckets add-iam-policy-binding gs://amphive-fw \
    --member=allUsers --role=roles/storage.objectViewer   # public read (user-approved)
```

Public-read is deliberate: images contain no secrets (all credentials live in
NVS, the broker CA cert is public) and are signature-verified on the device,
so a reader gains nothing and a forger is rejected.

## Per-release procedure

1. Bump `PROJECT_VER` in `firmware/CMakeLists.txt` (reported in the MQTT
   `online` status — it's how you confirm the OTA took).
2. `idf.py build` (IDF v5.3.3). Ship `build/amphive-gateway.bin` — already
   signed. **Never ship `amphive-gateway-unsigned.bin`.**
3. Publish + trigger — either run
   [`deploy/scripts/publish_firmware.ps1`](../scripts/publish_firmware.ps1)
   (uploads, prints the URL + trigger call), or by hand:

   ```bash
   gcloud storage cp firmware/build/amphive-gateway.bin \
       gs://amphive-fw/amphive-gateway-<version>.bin

   curl -X POST "http://<backend>/api/cpo/gateways/<gateway_id>/ota" \
       -H "Authorization: Bearer <CPO JWT>" -H "Content-Type: application/json" \
       -d '{"firmware_url":"https://storage.googleapis.com/amphive-fw/amphive-gateway-<version>.bin"}'
   ```

4. Watch the gateway's status topic / CPO dashboard: it goes offline, reboots,
   and reports the new `fw` version in its `online` status; the image commits
   (`marking image valid`) only once it re-reaches the broker, else the
   bootloader rolls back to the previous slot.

## Migration state — ROLLED OUT (2026-07-10, updated 2026-07-11)

**Current fleet firmware: signed `1.6.0-direct`** (provisioning-portal
lockdown, SEC §8.1). Rollout log, newest first:

- **2026-07-11 ~21:56 IST — `1.5.0-direct` → signed `1.6.0-direct`** on the
  real gateway `1cc3abb4fb54`. Image published via
  `deploy/scripts/publish_firmware.ps1`
  (`gs://amphive-fw/amphive-gateway-1.6.0-direct.bin`, anonymous-HTTPS-fetch
  verified), triggered through `POST /api/cpo/gateways/1cc3abb4fb54/ota`.
  Events feed: `OTA_STARTED` 21:55:39 → `OTA_OK_REBOOTING` 21:56:03 → back
  online reporting `1.6.0-direct` by 21:56:08; stayed online on subsequent
  checks (rollback cancelled). **Operational note:** from 1.6.0 the
  provisioning portal is WPA2-locked by a per-device setup code printed
  only over serial at portal start — if this gateway ever re-enters the
  portal (Wi-Fi loss/reprovisioning), the code must be read via
  `idf.py monitor` (see docs/ESP32_CONNECTION.md §6).
- **2026-07-10 — `1.3.2-direct` → signed `1.5.0-direct`** end-to-end over
  direct-MQTT (skipping 1.4.0; `OTA_OK_REBOOTING` → offline → back online
  on 1.5.0, rollback cancelled). From 1.4.0 onward only signed images
  install. The backend https-only validation (`backend/schemas.py`) is
  **deployed**.

**Do not push the older `amphive-gateway-1.4.0.bin`/`-1.5.0.bin` still in
the bucket — they would downgrade the device** and lose the portal-lockdown
(1.6.0) and safety-alarm/telemetry (1.5.0) features.

Published images (`gs://amphive-fw`): `amphive-gateway-1.4.0.bin` (historic),
`amphive-gateway-1.5.0.bin` (historic), `amphive-gateway-1.6.0-direct.bin`
(current). See [docs/IMPLEMENTATION_STATUS.md](../../docs/IMPLEMENTATION_STATUS.md)
for the canonical status row.
