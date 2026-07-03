# ESP32-S3 Connection & Flashing Guide

Quick reference for building, flashing, and monitoring the AmpHive gateway firmware.
Full firmware architecture is in [FIRMWARE.md](FIRMWARE.md).

---

## Hardware

- **Target board:** ESP32-S3-N16R8 (16 MB flash, 8 MB PSRAM)
- **Cable:** USB-C data cable (not charge-only — cheap cables silently fail)
- **Port (Windows):** Shows as `COMx` in Device Manager under "Silicon Labs CP210x" or "USB Serial Device"
- **Port (macOS):** `/dev/tty.usbmodem*` or `/dev/tty.SLAB_USBtoUART`
- **Port (Linux):** `/dev/ttyUSB0` or `/dev/ttyACM0`

To identify the port on Windows, run before and after plugging in:
```powershell
Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -match 'USB' } | Select-Object FriendlyName, Status
```

---

## Prerequisites

ESP-IDF v5.3 must be installed and activated. One-time setup:

```bash
# Windows (PowerShell, run once)
winget install Espressif.EspIdf

# After install, activate in every new terminal
. $env:IDF_PATH\export.ps1          # PowerShell
# or
source $IDF_PATH/export.sh          # bash / Git Bash
```

Verify:
```bash
idf.py --version   # should print 5.3.x
```

---

## Build, Flash & Monitor

```bash
cd firmware

# First time or after a target change
idf.py set-target esp32s3

# Build only
idf.py build

# Flash + open serial monitor (replace COM3 with your port)
idf.py -p COM3 flash monitor

# Monitor only (board already flashed)
idf.py -p COM3 monitor
```

**Monitor shortcuts:**
| Key | Action |
|-----|--------|
| `Ctrl+]` | Exit monitor |
| `Ctrl+T Ctrl+R` | Reset chip |
| `Ctrl+T Ctrl+F` | Flash without re-building |

---

## First Boot (Captive Portal)

On a board with no NVS config (factory or after `idf.py erase-flash`):

1. Board starts SoftAP: **`AmpHive_Setup_XXXX`** (open, no password)
2. Connect your laptop to that network
3. Navigate to `http://192.168.4.1`
4. Fill in: WiFi SSID, WiFi password, Tailscale auth key, device name, gateway ID, target plug IP
5. Submit → board reboots and connects to your WiFi

---

## Erase NVS / Full Reset

```bash
# Erase entire flash (wipes WiFi config, NVS sessions, offline telemetry)
idf.py -p COM3 erase-flash

# Erase only the NVS partition (preserves firmware)
parttool.py -p COM3 erase_partition --partition-name nvs
```

---

## Common Issues

| Symptom | Fix |
|---------|-----|
| `No serial data / port not found` | Try a different USB cable; check Device Manager for the COM port |
| `Failed to connect to ESP32` | Hold **BOOT** button while `idf.py flash` starts connecting, release after "Connecting…" |
| `Wrong target` error | Run `idf.py set-target esp32s3` then rebuild |
| Board reboots in a loop | Watch serial for the crash reason; likely NVS key mismatch — erase NVS |
| Monitor garbled output | Baud rate mismatch; set to **115200** (`idf.py monitor` uses this by default) |
| `Permission denied /dev/ttyUSB0` (Linux) | `sudo usermod -aG dialout $USER` then log out/in |

---

## Checking NVS Config Live

```bash
# In the serial monitor, the boot log prints stored NVS keys on startup
# Look for lines like:
#   [NVS] ssid=MyNetwork
#   [NVS] device_name=gateway-01
```

---

## Useful idf.py Flags

```bash
# Verbose build output
idf.py build -v

# Set log level to DEBUG
idf.py -p COM3 monitor --print-filter="*:D"

# Override a sdkconfig value without editing the file
idf.py -DCONFIG_LOG_DEFAULT_LEVEL=5 build
```
