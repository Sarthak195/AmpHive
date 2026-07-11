"""
Shared runtime state — created/bound by main.py's lifespan, read everywhere.

Always access these as attributes (`from backend import state`, then
`state.mqtt_manager`), never `from backend.state import mqtt_manager`: a
from-import copies the value at import time (None, before the lifespan runs)
and misses the rebind.
"""
from typing import Optional

from backend.services.session_reaper import SessionReaperService
from backend.services.tapo_direct import TapoDirectDriver
from backend.services.telemetry import TelemetryStore
from backend.services.telemetry_persistence import TelemetryPersistenceService

# MQTTManager instance — created in lifespan (it is also a singleton class,
# but routers must not construct it before lifespan configures it).
mqtt_manager = None
# [Direct Mode] Tapo direct driver (only when DIRECT_MODE=true)
tapo_driver: Optional[TapoDirectDriver] = None
# Buffered batch-flush service for time-series telemetry persistence
telemetry_persistence: Optional[TelemetryPersistenceService] = None
# Auto-finalizer for sessions whose telemetry went silent
session_reaper: Optional[SessionReaperService] = None
# Process-wide singleton (TelemetryStore.__new__ enforces it); safe at import.
telemetry_store = TelemetryStore()
