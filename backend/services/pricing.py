"""
AmpHive tariff / pricing resolution
=====================================
Resolves the coins-per-kWh rate that applies to a plug — the per-CPO/per-site
tariff foundation replacing the old single global ``COINS_PER_KWH`` env var.

Fallback chain (first match wins), mirrored on ``Tariff`` in
``database/models.py``::

    plug.tariff -> plug's charger group's tariff -> tenant.default_tariff
    -> the global COINS_PER_KWH env var (legacy, pre-tariff behavior)

This module only *resolves* a rate — it never bills anything. The resolved
Decimal must be SNAPSHOTTED onto ``ChargingSession.rate_coins_per_kwh`` at
session start (``routers/sessions.py`` ``start_charging_session``) so a
tariff edit or reassignment mid-session never retroactively changes what an
in-flight or already-billed session is charged. Every downstream billing
path — ``finalize_charging_session``, the mqtt_manager
balance-exhaustion auto-stop, and the live ``TelemetryStore`` cost calc —
reads that snapshot and only calls back into this module's env fallback
(:func:`default_rate`) when the snapshot is NULL (legacy sessions predating
this column).
"""
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.models import ChargerGroup, Gateway, Plug, Tariff, Tenant
from backend.services.money import to_money
from backend.services.telemetry import COINS_PER_KWH


def default_rate() -> Decimal:
    """The global env-configured rate (``COINS_PER_KWH``), as 2dp Decimal
    money. The last link in the fallback chain, and what legacy (pre-tariff)
    sessions with a NULL ``rate_coins_per_kwh`` snapshot still bill at."""
    return to_money(COINS_PER_KWH)


async def resolve_rate_for_plug(db: AsyncSession, plug: Plug) -> Decimal:
    """
    Resolve the coins-per-kWh rate for ``plug`` via the fallback chain:
    plug.tariff -> plug's group.tariff -> tenant.default_tariff -> env default.

    Returns 2dp Decimal money. Read-only — does not mutate or snapshot
    anything; callers that start a session must persist the result onto
    ``ChargingSession.rate_coins_per_kwh`` themselves.
    """
    # 1. Plug's own tariff.
    if plug.tariff_id is not None:
        tariff = (
            await db.execute(select(Tariff).where(Tariff.id == plug.tariff_id))
        ).scalar_one_or_none()
        if tariff is not None:
            return to_money(tariff.price_per_kwh)

    # 2. The plug's charger group's tariff (if grouped).
    if plug.group_id is not None:
        group = (
            await db.execute(select(ChargerGroup).where(ChargerGroup.id == plug.group_id))
        ).scalar_one_or_none()
        if group is not None and group.tariff_id is not None:
            tariff = (
                await db.execute(select(Tariff).where(Tariff.id == group.tariff_id))
            ).scalar_one_or_none()
            if tariff is not None:
                return to_money(tariff.price_per_kwh)

    # 3. The tenant's default tariff. Plug has no direct tenant_id — resolve
    #    via its gateway (Gateway.tenant_id is the authoritative owner link).
    gateway = (
        await db.execute(select(Gateway).where(Gateway.id == plug.gateway_id))
    ).scalar_one_or_none()
    if gateway is not None:
        tenant = (
            await db.execute(select(Tenant).where(Tenant.id == gateway.tenant_id))
        ).scalar_one_or_none()
        if tenant is not None and tenant.default_tariff_id is not None:
            tariff = (
                await db.execute(select(Tariff).where(Tariff.id == tenant.default_tariff_id))
            ).scalar_one_or_none()
            if tariff is not None:
                return to_money(tariff.price_per_kwh)

    # 4. Nothing configured anywhere in the chain — legacy global default.
    return default_rate()
