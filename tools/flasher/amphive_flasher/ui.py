"""Console UI helpers for a novice, double-click-an-EXE audience.

Plain language, no jargon, and - critically - the window must never vanish
before the user has had a chance to read what happened. That's what
:func:`pause_before_exit` is for.
"""

from __future__ import annotations

import textwrap

BANNER = r"""
============================================================
   AmpHive Gateway Flasher
   Puts the AmpHive software onto a plugged-in gateway board.
   This tool does NOT install any developer software on your
   computer - it just writes a ready-made firmware file.
============================================================
"""

NO_PORTS_MESSAGE = textwrap.dedent(
    """\
    I couldn't find any device plugged in.

    A few things to check:
      1. Make sure the gateway board is connected to this computer with a
         USB cable, and that the cable carries data (not just power) - some
         "charge-only" phone cables have no data wires and will never work
         for this. If in doubt, try a different cable, ideally the one the
         board came with.
      2. Try a different USB port on your computer, especially if you're
         using a hub - plug directly into the computer if you can.
      3. Windows may need a small driver installed for the board's USB chip
         before it shows up as a serial port. See the driver links in
         tools/flasher/README.md (or ask whoever gave you this tool).
      4. Give it a few seconds after plugging in, then try again.
    """
)


def print_banner() -> None:
    print(BANNER)


def pause_before_exit(interactive: bool = True) -> None:
    """Keep the console window open when double-clicked from Explorer.

    A user who launched this by double-clicking the EXE has no terminal to
    fall back to - if the window closes immediately they can't read the
    result. Skipped when running non-interactively (e.g. CI, --dry-run
    smoke tests) so automation never blocks on stdin.
    """
    if not interactive:
        return
    try:
        input("\nPress Enter to close this window...")
    except (EOFError, KeyboardInterrupt):
        pass


def default_confirm(prompt: str) -> bool:
    try:
        answer = input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ("y", "yes")
