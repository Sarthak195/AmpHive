"""Persistent state: backend-assigned plug_id map + per-session billing baselines.

The backend is authoritative for plug_id (it equals the global DB ``plugs.id``),
so the agent learns ``unique_id -> plug_id`` from the backend's ``assign`` message
and persists it here. Persisting means after the first assignment the agent adopts
ids immediately on restart (and keeps an in-flight session's energy baseline —
crash recovery, mirroring the firmware).
"""
from __future__ import annotations

import json
import threading
from pathlib import Path


class Store:
    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()
        self._data: dict = {"assignments": {}, "sessions": {}}
        if path.exists():
            try:
                loaded = json.loads(path.read_text())
                self._data["assignments"] = loaded.get("assignments", {})
                self._data["sessions"] = loaded.get("sessions", {})
            except Exception:
                pass  # start fresh on a corrupt file

    def _flush(self) -> None:
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._data, indent=2))
        tmp.replace(self._path)  # atomic

    # ---- backend-assigned plug_id map (unique_id -> plug_id) ----
    def get_assignment(self, unique_id: str) -> int | None:
        value = self._data["assignments"].get(unique_id)
        return int(value) if value is not None else None

    def set_assignment(self, unique_id: str, plug_id: int) -> bool:
        """Store an assignment; returns True if it was new/changed."""
        with self._lock:
            if self._data["assignments"].get(unique_id) == plug_id:
                return False
            self._data["assignments"][unique_id] = int(plug_id)
            self._flush()
            return True

    # ---- per-session billing baseline (keyed by plug_id) ----
    def get_session(self, plug_id: int) -> dict | None:
        return self._data["sessions"].get(str(plug_id))

    def set_session(self, plug_id: int, session: dict) -> None:
        with self._lock:
            self._data["sessions"][str(plug_id)] = session
            self._flush()

    def clear_session(self, plug_id: int) -> None:
        with self._lock:
            self._data["sessions"].pop(str(plug_id), None)
            self._flush()
