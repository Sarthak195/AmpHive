"""Serial port discovery and ranking.

Pure logic, deliberately kept free of any direct ``esptool`` or hardware I/O
so it can be unit tested without a real device attached. The only I/O this
module performs is the pyserial port enumeration itself
(:func:`list_serial_ports`), which is trivially mocked out in tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

# USB VID -> friendly label for chips/bridges commonly found on ESP32 dev
# boards and the AmpHive gateway carrier board. Lower tier number = more
# confident this is our device. Vendor IDs are the de-facto way to guess
# "this port is probably an ESP32" before ever talking to it.
KNOWN_VIDS: dict[int, tuple[int, str]] = {
    0x303A: (0, "Espressif native USB (e.g. ESP32-C3/S3 built-in USB-CDC)"),
    0x10C4: (1, "Silicon Labs CP210x USB-UART bridge (common ESP32 dev boards)"),
    0x1A86: (1, "QinHeng CH340/CH9102 USB-UART bridge (common ESP32 dev boards)"),
    0x0403: (1, "FTDI FT232 USB-UART bridge (some ESP32 boards)"),
}

# Tier assigned to a port whose VID isn't in KNOWN_VIDS (or has no VID at
# all, e.g. some virtual/Bluetooth COM ports). Still offered as a candidate
# - clone boards sometimes ship unlisted VIDs - just ranked behind known ones.
UNKNOWN_TIER = 2
UNKNOWN_LABEL = "Unrecognized device (could still be your gateway, could be something else)"


@dataclass(frozen=True)
class PortInfo:
    """Minimal, hashable view of a pyserial ``ListPortInfo``.

    Decoupling from ``serial.tools.list_ports_common.ListPortInfo`` keeps
    :func:`rank_ports` testable with plain data, no pyserial import required
    in tests.
    """

    device: str
    vid: int | None
    pid: int | None
    description: str
    manufacturer: str | None = None


@dataclass(frozen=True)
class RankedPort:
    port: PortInfo
    tier: int
    label: str


class PortDecisionKind(str, Enum):
    NONE_FOUND = "none_found"
    AUTO = "auto"
    AMBIGUOUS = "ambiguous"
    EXPLICIT = "explicit"


@dataclass(frozen=True)
class PortDecision:
    kind: PortDecisionKind
    port: PortInfo | None = None
    candidates: tuple[RankedPort, ...] = ()


def list_serial_ports() -> list[PortInfo]:
    """Enumerate serial ports via pyserial. The only I/O in this module."""
    import serial.tools.list_ports  # imported lazily so tests never need pyserial installed

    ports = []
    for p in serial.tools.list_ports.comports():
        ports.append(
            PortInfo(
                device=p.device,
                vid=p.vid,
                pid=p.pid,
                description=p.description or "",
                manufacturer=getattr(p, "manufacturer", None),
            )
        )
    return ports


def rank_ports(ports: list[PortInfo]) -> list[RankedPort]:
    """Score+sort candidate ports, best guess first.

    Known Espressif/UART-bridge VIDs sort first (native USB ahead of
    USB-UART bridges), then everything else, each group preserving the
    OS-reported enumeration order.
    """
    ranked = []
    for p in ports:
        if p.vid is not None and p.vid in KNOWN_VIDS:
            tier, label = KNOWN_VIDS[p.vid]
        else:
            tier, label = UNKNOWN_TIER, UNKNOWN_LABEL
        ranked.append(RankedPort(port=p, tier=tier, label=label))
    return sorted(ranked, key=lambda r: r.tier)


def choose_port(ports: list[PortInfo], requested_device: str | None = None) -> PortDecision:
    """Decide which port to use given the ranked candidates.

    - ``requested_device`` set (``--port``) always wins, even if it wasn't
      in the enumerated list (e.g. a port that only appears once opened).
    - No ports at all -> NONE_FOUND.
    - Exactly one port in the best (lowest-numbered) tier -> AUTO.
    - More than one port tied for the best tier -> AMBIGUOUS, caller must
      pick (interactively, or by probing each with esptool).
    """
    if requested_device:
        match = next((p for p in ports if p.device == requested_device), None)
        chosen = match or PortInfo(
            device=requested_device, vid=None, pid=None, description="(user-specified)"
        )
        return PortDecision(kind=PortDecisionKind.EXPLICIT, port=chosen)

    if not ports:
        return PortDecision(kind=PortDecisionKind.NONE_FOUND)

    ranked = rank_ports(ports)
    best_tier = ranked[0].tier
    best = [r for r in ranked if r.tier == best_tier]

    if len(best) == 1:
        return PortDecision(kind=PortDecisionKind.AUTO, port=best[0].port, candidates=tuple(ranked))

    return PortDecision(kind=PortDecisionKind.AMBIGUOUS, candidates=tuple(ranked))
