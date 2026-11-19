"""Admin health snapshot: sysinfo parsers, graceful degradation, endpoint shape.

DB-free (mocked AsyncSession). Must pass on Windows: every host source is
allowed to be missing and then reads as None.
"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.routers.admin import admin_health
from backend.services import sysinfo

MEMINFO = """MemTotal:        1013000 kB
MemFree:           50000 kB
MemAvailable:     204800 kB
SwapTotal:        524288 kB
SwapFree:         262144 kB
"""


def test_parse_meminfo():
    m = sysinfo.parse_meminfo(MEMINFO)
    assert m["available_mb"] == 200.0
    assert m["swap_total_mb"] == 512.0
    assert m["swap_used_mb"] == 256.0


@pytest.mark.parametrize("bad", [None, "", "garbage", "MemTotal: 5 kB\n"])
def test_parse_meminfo_degrades_to_none(bad):
    assert sysinfo.parse_meminfo(bad) is None


def test_parse_cgroup_limit_and_unlimited():
    assert sysinfo.parse_cgroup("104857600\n", "314572800\n") == {
        "current_mb": 100.0,
        "limit_mb": 300.0,
    }
    assert sysinfo.parse_cgroup("104857600", "max\n")["limit_mb"] is None
    assert sysinfo.parse_cgroup(None, None) is None


def test_parse_rss():
    assert sysinfo.parse_rss_mb("Name:\tpython\nVmRSS:\t   20480 kB\n") == 20.0
    assert sysinfo.parse_rss_mb(None) is None


def test_collect_never_raises_when_sources_missing():
    with patch.object(sysinfo, "_read", return_value=None):
        snap = sysinfo.collect()
    assert snap["memory"] is None
    assert snap["cgroup"] is None
    assert snap["backend_rss_mb"] is None
    assert snap["host_uptime_s"] is None
    assert snap["process_uptime_s"] >= 0


def _db(size, conns):
    def res(v):
        r = MagicMock()
        r.scalar.return_value = v
        return r

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[res(size), res(conns)])
    db.rollback = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_admin_health_shape_and_last_runs():
    stamp = datetime(2026, 11, 17, 1, 2, 3, tzinfo=timezone.utc)
    reaper = MagicMock(last_run_at=stamp)
    flusher = MagicMock(last_flush_at=None)
    with patch("backend.routers.admin.state") as st:
        st.session_reaper = reaper
        st.telemetry_persistence = flusher
        out = await admin_health(MagicMock(), _db(52 * 1024 * 1024, 7))
    assert out["database"] == {"size_mb": 52.0, "connections": 7}
    assert out["last_runs"] == {
        "session_reaper": stamp.isoformat(),
        "telemetry_flush": None,
    }
    for key in ("memory", "cgroup", "disk", "backend_rss_mb", "process_uptime_s", "generated_at"):
        assert key in out


@pytest.mark.asyncio
async def test_admin_health_survives_db_failure():
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=RuntimeError("db down"))
    db.rollback = AsyncMock()
    with patch("backend.routers.admin.state") as st:
        st.session_reaper = None
        st.telemetry_persistence = None
        out = await admin_health(MagicMock(), db)
    assert out["database"] == {"size_mb": None, "connections": None}
    assert out["last_runs"] == {"session_reaper": None, "telemetry_flush": None}
    db.rollback.assert_awaited()
