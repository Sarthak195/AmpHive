"""Environment-driven configuration."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    gateway_id: str
    broker_host: str
    broker_port: int
    mqtt_user: str
    mqtt_pass: str
    use_tls: bool
    # PEM bundle to validate the broker cert against (the AmpHive CA,
    # deploy/config/mqtt-certs/ca.crt). None = system trust store.
    ca_file: Path | None
    poll_s: float
    providers: list[str]
    state_path: Path

    # provider-specific
    tplink_user: str | None
    tplink_pass: str | None
    shelly_hosts: list[str]
    sim_count: int

    @classmethod
    def from_env(cls) -> "Config":
        gateway_id = os.environ.get("AMPHIVE_GATEWAY_ID")
        if not gateway_id:
            raise SystemExit("AMPHIVE_GATEWAY_ID is required")

        use_tls = _bool(os.environ.get("AMPHIVE_USE_TLS"), True)
        default_port = 8883 if use_tls else 1883
        providers = [p.strip() for p in os.environ.get("AMPHIVE_PROVIDERS", "kasa").split(",") if p.strip()]
        shelly_hosts = [h.strip() for h in os.environ.get("AMPHIVE_SHELLY_HOSTS", "").split(",") if h.strip()]

        return cls(
            gateway_id=gateway_id,
            broker_host=os.environ.get("AMPHIVE_BROKER", "mqtt.amphive.io"),
            broker_port=int(os.environ.get("AMPHIVE_BROKER_PORT", str(default_port))),
            mqtt_user=os.environ.get("AMPHIVE_MQTT_USER", ""),
            mqtt_pass=os.environ.get("AMPHIVE_MQTT_PASS", ""),
            use_tls=use_tls,
            ca_file=Path(os.environ["AMPHIVE_CA_FILE"]) if os.environ.get("AMPHIVE_CA_FILE") else None,
            poll_s=float(os.environ.get("AMPHIVE_POLL_S", "10")),
            providers=providers,
            state_path=Path(os.environ.get("AMPHIVE_STATE", "amphive_agent_state.json")),
            tplink_user=os.environ.get("TPLINK_USER"),
            tplink_pass=os.environ.get("TPLINK_PASS"),
            shelly_hosts=shelly_hosts,
            sim_count=int(os.environ.get("AMPHIVE_SIM_COUNT", "2")),
        )
