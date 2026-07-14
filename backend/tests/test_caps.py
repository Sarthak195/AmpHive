"""
Circuit admission control (services/caps.py) — DB-free.

Exercises the admission arithmetic and gating without a real DB by faking the
two queries check_circuit_admission runs (the locked group select, then the
Σ-active-caps load query). Proves: effective cap falls back to the default;
a start is admitted up to and including the exact circuit limit and rejected
past it; and it's a clean no-op when ungrouped, uncapped, or disabled.
"""
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import backend.services.caps as caps


def _plug(group_id=1, cap=16.0):
    return SimpleNamespace(id=99, group_id=group_id, max_current_a=cap)


class _Result:
    def __init__(self, scalar=None, rows=None, scalar_list=None):
        self._scalar = scalar
        self._rows = rows or []
        self._scalar_list = scalar_list or []

    def scalar_one_or_none(self):
        return self._scalar

    def scalar(self):
        return self._scalar

    def all(self):
        return self._rows

    def scalars(self):
        return SimpleNamespace(all=lambda: self._scalar_list)


class _FakeDb:
    """Returns queued results in order; records how many executes ran."""
    def __init__(self, results):
        self._results = list(results)
        self.calls = 0

    async def execute(self, *a, **k):
        r = self._results[self.calls]
        self.calls += 1
        return r

    async def commit(self):
        pass

    async def rollback(self):
        pass


def test_effective_plug_cap_uses_default_when_unset(monkeypatch):
    monkeypatch.setattr(caps, "DEFAULT_PLUG_CAP_A", 16.0)
    assert caps.effective_plug_cap(_plug(cap=None)) == 16.0
    assert caps.effective_plug_cap(_plug(cap=10.0)) == 10.0


@pytest.mark.asyncio
async def test_admission_allows_up_to_exact_capacity(monkeypatch):
    """One 16 A plug active on a 32 A circuit; a second 16 A start foots to
    exactly 32 A — admitted (no raise)."""
    monkeypatch.setattr(caps, "ENFORCE_CIRCUIT_ADMISSION", True)
    monkeypatch.setattr(caps, "DEFAULT_PLUG_CAP_A", 16.0)
    group = SimpleNamespace(id=1, max_current_a=32.0)
    db = _FakeDb([_Result(scalar=group), _Result(rows=[(16.0,)])])

    await caps.check_circuit_admission(db, _plug(cap=16.0))  # must not raise
    assert db.calls == 2


@pytest.mark.asyncio
async def test_admission_rejects_over_capacity(monkeypatch):
    """16 A already active on a 30 A circuit; a 16 A start would hit 32 A > 30 A
    -> 409."""
    monkeypatch.setattr(caps, "ENFORCE_CIRCUIT_ADMISSION", True)
    monkeypatch.setattr(caps, "DEFAULT_PLUG_CAP_A", 16.0)
    group = SimpleNamespace(id=1, max_current_a=30.0)
    db = _FakeDb([_Result(scalar=group), _Result(rows=[(16.0,)])])

    with pytest.raises(HTTPException) as ei:
        await caps.check_circuit_admission(db, _plug(cap=16.0))
    assert ei.value.status_code == 409
    assert ei.value.detail["code"] == "circuit_full"
    assert "capacity" in ei.value.detail["message"].lower()


@pytest.mark.asyncio
async def test_admission_noop_when_group_has_no_cap(monkeypatch):
    """A group with max_current_a None never limits — one query (the group), no
    load query, no raise."""
    monkeypatch.setattr(caps, "ENFORCE_CIRCUIT_ADMISSION", True)
    group = SimpleNamespace(id=1, max_current_a=None)
    db = _FakeDb([_Result(scalar=group)])

    await caps.check_circuit_admission(db, _plug(cap=16.0))
    assert db.calls == 1  # only the group load; never reached the sum query


@pytest.mark.asyncio
async def test_admission_noop_for_ungrouped_plug(monkeypatch):
    monkeypatch.setattr(caps, "ENFORCE_CIRCUIT_ADMISSION", True)
    db = _FakeDb([])  # must never execute
    await caps.check_circuit_admission(db, _plug(group_id=None))
    assert db.calls == 0


@pytest.mark.asyncio
async def test_admission_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(caps, "ENFORCE_CIRCUIT_ADMISSION", False)
    db = _FakeDb([])
    await caps.check_circuit_admission(db, _plug(cap=16.0))
    assert db.calls == 0


class _FakeStore:
    """Stand-in TelemetryStore exposing just get_latest for the live-load sum."""
    def __init__(self, snaps):
        self._snaps = snaps  # plug_id -> snapshot

    def get_latest(self, plug_id):
        return self._snaps.get(plug_id)


