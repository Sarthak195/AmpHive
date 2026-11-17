"""Entry point: python -m tools.ocpp_bridge [-v]. All settings come from env (docs/OCPP_BRIDGE.md)."""

import argparse
import asyncio
import logging
import sys

from .bridge import AuthError, Config, amain


def main() -> int:
    parser = argparse.ArgumentParser(prog="ocpp_bridge", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging for the bridge")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # websockets logs every handshake header (the Basic auth one too) at DEBUG,
    # and httpx logs each request at INFO: keep both quiet whatever -v says.
    logging.getLogger("websockets").setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        cfg = Config.from_env()
    except ValueError as exc:
        print(f"ocpp_bridge: {exc}", file=sys.stderr)
        return 2
    try:
        asyncio.run(amain(cfg))
    except AuthError as exc:
        print(f"ocpp_bridge: {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
