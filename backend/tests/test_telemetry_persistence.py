"""
Tests for time-series telemetry persistence.

Run:
    pip install -r backend/requirements.txt -r backend/requirements-dev.txt
    pytest backend/tests/test_telemetry_persistence.py

The two tests here are DB-free and exercise pure logic:
  1. The MQTT handler now forwards voltage/current/status into the persistence
     buffer (previously parsed but dropped).
  2. The persistence buffer is bounded (oldest dropped, dropped-count tracked).

The flush + enrichment path (tenant_id/session_id resolution, bulk insert) and
the CPO endpoint need a real Postgres (date_trunc, enum types); an outline is
left as a skipped test to be fleshed out against a throwaway PG container in CI.
"""

import pytest

from backend.services.mqtt_manager import MQTTManager
import backend.services.telemetry_persistence as tp


class _FakePersistence:
    """Captures enqueue() calls without touching a DB."""

    def __init__(self):
        self.readings = []

    def enqueue(self, reading):
        self.readings.append(reading)


def test_handler_forwards_voltage_current_status():
    """
    _handle_gateway_telemetry must enqueue the full sample, including the
    voltage/current/status fields that used to be parsed and then dropped.
    db_session_factory/event_loop are left unset so the session-totals branch
    (which needs a running loop) is skipped.
    """
    MQTTManager._instance = None  # bypass the singleton for a clean instance
    fake = _FakePersistence()
    mgr = MQTTManager(telemetry_persistence=fake)

    mgr._handle_gateway_telemetry("gw-1", {
        "plug_id": 7,
        "watts": 1200.5,
        "kwh": 0.45,
        "voltage": 230.0,
        "current": 5.2,
        "status": "occupied",
    })

    assert len(fake.readings) == 1
    r = fake.readings[0]
    assert r["plug_id"] == 7
    assert r["power_w"] == 1200.5
    assert r["energy_kwh"] == 0.45
    assert r["voltage_v"] == 230.0
    assert r["current_a"] == 5.2
    assert r["status"] == "occupied"
    assert "recorded_at" in r  # stamped in the handler

    MQTTManager._instance = None  # don't leak the instance to other tests


def test_buffer_is_bounded_and_counts_drops(monkeypatch):
    """enqueue() beyond BUFFER_MAX drops the oldest and increments _dropped."""
    monkeypatch.setattr(tp, "BUFFER_MAX", 5)
    svc = tp.TelemetryPersistenceService(db_session_factory=None)

    total = 5 + 100  # 5 fit, 100 over the bound
    for i in range(total):
        svc.enqueue({"plug_id": i})

    assert len(svc._buffer) == 5
    assert svc._dropped == 100
    # The deque keeps the most recent samples.
    assert svc._buffer[-1]["plug_id"] == total - 1


@pytest.mark.skip(reason="Needs a live Postgres (date_trunc + enum types); wire up in CI.")
async def test_flush_enriches_and_inserts():
    """
    Outline: seed plug -> gateway -> tenant + one ACTIVE session against a
    throwaway Postgres container, enqueue N readings, call _flush_once(), then
    assert N rows exist with the correct tenant_id and session_id. Also cover:
      - no active session  -> session_id IS NULL
      - unknown plug id    -> row skipped, batch still commits
    """
    ...
