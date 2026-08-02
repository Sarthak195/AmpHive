"""Interactive console flow: argument parsing + orchestration.

Talks to hardware and the network only through the small ``Dependencies``
seam below, so ``run()`` - the actual decision logic (which port, which
chip, which image, ask-or-don't-ask) - can be unit tested with everything
mocked out, no serial port or GitHub connection required.
"""

from __future__ import annotations

import argparse
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from amphive_flasher import flasher, images, ports, ui
from amphive_flasher.images import FlasherError, ResolvedImage
from amphive_flasher.ports import PortDecisionKind, PortInfo, RankedPort


@dataclass
class Dependencies:
    """Everything `run()` needs that isn't pure logic - swapped out in tests.

    Defaults are bound lazily via `default_factory` (an attribute lookup on
    the module at *construction* time) rather than captured directly at
    class-definition time, so `monkeypatch.setattr(cli.ports, "list_serial_ports", ...)`
    style patching - applied before a `Dependencies()`/`main()` call - works
    as expected instead of silently patching a copy nobody uses.
    """

    list_ports: Callable[[], list[PortInfo]] = field(default_factory=lambda: ports.list_serial_ports)
    detect_chip: Callable[[str], flasher.DetectedChip] = field(default_factory=lambda: flasher.detect_chip)
    flash_image: Callable[[str, str, Path, int], None] = field(
        default_factory=lambda: flasher.flash_image
    )
    verify_image: Callable[[str, str, Path, int], None] = field(
        default_factory=lambda: flasher.verify_image
    )
    resolve_image: Callable[[str, Path, "Path | None"], ResolvedImage] = field(
        default_factory=lambda: images.resolve_image
    )
    download_latest_image: Callable[[str, Path], ResolvedImage] = field(
        default_factory=lambda: images.download_latest_image
    )
    confirm: Callable[[str], bool] = field(default_factory=lambda: ui.default_confirm)
    prompt: Callable[[str], str] = field(default_factory=lambda: input)
    print: Callable[[str], None] = field(default_factory=lambda: print)


def default_images_dir() -> Path:
    """firmware-images/ next to the running program.

    Frozen (PyInstaller onefile): next to the .exe.
    Source checkout: tools/flasher/firmware-images/.
    """
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
    else:
        base = Path(__file__).resolve().parent.parent
    return base / "firmware-images"


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="amphive-flasher",
        description=(
            "Detects a plugged-in AmpHive gateway board and flashes it with a "
            "ready-made firmware image. Does not install or run any developer "
            "toolchain - see tools/flasher/README.md."
        ),
    )
    parser.add_argument("--port", help="Serial port to use, e.g. COM5 (skips auto-detect)")
    parser.add_argument(
        "--chip",
        choices=images.known_chip_names(),
        help="Force the chip type instead of asking esptool to detect it",
    )
    parser.add_argument(
        "--bin",
        dest="bin_path",
        type=Path,
        default=None,
        help="Use this exact firmware image file instead of looking one up",
    )
    parser.add_argument(
        "--images-dir",
        type=Path,
        default=None,
        help="Folder to look for (and save downloaded) firmware images in "
        "(default: firmware-images next to this program)",
    )
    parser.add_argument(
        "--no-download",
        action="store_true",
        help="Never try to fetch an image from GitHub releases; local files or --bin only",
    )
    parser.add_argument(
        "--yes", action="store_true", help="Don't ask for confirmation before writing to the device"
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Never prompt for input (fail with an error instead); implies --yes and skips "
        "the final 'press Enter' pause. Intended for scripting/CI, not for a novice user.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Detect the board and image, print what would happen, but don't touch the flash",
    )
    parser.add_argument(
        "--baud", type=int, default=flasher.DEFAULT_BAUD, help="Serial baud rate used while flashing"
    )
    return parser


def _describe_port(p: PortInfo, label: str) -> str:
    bits = [label]
    if p.vid is not None and p.pid is not None:
        bits.append(f"VID:PID {p.vid:04X}:{p.pid:04X}")
    if p.description:
        bits.append(p.description)
    return " - ".join(bits)


