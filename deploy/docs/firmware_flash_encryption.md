# Firmware Flash + NVS Encryption — Operator Runbook

*Status: **config prepared + designated the standard for new field units, not
yet burned.** The build recipe and partition table exist in-repo
(`firmware/sdkconfig.flashenc`, `firmware/partitions_ota_enc.csv`) and are
verified complete; **as of the 2026-08-06 deep-audit remediation (finding H2),
encrypted-NVS is the standard production build for all NEW
field/manufacturing units** — but no device has been encrypted yet, and the
**mainline default (`sdkconfig.defaults`) stays plaintext/reversible on
purpose** (so dev boards and already-fielded units keep a safe serial
re-flash). Burning encryption is a deliberate, per-device, **irreversible**
operator action done once at manufacture — decide with this doc.*

> **Why the split (default plaintext, but encrypted for new production units)?**
> Flipping the mainline default to enable flash encryption would make the next
> `idf.py flash` of ANY unit — including dev boards and existing gateways —
> burn eFuses irreversibly. So encryption lives in an opt-in build profile
> (`sdkconfig.flashenc`, layered over the defaults) that the manufacturing flow
> selects for new units, never the default target.

Closes the config half of [SECURITY.md §8.2](../../docs/SECURITY.md) (plaintext
NVS secrets) / TECH_DEBT device-security. See also
[ota-signing-key](../../docs/SECURITY.md) — losing the signing key while a device
is encrypted is unrecoverable (below).

