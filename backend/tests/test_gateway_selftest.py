"""
C10: gateway commissioning checklist.

Tier 1 (DB-free, always run): the pure check evaluation in
services/gateway_selftest.py (thresholds, unknown cases, overall roll-up) on
transient model instances.

Tier 2 (DB-gated, CI only): GET /api/cpo/gateways/{id}/selftest called
directly: cross-tenant 404, and a cpo_viewer gets 200 with nothing sensitive.
"""
import itertools
import os
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from fastapi import HTTPException

from backend.database.models import (
    FirmwareRelease,
    Gateway,
    GatewayEvent,
    GatewayStatus,
    Plug,
    Tenant,
    User,
    UserRole,
)
from backend.services import gateway_selftest as st
from backend.services.mqtt_manager import PLUG_POWER_STALE_SEC
from backend.services.session_lifecycle import GATEWAY_LIVENESS_WINDOW_SEC

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)


def _gw(status=GatewayStatus.ONLINE, seen_ago=10, fw="2.9.0-direct"):
    return Gateway(
        id="aa11bb22cc33", name="gw", vpn_ip="x", status=status,
        last_seen_at=NOW - timedelta(seconds=seen_ago), firmware_version=fw,
    )


def _plug(pid=1, name="Bay 1", tel_ago=5):
    return Plug(
        id=pid, gateway_id="aa11bb22cc33", name=name, local_ip="10.0.0.9",
        last_telemetry_at=None if tel_ago is None else NOW - timedelta(seconds=tel_ago),
    )


def _ev(kind, plug_id=1, ago=60, detail=None):
    return GatewayEvent(
        tenant_id=1, gateway_id="aa11bb22cc33", plug_id=plug_id, event_type=kind,
        detail=detail, created_at=NOW - timedelta(seconds=ago),
    )


def _boot(rssi, ago=3600):
    return _ev("GATEWAY_BOOT", None, ago, f"Gateway booted (fw 2.9.0-direct): boot #3, Wi-Fi RSSI {rssi} dBm after 2 attempts.")


# --- online ---------------------------------------------------------------

def test_online_pass_uses_the_liveness_window():
    assert st.check_online(_gw(seen_ago=GATEWAY_LIVENESS_WINDOW_SEC - 1), NOW)["status"] == "pass"
    stale = st.check_online(_gw(seen_ago=GATEWAY_LIVENESS_WINDOW_SEC + 1), NOW)
    assert stale["status"] == "fail" and stale["fix_hint"]


def test_online_fails_when_status_offline_even_if_recent():
    assert st.check_online(_gw(status=GatewayStatus.OFFLINE, seen_ago=1), NOW)["status"] == "fail"


# --- wifi -----------------------------------------------------------------

@pytest.mark.parametrize("rssi,expected", [(-55, "pass"), (-70, "pass"), (-71, "warn"),
                                           (-80, "warn"), (-81, "fail"), (-97, "fail")])
def test_wifi_thresholds(rssi, expected):
    c = st.check_wifi(_boot(rssi), NOW)
    assert c["status"] == expected
    assert "at boot" in c["label"] and c["observed_at"]


def test_wifi_unknown_without_boot_event_or_reading():
    assert st.check_wifi(None, NOW)["status"] == "unknown"
    assert st.check_wifi(_ev("GATEWAY_BOOT", None, 5, "Gateway booted."), NOW)["status"] == "unknown"


# --- firmware -------------------------------------------------------------

def test_firmware_behind_current_and_unknown():
    rel = ["2.9.0-direct", "2.10.0-direct", "3.0.0", "1.0.0-direct"]
    assert st.check_firmware("2.9.0-direct", rel)["status"] == "warn"  # 2.10 > 2.9, numeric
    assert st.check_firmware("2.10.0-direct", rel)["status"] == "pass"
    assert st.check_firmware("2.11.0-direct", rel)["status"] == "pass"  # ahead of catalogue
    assert st.check_firmware("2.9.0-direct", ["3.0.0"])["status"] == "unknown"  # other variant
    assert st.check_firmware(None, rel)["status"] == "unknown"
    assert st.check_firmware("garbage", rel)["status"] == "unknown"


