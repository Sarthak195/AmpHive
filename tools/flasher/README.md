# AmpHive Gateway Flasher

A single Windows program that puts the AmpHive gateway software onto a
plugged-in ESP32 board — no developer tools, no Python, no command line
required for the person doing the flashing.

## Key design decision: this never compiles firmware

**The EXE does not install or run the ESP-IDF toolchain, and it never
compiles firmware on the end user's machine.** ESP-IDF (the compiler suite
AmpHive's firmware is built with) is multi-gigabyte and nothing per-device
requires a rebuild: Wi-Fi credentials, MQTT broker credentials, and the plug
roster all live in the gateway's own NVS storage and are set at runtime
through its captive portal (see [`docs/FIRMWARE.md`](../../docs/FIRMWARE.md)
§2) — never baked into the binary.

So the flasher's whole job is: find the board, pick the matching **prebuilt**
firmware image, write it with [esptool](https://github.com/espressif/esptool),
and hand off to the captive portal for the rest. A maintainer with the real
toolchain builds those images ahead of time (see
["Maintainer: producing images"](#maintainer-producing-images) below); the EXE
itself only ever links against `esptool` + `pyserial`.

## For the person flashing a gateway

1. Get `AmpHiveFlasher.exe` from whoever gave you this (built by
   [`.github/workflows/flasher-exe.yml`](../../.github/workflows/flasher-exe.yml),
   or built locally — see below).
2. Plug the gateway board into this computer with a USB cable that carries
   data (not a phone charge-only cable).
3. Double-click `AmpHiveFlasher.exe`. A window opens and walks you through
   the rest in plain language — it detects the board, finds the right
   firmware image, asks you to confirm, flashes it, and verifies the write.
4. When it's done, follow the on-screen instructions: connect to the
   gateway's `AmpHive_Setup_XXXX` Wi-Fi network (password = the setup code
   on the unit's label) and open `http://192.168.4.1` to finish setup (your
   site's Wi-Fi, your Tapo account, and confirm the setup code).

If nothing shows up when you plug the board in, the tool explains the most
common reasons (charge-only cable, wrong USB port, missing driver) right in
the window.

### If Windows doesn't see the board (driver notes)

Most boards work with Windows' built-in drivers. If a board's USB-serial
chip isn't recognized, install the driver for whichever chip is on it:

- **CP210x** (Silicon Labs) — <https://www.silabs.com/developer-tools/usb-to-uart-bridge-vcp-drivers>
- **CH340/CH9102** (WCH) — <https://www.wch.cn/downloads/CH341SER_EXE.html>
- **FTDI FT232** — <https://ftdichip.com/drivers/vcp-drivers/>
- Boards with **native USB** (Espressif VID `303A`, e.g. most ESP32-C3/S3
  dev boards) normally need no separate driver on Windows 10/11.

## Command-line options

Double-clicking is the intended flow, but everything is also scriptable:

```
AmpHiveFlasher.exe [--port COM5] [--chip {esp32,esp32c3}] [--bin PATH]
                    [--images-dir DIR] [--no-download] [--yes]
                    [--non-interactive] [--dry-run] [--baud N]
```

| Flag | What it does |
|------|--------------|
| `--port` | Use this serial port, skip auto-detection |
| `--chip` | Force the chip type, skip esptool auto-detection |
| `--bin` | Flash this exact image file, skip the lookup/download logic |
| `--images-dir` | Look for (and save downloaded) images here (default: `firmware-images/` next to the program) |
| `--no-download` | Never try to fetch an image from GitHub releases |
| `--yes` | Don't ask for confirmation before writing to the device |
| `--non-interactive` | Never prompt for input (fail instead); implies `--yes`; skips the closing "press Enter" pause — for scripting, not for the novice flow |
| `--dry-run` | Detect everything and print what *would* happen, but never touch the device's flash |
| `--baud` | Serial baud rate while flashing (default 460800) |

Run `AmpHiveFlasher.exe --help` (from a terminal — this still works, it just
also pops its own console when double-clicked) for the same list.

## How image selection works

1. `esptool` detects the connected chip (`esp32` or `esp32c3` today).
2. The tool looks for `amphive-gateway-<chip>-merged.bin` in a
   `firmware-images/` folder next to the EXE.
3. If it's not there (and `--no-download` wasn't passed), it tries to
   download the matching asset from the latest GitHub release of
   [`Sarthak195/AmpHive`](https://github.com/Sarthak195/AmpHive/releases).
   If the repo is private, unreachable, or has no matching release asset yet,
   you get a plain-language explanation instead of a stack trace — drop the
   file in `firmware-images/` or pass `--bin <path>` instead.
4. `--bin <path>` always wins outright, skipping steps 2–3.

The image is written as a single blob at flash offset `0x0` — it already
contains the bootloader, partition table, and app, merged ahead of time (see
below), so one `esptool write-flash 0x0 <file>` is the entire flash
operation. After writing, the tool reads flash back and compares it against
the file before declaring success.

## Maintainer: producing images

Images are built with the real ESP-IDF toolchain and `idf.py merge-bin`,
**not** by this tool or by CI. From a shell with your ESP-IDF environment
already active (`export.ps1`/`export.bat` — this repo's firmware is IDF
v5.3.x, not v6):

```powershell
. C:\path\to\your\esp-idf\export.ps1     # your IDF install, not this repo's business
.\tools\flasher\scripts\build-merged-image.ps1 -Chip esp32c3
```

This runs `idf.py set-target esp32c3`, `idf.py build`, then
`idf.py merge-bin`, and writes
`tools/flasher/firmware-images/amphive-gateway-esp32c3-merged.bin`. The
script never hardcodes an IDF path (it only checks `$env:IDF_PATH` is set) —
so it works regardless of where ESP-IDF happens to be installed on whichever
machine runs it.

Today only `esp32c3` actually builds from this repo's `firmware/` tree —
`sdkconfig.defaults` is tuned specifically for the real fielded hardware (no
PSRAM, dual-OTA partitions sized for it; see
[`docs/FIRMWARE.md`](../../docs/FIRMWARE.md) §6). `esp32` is supported by
this tool's chip→image mapping for forward compatibility, but producing a
real image for it needs its own `sdkconfig`/partition work first.

To publish an image so the flasher (and end users) can find it automatically:
attach `amphive-gateway-<chip>-merged.bin` as a release asset on a
[GitHub release](https://github.com/Sarthak195/AmpHive/releases) of this
repo, using exactly that filename.

## Maintainer: building the EXE

Locally:

```powershell
pip install -r tools/flasher/requirements.txt
pytest tools/flasher/tests -v
pyinstaller tools/flasher/AmpHiveFlasher.spec --distpath tools/flasher/dist --workpath tools/flasher/build
```

Or push a `flasher-v*` tag (or run the workflow manually) — see
[`.github/workflows/flasher-exe.yml`](../../.github/workflows/flasher-exe.yml).
It runs the test suite, then builds and uploads `AmpHiveFlasher.exe` as a
workflow artifact on `windows-latest`. It does **not** build firmware — that
stays a manual, maintainer-run step (above), since it would mean installing
the multi-gigabyte ESP-IDF on every CI run for an artifact that only changes
when firmware does.

The built EXE does not bundle any firmware images — it finds them at
runtime via `firmware-images/`, `--bin`, or a GitHub release, exactly as
described above.

## Development

```powershell
pip install -r tools/flasher/requirements.txt
pytest tools/flasher/tests -v          # pure logic only, serial/esptool/network all mocked
python -m amphive_flasher --dry-run --non-interactive   # exercise the real CLI, no device needed
```

The code is split so hardware and network I/O stay out of the parts that
need to be tested without a real board:

- `amphive_flasher/ports.py` — pure port-ranking/selection logic (the only
  I/O is the pyserial enumeration call itself).
- `amphive_flasher/images.py` — chip→image filename mapping, local lookup,
  and the GitHub-release download fallback (network calls go through
  `urllib`, easily mocked).
- `amphive_flasher/flasher.py` — the only module that actually talks to
  `esptool`/hardware; deliberately thin.
- `amphive_flasher/cli.py` — argument parsing and orchestration (`run()`),
  wired to the above through a small `Dependencies` dataclass that tests
  swap out wholesale — no serial port or network connection is ever touched
  by `pytest tools/flasher/tests`.
- `amphive_flasher/ui.py` — console text: banner, the "no device found"
  explanation, the exit-pause. Plain ASCII on purpose — some Windows
  consoles mangle non-ASCII punctuation depending on the active codepage,
  and this tool is for someone who can't debug that.
