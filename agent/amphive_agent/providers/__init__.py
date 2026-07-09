"""Provider registry — instantiate the enabled providers from config.

Imports are lazy per-provider so you only need the deps for the providers you
enable (e.g. ``AMPHIVE_PROVIDERS=sim`` needs nothing beyond paho-mqtt).
"""
from __future__ import annotations

import logging

from ..config import Config
from ..model import PlugProvider

log = logging.getLogger(__name__)


def build_providers(config: Config) -> list[PlugProvider]:
    providers: list[PlugProvider] = []
    for name in config.providers:
        try:
            if name == "kasa":
                from .kasa import KasaProvider
                providers.append(KasaProvider(config.tplink_user, config.tplink_pass))
            elif name == "shelly":
                from .shelly import ShellyProvider
                providers.append(ShellyProvider(config.shelly_hosts))
            elif name == "sim":
                from .sim import SimProvider
                providers.append(SimProvider(config.sim_count))
            else:
                log.warning("unknown provider %r, skipping", name)
                continue
            log.info("provider enabled: %s", name)
        except Exception:
            log.exception("failed to init provider %r", name)
    return providers
