# AmpHive Agent — multi-brand software gateway (docs/AMPHIVE_AGENT.md).
#
# Build (from the agent/ dir):
#   docker build -t amphive-agent --build-arg EXTRAS=all .
# Run (sim provider, no hardware):
#   docker run --rm -e AMPHIVE_GATEWAY_ID=agent-lab-01 -e AMPHIVE_PROVIDERS=sim \
#     -e AMPHIVE_BROKER=8.231.81.12 -e AMPHIVE_CA_FILE=/certs/ca.crt \
#     -e AMPHIVE_MQTT_USER=agent-lab-01 -e AMPHIVE_MQTT_PASS=... \
#     -v /path/to/ca.crt:/certs/ca.crt:ro -v amphive-agent-state:/state \
#     -e AMPHIVE_STATE=/state/agent.json amphive-agent
#
# LAN discovery (kasa/shelly) needs the host network to reach plugs + receive
# UDP broadcast replies:  add  --network host  (Linux).
FROM python:3.12-slim

# Which provider extras to install (all | kasa | shelly | "" for sim-only).
ARG EXTRAS=all

WORKDIR /app
COPY pyproject.toml README.md ./
COPY amphive_agent ./amphive_agent

# Install the package (+ optional provider deps). pip resolves the extras.
RUN if [ -n "$EXTRAS" ]; then pip install --no-cache-dir ".[$EXTRAS]"; \
    else pip install --no-cache-dir .; fi

# Run as a fixed non-root uid/gid (10001). The agent only needs to write its
# state file; it uses unprivileged ports only (outbound MQTT, UDP discovery
# from an ephemeral port), so no capability is required, even with
# --network host. /state is created and owned by the agent user BEFORE the
# VOLUME line so a fresh named volume inherits that ownership; an existing
# root-owned volume must be chowned once: docker run --rm -v <vol>:/state
# alpine chown -R 10001:10001 /state
RUN groupadd --system --gid 10001 amphive \
    && useradd --system --uid 10001 --gid 10001 --no-create-home \
        --home-dir /nonexistent --shell /usr/sbin/nologin amphive \
    && mkdir -p /state && chown 10001:10001 /state

# Persist the unique_id->plug_id map + session baselines across restarts.
ENV AMPHIVE_STATE=/state/amphive_agent_state.json
VOLUME ["/state"]
USER 10001:10001

# Unbuffered logs; run as the console entry point.
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["amphive-agent"]
