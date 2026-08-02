#!/usr/bin/env python3
"""AmpHive — P110 emulator CLI.

Runs N independently-addressable simulated Tapo P110 plugs so ONE real
ESP32-C3 gateway (running firmware/main/tapo_protocol.c's real KLAP driver)
can be bench-tested against multiple plugs without owning that much
hardware. See README.md in this directory for the full bench procedure.

Usage (most common — one host, incrementing ports; needs the firmware's
":port" roster-addressing support, see README.md "Addressing"):

    python tools/p110_sim/cli.py --count 3 --host 0.0.0.0 --base-port 9440 \\
        --email you@example.com --password 'your-tapo-password' \\
        --watts 1500,3300,0 --start-kwh 0,12.4,0

Everything needed is either stdlib or already a pinned dependency
(`cryptography`, used the same way tools/klap_probe.py already uses it).
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from pathlib import Path
from typing import Optional

from crypto import auth_hash
from plug import PlugConfig, SimulatedPlug
from server import PlugHTTPServer, make_server
from state import StateStore

log = logging.getLogger("p110sim")


def _parse_per_plug_floats(raw: Optional[str], count: int, default: float) -> list[float]:
    """A CSV of `count` values, or a single value broadcast to all plugs, or
    (if `raw` is None) `default` for every plug."""
    if raw is None:
        return [default] * count
    parts = [p.strip() for p in raw.split(",") if p.strip() != ""]
    if len(parts) == 1:
        return [float(parts[0])] * count
    if len(parts) != count:
        raise argparse.ArgumentTypeError(
            f"expected 1 or {count} comma-separated values, got {len(parts)}"
        )
    return [float(p) for p in parts]


def _parse_reset_specs(specs: list[str]) -> dict[int, tuple[float, float]]:
    """--reset-counter PLUGID@SECONDS[@VALUE_KWH] -> {plug_id: (after_s, value_kwh)}."""
    out: dict[int, tuple[float, float]] = {}
    for spec in specs:
        fields = spec.split("@")
        if len(fields) not in (2, 3):
            raise argparse.ArgumentTypeError(
                f"--reset-counter {spec!r}: expected PLUGID@SECONDS[@VALUE_KWH]"
            )
        plug_id = int(fields[0])
        after_s = float(fields[1])
        value_kwh = float(fields[2]) if len(fields) == 3 else 0.0
        out[plug_id] = (after_s, value_kwh)
    return out


def _parse_drop_rate_map(specs: list[str]) -> dict[int, float]:
    """--drop-rate-map PLUGID=RATE (repeatable, or comma-separated in one arg)."""
    out: dict[int, float] = {}
    for spec in specs:
        for item in spec.split(","):
            item = item.strip()
            if not item:
                continue
            if "=" not in item:
                raise argparse.ArgumentTypeError(f"--drop-rate-map {item!r}: expected PLUGID=RATE")
            pid, rate = item.split("=", 1)
            out[int(pid)] = float(rate)
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--count", type=int, default=1, help="Number of plugs to emulate.")
    p.add_argument(
        "--host",
        default="0.0.0.0",
        help="Bind host for all plugs (single-host, incrementing-port mode).",
    )
    p.add_argument(
        "--base-port",
        type=int,
        default=9440,
        help="First plug's port; plug i binds to base-port+i (single-host mode).",
    )
    p.add_argument(
        "--hosts",
        default=None,
        help="Comma-separated list of --count distinct bind IPs, one per plug, all "
        "on --port (multi-IP mode: no firmware change needed — see README.md "
        "'Addressing' for netsh/ip-addr-add setup). Overrides --host/--base-port.",
    )
    p.add_argument("--port", type=int, default=80, help="Shared port for --hosts mode.")
    p.add_argument(
        "--plug-ids",
        default=None,
        help="Comma-separated DB-style plug ids (default: 1..count).",
    )
    p.add_argument(
        "--email",
        default=None,
        help="Tapo account email the emulator accepts (falls back to TAPO_EMAIL "
        "or TAPO_USERNAME env var).",
    )
    p.add_argument(
        "--password",
        default=None,
        help="Tapo account password the emulator accepts (falls back to the "
        "TAPO_PASSWORD env var — preferred over the flag, which leaks the "
        "credential into shell history and process listings).",
    )
    p.add_argument(
        "--watts",
        default=None,
        help="Per-plug steady-state load in watts while relay is ON. Single value "
        "broadcasts to all plugs, or a --count-length CSV. Default 1500.",
    )
    p.add_argument("--jitter", type=float, default=0.02, help="Fractional load noise (0.02 = +/-2%%).")
    p.add_argument("--voltage", type=float, default=230.0, help="Nominal mains voltage.")
    p.add_argument(
        "--power-factor",
        type=float,
        default=0.95,
        help="Simulated load power factor (< 1 so reported current != watts/voltage).",
    )
    p.add_argument(
        "--start-kwh",
        default=None,
        help="Per-plug starting energy counter in kWh (ignored on a resumed run if "
        "--state-file already has that plug's state). Single value or CSV. Default 0.",
    )
    p.add_argument(
        "--state-file",
        default="p110_sim_state.json",
        help="JSON file the emulator's relay/energy state persists to across restarts.",
    )
    p.add_argument(
        "--reset-counter",
        action="append",
        default=[],
        metavar="PLUGID@SECONDS[@VALUE_KWH]",
        help="Repeatable. After SECONDS of uptime, reset that plug's energy counter to "
        "VALUE_KWH (default 0) and force a KLAP re-handshake (simulates a physical "
        "power-cycle). See README.md for what this does and does not exercise "
        "on the backend.",
    )
    p.add_argument(
        "--drop-rate",
        type=float,
        default=0.0,
        help="Global probability [0,1) that any given HTTP request is dropped "
        "(simulates a flaky/unreachable plug).",
    )
    p.add_argument(
        "--drop-rate-map",
        action="append",
        default=[],
        metavar="PLUGID=RATE",
        help="Repeatable/CSV. Per-plug drop-rate override.",
    )
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p


def build_configs(args: argparse.Namespace) -> list[PlugConfig]:
    count = args.count
    if args.hosts:
        hosts = [h.strip() for h in args.hosts.split(",") if h.strip()]
        if len(hosts) != count:
            raise SystemExit(f"--hosts has {len(hosts)} entries but --count is {count}")
        addr = [(h, args.port) for h in hosts]
    else:
        addr = [(args.host, args.base_port + i) for i in range(count)]

    if args.plug_ids:
        ids = [int(x) for x in args.plug_ids.split(",")]
        if len(ids) != count:
            raise SystemExit(f"--plug-ids has {len(ids)} entries but --count is {count}")
    else:
        ids = list(range(1, count + 1))

    watts = _parse_per_plug_floats(args.watts, count, 1500.0)
    start_kwh = _parse_per_plug_floats(args.start_kwh, count, 0.0)
    resets = _parse_reset_specs(args.reset_counter)
    drop_map = _parse_drop_rate_map(args.drop_rate_map)

    configs = []
    for i in range(count):
        plug_id = ids[i]
        host, port = addr[i]
        after_s, reset_val = resets.get(plug_id, (None, 0.0))
        configs.append(
            PlugConfig(
                plug_id=plug_id,
                label=f"plug{plug_id}",
                host=host,
                port=port,
                watts=watts[i],
                jitter=args.jitter,
                voltage=args.voltage,
                power_factor=args.power_factor,
                start_kwh=start_kwh[i],
                drop_rate=drop_map.get(plug_id, args.drop_rate),
                reset_after_s=after_s,
                reset_value_kwh=reset_val,
            )
        )
    return configs


def run(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(name)-18s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    email = args.email or os.environ.get("TAPO_EMAIL") or os.environ.get("TAPO_USERNAME")
    password = args.password or os.environ.get("TAPO_PASSWORD")
    if not email or not password:
        log.error(
            "Tapo credentials missing: pass --email/--password or set "
            "TAPO_EMAIL (or TAPO_USERNAME) and TAPO_PASSWORD env vars."
        )
        return 2

    configs = build_configs(args)
    ah = auth_hash(email, password)
    store = StateStore(Path(args.state_file))

    servers: list[PlugHTTPServer] = []
    for cfg in configs:
        initial = store.get(cfg.plug_id)
        plug = SimulatedPlug(cfg, ah, initial=initial)
        plug.on_state_changed = lambda p: store.set(p.cfg.plug_id, p.snapshot())
        srv = make_server(plug, store)
        # The reset callback needs to invalidate KLAP sessions on the server
        # (simulating a power-cycle), so it's wired after srv exists.
        plug.on_counter_reset = _make_reset_handler(srv, store)
        servers.append(srv)
        log.info(
            "plug %-4d %s:%-6d  %5.0f W  start=%.3f kWh  drop_rate=%.2f%s",
            cfg.plug_id,
            cfg.host,
            cfg.port,
            cfg.watts,
            cfg.start_kwh if initial is None else (initial.get("energy_wh", 0.0) / 1000.0),
            cfg.drop_rate,
            f"  reset-at=+{cfg.reset_after_s:.0f}s->{cfg.reset_value_kwh:.3f}kWh"
            if cfg.reset_after_s is not None
            else "",
        )

    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
    for t in threads:
        t.start()

    log.info("%d plug(s) running. Account: %s. Ctrl-C to stop.", len(servers), email)

    stop_event = threading.Event()

    def _shutdown(signum, frame):  # noqa: ARG001
        log.info("Shutting down...")
        stop_event.set()

    signal.signal(signal.SIGINT, _shutdown)
    try:
        signal.signal(signal.SIGTERM, _shutdown)
    except (ValueError, AttributeError):
        pass

    stop_event.wait()
    for s in servers:
        s.shutdown()
        s.server_close()
    log.info("Stopped.")
    return 0


def _make_reset_handler(srv: PlugHTTPServer, store: StateStore):
    def _cb(plug: SimulatedPlug) -> None:
        log.warning(
            "plug %d: scheduled counter reset fired (-> %.3f kWh); invalidating KLAP "
            "sessions to simulate a power-cycle",
            plug.cfg.plug_id,
            plug.cfg.reset_value_kwh,
        )
        srv.reset_sessions()
        store.set(plug.cfg.plug_id, plug.snapshot())

    return _cb


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
