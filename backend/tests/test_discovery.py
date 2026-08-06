"""
Per-gateway plug-discovery cap (services/mqtt/discovery.py MAX_PLUGS_PER_GATEWAY).

A single *claimed* gateway that streams unbounded distinct `unique_id`s must not
be able to spawn one Plug row per id (storage / roster-bloat DoS). These tests
pin the cap: a NEW plug over the cap is dropped (no insert, no assign-map
publish), while a new plug under the cap and updates to already-known plugs
still apply. Self-contained fakes so this file stands alone from
test_mqtt_manager.py.
"""
import asyncio
import json
from unittest.mock import MagicMock

import pytest

import backend.services.mqtt.discovery as discovery_mod
from backend.services.mqtt_manager import MQTTManager


class _Result:
    """SQLAlchemy Result stand-in: scalar_one_or_none() / scalar_one() / all()."""

    def __init__(self, scalar="__unset__", rows=None):
        self._scalar = scalar
        self._rows = rows or []

    def scalar_one_or_none(self):
        return None if self._scalar == "__unset__" else self._scalar

    def scalar_one(self):
        return 0 if self._scalar == "__unset__" else self._scalar

    def all(self):
        return self._rows


class _Session:
    """Async-context session returning queued results in order; records adds.

    (The trailing `_publish_roster_for_gateway` call issues one more execute()
    than we queue; it wraps its DB work in a try/except that swallows the
    resulting StopIteration, so the roster republish is simply skipped here —
    exactly as in test_mqtt_manager.py's discovery test.)
    """

    def __init__(self, results):
        self._results = iter(results)
        self.added = []
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, *_a, **_k):
        return next(self._results)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True


def _mgr(session):
    MQTTManager._instance = None
    mgr = MQTTManager(
        db_session_factory=lambda: session, event_loop=asyncio.get_running_loop()
    )
    mgr.client = MagicMock()
    return mgr


@pytest.mark.asyncio
async def test_persist_discovery_drops_new_plug_over_cap(monkeypatch):
    """At the per-gateway cap, a brand-new unique_id is dropped: no insert, no
    commit, no assign-map publish."""
    monkeypatch.setattr(discovery_mod, "MAX_PLUGS_PER_GATEWAY", 3)
    session = _Session([
        _Result(scalar=MagicMock()),   # gateway exists (claimed)
        _Result(scalar=None),          # plug is new
        _Result(scalar=3),             # gateway already at the cap
    ])
    mgr = _mgr(session)

    await mgr._persist_plug_discovery("gw-1", {"unique_id": "kasa:NEW", "alias": "Bay 9"})

    assert session.added == []                # nothing inserted
    assert session.committed is False         # nothing committed
    mgr.client.publish.assert_not_called()    # no assign map published
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_persist_discovery_adds_new_plug_under_cap(monkeypatch):
    """Under the cap the new plug is inserted and the assign map is published."""
    monkeypatch.setattr(discovery_mod, "MAX_PLUGS_PER_GATEWAY", 64)
    session = _Session([
        _Result(scalar=MagicMock()),           # gateway exists
        _Result(scalar=None),                  # plug is new
        _Result(scalar=2),                     # under the cap
        _Result(rows=[("kasa:NEW", 5)]),       # rebuilt assign map
    ])
    mgr = _mgr(session)

    await mgr._persist_plug_discovery("gw-1", {"unique_id": "kasa:NEW", "alias": "Bay 9"})

    assert len(session.added) == 1
    assert session.added[0].unique_id == "kasa:NEW"
    assert session.committed is True
    mgr.client.publish.assert_called_once()   # assign map (roster republish swallowed)
    args, _kwargs = mgr.client.publish.call_args
    assert args[0] == "amphive/gateways/gw-1/assign"
    assert json.loads(args[1]) == {"kasa:NEW": 5}
    MQTTManager._instance = None


@pytest.mark.asyncio
async def test_persist_discovery_update_existing_is_not_capped(monkeypatch):
    """Updating an already-known plug never consults the cap (no count query is
    issued), so a gateway at/over the cap can still refresh its existing plugs."""
    monkeypatch.setattr(discovery_mod, "MAX_PLUGS_PER_GATEWAY", 1)
    existing_plug = MagicMock()
    session = _Session([
        _Result(scalar=MagicMock()),     # gateway exists
        _Result(scalar=existing_plug),   # plug already known → update path
        _Result(rows=[("kasa:OLD", 5)]),  # rebuilt assign map
    ])
    mgr = _mgr(session)

    await mgr._persist_plug_discovery("gw-1", {"unique_id": "kasa:OLD", "alias": "Renamed"})

    assert session.added == []                 # updated in place, not inserted
    assert existing_plug.name == "Renamed"
    assert session.committed is True
    mgr.client.publish.assert_called_once()
    MQTTManager._instance = None
