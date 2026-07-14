"""
Circuit admission control (services/caps.py) — DB-free.

Exercises the admission arithmetic and gating without a real DB by faking the
two queries check_circuit_admission runs (the locked group select, then the
Σ-active-caps load query). Proves: effective cap falls back to the default;
a start is admitted up to and including the exact circuit limit and rejected
past it; and it's a clean no-op when ungrouped, uncapped, or disabled.
"""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import backend.services.caps as caps


def _plug(group_id=1, cap=16.0):
    return SimpleNamespace(id=99, group_id=group_id, max_current_a=cap)


class _Result:
    def __init__(self, scalar=None, rows=None):
        self._scalar = scalar
        self._rows = rows or []

    def scalar_one_or_none(self):
        return self._scalar

    def all(self):
        return self._rows


class _FakeDb:
    """Returns queued results in order; records how many executes ran."""
    def __init__(self, results):
        self._results = list(results)
        self.calls = 0

    async def execute(self, *a, **k):
        r = self._results[self.calls]
        self.calls += 1
        return r


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
    assert "capacity" in ei.value.detail.lower()


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
