"""JSON-file state persistence, shared by every simulated plug in one
process. One file holds every plug's state keyed by plug id, so a restart
(`--state-file` pointing at the same path) resumes each plug's energy
counter and relay state instead of starting cold — mirrors the firmware's
own NVS-persisted per-plug energy integrator (tapo_protocol.c) and relay
state, just on disk instead of flash.

Write-through with a lock: a bench tool serving a handful of plugs at a
~10s poll cadence has no throughput need for batching, and write-through
means a `kill -9` never loses more than the last tick.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Optional

log = logging.getLogger("p110sim.state")


class StateStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._data = raw
        except (OSError, json.JSONDecodeError) as e:
            log.warning("Ignoring unreadable state file %s: %s", self.path, e)

    def get(self, plug_id: int) -> Optional[dict]:
        with self._lock:
            return self._data.get(str(plug_id))

    def set(self, plug_id: int, snapshot: dict) -> None:
        with self._lock:
            self._data[str(plug_id)] = snapshot
            self._write_locked()

    def _write_locked(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            tmp.write_text(json.dumps(self._data, indent=2, sort_keys=True), encoding="utf-8")
            tmp.replace(self.path)  # atomic on POSIX and Windows (same volume)
        except OSError as e:
            log.warning("Failed to persist state to %s: %s", self.path, e)
