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

## Migration state (as of 2026-07-10)

- Signed `1.4.0-direct` is built and published at
  `https://storage.googleapis.com/amphive-fw/amphive-gateway-1.4.0.bin`, but
  the push to the real gateway (`1cc3abb4fb54`, on 1.3.x) is **pending**.
  Pre-1.4.0 firmware doesn't verify signatures (the trailer is ignored), so
  the jump is safe; from 1.4.0 onward only signed images install.
- The backend https-only validation (`backend/schemas.py`) is committed but
  **not yet deployed** — ship it with the next `deploy.ps1` run.