def _prompt_for_port(candidates: tuple[RankedPort, ...], deps: Dependencies) -> str:
    deps.print("\nI found more than one possible device. Which one is the AmpHive gateway?\n")
    for i, r in enumerate(candidates, start=1):
        deps.print(f"  {i}. {r.port.device} - {_describe_port(r.port, r.label)}")
    deps.print("")

    for _attempt in range(3):
        raw = deps.prompt(f"Enter a number (1-{len(candidates)}): ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(candidates):
            return candidates[int(raw) - 1].port.device
        deps.print("That's not one of the listed numbers - try again.")

    raise FlasherError("Didn't get a valid choice after 3 tries. Re-run and try again, or use --port.")


def _select_port(args: argparse.Namespace, deps: Dependencies) -> tuple[str, str]:
    if args.port:
        # No need to enumerate every serial port on the system when the user
        # already told us which one to use.
        decision = ports.choose_port([], args.port)
        assert decision.port is not None
        return decision.port.device, "specified with --port"

    all_ports = deps.list_ports()
    decision = ports.choose_port(all_ports)

    if decision.kind == PortDecisionKind.NONE_FOUND:
        raise FlasherError(ui.NO_PORTS_MESSAGE)

    if decision.kind == PortDecisionKind.AUTO:
        assert decision.port is not None
        label = next((r.label for r in decision.candidates if r.port == decision.port), "")
        deps.print(f"Found a likely gateway on {decision.port.device} ({label}).")
        return decision.port.device, "auto-detected"

    # AMBIGUOUS
    if args.non_interactive:
        listing = "\n".join(
            f"  - {r.port.device}: {_describe_port(r.port, r.label)}" for r in decision.candidates
        )
        raise FlasherError(
            "Found more than one possible device and --non-interactive was set, so I can't "
            f"ask which to use:\n{listing}\nRe-run with --port <name>."
        )
    chosen = _prompt_for_port(decision.candidates, deps)
    return chosen, "chosen from a list"


def run(args: argparse.Namespace, deps: Dependencies) -> int:
    images_dir = args.images_dir or default_images_dir()

    port_str, how = _select_port(args, deps)
    deps.print(f"Using port {port_str} ({how}).")

    if args.chip:
        chip_key = images.normalize_chip_name(args.chip)
        deps.print(f"Using chip type '{chip_key}' (forced with --chip, skipping detection).")
    else:
        deps.print(f"Checking what's connected on {port_str} ...")
        detected = deps.detect_chip(port_str)
        chip_key = images.normalize_chip_name(detected.chip_name)
        deps.print(f"Found a {detected.chip_name}.")

    try:
        resolved = deps.resolve_image(chip_key, images_dir, args.bin_path)
    except FlasherError:
        if args.bin_path is not None or args.no_download:
            raise
        deps.print("No local image found - trying to fetch the latest one from GitHub ...")
        resolved = deps.download_latest_image(chip_key, images_dir)

    size_mb = resolved.path.stat().st_size / (1024 * 1024)
    deps.print(f"Firmware image: {resolved.path} ({size_mb:.2f} MB, {resolved.source}).")

    if args.dry_run:
        deps.print("\nDRY RUN - nothing was written to the device.")
        deps.print(f"  Would flash: port={port_str} chip={chip_key} image={resolved.path}")
        return 0

    if not args.yes and not args.non_interactive:
        ok = deps.confirm(
            f"\nThis will ERASE the current firmware and Wi-Fi setup on the device at "
            f"{port_str} and replace it with {resolved.path.name}.\nContinue? [y/N] "
        )
        if not ok:
            deps.print("Cancelled - nothing was changed on the device.")
            return 1

    deps.print("\nFlashing now - this takes a minute or two. Please don't unplug the board.")
    deps.flash_image(port_str, chip_key, resolved.path, args.baud)
    deps.print("Verifying the write...")
    deps.verify_image(port_str, chip_key, resolved.path, args.baud)

    _print_success(port_str, deps)
    return 0


def _print_success(port: str, deps: Dependencies) -> None:
    deps.print(
        textwrap.dedent(
            f"""
            Done! The gateway on {port} is now running the new firmware and has
            already restarted.

            Next steps:
              1. If it doesn't power up on its own, unplug and replug the USB
                 cable (or its normal power adapter).
              2. On your phone or laptop's Wi-Fi list, look for a network named
                 something like "AmpHive_Setup_XXXX" and connect to it. The
                 password is the device's setup code - printed on the unit's
                 label.
              3. Once connected, open a web browser to http://192.168.4.1 -
                 that's the gateway's setup page. Enter your site's Wi-Fi name
                 and password, your Tapo app account email + password, and the
                 setup code again to confirm, then submit.
              4. The gateway will restart onto your Wi-Fi and connect to
                 AmpHive on its own - the USB cable isn't needed after this.

            If the setup Wi-Fi network doesn't appear within a minute, unplug
            and replug the gateway once (the setup page gives up and restarts
            itself after 10 minutes with no activity).
            """
        ).strip()
    )


def _make_console_output_crash_proof() -> None:
    """Some Windows consoles use a legacy codepage that can't encode every
    Unicode character. Degrade to '?' substitution instead of crashing a
    novice user's session with a raw UnicodeEncodeError mid-flash."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):  # pragma: no cover - platform dependent
            pass


def main(argv: list[str] | None = None) -> int:
    _make_console_output_crash_proof()
    args = build_arg_parser().parse_args(argv)
    deps = Dependencies()

    deps.print(ui.BANNER)
    deps.print(
        "This tool only writes ready-made firmware files - it never downloads or runs "
        "a compiler on this computer.\n"
    )

    exit_code = 0
    try:
        exit_code = run(args, deps)
    except FlasherError as exc:
        deps.print(f"\n{exc}\n")
        exit_code = 1
    except KeyboardInterrupt:
        deps.print("\nCancelled.")
        exit_code = 1

    ui.pause_before_exit(interactive=not args.non_interactive)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
