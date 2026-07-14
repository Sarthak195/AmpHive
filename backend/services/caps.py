"""
AmpHive circuit admission control (caps + circuits feature).

Hard guarantee that a group's plugs never collectively exceed the shared
circuit/line capacity (``ChargerGroup.max_current_a``): a session start is
admitted only if Σ(effective current caps of the group's ALREADY-ACTIVE plugs)
plus the starting plug's cap stays within the circuit limit. Because a plug
can't draw more than its cap, admission is a structural guarantee — there is no
mid-session load modulation (a P110 is relay+meter, it can't throttle) and no
grace window (explicitly rejected in the design).

Effective per-plug cap = ``Plug.max_current_a``, or ``DEFAULT_PLUG_CAP_A`` (the
16 A hardware cutoff) when unset. NOTE: firmware enforcement of a SUB-default
plug cap is a pending OTA, so today the guarantee is hard at the default and
admission-advisory below it.
"""
import logging
import os
import time
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.models import ChargerGroup, ChargingSession, Plug, SessionStatus

logger = logging.getLogger("amphive.caps")

# The current a plug is assumed to draw when it has no explicit cap — the P110's
# hardware auto-cutoff. Used for both admission math and the ON-payload default.
DEFAULT_PLUG_CAP_A = float(os.getenv("DEFAULT_PLUG_CAP_A", "16"))
# Master switch for the circuit admission gate (default on). Off = pre-caps
# behaviour (a group's plugs can all run regardless of its max_current_a).
ENFORCE_CIRCUIT_ADMISSION = os.getenv(
    "ENFORCE_CIRCUIT_ADMISSION", "true"
).lower() in ("1", "true", "yes")


def effective_plug_cap(plug) -> float:
    """A plug's admission current cap (amps): its own ``max_current_a``, or the
    default hardware cutoff when unset."""
    return plug.max_current_a if plug.max_current_a is not None else DEFAULT_PLUG_CAP_A


async def circuit_load_a(db: AsyncSession, group_id: int, exclude_plug_id=None) -> float:
    """Σ effective caps of the group's plugs that currently hold an ACTIVE
    session — the committed load on the circuit. ``exclude_plug_id`` drops one
    plug (e.g. the one being started, so it is never double-counted)."""
    q = (
        select(Plug.max_current_a)
        .join(ChargingSession, ChargingSession.plug_id == Plug.id)
        .where(
            and_(
                Plug.group_id == group_id,
                ChargingSession.status == SessionStatus.ACTIVE,
            )
        )
    )
    if exclude_plug_id is not None:
        q = q.where(Plug.id != exclude_plug_id)
    rows = (await db.execute(q)).all()
    return sum((cap if cap is not None else DEFAULT_PLUG_CAP_A) for (cap,) in rows)


def _fresh_measured_current_a(telemetry_store, plug_id: int) -> Optional[float]:
    """The plug's latest MEASURED current (amps) from the live telemetry
    snapshot, or None when unavailable/stale. A real plug reports current ~every
    1 s during a session, so a snapshot older than the stream's staleness window
    (the gateway link dropped) is treated as unavailable and the caller falls
    back to the configured cap."""
    if telemetry_store is None:
        return None
    snap = telemetry_store.get_latest(plug_id)
    if snap is None or snap.current_a is None or snap.current_a <= 0:
        return None
    from backend.services.telemetry import TELEMETRY_STALE_AFTER_SEC
    if (time.time() - snap.updated_at) > TELEMETRY_STALE_AFTER_SEC:
        return None
    return float(snap.current_a)


async def measured_circuit_load_a(
    db: AsyncSession, group_id: int, telemetry_store=None, exclude_plug_id=None
) -> float:
    """LIVE load figure for the operator's "X / Y A in use" display: Σ over the
    group's ACTIVE plugs of each plug's MEASURED current when a fresh live
    reading is available, else its configured effective cap.

    Distinct from ``circuit_load_a`` (which sums configured caps): because active
    power factor is < 1 the measured amps a plug actually draws run below its
    cap, so this shows the operator the real committed load. Admission
    (``check_circuit_admission``) must NOT use this — there is no measured
    current before a plug starts, so start-time gating stays on configured caps.
    """
    q = (
        select(Plug.id, Plug.max_current_a)
        .join(ChargingSession, ChargingSession.plug_id == Plug.id)
        .where(
            and_(
                Plug.group_id == group_id,
                ChargingSession.status == SessionStatus.ACTIVE,
            )
        )
    )
    if exclude_plug_id is not None:
        q = q.where(Plug.id != exclude_plug_id)
    rows = (await db.execute(q)).all()

    total = 0.0
    for plug_id, cap in rows:
        measured = _fresh_measured_current_a(telemetry_store, plug_id)
        if measured is not None:
            total += measured
        else:
            total += cap if cap is not None else DEFAULT_PLUG_CAP_A
    return total


async def check_circuit_admission(db: AsyncSession, plug: Plug) -> None:
    """Raise 409 if starting ``plug`` would push its circuit past capacity.
    No-op when disabled, the plug is ungrouped, or the group has no cap set.

    Call under the plug row lock at session start (after the AVAILABLE check).
    The group row is locked here too so concurrent starts on DIFFERENT plugs of
    the SAME circuit serialize — otherwise each could read the load before the
    other's session commits and both admit, overshooting the circuit.
    Lock order is plug → group (nothing locks group → plug).
    # ponytail: one lock per group (coarse). Split per-circuit only if a busy
    # site's start throughput ever contends on it.
    """
    if not ENFORCE_CIRCUIT_ADMISSION or plug.group_id is None:
        return
    group = (
        await db.execute(
            select(ChargerGroup)
            .where(ChargerGroup.id == plug.group_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if group is None or group.max_current_a is None:
        return

    load = await circuit_load_a(db, group.id, exclude_plug_id=plug.id)
    projected = load + effective_plug_cap(plug)
    # Small epsilon so a load that foots EXACTLY to the cap (float sums) admits.
    if projected > group.max_current_a + 1e-6:
        available = max(0.0, group.max_current_a - load)
        # Structured detail so the driver UI can offer "Request capacity" on this
        # specific block (frontend api client surfaces detail.code). The plain
        # message still reads fine for any client that only shows detail.message.
        raise HTTPException(
            status_code=409,
            detail={
                "code": "circuit_full",
                "plug_id": plug.id,
                "message": (
                    f"This circuit is at capacity: {load:g} A of {group.max_current_a:g} A "
                    f"in use, and this charger needs {effective_plug_cap(plug):g} A "
                    f"({available:g} A free). Wait for a session to finish, or tap "
                    "'Request capacity' to ask the operator."
                ),
            },
        )