# --- plug telemetry / roster ---------------------------------------------

def test_plug_telemetry_fresh_stale_and_silent():
    assert st.check_plug_telemetry(_plug(tel_ago=PLUG_POWER_STALE_SEC - 1), NOW)["status"] == "pass"
    assert st.check_plug_telemetry(_plug(tel_ago=PLUG_POWER_STALE_SEC + 1), NOW)["status"] == "warn"
    assert st.check_plug_telemetry(_plug(tel_ago=23 * 3600), NOW)["status"] == "warn"
    assert st.check_plug_telemetry(_plug(tel_ago=25 * 3600), NOW)["status"] == "fail"
    assert st.check_plug_telemetry(_plug(tel_ago=None), NOW)["status"] == "fail"


def test_roster_pass_unknown_and_no_plugs():
    assert st.check_roster([_plug(1), _plug(2)], NOW)["status"] == "pass"
    assert st.check_roster([_plug(1), _plug(2, tel_ago=None)], NOW)["status"] == "unknown"
    assert st.check_roster([], NOW)["status"] == "warn"


# --- unresolved PLUG_UNREACHABLE -----------------------------------------

def test_unreachable_needs_a_later_recovery_to_clear():
    plugs = [_plug(1, "Bay 1"), _plug(2, "Bay 2")]
    only_down = [_ev("PLUG_UNREACHABLE", 1, 600)]
    c = st.check_unreachable(only_down, plugs)
    assert c["status"] == "warn" and "Bay 1" in c["detail"]

    recovered = [_ev("PLUG_UNREACHABLE", 1, 600), _ev("PLUG_RECOVERED", 1, 300)]
    assert st.check_unreachable(recovered, plugs)["status"] == "pass"

    # Recovery came BEFORE the newest unreachable: still down. Plug 2 is fine.
    relapsed = [_ev("PLUG_RECOVERED", 1, 900), _ev("PLUG_UNREACHABLE", 1, 100),
                _ev("PLUG_UNREACHABLE", 2, 800), _ev("PLUG_RECOVERED", 2, 700)]
    c = st.check_unreachable(relapsed, plugs)
    assert c["status"] == "warn" and "Bay 1" in c["detail"] and "Bay 2" not in c["detail"]

    assert st.check_unreachable([], plugs)["status"] == "pass"


# --- roll-up --------------------------------------------------------------

def test_overall_rolls_up_worst_status_and_ignores_unknown():
    def cs(*s):
        return [{"status": x} for x in s]
    assert st.overall_status(cs("pass", "unknown")) == "pass"
    assert st.overall_status(cs("pass", "warn", "unknown")) == "warn"
    assert st.overall_status(cs("warn", "fail", "pass")) == "fail"


def test_build_selftest_shape_and_no_sensitive_fields():
    out = st.build_selftest(_gw(), [_plug()], _boot(-60), [], ["2.9.0-direct"], NOW)
    assert set(out) == {"gateway_id", "generated_at", "overall", "checks"}
    assert out["overall"] == "pass"
    keys = [c["key"] for c in out["checks"]]
    assert keys == ["online", "wifi", "firmware", "roster", "plug_1", "plug_faults"]
    for c in out["checks"]:
        assert set(c) == {"key", "label", "status", "detail", "fix_hint", "observed_at"}
    assert "10.0.0.9" not in str(out)


# ===========================================================================
# Tier 2: DB-gated route test
# ===========================================================================

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
requires_db = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="needs TEST_DATABASE_URL pointing at a throwaway Postgres (CI service)",
)
ENUM_TYPES = [
    "gateway_status", "plug_status", "session_status", "tx_type", "user_role",
    "payout_status",
]
_n = itertools.count(1)


