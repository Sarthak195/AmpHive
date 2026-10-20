# Gateway provisioning over USB (`provision_nvs.py`)

Sets up an AmpHive ESP32 gateway's configuration **over a USB cable instead of
the phone setup portal**. It builds the gateway's NVS partition (the on-device
settings store) from a small secrets file, checks it, and can write it to the
board with `esptool`. The portal and this tool store the same keys; use
whichever is easier. The full runbook is
[deploy/docs/gateway_provisioning.md](../../deploy/docs/gateway_provisioning.md)
("Provision over USB (no phone)").

> **Never write while a charging session is active on that gateway.** Writing
> replaces the *whole* NVS partition and reboots the board. That cuts the
> session and erases its crash-recovery record (plus the persisted energy
> counters and the offline log). On 2026-10-20 this happened to a live
> session: the rebooted gateway saw the relay on with no session and switched
> the plug off as `UNAUTHORIZED_ON`. Check the CPO dashboard first. The tool
> also makes you type the gateway id before it writes (`--yes` skips that).

## What you need

- ESP-IDF v5.3.x installed (its python env provides `esp-idf-nvs-partition-gen`
  and `esptool`; `nvs_tool.py` ships inside IDF). The easiest way is to run IDF's
  `export.ps1` in the shell first. That sets `IDF_PATH` and `IDF_PYTHON_ENV_PATH`,
  which the tool picks up. Otherwise pass `--idf-python <python of the IDF env>`
  and `--nvs-tool <IDF>/components/nvs_flash/nvs_partition_tool/nvs_tool.py`.
- The gateway id: its MAC as 12 lowercase hex characters, e.g. `1cc3abb4fb54`.
  It's shown on the setup portal page and in the serial log.
- Its broker password, from `deploy/scripts/add_gateway_user.ps1`. For a
  gateway that already has an account this **resets** it, which locks the
  gateway out of the broker until you write the new password, so check that
  no session is active *before* running it (it refuses while the database
  shows one), then build and write in one sitting.
- A secrets file **kept outside the repo**:

  ```
  # gw-<id>.local.txt   (KEY=VALUE, # comments allowed)
  wifi_ssid=<site Wi-Fi name>
  wifi_pwd=<site Wi-Fi password>
  mqtt_pwd=<broker password from add_gateway_user.ps1>
  tapo_email=<Tapo account email>
  tapo_pwd=<Tapo account password>
  setup_code=<the code on the unit's label, 8-10 chars>   # optional, see below
  # plug_cap_a=<default current cap in amps>              # optional
  # broker_url=<only to override the compiled-in broker>  # optional
  ```

  `tapo_email`/`tapo_pwd` can come from a `.env` instead (`--env`, keys
  `TAPO_USERNAME` or `TAPO_EMAIL`, and `TAPO_PASSWORD`).

  **`setup_code`:** if you leave it out, the firmware makes up a new one the
  next time its setup portal starts, and the code printed on the unit's label
  stops working. Copy the existing code in if the unit has a label.

## Use

```powershell
# 1. Build + verify only (no device touched). Prints key names and lengths, never values.
python tools\gateway_provision\provision_nvs.py --gateway-id 1cc3abb4fb54 `
    --secrets C:\path\outside\repo\gw-1cc3abb4fb54.local.txt --out C:\path\outside\repo\nvs.bin

# 2. Write it. Only when NO session is active on that gateway.
python tools\gateway_provision\provision_nvs.py --gateway-id 1cc3abb4fb54 `
    --secrets C:\path\outside\repo\gw-1cc3abb4fb54.local.txt --out C:\path\outside\repo\nvs.bin --write COM5
```

`--chip` defaults to esptool's auto-detect (`esp32` or `esp32c3` to force it).
`--baud` defaults to 115200, which is slow but survives a flaky USB cable. The
nvs offset/size come from `firmware/partitions_ota.csv`.

## Good to know

- **Secrets are never printed.** Tool errors are scrubbed of secret values.
  The temporary CSV the generator needs (plain text) is always deleted, even
  when the build fails. The built `nvs.bin` *does* hold the secrets in plain
  text, so delete it when you're done.
- **Secrets stay out of git.** The tool refuses a `--secrets` or `--out` path
  inside the repo checkout (`--allow-in-repo` overrides, for a gitignored
  path only), and `.gitignore` also covers `*.local.txt`, `nvs*.bin` and
  `*.bin.csv` as a backstop.
- **Only the `storage` settings are written.** Every other NVS key is gone
  afterwards: the session record, energy counters, the offline log, and any
  setting not in your file.
- Not for flash-encrypted units (`firmware/partitions_ota_enc.csv`); those need
  encrypted NVS, which this tool doesn't produce. No fielded unit uses it.
- Tests (pure logic, no IDF/serial needed; they include the exact esptool
  command `--write` runs and its confirmation gate):
  `python -m pytest tools/tests/test_provision_nvs.py`. CI runs all of
  `tools/tests` in its backend job.
