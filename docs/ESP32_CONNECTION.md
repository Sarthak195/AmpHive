# ESP32 Connection, Build & Flashing Guide

Complete reference for building, flashing, and monitoring the AmpHive gateway
firmware — on the real fielded **ESP32-C3** board and on **other ESP32 models**.
Firmware architecture is in [FIRMWARE.md](FIRMWARE.md).

> **Toolchain version matters.** This firmware is written for **ESP-IDF v5.3**
> (v5.x LTS). It does **not** build as-is on **ESP-IDF v6.0** — v6 removed the
> `json` and `mqtt` core components and upgraded to mbedTLS 4.x, which the
> vendored `microlink` client is not compatible with. See
> [§8 ESP-IDF v6 incompatibilities](#8-esp-idf-v6-incompatibilities) before using v6.

---

## 1. Hardware

| Item | Detail |
|------|--------|
| **Default board (real fielded hardware)** | ESP32-C3 (~4 MB flash, **no PSRAM**) |
| **Cable** | USB-C **data** cable — charge-only cables silently fail to enumerate |
| **USB-UART bridge** | Onboard CP210x or native USB-Serial/JTAG, depending on board |

The firmware also runs on other ESP32 targets (see [§5](#5-flashing-other-esp32-models)),
but the committed `sdkconfig.defaults` is tuned for the C3 (4 MB flash, no PSRAM —
no `CONFIG_SPIRAM*` lines are set). Other chips, especially ones **with** PSRAM,
need flash/PSRAM settings adjusted (see §5). An earlier revision of this doc
described an ESP32-S3-N16R8 (16 MB flash / 8 MB octal PSRAM) default; that was
never what `sdkconfig.defaults` actually targets and has been corrected here.

### Identify the serial port

| OS | Port looks like | How to find it |
|----|-----------------|----------------|
| **Windows** | `COM3`, `COM7`, … | Device Manager → *Ports (COM & LPT)*, or the PowerShell command below |
| **macOS** | `/dev/tty.usbserial-*`, `/dev/tty.usbmodem*`, `/dev/tty.SLAB_USBtoUART` | `ls /dev/tty.*` before/after plugging in |
| **Linux** | `/dev/ttyUSB0` (CP210x) or `/dev/ttyACM0` (native USB) | `ls /dev/ttyUSB* /dev/ttyACM*`; `dmesg | tail` after plugging in |

Windows — run before and after plugging in and diff the list:
```powershell
Get-PnpDevice -PresentOnly -Class Ports | Select-Object FriendlyName, Status
# or, to see just COM names:
[System.IO.Ports.SerialPort]::GetPortNames()
```

---

## 2. Install the toolchain (ESP-IDF v5.3)

Pick **one** install method. **Target version: v5.3.x** (5.4.x also works; avoid 6.x).

**A. Espressif IDF Installation Manager (EIM) / Windows installer** — recommended on Windows.
Download from <https://dl.espressif.com/dl/esp-idf/> and select version **v5.3**. EIM
installs the framework under e.g. `C:\esp\v5.3\esp-idf` and the tools under `C:\Espressif`.

**B. Manual git clone (any OS):**
```bash
mkdir -p ~/esp && cd ~/esp
git clone -b v5.3.2 --recursive https://github.com/espressif/esp-idf.git
cd esp-idf && ./install.sh esp32c3        # Windows: .\install.ps1 esp32c3
```

**C. VS Code** — the *Espressif IDF* extension installs and manages v5.3 for you.

> ⚠️ Do not `winget install Espressif.EspIdf` blindly — it may pull **v6.x**, which
> this firmware does not build against (see [§8](#8-esp-idf-v6-incompatibilities)).

---

## 3. Activate the environment (every new terminal)

ESP-IDF must be "exported" into your shell before `idf.py` works.

**Windows PowerShell** (native git-clone install):
```powershell
. $HOME\esp\esp-idf\export.ps1
```

**macOS / Linux (bash/zsh):**
```bash
. ~/esp/esp-idf/export.sh
```

**Windows via EIM** — EIM writes a PowerShell activation profile. Dot-source it:
```powershell
. "C:\Espressif\tools\Microsoft.<ver>.PowerShell_profile.ps1"
```

### ⚠️ Git Bash / MSYS is NOT supported
ESP-IDF's `export.sh` refuses to run under Git Bash / MSYS/MinGW
(*"MSys/Mingw is not supported"*). On Windows, **use PowerShell**, not Git Bash.

To drive a build from a Git Bash (or CI) shell, shell out to PowerShell and load the
profile first — this is the recipe that works on the current dev machine (EIM install):
```bash
powershell -NoProfile -ExecutionPolicy Bypass -Command \
  'Remove-Item Env:MSYSTEM -ErrorAction SilentlyContinue; \
   . "C:\Espressif\tools\Microsoft.v5.3.PowerShell_profile.ps1" *> $null; \
   Set-Location "C:\Users\<you>\Documents\AmpHive\firmware"; \
   idf.py -p COM3 flash monitor'
```
(`Remove-Item Env:MSYSTEM` stops idf.py mistaking the child process for an MSYS shell.)

Verify activation:
```bash
idf.py --version    # expect: ESP-IDF v5.3.x
```

---

## 4. Build, flash & monitor (default ESP32-C3)

```bash
cd firmware

# 1. One-time (or after changing chip target): generates sdkconfig from sdkconfig.defaults
idf.py set-target esp32c3

# 2. Build
idf.py build

# 3. Flash + open serial monitor (replace COM3 with your port from §1)
idf.py -p COM3 flash monitor
```

Split commands if you prefer:
```bash
idf.py -p COM3 flash       # flash only
idf.py -p COM3 monitor     # monitor only (already-flashed board)
idf.py -p COM3 app-flash   # flash only the app partition (faster; skips bootloader/table)
```

**Serial monitor shortcuts:**
| Keys | Action |
|------|--------|
| `Ctrl+]` | Exit monitor |
| `Ctrl+T` then `Ctrl+R` | Reset the chip |
| `Ctrl+T` then `Ctrl+F` | Rebuild + flash app, keep monitoring |
| `Ctrl+T` then `Ctrl+H` | Help (all shortcuts) |

If flashing can't sync (`Failed to connect… No serial data received`): hold the **BOOT**
button, tap **RESET/EN**, release BOOT once "Connecting…" appears (forces download mode).

---

## 5. Flashing other ESP32 models

The build is retargetable. The only per-chip step is `set-target`, plus adjusting
PSRAM/flash config for boards that differ from the real fielded C3 (which has none).

```bash
# Pick the chip, then rebuild + flash
idf.py set-target esp32c3     # ESP32-C3  (default; RISC-V, no PSRAM — real fielded hardware)
idf.py set-target esp32       # classic ESP32 (Xtensa)
idf.py set-target esp32s2     # ESP32-S2  (Xtensa, single core)
idf.py set-target esp32s3     # ESP32-S3  (Xtensa, PSRAM-capable — historical dev-board target, not fielded)
idf.py set-target esp32c6     # ESP32-C6  (RISC-V, Wi-Fi 6)
idf.py set-target esp32h2     # ESP32-H2  (RISC-V, 802.15.4 — no Wi-Fi, not usable as a gateway)
idf.py build
idf.py -p <PORT> flash monitor
```

`set-target` **wipes `sdkconfig`** and regenerates it from `sdkconfig.defaults`, so
re-apply any board-specific settings afterward (or put them in `sdkconfig.defaults`):

- **PSRAM:** `sdkconfig.defaults` has **no `CONFIG_SPIRAM*` lines** — correct for the
  real fielded **C3, which has no PSRAM**. Boards **with** PSRAM (S3 modules) need
  `CONFIG_SPIRAM_MODE_OCT` (octal, e.g. S3-N16R8) or `CONFIG_SPIRAM_MODE_QUAD`
  (quad, most WROVER/other S3 modules) added back. This matters much less than it
  once did: the only PSRAM-hungry code was the `microlink` overlay client's 32 KB
  task, and `microlink` was **retired by the 2026-07-10 direct-MQTT pivot and
  removed 2026-08-02** (see [FIRMWARE.md §5](FIRMWARE.md#5-historical-microlink-removed-2026-08-02)) —
  so a no-PSRAM chip building the default direct-MQTT firmware is expected, not a
  degraded fallback. See AGENTS.md rule 3.
- **Flash size:** set `CONFIG_ESPTOOLPY_FLASHSIZE_*` to match the module (the
  fielded C3 uses `4MB`).
- **Chip family:** RISC-V targets (C3/C6/H2) use a different toolchain, installed
  automatically by `install.sh <target>` or EIM.
- Use `idf.py menuconfig` to change these interactively, or edit `sdkconfig.defaults`
  and `rm -rf build sdkconfig` for a clean regeneration.

> Practical note: the gateway just needs **Wi-Fi**; it no longer needs "enough RAM
> for the overlay client" now that direct MQTT is the only transport and
> `microlink` has been removed. **ESP32-C3 (no PSRAM) is the intended, fielded
> target.** ESP32-S3 (with PSRAM) remains buildable if you need a PSRAM-capable
> dev board for other reasons, but there is no overlay build left to flip back to.

---

## 6. First boot — captive-portal provisioning

On a board with no stored config (factory-fresh or after `erase-flash`):

1. The board starts a **WPA2** SoftAP: **`AmpHive_Setup_XXXX`** (XXXX = last MAC
   bytes). Since fw 1.6.0 the passphrase is the per-device **setup code** —
   generated on first portal start, persisted in NVS, and printed prominently
   on the serial console (`idf.py monitor`). **Copy it onto the unit label**;
   it's needed for every (re-)provisioning. A full NVS erase generates a new
   code — re-label.
2. Join that Wi-Fi network from a phone (password = setup code) — the portal
   (2026-08-02 rework) is a mobile-first two-step wizard; a laptop works too.
3. Browse to **`http://192.168.4.1`**.
4. **Step 1 of 2 — Wi-Fi:** enter the setup code, then either tap a network
   from the scanned list (`GET /scan` — a live "nearby networks" list; falls
   back to manual typing if the scan finds nothing or the browser blocks it)
   or type the SSID directly, plus the Wi-Fi password. `gateway_id`, device
   name, and MQTT username are derived from the MAC, not typed — the
   auto-detected Device ID is shown at the top of the page.
5. **Step 2 of 2 — smart plug:** Tapo account email + password (same login as
   the Tapo app). The per-gateway **MQTT password is optional** here — see
   the table below.
   | Field | Example | Notes |
   |-------|---------|-------|
   | Setup Code | `x7kq2m9pfw` | from the unit label / serial log; gates `/save` |
   | WiFi network | `HomeNet` | 2.4 GHz network the plug is on; tap-to-fill or type |
   | WiFi Password | | |
   | **Tapo Account Email** | `you@example.com` | Tapo cloud login; used for KLAP auth |
   | **Tapo Account Password** | | stored in NVS (plaintext, prototype) |
   | MQTT Password *(under "Installer options", optional)* | | the per-gateway broker credential (`add_gateway_user.ps1`); username == gateway id. A blank submission does **not** overwrite an existing NVS value — a preflashed unit can ship with this pre-provisioned so an ordinary buyer never sees or types it (deploy/docs/preflashed_unit_runbook.md). Installers/re-provisioning still set it here as before. |
6. Submit ("Finish setup") → config is written to NVS and the board reboots
   into normal operation. A wrong setup code gets a friendly error page (403,
   1 s throttle per attempt) and nothing is saved; a Wi-Fi association
   failure (TD#31 pre-check) shows a similar error without rebooting. The
   portal reboots the board after **10 min** with no HTTP activity.
7. **Then, on the CPO portal (`cpo.amphive.app`):** "Add gateway" → enter the
   **claim code** printed on the unit's label to bind it to your account (see
   docs/API_REFERENCE.md's `POST /api/cpo/gateways/claim` and
   deploy/docs/buyer_setup_card.md) — no operator hand-registration needed.

> **Plug IPs are managed in the backend, not here (fw ≥ 2.0.0-direct).** The
> gateway no longer takes a plug IP at provisioning — the operator registers each
> plug (with its LAN IP) in the CPO dashboard, and the backend pushes the full
> plug roster to the gateway over MQTT (retained `/config` topic). A plug's IP is
> editable later via `PUT /api/cpo/plugs/{id}` if a DHCP lease changes. The Tapo
> plug still needs **"Third-Party Compatibility" enabled** in the Tapo app for the
> gateway's local KLAP control to work.

---

## 7. Erase / reprovision / reset

```bash
# Wipe the whole flash (firmware + ALL NVS: Wi-Fi, sessions, offline telemetry, Tapo creds)
idf.py -p COM3 erase-flash

# Wipe ONLY the NVS partition (keeps firmware; forces re-provisioning on next boot)
parttool.py -p COM3 erase_partition --partition-name nvs

# Re-provision without erasing: AmpHive_Setup_XXXX only appears if config is
# missing/invalid — to force it, erase NVS as above, or change Wi-Fi so STA connect
# fails. The setup code survives re-provisioning (NVS key `setup_code`); only a
# full NVS/flash erase regenerates it (watch serial on next portal start, re-label).
```

Raw esptool equivalents (when idf.py isn't available, e.g. flashing a prebuilt binary):
```bash
esptool.py -p COM3 -b 460800 erase_flash
esptool.py -p COM3 -b 460800 --chip esp32c3 write_flash 0x0 build/flash_image.bin
# or the individual images idf.py prints after a build:
#   0x0 bootloader/bootloader.bin, 0x8000 partition_table/partition-table.bin,
#   0x10000 amphive-gateway.bin
```

---

## 8. ESP-IDF v6 incompatibilities

> Recorded so future work doesn't repeat the investigation. As of 2026-07, the dev
> machine had **v6.0.1** installed; the firmware targets **v5.3**. Building on v6.0.1
> surfaced a cascade of breaking changes:

| v6 change | Symptom | Workaround if you must use v6 |
|-----------|---------|-------------------------------|
| `json` (cJSON) removed from core | `Failed to resolve component 'json' required by 'main'` | Vendored locally at `firmware/components/json/` (cJSON v1.7.18) — **already applied** |
| `mqtt` (esp-mqtt) removed from core | `Failed to resolve component 'mqtt' required by 'main'` | Added `espressif/mqtt: "^1.0.0"` in `firmware/main/idf_component.yml` — **already applied** |

The two rows below applied only to the retired `microlink`/`wireguard_lwip`
components (removed 2026-08-02) and no longer apply — kept as historical
reference for anyone reviving that transport from git history:

| v6 change (historical, pre-removal) | Symptom | Workaround if you must use v6 |
|-----------|---------|-------------------------------|
| ~~GCC 15 `-Werror` (new warnings)~~ | `-Werror=unterminated-string-initialization` in `wireguard_lwip`/`microlink` | `-Wno-error` was added to those two components' `CMakeLists.txt` |
| ~~mbedTLS 3.x → 4.x~~ | `fatal error: mbedtls/entropy.h: No such file or directory` in `microlink_derp.c` | Not resolved — microlink used mbedTLS 3.x entropy/DRBG/SSL APIs removed in 4.x |

**Bottom line:** use **ESP-IDF v5.3** to build this firmware. The v6 fixes above
(json/mqtt vendoring) are committed and harmless on v5.3; whether the remaining
`main/`-only sources build cleanly on v6 is unverified — v5.3 remains the tested
target.

---

## 9. Troubleshooting

| Symptom | Fix |
|---------|-----|
| `MSys/Mingw is not supported` / `idf.py: command not found` in Git Bash | Use **PowerShell**, or shell out to it (see [§3](#3-activate-the-environment-every-new-terminal)) |
| `No serial data received` / `Failed to connect to ESP32` | Bad/charge-only cable; wrong COM port; hold **BOOT**, tap **RESET**, release BOOT after "Connecting…" |
| `A fatal error occurred: Wrong boot mode detected` | Board stuck in download mode — press **RESET/EN** once |
| `Failed to resolve component 'json'` / `'mqtt'` | You're on ESP-IDF v6 — see [§8](#8-esp-idf-v6-incompatibilities); switch to v5.3 |
| `mbedtls/entropy.h: No such file` | ESP-IDF v6 (mbedTLS 4.x) — switch to v5.3 |
| Board boot-loops after flashing | Watch serial for the panic; common causes: NVS key mismatch (→ erase NVS), or PSRAM config wrong for the board (→ [§5](#5-flashing-other-esp32-models)) |
| `Brownout detector was triggered` | Underpowered USB port/cable — use a powered hub or a better cable |
| Garbled monitor output | Baud mismatch — `idf.py monitor` uses **115200**; don't override it |
| `Permission denied: /dev/ttyUSB0` (Linux) | `sudo usermod -aG dialout $USER`, then log out/in |
| Plug commands do nothing | Wrong plug IP (DHCP changed it) or Tapo creds. Fix the plug's `local_ip` in the CPO dashboard (`PUT /api/cpo/plugs/{id}`) — the backend re-pushes the retained roster and the gateway re-IPs the slot; no re-provisioning needed (fw ≥ 2.0.0-direct). Validate creds with `python tools/klap_probe.py <ip>` |
| KLAP `handshake1 auth mismatch` in serial log | Wrong Tapo email/password, or "Third-Party Compatibility" disabled in the Tapo app. **Also happens after rotating the Tapo account password** — the gateway's NVS still holds the old one (bit us 2026-07-06). Update the `tapo_pwd` key in NVS namespace `storage` (a minimal one-off app calling `nvs_set_str` works, and preserves the machine key/Wi-Fi/energy state) or erase NVS and re-provision |

---

## 10. Verifying the Implementation (NAT Traversal & Magicsock) — RETIRED

> **RETIRED (2026-07-10 direct-MQTT pivot; code removed 2026-08-02).** This
> whole verification procedure is for the `microlink` Tailscale-overlay
> transport, which was defeated by symmetric NAT (root-caused 2026-07-09) and
> has been **removed from the tree** (`AMPHIVE_DIRECT_MQTT=1` is now the only
> transport; see [FIRMWARE.md §5](FIRMWARE.md#5-historical-microlink-removed-2026-08-02)).
> There is no GCP-VM Tailscale node or magicsock path to verify on a
> direct-MQTT gateway anymore — the equivalent live check is confirming a TLS
> session to the public broker (`mqtts://mqtt.amphive.app:8883`) in the
> gateway's serial log and an `online` MQTT status. Kept below only as
> historical reference (see git history prior to 2026-08-02 for the removed
> `microlink`/`wireguard_lwip` source).

To verify that the unified port architecture (magicsock mode) is functioning correctly and a direct connection is established between the ESP32 and the GCP VM:

1. **Monitor the ESP32 logs**:
   Run the serial monitor at 115200 baud (replace `COM5` with your active port):
   ```powershell
   . "C:\Espressif\tools\Microsoft.v6.0.1.PowerShell_profile.ps1"
   cd firmware
   idf.py -p COM5 monitor
   ```
   Look for the following log outputs:
   - **Socket Binding**:
     ```
     I (1109) ml_disco: Initializing DISCO protocol (IPv4 + IPv6 + fast PONG task)
     ...
     I (1144) ml_disco: DISCO/magicsock bound to port 41641 (shared with STUN)
     ```
   - **STUN Probing**:
     ```
     I (100) ml_connection: Running STUN probe BEFORE MapRequest to discover endpoint...
     I (230) ml_stun: Trying STUN server stun.l.google.com:19302 (via DISCO socket fd=52, port=41641)
     I (309) ml_stun: STUN probe successful: public endpoint 106.222.215.123:17014
     ```
   - **Endpoint Advertisement**:
     ```
     I (2650) ml_coord: Advertising local endpoint: 192.168.1.12:41641 (type: Local, magicsock port)
     I (2665) ml_coord: Advertising STUN endpoint: 106.222.215.123:17014 (type: STUN)
     ```
   - **Simultaneous Hole Punching**:
     ```
     I (21990) ml_disco: CallMeMaybe from peer 1 (amphive-vm-in.tail36e589.ts.net)
     I (22030) ml_disco: SIMULTANEOUS HOLE PUNCH: Probing 5 CMM endpoints IMMEDIATELY!
     I (22330) ml_disco: Sending DISCO PING to 8.231.81.12:41641
     ```

2. **Verify status on the GCP VM**:
   SSH into the VM and query the Tailscale status:
   ```bash
   gcloud compute ssh amphive-vm-in --zone=asia-south1-a --command="sudo tailscale status"
   ```
   Confirm that the path for `gateway-1` is marked as `active; direct <IP>:<PORT>` instead of `relay "blr"`:
   ```
   100.x.x.x      gateway-1        you@example.com  linux  active; direct <public-ip>:<port>, tx <n> rx <n>
   ```

3. **Ping from the GCP VM**:
   Trigger a direct ping command from the VM to the ESP32:
   ```bash
   gcloud compute ssh amphive-vm-in --zone=asia-south1-a --command="sudo tailscale ping 100.83.175.20"
   ```
   Ensure it receives a direct response:
   ```
   pong from gateway-1 (100.83.175.20) via 106.222.215.123:17014 in 505ms
   ```

---

## 11. Quick reference

```bash
# --- one-time toolchain (v5.3) ---
# install ESP-IDF v5.3 (EIM on Windows, or git clone -b v5.3.2 --recursive)

# --- every terminal ---
. ~/esp/esp-idf/export.sh                 # macOS/Linux
. $HOME\esp\esp-idf\export.ps1            # Windows PowerShell
# on this workstation (v5.3.3 lives at C:\esp\v5.3.3; don't use the EIM v6.0.1 profile):
$env:IDF_TOOLS_PATH = "$env:USERPROFILE\.espressif"; . C:\esp\v5.3.3\esp-idf\export.ps1

# --- build/flash cycle ---
cd firmware
idf.py set-target esp32c3                 # real fielded target: ESP32-C3, no PSRAM, direct-MQTT default
idf.py build
idf.py -p COM5 flash monitor              # replace COM5 with your CP210x COM port

# --- reset ---
idf.py -p COM5 erase-flash                # wipe everything -> re-provision on boot

# --- validate the plug independently (host, not the ESP32) ---
python tools/klap_probe.py 192.168.1.5    # handshake + real power read
```

See also: [FIRMWARE.md](FIRMWARE.md) (architecture), [MQTT_CONTRACT.md](MQTT_CONTRACT.md)
(topics), [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md) (what works today).
