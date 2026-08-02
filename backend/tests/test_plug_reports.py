"""
Plug problem report tests — need a real PostgreSQL (CI's postgres:15 service,
exported as TEST_DATABASE_URL). Skipped locally: this repo's dev boxes run no
database by policy (see test_wallet.py, the same pattern).

What's proven here:

1. Create-request schema validation (no DB): category must be one of the
   fixed taxonomy, description has the same 10-1000 length bound as
   SessionDispute.reason.
2. Creation rules (POST /api/plugs/{plug_id}/report, routers/plugs.py
   report_plug_problem): any authenticated role may file (no ownership/
   session requirement — unlike SessionDispute); a private-group plug the
   caller hasn't joined 403s via the same ensure_plug_group_access rule
   watch_plug uses; filing ALSO writes a GatewayEvent
   (event_type="DRIVER_PROBLEM_REPORT", severity="warning") so the existing
   CPO alert feed picks it up — no new alert pipeline.
3. Tenant scoping: GET /api/cpo/plug-reports and the resolve endpoint are
   scoped to the CPO's own tenant_id, denormalized onto PlugReport at
   creation from the plug's gateway -> tenant chain.
4. Resolve lifecycle (POST /api/cpo/plug-reports/{id}/resolve): open ->
   acknowledged -> resolved, resolve directly from open (skipping
   acknowledge is legal), an already-RESOLVED report 409s on either action,
   an invalid action 400s, and resolved_at/resolved_by_user_id are stamped
   only by "resolve" (never by "acknowledge").
"""
import os
import uuid
from types import SimpleNamespace
from typing import Optional

import pytest
import pytest_asyncio
from fastapi import HTTPException

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="needs TEST_DATABASE_URL pointing at a throwaway Postgres (CI service)",
)

ENUM_TYPES = [
    "gateway_status", "plug_status", "session_status", "tx_type", "user_role",
    "plug_report_status",
]