@pytest_asyncio.fixture()
async def factory():
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from backend.database.models import Base

    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        for enum_name in ENUM_TYPES:
            await conn.execute(text(f"DROP TYPE IF EXISTS {enum_name} CASCADE"))
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


@requires_db
@pytest.mark.asyncio
async def test_selftest_route_tenant_scope_and_viewer_masking(factory):
    from sqlalchemy import select

    from backend.routers.cpo import cpo_gateway_selftest

    now = datetime.now(timezone.utc)
    async with factory() as db:
        t1, t2 = Tenant(name=f"t1-{next(_n)}"), Tenant(name=f"t2-{next(_n)}")
        db.add_all([t1, t2])
        await db.flush()
        viewer = User(email=f"v{next(_n)}@example.com", hashed_password="x",
                      full_name="Viewer", role=UserRole.CPO_VIEWER, tenant_id=t1.id)
        other = User(email=f"o{next(_n)}@example.com", hashed_password="x",
                     full_name="Other", role=UserRole.CPO, tenant_id=t2.id)
        gw = Gateway(id=f"gw{next(_n)}", tenant_id=t1.id, name="gw", vpn_ip=f"vpn{next(_n)}",
                     status=GatewayStatus.ONLINE, last_seen_at=now, firmware_version="2.8.0-direct")
        db.add_all([viewer, other, gw, FirmwareRelease(version="2.9.0-direct", url="https://x/y.bin")])
        await db.flush()
        plug = Plug(gateway_id=gw.id, name="Bay 1", local_ip="192.168.1.77", last_telemetry_at=now)
        plug_b = Plug(gateway_id=gw.id, name="Bay 2", local_ip="192.168.1.78", last_telemetry_at=now)
        db.add_all([plug, plug_b])
        await db.flush()
        db.add_all([
            # A stable gateway's boot report can be older than any date cap.
            GatewayEvent(tenant_id=t1.id, gateway_id=gw.id, event_type="GATEWAY_BOOT",
                         detail="Gateway booted: Wi-Fi RSSI -75 dBm after 1 attempt.",
                         created_at=now - timedelta(days=45)),
            # Bay 1 flaps and recovers; Bay 2 went down earlier and never came back.
            GatewayEvent(tenant_id=t1.id, gateway_id=gw.id, plug_id=plug_b.id,
                         event_type="PLUG_UNREACHABLE", created_at=now - timedelta(hours=10)),
            GatewayEvent(tenant_id=t1.id, gateway_id=gw.id, plug_id=plug.id,
                         event_type="PLUG_UNREACHABLE", created_at=now - timedelta(minutes=9)),
            GatewayEvent(tenant_id=t1.id, gateway_id=gw.id, plug_id=plug.id,
                         event_type="PLUG_RECOVERED", created_at=now - timedelta(minutes=5)),
        ])
        await db.commit()
        gw_id = gw.id

    async with factory() as db:
        viewer = (await db.execute(
            select(User).where(User.role == UserRole.CPO_VIEWER))).scalar_one()
        other = (await db.execute(
            select(User).where(User.role == UserRole.CPO))).scalar_one()

        with pytest.raises(HTTPException) as exc:
            await cpo_gateway_selftest(gw_id, other, db)
        assert exc.value.status_code == 404
        with pytest.raises(HTTPException) as exc:
            await cpo_gateway_selftest("no-such-gateway", viewer, db)
        assert exc.value.status_code == 404

        out = await cpo_gateway_selftest(gw_id, viewer, db)

    by_key = {c["key"]: c for c in out["checks"]}
    assert out["overall"] == "warn"
    assert by_key["online"]["status"] == "pass"
    assert by_key["wifi"]["status"] == "warn"
    assert by_key["firmware"]["status"] == "warn"
    assert by_key["plug_faults"]["status"] == "warn"
    assert "Bay 2" in by_key["plug_faults"]["detail"]
    assert "Bay 1" not in by_key["plug_faults"]["detail"]
    assert "192.168.1.77" not in str(out)
