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

import backend.services.telemetry_persistence as tp
from backend.services.mqtt_manager import MQTTManager


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


@pytest.mark.asyncio
async def test_prune_filters_on_plug_id_so_the_index_is_usable(monkeypatch):
    """The hourly prune must filter on plug_id as well as recorded_at:
    recorded_at alone has no index, which made it a full-table scan."""
    from sqlalchemy.dialects import postgresql

    monkeypatch.setattr(tp, "RETENTION_DAYS", 90)
    executed = []

    class _Result:
        rowcount = 0

        def scalars(self):
            return self

        def all(self):
            return [1, 2]

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, stmt):
            executed.append(stmt)
            return _Result()

        async def commit(self):
            pass

    await tp.TelemetryPersistenceService(db_session_factory=_Session)._prune_old()

    sql = str(executed[-1].compile(dialect=postgresql.dialect()))
    assert sql.startswith("DELETE FROM telemetry_readings")
    assert "telemetry_readings.plug_id IN" in sql
    assert "telemetry_readings.recorded_at <" in sql


class _FlushSession:
    """Fake AsyncSession: 2 selects (tenant, active session), then inserts. An
    insert containing a row with bad=True raises IntegrityError (atomically)."""

    def __init__(self, store, error):
        self.store, self.error, self.pending, self.selects = store, error, [], 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt, params=None):
        if params is None:
            self.selects += 1

            class _R:
                def all(_self, n=self.selects):
                    return [(1, 10)] if n == 1 else []

            return _R()
        if self.error is not None:
            raise self.error
        if any(r.get("bad") for r in params):
            from sqlalchemy.exc import IntegrityError

            raise IntegrityError("INSERT", {}, Exception("check constraint"))
        self.pending.extend(params)

    def begin_nested(self):
        return self

    async def commit(self):
        self.store.extend(self.pending)
        self.pending = []

    async def rollback(self):
        self.pending = []


def _flush_svc(store, error=None):
    svc = tp.TelemetryPersistenceService(db_session_factory=lambda: _FlushSession(store, error))
    for i, bad in enumerate([False, True, False]):
        svc.enqueue({"plug_id": 1, "power_w": float(i), "bad": bad})
    return svc


@pytest.mark.asyncio
async def test_flush_isolates_bad_row_and_persists_good_rows():
    """U6: one bad row must not block the rest of the batch forever."""
    store = []
    svc = _flush_svc(store)
    await svc._flush_once()

    assert [r["power_w"] for r in store] == [0.0, 2.0]
    assert svc._bad_rows_dropped == 1
    assert len(svc._buffer) == 0  # nothing re-buffered, no infinite retry


class _PgOrig(Exception):
    """Stand-in for SQLAlchemy's translated asyncpg error (carries sqlstate)."""

    def __init__(self, msg, sqlstate=None):
        super().__init__(msg)
        self.sqlstate = self.pgcode = sqlstate


def _asyncpg_style_error(sqlstate):
    """A plain DBAPIError (not IntegrityError/DataError), as the asyncpg adapter
    produces for Postgres class-22 errors such as StringDataRightTruncation."""
    from sqlalchemy.exc import DBAPIError

    return DBAPIError("INSERT", {}, _PgOrig("value too long", sqlstate))


@pytest.mark.asyncio
async def test_flush_isolates_asyncpg_class22_error():
    """U6: asyncpg class-22 errors arrive as a generic DBAPIError; the SQLSTATE,
    not the wrapper class, must mark them as bad-row errors (e.g. status > 20 chars)."""
    assert tp._is_data_error(_asyncpg_style_error("22001"))
    assert tp._is_data_error(_asyncpg_style_error("22003"))
    assert not tp._is_data_error(_asyncpg_style_error("08006"))
    assert not tp._is_data_error(_asyncpg_style_error(None))

    class DataError(Exception):  # name-matches asyncpg's client-side DataError
        pass

    client_side = _asyncpg_style_error(None)
    client_side.orig.__cause__ = DataError("invalid input for query argument $1")
    assert tp._is_data_error(client_side)

    class _Session(_FlushSession):
        async def execute(self, stmt, params=None):
            if params is not None and any(r.get("bad") for r in params):
                raise _asyncpg_style_error("22001")
            return await super().execute(stmt, params)

    store = []
    svc = tp.TelemetryPersistenceService(db_session_factory=lambda: _Session(store, None))
    for i, bad in enumerate([False, True, False]):
        svc.enqueue({"plug_id": 1, "power_w": float(i), "bad": bad})
    await svc._flush_once()

    assert [r["power_w"] for r in store] == [0.0, 2.0]
    assert svc._bad_rows_dropped == 1
    assert len(svc._buffer) == 0


@pytest.mark.asyncio
async def test_flush_rebuffers_batch_on_transient_error():
    """A connection/DB-down error still re-buffers the whole batch, in order."""
    from sqlalchemy.exc import OperationalError

    store = []
    svc = _flush_svc(store, error=OperationalError("INSERT", {}, Exception("conn lost")))
    await svc._flush_once()

    assert store == []
    assert [r["power_w"] for r in svc._buffer] == [0.0, 1.0, 2.0]
    assert svc._bad_rows_dropped == 0


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
