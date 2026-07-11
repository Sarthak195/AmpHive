"""Entry point: ``python -m amphive_agent`` (or the ``amphive-agent`` script)."""
from __future__ import annotations

import asyncio
import logging

from .config import Config
from .core import AmpHiveAgent


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = Config.from_env()
    agent = AmpHiveAgent(config)
    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
