"""Thin wrapper around esptool: chip detection, flashing, verification.

This is the only module that talks to real hardware. It is intentionally
small and side-effect-isolated so ``cli.py`` can depend on it through a
narrow interface that tests replace with fakes - nothing in here is
exercised by the test suite directly (that would require real hardware);
instead the orchestration logic in ``cli.py`` is tested with these functions
mocked out.

esptool's public, documented entry points (``esptool.detect_chip`` and
``esptool.main``) are used rather than shelling out to a separate process,
because the PyInstaller onefile build has no separate Python/esptool
executable to shell out to - ``sys.executable`` *is* the frozen app.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from amphive_flasher.images import FlasherError

FLASH_OFFSET = "0x0"  # merged images are built to be written at offset 0x0
DEFAULT_BAUD = 460800


@dataclass(frozen=True)
class DetectedChip:
    chip_name: str  # esptool's own name, e.g. "ESP32-C3"
    port: str


def detect_chip(port: str, baud: int = 115200) -> DetectedChip:
    """Open ``port``, confirm a real Espressif chip answers, report its type.

    Closes the port again before returning so a subsequent flash/verify call
    (which opens its own connection) doesn't fight over the handle.
    """
    import esptool

    try:
        esp = esptool.detect_chip(port=port, baud=baud)
    except esptool.FatalError as exc:
        raise FlasherError(
            f"Couldn't talk to a chip on {port}: {exc}\n"
            "  This usually means: the cable is charge-only (no data lines), the board "
            "needs its BOOT button held while powering on, another program has the port "
            "open (close serial monitors/Arduino IDE), or it's simply not an ESP32."
        ) from exc
    except Exception as exc:  # pragma: no cover - defensive catch-all for pyserial errors etc.
        raise FlasherError(f"Couldn't open {port}: {exc}") from exc

    try:
        chip_name = esp.CHIP_NAME
    finally:
        try:
            esp.serial_port.close()
        except Exception:  # pragma: no cover - best-effort cleanup
            pass

    return DetectedChip(chip_name=chip_name, port=port)


def flash_image(port: str, chip: str, image_path: Path, baud: int = DEFAULT_BAUD) -> None:
    """Write ``image_path`` to flash at offset 0x0 on ``chip``/``port``."""
    import esptool

    argv = [
        "--chip",
        chip,
        "--port",
        port,
        "--baud",
        str(baud),
        "write-flash",
        FLASH_OFFSET,
        str(image_path),
    ]
    try:
        esptool.main(argv)
    except SystemExit as exc:
        raise FlasherError(
            f"Flashing failed (esptool exited with an error). Common causes: wrong chip "
            f"selected, a loose USB connection mid-flash, or the board needs BOOT held "
            f"during the write. Try again with the board freshly power-cycled.\n"
            f"  esptool exit detail: {exc}"
        ) from exc
    except Exception as exc:  # pragma: no cover - defensive catch-all
        raise FlasherError(f"Flashing failed: {exc}") from exc


def verify_image(port: str, chip: str, image_path: Path, baud: int = DEFAULT_BAUD) -> None:
    """Read back flash at offset 0x0 and confirm it matches ``image_path``."""
    import esptool

    argv = [
        "--chip",
        chip,
        "--port",
        port,
        "--baud",
        str(baud),
        "verify-flash",
        FLASH_OFFSET,
        str(image_path),
    ]
    try:
        esptool.main(argv)
    except SystemExit as exc:
        raise FlasherError(
            "Verification failed: the flash content doesn't match the image that was just "
            "written. Don't power-cycle the device yet - re-run the flasher against the "
            f"same port.\n  esptool exit detail: {exc}"
        ) from exc
    except Exception as exc:  # pragma: no cover - defensive catch-all
        raise FlasherError(f"Verification failed: {exc}") from exc