def _snap(current_a, age_sec=0.0):
    return SimpleNamespace(current_a=current_a, updated_at=time.time() - age_sec)


@pytest.mark.asyncio
async def test_measured_load_uses_measured_current_falls_back_to_cap(monkeypatch):
    """The live figure sums MEASURED current when a fresh snapshot exists, and
    falls back to each plug's configured cap otherwise. Two active plugs (caps
    16 A / 10 A): plug 1 measures 8 A (below its cap, power factor), plug 2 has
    no live reading -> 8 + 10 = 18 A."""
    monkeypatch.setattr(caps, "DEFAULT_PLUG_CAP_A", 16.0)
    db = _FakeDb([_Result(rows=[(1, 16.0), (2, 10.0)])])
    store = _FakeStore({1: _snap(8.0)})

    load = await caps.measured_circuit_load_a(db, 1, store)
    assert load == 18.0
    assert db.calls == 1  # single active-plugs query


@pytest.mark.asyncio
async def test_measured_load_ignores_stale_and_zero_snapshots(monkeypatch):
    """A stale snapshot (gateway link dropped) or a non-positive current is
    treated as unavailable -> the plug falls back to its cap. Plug 1 stale,
    plug 2 reports 0 A -> both use caps (16 + 10 = 26 A)."""
    monkeypatch.setattr(caps, "DEFAULT_PLUG_CAP_A", 16.0)
    db = _FakeDb([_Result(rows=[(1, 16.0), (2, 10.0)])])
    store = _FakeStore({1: _snap(9.0, age_sec=10_000.0), 2: _snap(0.0)})

    load = await caps.measured_circuit_load_a(db, 1, store)
    assert load == 26.0


@pytest.mark.asyncio
async def test_measured_load_no_store_uses_caps(monkeypatch):
    """Without a telemetry store (e.g. no live data yet) every active plug falls
    back to its configured cap, matching circuit_load_a. Uncapped plug uses the
    default (16 A)."""
    monkeypatch.setattr(caps, "DEFAULT_PLUG_CAP_A", 16.0)
    db = _FakeDb([_Result(rows=[(1, 10.0), (2, None)])])

    load = await caps.measured_circuit_load_a(db, 1, telemetry_store=None)
    assert load == 26.0  # 10 (cap) + 16 (default)


@pytest.mark.asyncio
async def test_notify_capacity_available_notifies_fitting_request(monkeypatch):
    """A freed circuit (load 16 A of 32 A) notifies the waiting driver whose
    16 A plug now fits, and clears their request row (one-shot)."""
    import backend.services.capacity as capacity

    monkeypatch.setattr(caps, "DEFAULT_PLUG_CAP_A", 16.0)
    group = SimpleNamespace(id=1, max_current_a=32.0)
    plug = SimpleNamespace(id=99, name="Bay 9", max_current_a=16.0)
    db = _FakeDb([
        _Result(scalar=group),                # group load
        _Result(rows=[(50, 7, 99)]),          # requests: (id, user_id, plug_id)
        _Result(rows=[(16.0,)]),              # circuit_load_a -> 16 A committed
        _Result(scalar_list=[plug]),          # the requested plugs
        _Result(),                            # delete of fired rows
    ])
    notified = []

    async def fake_notify(user_id, ntype, title, body, **kw):
        notified.append((user_id, ntype))

    monkeypatch.setattr("backend.services.notifications.notify", fake_notify)

    n = await capacity.notify_capacity_available(db, group_id=1)
    assert n == 1
    assert notified == [(7, "capacity_available")]


@pytest.mark.asyncio
async def test_notify_capacity_skips_request_that_still_wont_fit(monkeypatch):
    """A circuit still full (30 A of 32 A) can't fit a 16 A plug -> no notify,
    the request stays armed (no delete)."""
    import backend.services.capacity as capacity

    monkeypatch.setattr(caps, "DEFAULT_PLUG_CAP_A", 16.0)
    group = SimpleNamespace(id=1, max_current_a=32.0)
    plug = SimpleNamespace(id=99, name="Bay 9", max_current_a=16.0)
    db = _FakeDb([
        _Result(scalar=group),
        _Result(rows=[(50, 7, 99)]),
        _Result(rows=[(30.0,)]),              # 30 A committed; 30+16 > 32
        _Result(scalar_list=[plug]),
    ])
    notified = []
    monkeypatch.setattr(
        "backend.services.notifications.notify",
        lambda *a, **k: notified.append(a),
    )

    n = await capacity.notify_capacity_available(db, group_id=1)
    assert n == 0
    assert notified == []