> ### ⚠️ Decide this FIRST — encryption permanently dedicates the board
>
> Burning encryption is a **one-way, per-chip commitment**. Once burned, that
> ESP32 can no longer be freely repurposed:
> - **Release mode:** UART flashing is disabled **permanently** — only a signed
>   OTA can ever update it. It is effectively locked to AmpHive for life; you
>   cannot reclaim it as a general dev board.
> - **Development mode:** still serial-reflashable, but the flash stays encrypted
>   for life, every reflash must be encryption-aware (`idf.py encrypted-flash`),
>   and the number of plaintext reflash cycles is bounded by the `FLASH_CRYPT_CNT`
>   eFuse. Casual "flash a random sketch" reuse is gone.
>
> **Encrypt every unit you are committing to production AmpHive gateways — this
> is now the standard for new field/manufacturing units (finding H2) — but never
> a board you may want to reclaim** for prototyping or another project. Any unit
> being shipped to a customer/site IS a production unit and should be encrypted;
> if instead you rotate a small pool of dev boards between projects, leave them
> **plaintext** and rely on physical security of the enclosure instead (a
> legitimate choice for a low-physical-risk site, e.g. a locked cabinet).
>
> **Encryption does NOT freeze credentials.** Changing the Wi-Fi / Tapo / MQTT
> password later still works normally — NVS encryption is transparent on write, so
> re-provisioning through the captive portal (e.g. after a monthly Wi-Fi-password
> rotation) needs **no** eFuse re-burn and **no** serial reflash. Encryption
> protects the secrets *at rest*; it does not make NVS read-only. (Tip: to avoid
> re-provisioning every rotation, keep gateways on a dedicated IoT SSID whose
> password you don't rotate.)

---

## 1. What this protects, and why NVS encryption (not just flash encryption)

The gateway stores its Wi-Fi password, the **Tapo account** email+password, the
per-gateway **MQTT** credential, and the captive-portal **setup code** as
plaintext in the `nvs` partition (`save_config_to_nvs` in `main.c`). Brief
physical access + `esptool read_flash` yields all of them — the victim's real
Tapo account included.

**Key fact for ESP32:** flash encryption encrypts the bootloader, partition
table, and app slots — but **not data partitions**. So `CONFIG_SECURE_FLASH_ENC_ENABLED`
*alone* leaves `nvs` readable. Protecting the secrets requires **NVS encryption**
(`CONFIG_NVS_ENCRYPTION`), which needs a dedicated `nvs_keys` partition — and NVS
encryption in turn depends on flash encryption to protect that key partition.
There is no meaningful "encrypt NVS only" path. Both are enabled together by
`firmware/sdkconfig.flashenc` + `firmware/partitions_ota_enc.csv`.

`nvs_flash_init()` uses the encrypted NVS transparently once these are set — **no
application code change**.

## 2. It CANNOT be delivered by OTA

Encryption is a bootloader change + an eFuse burn. OTA only rewrites app slots,
never the bootloader or eFuses. **Every device must be physically retrieved and
serial-flashed once** to become encrypted. New/RMA units get it at flash time;
existing field units (e.g. `1cc3abb4fb54`) need a bench visit.

## 3. Development vs Release mode

| | Development (this fragment) | Release |
|--|--|--|
| First-boot in-place encrypt | yes | yes |
| UART plaintext re-flash after | **yes** (bounded by `FLASH_CRYPT_CNT`) | **no — permanent** |
| Recoverable from a bad image | yes (serial reflash) | only via OTA |
| Use for | all first units, validation | production, only after Development-mode validation |

`sdkconfig.flashenc` ships **Development mode** deliberately. Do **not** switch to
`CONFIG_SECURE_FLASH_ENCRYPTION_MODE_RELEASE` until a sacrificial dev unit has run
the whole flow. **Release mode + a lost signing key = hard brick** (no OTA path
*and* no plaintext serial path).

## 4. Interaction with signed OTA (unchanged)

Flash encryption and the existing ECDSA signed-OTA are independent. Signing still
uses `firmware/secure_boot_signing_key.pem` (gitignored, this box only — **never
regenerate**). Future OTAs keep working on an encrypted device (the flash driver
encrypts app-slot writes transparently). Secure Boot stays **off**.

> **Back up `secure_boot_signing_key.pem` off-box BEFORE encrypting any unit.**
> Encryption removes the plaintext-reflash safety net, so the signing key becomes
> the only remaining recovery path for a Release-mode device.

## 5. Procedure (Development mode, on a sacrificial dev unit first)

Pre-req: ESP-IDF v5.3.3 export sourced; the signing key present and backed up.

1. **Build the encrypted image** (layer the fragment over the defaults):
   ```pwsh
   idf.py -D SDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.flashenc" build
   ```
   Confirm the build selects `partitions_ota_enc.csv` and shows flash-encryption +
   NVS-encryption enabled.
2. **Serial flash it** (the device generates its key and encrypts in place on the
   first boot; subsequent flashes need the `--encrypt` path):
   ```pwsh
   idf.py -p COM5 flash monitor
   ```
   Watch the log for flash-encryption enable + first-boot re-encrypt, then a
   normal boot to the app.
3. **Re-provision** over the captive portal (NVS is fresh after the key burn), as
   in the serial-reflash runbook (rotate the broker password if the old plaintext
   is unrecoverable — see the prod-VM ops notes).
4. **Verify the secrets are now ciphertext:**
   ```pwsh
   python -m esptool -p COM5 read_flash 0x9000 0x6000 nvs_dump.bin
   ```
   The Wi-Fi/Tapo/MQTT strings must **not** appear in `nvs_dump.bin` (grep for the
   SSID / Tapo email — expect no plaintext hit).
5. **Confirm normal operation:** MQTT online, telemetry flowing, a billed session
   end-to-end.

Only after a dev unit passes 1–5 should encryption roll to real units — and only
consider Release mode for production units after that.

## 6. Bricking / recovery risks (read before burning)

1. Release mode is irreversible — UART plaintext read/write is disabled; a bad
   image with no working OTA = dead device.
2. `FLASH_CRYPT_CNT` limits plaintext re-flash cycles even in Development mode.
3. Release mode **and** a lost signing key = unrecoverable.
4. After encryption, a normal `idf.py flash` no longer writes valid plaintext —
   use `idf.py encrypted-flash` (or `--encrypt`).

## 7. What is NOT done here

- No eFuse has been burned; no device is encrypted.
- Secure Boot (boot-time signature verification) is a separate, also-irreversible
  step and is **not** enabled by this fragment.
- `PROJECT_VER` should bump when an encrypted build ships so the fleet can tell
  encrypted vs plaintext units apart — but the bump is not the delivery mechanism
  (serial-only, per §2).
