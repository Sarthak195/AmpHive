# OTA image publishing — signed firmware on the public HTTPS bucket

*Runbook for shipping gateway firmware over the direct-MQTT path. Canonical
background: [docs/FIRMWARE.md](../../docs/FIRMWARE.md) §6–§8,
[docs/SECURITY.md](../../docs/SECURITY.md) §3.*

> **2026-08-04: this runbook is now the FALLBACK path.** The primary publish
> flow is the admin portal's upload widget (Admin → Firmware Releases →
> Upload image): it takes the signed `build/amphive-gateway.bin`, validates
> the ESP image header, reads the version out of the embedded app
> descriptor, **publishes the image straight to `gs://amphive-fw` (keyless,
> via the VM's own compute service account — no key file, no `gcloud`), and
> auto-registers the bucket URL.** See docs/FIRMWARE.md §7 and
> `backend/services/firmware_publish.py`. The manual `gcloud storage cp` flow
> below still works (release rows store full URLs) and remains useful when the
> backend is unreachable or for pre-registry images.
>
> **Why the upload endpoint publishes to GCS (not self-host):** the earlier
> cut self-hosted the image on the backend and served it from
> `GET /api/firmware/images/{file}`. Real devices could not finish that
> download (the ESP32 TLS stream breaks mid-way) and the uploaded file was
> non-persistent (`deploy.ps1` doesn't ship the `firmware_images` volume), so
> it vanished on redeploy. Every historically-successful OTA fetched from
> `gs://amphive-fw`, so uploads now go there. That legacy GET endpoint is
> retained only for back-compat with any release row still pointing at a
> self-hosted URL.
>
> **INFRA PREREQUISITE (apply once, BEFORE deploying the upload feature):** the
> compute service account needs `roles/storage.objectCreator` on
> `gs://amphive-fw`. Today it only holds that on `gs://amphive-db-backups`
> (used by `backup_db.sh`); the VM's `devstorage.read_write` access scope is
> already set. Grant it:
>
> ```bash
> gcloud storage buckets add-iam-policy-binding gs://amphive-fw \
>     --member=serviceAccount:930756667383-compute@developer.gserviceaccount.com \
>     --role=roles/storage.objectCreator
> ```
>
> Without this the upload endpoint returns **502 "Failed to publish firmware
> image to the storage bucket"** (the GCS POST 403s). The bucket name is
> overridable via the `FIRMWARE_GCS_BUCKET` env var (default `amphive-fw`).

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
3. Publish — either run
   [`deploy/scripts/publish_firmware.ps1`](../scripts/publish_firmware.ps1)
   (uploads, prints the registration + trigger calls below), or by hand:

   ```bash
   gcloud storage cp firmware/build/amphive-gateway.bin \
       gs://amphive-fw/amphive-gateway-<version>.bin
   ```

4. **Register the release** (feat/ota-version-picker, 2026-08-02) so it shows
   up in the CPO portal's OTA dropdown instead of anyone hand-pasting the URL:

   ```bash
   curl -X POST "http://<backend>/api/admin/firmware-releases" \
       -H "Authorization: Bearer <admin JWT>" -H "Content-Type: application/json" \
       -d '{"version":"<version>","url":"https://storage.googleapis.com/amphive-fw/amphive-gateway-<version>.bin"}'
   ```

   This only registers a URL pointer — it does not upload/store a binary
   (step 3 already did the actual publish). See `GET`/`POST
   /api/admin/firmware-releases` in
   [docs/API_REFERENCE.md](../../docs/API_REFERENCE.md).

5. Trigger — from the CPO portal (Gateways → **Update firmware** → pick the
   version from the dropdown), or by hand (bypasses the registry; the raw
   `firmware_url` field is admin-only — a `cpo`-role caller gets 403 and must
   use `release_id` instead):

   ```bash
   curl -X POST "http://<backend>/api/cpo/gateways/<gateway_id>/ota" \
       -H "Authorization: Bearer <admin JWT>" -H "Content-Type: application/json" \
       -d '{"firmware_url":"https://storage.googleapis.com/amphive-fw/amphive-gateway-<version>.bin"}'
   ```

6. Watch the gateway's status topic / CPO dashboard: it goes offline, reboots,
   and reports the new `fw` version in its `online` status; the image commits
   (`marking image valid`) only once it re-reaches the broker, else the
   bootloader rolls back to the previous slot.

## Migration state — ROLLED OUT (2026-07-10, updated 2026-07-11)

**Current fleet firmware: signed `2.0.0-direct`** (multi-plug roster
provisioning — backend-pushed retained plug roster; captive-portal `plug_ip`
field and provisional boot slot removed). Rollout log, newest first (NOTE: the
`1.7.1`→`1.8.0`→`1.9.0` interim rollouts are not individually re-logged here —
see git history / `docs/IMPLEMENTATION_STATUS.md`):

- **2026-07-15 — `1.9.0-direct` → signed `2.0.0-direct`** on the real gateway
  `1cc3abb4fb54` (multi-plug roster provisioning, PR #52). **Backend deployed
  first** (publishes the retained roster on `amphive/gateways/{gw}/config`),
  then the image was published via `deploy/scripts/publish_firmware.ps1`
  (`gs://amphive-fw/amphive-gateway-2.0.0-direct.bin`, anonymous-HTTPS-fetch
  verified) and triggered through `POST /api/cpo/gateways/1cc3abb4fb54/ota`.
  Gateway rebooted `1.9.0`→`2.0.0` in ~30s (rollback cancelled); **`last_seen`
  then advanced on 2.0.0**, confirming idle telemetry flows from the retained
  roster (the device learned plug 1 @192.168.1.6 from `.../config`, not the
  removed provisional slot). Built with **ESP-IDF v5.3.3** — the field-consistent
  toolchain. Published image: `amphive-gateway-2.0.0-direct.bin` (**current**).

- **2026-07-12 — `1.7.0-direct` → `1.7.1-direct`** on the real gateway
  `1cc3abb4fb54` (multi-plug regression fix + on-device verification).
  **1.7.0 shipped a regression** and was **pulled**: the multi-plug refactor
  dropped the pre-multi-plug "poll the provisioned plug from boot" behaviour, so
  a session-less gateway published no idle telemetry, its `last_seen` froze, and
  session starts 409'd "gateway offline" (confirmed live: last_seen frozen ~17
  min on 1.7.0). **1.7.1** pre-registers the provisioned plug at boot (idle
  telemetry flows immediately; real `plug_id` adopted by IP on first command).
  After OTA to 1.7.1 telemetry resumed (last_seen advancing) and a **billed
  single-plug session ran end-to-end** (session 28: 0.014 kWh, peak 681 W → 0.07
  coins, balance 497.79 → 497.72, ledger reconciled). Events: 1.7.0
  `OTA_OK_REBOOTING` 04:37:21 → (regression) → 1.7.1 back online 04:54:38 →
  telemetry confirmed 04:55:40. **Do not push `amphive-gateway-1.7.0-direct.bin`
  — it has the liveness regression.**
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

**Do not push the older `amphive-gateway-1.4.0.bin`/`-1.5.0.bin`/`-1.6.0-direct.bin`
(downgrade) or `-1.7.0-direct.bin` (liveness regression) still in the bucket** —
`1.7.1-direct` is the good image.

Published images (`gs://amphive-fw`): `amphive-gateway-1.4.0.bin` (historic),
`amphive-gateway-1.5.0.bin` (historic), `amphive-gateway-1.6.0-direct.bin`
(historic), `amphive-gateway-1.7.0-direct.bin` (**pulled — regression**),
`amphive-gateway-1.7.1-direct.bin` (**current**). See
[docs/IMPLEMENTATION_STATUS.md](../../docs/IMPLEMENTATION_STATUS.md) for the
canonical status row.