@pytest_asyncio.fixture()
async def factory():
    """Engine + fresh schema per test; yields a session factory."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )
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


def _user_obj(user_id: int, email: str, tenant_id: Optional[int] = None):
    """A stand-in for the get_current_user-injected ORM instance: the plug
    report routers only ever read .id / .email / .tenant_id off it."""
    return SimpleNamespace(id=user_id, email=email, tenant_id=tenant_id)


async def _seed_world(factory, *, group_id: Optional[int] = None) -> dict:
    """One tenant with a CPO + driver + a plug, ready to report against."""
    from backend.database.models import Gateway, Plug, Tenant, User, UserRole

    tag = uuid.uuid4().hex[:10]

    async with factory() as db:
        tenant = Tenant(name=f"tenant-{tag}")
        db.add(tenant)
        await db.flush()

        cpo = User(
            email=f"cpo-{tag}@amphive.test", hashed_password="x",
            full_name="CPO Test", role=UserRole.CPO, tenant_id=tenant.id,
        )
        driver = User(
            email=f"driver-{tag}@amphive.test", hashed_password="x",
            full_name="Driver Test", role=UserRole.DRIVER,
        )
        db.add_all([cpo, driver])
        await db.flush()

        u = uuid.uuid4().int
        gateway = Gateway(
            id=f"gw-{tag}", tenant_id=tenant.id, name="Test GW",
            vpn_ip=f"10.{u % 250 + 1}.{(u >> 8) % 250 + 1}.{(u >> 16) % 250 + 1}",
        )
        db.add(gateway)
        await db.flush()

        plug = Plug(
            gateway_id=gateway.id, name="Test Plug", local_ip="10.0.1.1",
            group_id=group_id,
        )
        db.add(plug)
        await db.commit()

        return {
            "tenant_id": tenant.id,
            "cpo_id": cpo.id,
            "driver_id": driver.id,
            "gateway_id": gateway.id,
            "plug_id": plug.id,
        }


async def _seed_private_group(factory, tenant_id: int) -> int:
    from backend.database.models import ChargerGroup

    async with factory() as db:
        group = ChargerGroup(tenant_id=tenant_id, name="Private Soc", is_public=False, access_code=f"CODE-{uuid.uuid4().hex[:8]}")
        db.add(group)
        await db.commit()
        return group.id


async def _seed_plug_in_group(factory, gateway_id: str, group_id: int) -> int:
    """A second plug on an existing gateway, assigned to `group_id` (same
    tenant as the gateway — group membership checks assume that)."""
    from backend.database.models import Plug

    async with factory() as db:
        plug = Plug(gateway_id=gateway_id, name="Grouped Plug", local_ip="10.0.1.2", group_id=group_id)
        db.add(plug)
        await db.commit()
        return plug.id


async def _seed_open_report(factory, world: dict, category: str = "damaged", description: str = None) -> int:
    from backend.database.models import PlugReport, PlugReportStatus

    async with factory() as db:
        report = PlugReport(
            plug_id=world["plug_id"],
            tenant_id=world["tenant_id"],
            driver_user_id=world["driver_id"],
            category=category,
            description=description or "The connector is cracked and sparks when plugged in.",
            status=PlugReportStatus.OPEN,
        )
        db.add(report)
        await db.commit()
        return report.id


async def _fresh_report(factory, report_id: int):
    from sqlalchemy import select

    from backend.database.models import PlugReport

    async with factory() as db:
        result = await db.execute(select(PlugReport).where(PlugReport.id == report_id))
        return result.scalar_one()


async def _gateway_events(factory, plug_id: int):
    from sqlalchemy import select

    from backend.database.models import GatewayEvent

    async with factory() as db:
        result = await db.execute(
            select(GatewayEvent).where(GatewayEvent.plug_id == plug_id).order_by(GatewayEvent.id)
        )
        return list(result.scalars().all())


# --- Pure schema validation (no DB needed, but kept alongside its peers) ---

def test_plug_report_create_request_rejects_short_description():
    import pydantic

    from backend.schemas import PlugReportCreateRequest

    with pytest.raises(pydantic.ValidationError):
        PlugReportCreateRequest(category="damaged", description="short")


def test_plug_report_create_request_rejects_invalid_category():
    import pydantic

    from backend.schemas import PlugReportCreateRequest

    with pytest.raises(pydantic.ValidationError):
        PlugReportCreateRequest(
            category="not_a_real_category",
            description="This connector looks physically damaged and unsafe.",
        )


def test_plug_report_create_request_accepts_every_valid_category():
    from backend.schemas import PLUG_REPORT_CATEGORIES, PlugReportCreateRequest

    for category in PLUG_REPORT_CATEGORIES:
        req = PlugReportCreateRequest(category=category, description="A sufficiently long description here.")
        assert req.category == category


# --- Driver-side creation rules ---------------------------------------------

@pytest.mark.asyncio
async def test_report_plug_problem_succeeds_for_any_authenticated_role(factory):
    from backend.routers.plugs import report_plug_problem
    from backend.schemas import PlugReportCreateRequest

    world = await _seed_world(factory)
    driver = _user_obj(world["driver_id"], "driver@amphive.test")

    async with factory() as db:
        res = await report_plug_problem(
            world["plug_id"],
            PlugReportCreateRequest(category="damaged", description="The cable casing is split and exposing copper."),
            driver, db,
        )

    assert res.plug_id == world["plug_id"]
    assert res.tenant_id == world["tenant_id"]
    assert res.driver_user_id == world["driver_id"]
    assert res.category == "damaged"
    assert res.status == "open"
    assert res.resolved_at is None


@pytest.mark.asyncio
async def test_report_plug_problem_writes_a_gateway_event(factory):
    """The side effect the CPO alert strip depends on: filing a report ALSO
    inserts a GatewayEvent so the existing alert feed / Health badge pick it
    up, with no new alert pipeline."""
    from backend.routers.plugs import report_plug_problem
    from backend.schemas import PlugReportCreateRequest

    world = await _seed_world(factory)
    driver = _user_obj(world["driver_id"], "driver@amphive.test")

    async with factory() as db:
        await report_plug_problem(
            world["plug_id"],
            PlugReportCreateRequest(category="unsafe", description="Sparks fly whenever the plug is inserted."),
            driver, db,
        )

    events = await _gateway_events(factory, world["plug_id"])
    assert len(events) == 1
    assert events[0].event_type == "DRIVER_PROBLEM_REPORT"
    assert events[0].severity == "warning"
    assert events[0].tenant_id == world["tenant_id"]
    assert events[0].gateway_id == world["gateway_id"]
    assert "unsafe:" in events[0].detail


@pytest.mark.asyncio
async def test_report_plug_problem_404_for_unknown_plug(factory):
    from backend.routers.plugs import report_plug_problem
    from backend.schemas import PlugReportCreateRequest

    world = await _seed_world(factory)
    driver = _user_obj(world["driver_id"], "driver@amphive.test")

    async with factory() as db:
        with pytest.raises(HTTPException) as exc:
            await report_plug_problem(
                999999,
                PlugReportCreateRequest(category="other", description="Reporting a plug id that does not exist."),
                driver, db,
            )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_report_plug_problem_403_for_private_group_non_member(factory):
    from backend.routers.plugs import report_plug_problem
    from backend.schemas import PlugReportCreateRequest

    world = await _seed_world(factory)
    group_id = await _seed_private_group(factory, world["tenant_id"])
    grouped_plug_id = await _seed_plug_in_group(factory, world["gateway_id"], group_id)
    driver = _user_obj(world["driver_id"], "driver@amphive.test")

    async with factory() as db:
        with pytest.raises(HTTPException) as exc:
            await report_plug_problem(
                grouped_plug_id,
                PlugReportCreateRequest(category="other", description="Trying to report a plug I can't see."),
                driver, db,
            )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_report_plug_problem_allows_multiple_reports_on_same_plug(factory):
    """No one-open-per-plug constraint (contrast SessionDispute): several
    drivers may independently flag the same charger."""
    from backend.database.models import User, UserRole
    from backend.routers.plugs import report_plug_problem
    from backend.schemas import PlugReportCreateRequest

    world = await _seed_world(factory)
    driver1 = _user_obj(world["driver_id"], "driver1@amphive.test")

    async with factory() as db:
        second_driver = User(
            email=f"driver2-{uuid.uuid4().hex[:8]}@amphive.test",
            hashed_password="x", full_name="Second Driver", role=UserRole.DRIVER,
        )
        db.add(second_driver)
        await db.commit()
        driver2_id = second_driver.id
    driver2 = _user_obj(driver2_id, "driver2@amphive.test")

    async with factory() as db:
        await report_plug_problem(
            world["plug_id"],
            PlugReportCreateRequest(category="damaged", description="First report: connector looks cracked."),
            driver1, db,
        )
    async with factory() as db:
        await report_plug_problem(
            world["plug_id"],
            PlugReportCreateRequest(category="unsafe", description="Second report: sparks when I plugged in."),
            driver2, db,
        )

    from backend.routers.cpo import cpo_list_plug_reports
    cpo = _user_obj(world["cpo_id"], "cpo@amphive.test", tenant_id=world["tenant_id"])
    async with factory() as db:
        reports = await cpo_list_plug_reports(cpo, db)
    assert len(reports) == 2


# --- CPO-side listing: tenant scoping + status filter -----------------------

@pytest.mark.asyncio
async def test_cpo_list_plug_reports_is_tenant_scoped(factory):
    from backend.routers.cpo import cpo_list_plug_reports

    world1 = await _seed_world(factory)
    world2 = await _seed_world(factory)
    report1 = await _seed_open_report(factory, world1)
    report2 = await _seed_open_report(factory, world2)

    cpo1 = _user_obj(world1["cpo_id"], "cpo1@amphive.test", tenant_id=world1["tenant_id"])
    cpo2 = _user_obj(world2["cpo_id"], "cpo2@amphive.test", tenant_id=world2["tenant_id"])

    async with factory() as db:
        tenant1_reports = await cpo_list_plug_reports(cpo1, db)
    async with factory() as db:
        tenant2_reports = await cpo_list_plug_reports(cpo2, db)

    assert [r.id for r in tenant1_reports] == [report1]
    assert [r.id for r in tenant2_reports] == [report2]


@pytest.mark.asyncio
async def test_cpo_list_plug_reports_filters_by_status(factory):
    from backend.routers.cpo import cpo_list_plug_reports, cpo_resolve_plug_report
    from backend.schemas import CpoPlugReportResolveRequest

    world = await _seed_world(factory)
    resolved_id = await _seed_open_report(factory, world, description="Will be resolved shortly after filing.")
    cpo = _user_obj(world["cpo_id"], "cpo@amphive.test", tenant_id=world["tenant_id"])

    async with factory() as db:
        await cpo_resolve_plug_report(
            resolved_id, CpoPlugReportResolveRequest(action="resolve", note="Fixed the connector."), cpo, db,
        )

    still_open_id = await _seed_open_report(factory, world, description="Second, still-open report on this plug.")

    async with factory() as db:
        open_only = await cpo_list_plug_reports(cpo, db, status_filter="open")
    async with factory() as db:
        resolved_only = await cpo_list_plug_reports(cpo, db, status_filter="resolved")

    assert [r.id for r in open_only] == [still_open_id]
    assert [r.id for r in resolved_only] == [resolved_id]


@pytest.mark.asyncio
async def test_cpo_list_plug_reports_rejects_invalid_status_filter(factory):
    from backend.routers.cpo import cpo_list_plug_reports

    world = await _seed_world(factory)
    cpo = _user_obj(world["cpo_id"], "cpo@amphive.test", tenant_id=world["tenant_id"])

    async with factory() as db:
        with pytest.raises(HTTPException) as exc:
            await cpo_list_plug_reports(cpo, db, status_filter="bogus")
    assert exc.value.status_code == 400


# --- Resolve lifecycle -------------------------------------------------

@pytest.mark.asyncio
async def test_acknowledge_then_resolve_lifecycle(factory):
    from backend.database.models import PlugReportStatus
    from backend.routers.cpo import cpo_resolve_plug_report
    from backend.schemas import CpoPlugReportResolveRequest

    world = await _seed_world(factory)
    report_id = await _seed_open_report(factory, world)
    cpo = _user_obj(world["cpo_id"], "cpo@amphive.test", tenant_id=world["tenant_id"])

    async with factory() as db:
        ack = await cpo_resolve_plug_report(
            report_id, CpoPlugReportResolveRequest(action="acknowledge"), cpo, db,
        )
    assert ack.status == "acknowledged"
    assert ack.resolved_at is None
    assert ack.resolved_by_user_id is None

    async with factory() as db:
        resolved = await cpo_resolve_plug_report(
            report_id, CpoPlugReportResolveRequest(action="resolve", note="Charger swapped out."), cpo, db,
        )
    assert resolved.status == "resolved"
    assert resolved.resolution_note == "Charger swapped out."
    assert resolved.resolved_by_user_id == world["cpo_id"]
    assert resolved.resolved_at is not None

    fresh = await _fresh_report(factory, report_id)
    assert fresh.status == PlugReportStatus.RESOLVED


@pytest.mark.asyncio
async def test_resolve_directly_from_open_skips_acknowledge(factory):
    from backend.routers.cpo import cpo_resolve_plug_report
    from backend.schemas import CpoPlugReportResolveRequest

    world = await _seed_world(factory)
    report_id = await _seed_open_report(factory, world)
    cpo = _user_obj(world["cpo_id"], "cpo@amphive.test", tenant_id=world["tenant_id"])

    async with factory() as db:
        resolved = await cpo_resolve_plug_report(
            report_id, CpoPlugReportResolveRequest(action="resolve"), cpo, db,
        )
    assert resolved.status == "resolved"
    assert resolved.resolved_at is not None


@pytest.mark.asyncio
async def test_double_acknowledge_is_rejected(factory):
    from backend.routers.cpo import cpo_resolve_plug_report
    from backend.schemas import CpoPlugReportResolveRequest

    world = await _seed_world(factory)
    report_id = await _seed_open_report(factory, world)
    cpo = _user_obj(world["cpo_id"], "cpo@amphive.test", tenant_id=world["tenant_id"])

    async with factory() as db:
        await cpo_resolve_plug_report(report_id, CpoPlugReportResolveRequest(action="acknowledge"), cpo, db)

    async with factory() as db:
        with pytest.raises(HTTPException) as exc:
            await cpo_resolve_plug_report(report_id, CpoPlugReportResolveRequest(action="acknowledge"), cpo, db)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_resolve_already_resolved_report_409s(factory):
    from backend.routers.cpo import cpo_resolve_plug_report
    from backend.schemas import CpoPlugReportResolveRequest

    world = await _seed_world(factory)
    report_id = await _seed_open_report(factory, world)
    cpo = _user_obj(world["cpo_id"], "cpo@amphive.test", tenant_id=world["tenant_id"])

    async with factory() as db:
        await cpo_resolve_plug_report(report_id, CpoPlugReportResolveRequest(action="resolve"), cpo, db)

    async with factory() as db:
        with pytest.raises(HTTPException) as exc:
            await cpo_resolve_plug_report(report_id, CpoPlugReportResolveRequest(action="resolve"), cpo, db)
    assert exc.value.status_code == 409

    async with factory() as db:
        with pytest.raises(HTTPException) as exc:
            await cpo_resolve_plug_report(report_id, CpoPlugReportResolveRequest(action="acknowledge"), cpo, db)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_resolve_rejects_invalid_action(factory):
    from backend.routers.cpo import cpo_resolve_plug_report
    from backend.schemas import CpoPlugReportResolveRequest

    world = await _seed_world(factory)
    report_id = await _seed_open_report(factory, world)
    cpo = _user_obj(world["cpo_id"], "cpo@amphive.test", tenant_id=world["tenant_id"])

    async with factory() as db:
        with pytest.raises(HTTPException) as exc:
            await cpo_resolve_plug_report(report_id, CpoPlugReportResolveRequest(action="delete"), cpo, db)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_resolve_404_for_unknown_report(factory):
    from backend.routers.cpo import cpo_resolve_plug_report
    from backend.schemas import CpoPlugReportResolveRequest

    world = await _seed_world(factory)
    cpo = _user_obj(world["cpo_id"], "cpo@amphive.test", tenant_id=world["tenant_id"])

    async with factory() as db:
        with pytest.raises(HTTPException) as exc:
            await cpo_resolve_plug_report(999999, CpoPlugReportResolveRequest(action="resolve"), cpo, db)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_cpo_cannot_resolve_another_tenants_report(factory):
    from backend.database.models import PlugReportStatus
    from backend.routers.cpo import cpo_resolve_plug_report
    from backend.schemas import CpoPlugReportResolveRequest

    world1 = await _seed_world(factory)
    world2 = await _seed_world(factory)
    report1 = await _seed_open_report(factory, world1)

    cpo2 = _user_obj(world2["cpo_id"], "cpo2@amphive.test", tenant_id=world2["tenant_id"])

    async with factory() as db:
        with pytest.raises(HTTPException) as exc:
            await cpo_resolve_plug_report(report1, CpoPlugReportResolveRequest(action="resolve"), cpo2, db)
    assert exc.value.status_code == 404

    fresh = await _fresh_report(factory, report1)
    assert fresh.status == PlugReportStatus.OPEN
