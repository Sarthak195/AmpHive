"""
OCPP 1.6J central-system adapter (feature C2): the message layer.

A flag-gated development and portfolio module. main.py imports this module
(through routers/ocpp.py) ONLY when OCPP_CSMS_ENABLED is on; it is off by
default and off in production, where nothing routes /ocpp to the backend.

Scope is the OCPP 1.6 Core profile, charge point to central system only:
BootNotification, Heartbeat, StatusNotification, Authorize,
StartTransaction, StopTransaction and MeterValues. The central system never
sends a CALL (no RemoteStart/RemoteStop, no ChangeConfiguration), so it
never actuates anything: an OCPP charger energizes itself.

Mapping onto the existing platform (docs/OCPP_CSMS.md):
- A charge point is an OcppChargePoint row backed by an ordinary Gateway
  ("ocpp-<id>"); connector N is the ordinary Plug on that gateway whose
  unique_id is "ocpp:<id>:<N>". The backing gateway is never marked ONLINE,
  so the app's own start paths (walk-up, queued, routine) refuse it at the
  gateway_is_live gate and only StartTransaction can start a session there.
- StartTransaction replays the walk-up route's caller-side gates
  (routers/sessions.py start_charging_session) in the same user -> plug lock
  order, then calls the SAME begin_active_session (circuit caps, opening
  hours, rate snapshot, wallet floor and hold) with actuate=False, so no MQTT
  command is published. The OCPP transactionId is the session id.
- MeterValues and the StopTransaction meterStop feed session-relative kWh
  into MQTTManager._persist_telemetry, the same persist path ESP32 telemetry
  takes (ownership check, monotonic energy, TOD reprice, wallet and limit
  auto-stops, telemetry_readings enqueue), so billing has one metering
  source.
- StopTransaction finalizes through finalize_charging_session with
  actuate=False (ledger debit, receipt, plug freed, no MQTT OFF).

Every lookup is scoped to the authenticated charge point: connectors to its
own backing gateway, transactions to its own ocpp_transactions rows. Nothing
here logs a password, an Authorization header, a clear idTag or a raw frame.
"""
import asyncio
import hashlib
import json
import logging
import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional, Tuple, Type

from fastapi import HTTPException
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import and_, func, select, update

from backend import state
from backend.database import db as db_module
from backend.database.models import (
    ChargerGroup,
    ChargingSession,
    Gateway,
    GroupMembership,
    OcppChargePoint,
    OcppIdTag,
    OcppTransaction,
    Plug,
    PlugStatus,
    QueuedCharge,
    QueuedChargeStatus,
    Reservation,
    ReservationStatus,
    SessionStatus,
    User,
)
from backend.services.auth import _DUMMY_PASSWORD_HASH, verify_password

logger = logging.getLogger("amphive.ocpp")

# Charge point identity (the last path segment of the WebSocket URL). Kept to
# a safe, short charset so "ocpp-" + id fits gateways.id String(50) and the
# id can never smuggle a path, a topic level or a log-injection byte.
CHARGE_POINT_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
GATEWAY_ID_PREFIX = "ocpp-"
MAX_CONNECTORS = 4
# Interval (seconds) the charge point is told to heartbeat at. The idle
# timeout in routers/ocpp.py is a multiple of it.
HEARTBEAT_INTERVAL_SEC = int(os.getenv("OCPP_HEARTBEAT_INTERVAL_SEC") or "60")

CALL, CALLRESULT, CALLERROR = 2, 3, 4

# Upper bound for any energy register (Wh): 1 TWh. Anything above is garbage.
_MAX_REGISTER_WH = 10**12


def gateway_id_for(charge_point_id: str) -> str:
    return f"{GATEWAY_ID_PREFIX}{charge_point_id}"


def connector_unique_id(charge_point_id: str, connector_id: int) -> str:
    return f"ocpp:{charge_point_id}:{connector_id}"


def hash_id_tag(id_tag: str) -> str:
    """SHA-256 hex of an idTag. OCPP idTags are case-insensitive (CiString),
    so the tag is upper-cased first."""
    return hashlib.sha256(id_tag.strip().upper().encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# OCPP-J framing
# ---------------------------------------------------------------------------

class OcppError(Exception):
    """A CALLERROR to send back (code from the OCPP-J error code list)."""

    def __init__(self, code: str, description: str):
        super().__init__(code)
        self.code = code
        self.description = description


def parse_frame(raw: str) -> Optional[Tuple[str, str, Dict[str, Any]]]:
    """Parse one OCPP-J frame. Returns (unique_id, action, payload) for a
    CALL, None for a CALLRESULT/CALLERROR (this central system never sends a
    CALL, so there is nothing to correlate them with), and raises OcppError
    for a malformed frame (frame_unique_id recovers the id for the reply)."""
    try:
        msg = json.loads(raw)
    except ValueError:
        raise OcppError("FormationViolation", "Frame is not valid JSON.")
    if not isinstance(msg, list) or len(msg) < 3 or not isinstance(msg[0], int):
        raise OcppError("FormationViolation", "Frame is not an OCPP-J message array.")
    if msg[0] in (CALLRESULT, CALLERROR):
        return None
    if msg[0] != CALL or len(msg) != 4:
        raise OcppError("FormationViolation", "Unsupported message type.")
    unique_id, action, payload = msg[1], msg[2], msg[3]
    if not isinstance(unique_id, str) or not 0 < len(unique_id) <= 36:
        raise OcppError("FormationViolation", "Invalid message id.")
    if not isinstance(action, str) or not isinstance(payload, dict):
        raise OcppError("FormationViolation", "Invalid action or payload.")
    return unique_id, action, payload


def frame_unique_id(raw: str) -> str:
    """Best-effort message id for a CALLERROR on a frame parse_frame refused."""
    try:
        msg = json.loads(raw)
        if isinstance(msg, list) and len(msg) > 1 and isinstance(msg[1], str):
            return msg[1][:36]
    except ValueError:
        pass
    return ""


def call_result(unique_id: str, payload: Dict[str, Any]) -> str:
    return json.dumps([CALLRESULT, unique_id, payload])


def call_error(unique_id: str, code: str, description: str) -> str:
    return json.dumps([CALLERROR, unique_id, code, description, {}])


# ---------------------------------------------------------------------------
# Core profile payloads (pydantic). Only the fields this adapter reads;
# unknown fields are ignored. Lengths follow the OCPP 1.6 CiString limits and
# every list is capped so one frame cannot fan out into unbounded work.
# ---------------------------------------------------------------------------

class BootNotificationReq(BaseModel):
    chargePointVendor: str = Field(min_length=1, max_length=20)
    chargePointModel: str = Field(min_length=1, max_length=20)
    firmwareVersion: Optional[str] = Field(default=None, max_length=50)


class HeartbeatReq(BaseModel):
    pass


class StatusNotificationReq(BaseModel):
    connectorId: int = Field(ge=0, le=MAX_CONNECTORS)
    errorCode: str = Field(max_length=50)
    status: Literal[
        "Available", "Preparing", "Charging", "SuspendedEVSE", "SuspendedEV",
        "Finishing", "Reserved", "Unavailable", "Faulted",
    ]


class AuthorizeReq(BaseModel):
    idTag: str = Field(min_length=1, max_length=20)


class StartTransactionReq(BaseModel):
    connectorId: int = Field(ge=1, le=MAX_CONNECTORS)
    idTag: str = Field(min_length=1, max_length=20)
    meterStart: int = Field(ge=0, le=_MAX_REGISTER_WH)
    timestamp: str = Field(max_length=40)
    reservationId: Optional[int] = None


class SampledValue(BaseModel):
    value: str = Field(max_length=32)
    measurand: str = Field(default="Energy.Active.Import.Register", max_length=50)
    unit: Optional[str] = Field(default=None, max_length=10)
    phase: Optional[str] = Field(default=None, max_length=10)


class MeterValue(BaseModel):
    timestamp: str = Field(max_length=40)
    sampledValue: List[SampledValue] = Field(min_length=1, max_length=32)


class MeterValuesReq(BaseModel):
    connectorId: int = Field(ge=0, le=MAX_CONNECTORS)
    transactionId: Optional[int] = None
    meterValue: List[MeterValue] = Field(min_length=1, max_length=16)


class StopTransactionReq(BaseModel):
    transactionId: int
    meterStop: int = Field(ge=0, le=_MAX_REGISTER_WH)
    timestamp: str = Field(max_length=40)
    idTag: Optional[str] = Field(default=None, min_length=1, max_length=20)
    reason: Optional[Literal[
        "EmergencyStop", "EVDisconnected", "HardReset", "Local", "Other",
        "PowerLoss", "Reboot", "Remote", "SoftReset", "UnlockCommand",
        "DeAuthorized",
    ]] = None
    transactionData: Optional[List[MeterValue]] = Field(default=None, max_length=16)


@dataclass(frozen=True)
class ChargePointCtx:
    """The authenticated charge point a connection speaks for."""
    id: int
    charge_point_id: str
    tenant_id: int
    gateway_id: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _session_factory():
    # Looked up per call so tests can point it at their own database.
    return db_module.async_session_factory()


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

async def authenticate(charge_point_id: str, password: str) -> Optional[ChargePointCtx]:
    """Check HTTP Basic credentials for a charge point. Always pays one
    bcrypt verification (against a dummy hash for an unknown id) so the
    response time does not reveal which ids exist."""
    row = None
    if CHARGE_POINT_ID_RE.match(charge_point_id):
        async with _session_factory() as db:
            row = (await db.execute(
                select(OcppChargePoint).where(
                    OcppChargePoint.charge_point_id == charge_point_id
                )
            )).scalar_one_or_none()
    stored = row.password_hash if row is not None else _DUMMY_PASSWORD_HASH
    ok = await asyncio.to_thread(verify_password, password, stored)
    if row is None or not ok:
        return None
    return ChargePointCtx(row.id, row.charge_point_id, row.tenant_id, row.gateway_id)


# ---------------------------------------------------------------------------
# Scoped lookups
# ---------------------------------------------------------------------------

async def _connector_plug_id(db, cp: ChargePointCtx, connector_id: int) -> Optional[int]:
    """The plug id behind connector N of THIS charge point (never another's)."""
    return (await db.execute(
        select(Plug.id).where(and_(
            Plug.gateway_id == cp.gateway_id,
            Plug.unique_id == connector_unique_id(cp.charge_point_id, connector_id),
        ))
    )).scalar_one_or_none()


async def _tag_user_id(db, tag_hash: str) -> Optional[int]:
    return (await db.execute(
        select(OcppIdTag.user_id).where(OcppIdTag.tag_hash == tag_hash)
    )).scalar_one_or_none()


async def _own_transaction(db, cp: ChargePointCtx, transaction_id: int):
    """(OcppTransaction, session status, plug id) for a transaction THIS
    charge point started on its own backing gateway, else None."""
    return (await db.execute(
        select(OcppTransaction, ChargingSession.status, ChargingSession.plug_id)
        .join(ChargingSession, ChargingSession.id == OcppTransaction.session_id)
        .join(Plug, Plug.id == ChargingSession.plug_id)
        .where(and_(
            OcppTransaction.session_id == transaction_id,
            OcppTransaction.charge_point_id == cp.id,
            Plug.gateway_id == cp.gateway_id,
        ))
    )).first()


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def handle_boot(cp: ChargePointCtx, req: BootNotificationReq) -> Dict[str, Any]:
    async with _session_factory() as db:
        await db.execute(
            update(OcppChargePoint)
            .where(OcppChargePoint.id == cp.id)
            .values(
                vendor=req.chargePointVendor,
                model=req.chargePointModel,
                firmware_version=req.firmwareVersion,
                last_boot_at=datetime.now(timezone.utc),
            )
        )
        await db.commit()
    logger.info("OCPP boot", extra={"charge_point_id": cp.charge_point_id})
    return {"status": "Accepted", "currentTime": _now_iso(), "interval": HEARTBEAT_INTERVAL_SEC}


async def handle_heartbeat(cp: ChargePointCtx, req: HeartbeatReq) -> Dict[str, Any]:
    # No DB write: connection liveness is tracked in memory by the router.
    return {"currentTime": _now_iso()}


async def handle_status(cp: ChargePointCtx, req: StatusNotificationReq) -> Dict[str, Any]:
    """A Faulted connector is taken out of service (AVAILABLE -> OFFLINE),
    the safe direction; the operator puts it back in the CPO portal. Every
    other status is informational and writes nothing."""
    if req.connectorId == 0:
        return {}
    async with _session_factory() as db:
        plug_id = await _connector_plug_id(db, cp, req.connectorId)
        if plug_id is None:
            raise OcppError("PropertyConstraintViolation", "Unknown connector.")
        if req.status == "Faulted":
            result = await db.execute(
                update(Plug)
                .where(and_(Plug.id == plug_id, Plug.status == PlugStatus.AVAILABLE))
                .values(status=PlugStatus.OFFLINE)
            )
            await db.commit()
            if getattr(result, "rowcount", 0):
                from backend.services.socketio_manager import emit_plug_status
                await emit_plug_status(plug_id, PlugStatus.OFFLINE.value)
                logger.warning(
                    "OCPP connector faulted, plug taken out of service",
                    extra={"charge_point_id": cp.charge_point_id, "plug_id": plug_id},
                )
    return {}


def _id_tag_info(status: str) -> Dict[str, Any]:
    return {"idTagInfo": {"status": status}}


async def handle_authorize(cp: ChargePointCtx, req: AuthorizeReq) -> Dict[str, Any]:
    """Accepted only for a tag bound to a live account whose available
    balance clears the same start floor the walk-up start enforces."""
    from backend.services.session_start import MIN_START_BALANCE_COINS
    from backend.services.wallet import available_balance

    async with _session_factory() as db:
        user_id = await _tag_user_id(db, hash_id_tag(req.idTag))
        if user_id is None:
            return _id_tag_info("Invalid")
        user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
        if user is None or user.is_disabled or user.deleted_at is not None:
            return _id_tag_info("Blocked")
        if await available_balance(db, user_id) < MIN_START_BALANCE_COINS:
            return _id_tag_info("Blocked")
    return _id_tag_info("Accepted")


async def _admit_and_begin(db, cp: ChargePointCtx, user_id: int, plug_id: int) -> ChargingSession:
    """Replay the walk-up route's caller-side gates (routers/sessions.py
    start_charging_session), same order and lock order (user -> plug), then
    run the shared begin_active_session with actuate=False. Raises the same
    HTTPException the walk-up route would.

    Deliberately skipped: the gateway liveness gate (the charger is talking
    to us right now; its backing gateway is never marked ONLINE, which is what
    keeps the app's own start paths off it) and the plug power gate (the
    charger meters itself)."""
    from backend.services.reservations import expire_lapsed_reservations
    from backend.services.session_reaper import MAX_ACTIVE_SESSIONS_PER_USER
    from backend.services.session_start import begin_active_session

    user = (await db.execute(
        select(User).where(User.id == user_id).with_for_update()
    )).scalar_one_or_none()
    if user is None or user.is_disabled or user.deleted_at is not None:
        raise HTTPException(status_code=403, detail="Account unavailable.")
    active_count = (await db.execute(
        select(func.count()).select_from(ChargingSession).where(and_(
            ChargingSession.user_id == user.id,
            ChargingSession.status == SessionStatus.ACTIVE,
        ))
    )).scalar_one()
    if active_count >= MAX_ACTIVE_SESSIONS_PER_USER:
        raise HTTPException(status_code=409, detail="Concurrent session cap reached.")

    plug = (await db.execute(
        select(Plug).where(Plug.id == plug_id).with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one()

    if plug.group_id:
        group = (await db.execute(
            select(ChargerGroup).where(ChargerGroup.id == plug.group_id)
        )).scalar_one_or_none()
        if group and not group.is_public:
            member = (await db.execute(
                select(GroupMembership).where(and_(
                    GroupMembership.user_id == user.id,
                    GroupMembership.group_id == group.id,
                ))
            )).scalar_one_or_none()
            if member is None:
                raise HTTPException(status_code=403, detail="Not a member of this plug's group.")

    if plug.status != PlugStatus.AVAILABLE:
        raise HTTPException(status_code=409, detail="Plug not available.")

    now = datetime.now(timezone.utc)
    await expire_lapsed_reservations(db, plug_id=plug.id, now=now)
    covering = (await db.execute(
        select(Reservation).where(and_(
            Reservation.plug_id == plug.id,
            Reservation.status == ReservationStatus.BOOKED,
            Reservation.start_at <= now,
            Reservation.end_at > now,
        )).order_by(Reservation.start_at).limit(1)
    )).scalar_one_or_none()
    if covering is not None and covering.user_id != user.id:
        raise HTTPException(status_code=409, detail="Plug is reserved.")

    scheduled = (await db.execute(
        select(QueuedCharge.id, QueuedCharge.user_id).where(and_(
            QueuedCharge.plug_id == plug.id,
            QueuedCharge.status == QueuedChargeStatus.WAITING,
            QueuedCharge.not_before.is_not(None),
        ))
    )).all()
    if any(row.user_id != user.id for row in scheduled):
        raise HTTPException(status_code=409, detail="Another driver is scheduled on this plug.")
    own_scheduled = [row.id for row in scheduled]

    gateway = (await db.execute(
        select(Gateway).where(Gateway.id == plug.gateway_id)
    )).scalar_one()
    session = await begin_active_session(
        db, user, plug, gateway,
        max_kwh=None, max_duration_seconds=None,
        covering_reservation=covering,
        actuate=False,
    )
    if own_scheduled:
        await db.execute(
            update(QueuedCharge)
            .where(and_(
                QueuedCharge.id.in_(own_scheduled),
                QueuedCharge.status == QueuedChargeStatus.WAITING,
            ))
            .values(status=QueuedChargeStatus.CANCELLED)
        )
        await db.commit()
    return session


async def handle_start(cp: ChargePointCtx, req: StartTransactionReq) -> Dict[str, Any]:
    """Admission identical to a walk-up start; Invalid for an unknown tag or
    connector, Blocked when any gate refuses. A retried StartTransaction for a
    transaction already running (same connector, tag and meterStart) gets
    the same transactionId back instead of a refusal."""
    from backend.services.session_lifecycle import finalize_charging_session

    tag_hash = hash_id_tag(req.idTag)
    async with _session_factory() as db:
        plug_id = await _connector_plug_id(db, cp, req.connectorId)
        if plug_id is None:
            return {"transactionId": 0, **_id_tag_info("Invalid")}
        existing = (await db.execute(
            select(OcppTransaction.session_id)
            .join(ChargingSession, ChargingSession.id == OcppTransaction.session_id)
            .where(and_(
                OcppTransaction.charge_point_id == cp.id,
                OcppTransaction.connector_id == req.connectorId,
                OcppTransaction.id_tag_hash == tag_hash,
                OcppTransaction.meter_start_wh == req.meterStart,
                ChargingSession.plug_id == plug_id,
                ChargingSession.status == SessionStatus.ACTIVE,
            ))
        )).scalar_one_or_none()
        if existing is not None:
            return {"transactionId": existing, **_id_tag_info("Accepted")}
        user_id = await _tag_user_id(db, tag_hash)
        if user_id is None:
            return {"transactionId": 0, **_id_tag_info("Invalid")}
        try:
            session = await _admit_and_begin(db, cp, user_id, plug_id)
        except HTTPException as exc:
            await db.rollback()
            logger.info(
                "OCPP start refused",
                extra={
                    "charge_point_id": cp.charge_point_id, "plug_id": plug_id,
                    "status_code": exc.status_code,
                },
            )
            return {"transactionId": 0, **_id_tag_info("Blocked")}
        session_id = session.id
        try:
            db.add(OcppTransaction(
                session_id=session_id, charge_point_id=cp.id,
                connector_id=req.connectorId, id_tag_hash=tag_hash,
                meter_start_wh=req.meterStart,
            ))
            await db.commit()
        except Exception:
            # The session is already committed ACTIVE: without its OCPP row
            # nothing could meter or stop it, so close it out at zero energy.
            await db.rollback()
            logger.exception(
                "OCPP transaction record failed; closing the session",
                extra={"charge_point_id": cp.charge_point_id, "session_id": session_id},
            )
            async with _session_factory() as db2:
                await finalize_charging_session(
                    db2, session_id, reason="ocpp: start record failed", actuate=False,
                )
            return {"transactionId": 0, **_id_tag_info("Invalid")}
    logger.info(
        "OCPP transaction started",
        extra={"charge_point_id": cp.charge_point_id, "session_id": session_id, "plug_id": plug_id},
    )
    return {"transactionId": session_id, **_id_tag_info("Accepted")}


def _to_float(raw: str) -> Optional[float]:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def latest_reading(meter_values: List[MeterValue]) -> Dict[str, Optional[float]]:
    """The newest whole-meter (no phase) energy register (Wh), power (W),
    current (A) and voltage (V) across a MeterValues batch. Later entries
    win; kWh / kW units are converted."""
    out: Dict[str, Optional[float]] = {
        "energy_wh": None, "power_w": None, "current_a": None, "voltage_v": None,
    }
    for mv in meter_values:
        for sv in mv.sampledValue:
            if sv.phase is not None:
                continue
            value = _to_float(sv.value)
            if value is None:
                continue
            unit = (sv.unit or "").lower()
            if sv.measurand == "Energy.Active.Import.Register":
                out["energy_wh"] = value * 1000 if unit == "kwh" else value
            elif sv.measurand == "Power.Active.Import":
                out["power_w"] = value * 1000 if unit == "kw" else value
            elif sv.measurand == "Current.Import":
                out["current_a"] = value
            elif sv.measurand == "Voltage":
                out["voltage_v"] = value
    return out


async def _feed_meter(cp: ChargePointCtx, plug_id: int, session_id: int,
                      meter_start_wh: int, reading: Dict[str, Optional[float]]) -> None:
    """Hand one reading to the existing telemetry persist path as a live
    frame for this session (session-relative kWh)."""
    from backend.services.mqtt.telemetry import MAX_PLAUSIBLE_KWH

    energy_wh = reading["energy_wh"]
    if energy_wh is None:
        return
    kwh = max(0.0, (energy_wh - meter_start_wh) / 1000.0)
    if kwh > MAX_PLAUSIBLE_KWH:
        logger.warning(
            "OCPP meter value outside plausible bounds, dropped",
            extra={"charge_point_id": cp.charge_point_id, "session_id": session_id},
        )
        return
    watts = reading["power_w"]
    manager = state.mqtt_manager
    if manager is None:
        logger.warning("OCPP meter value dropped: telemetry pipeline not running")
        return
    sample = {
        "plug_id": plug_id,
        "recorded_at": datetime.now(timezone.utc),
        "power_w": watts if watts is not None else 0.0,
        "energy_kwh": kwh,
        "voltage_v": reading["voltage_v"] if reading["voltage_v"] is not None else 230.0,
        "current_a": reading["current_a"] if reading["current_a"] is not None else 0.0,
        "status": "occupied",
    }
    # relay_on=True: only ever fed for a transaction verified ACTIVE on this
    # charge point, so the REC-02 orphan-OFF branch is not reached.
    await manager._persist_telemetry(
        cp.gateway_id, plug_id, sample["power_w"], kwh, session_id, sample,
        relay_on=True, watts_reported=watts is not None,
    )


async def handle_meter_values(cp: ChargePointCtx, req: MeterValuesReq) -> Dict[str, Any]:
    """Only readings tied to one of this charge point's ACTIVE transactions
    are persisted; idle readings write nothing."""
    if req.transactionId is None:
        return {}
    async with _session_factory() as db:
        row = await _own_transaction(db, cp, req.transactionId)
    if row is None:
        return {}
    tx, status, plug_id = row
    if status != SessionStatus.ACTIVE or (req.connectorId and req.connectorId != tx.connector_id):
        return {}
    await _feed_meter(cp, plug_id, tx.session_id, tx.meter_start_wh, latest_reading(req.meterValue))
    return {}


async def handle_stop(cp: ChargePointCtx, req: StopTransactionReq) -> Dict[str, Any]:
    """Feed the final register, then bill through finalize_charging_session
    (actuate=False). Always answered, also for a transaction the backend
    already closed (balance or limit auto-stop, reaper)."""
    from backend.services.session_lifecycle import finalize_charging_session

    reply: Dict[str, Any] = _id_tag_info("Accepted") if req.idTag else {}
    async with _session_factory() as db:
        row = await _own_transaction(db, cp, req.transactionId)
    if row is None:
        logger.info(
            "OCPP stop for an unknown transaction",
            extra={"charge_point_id": cp.charge_point_id},
        )
        return _id_tag_info("Invalid") if req.idTag else {}
    tx, status, plug_id = row
    if status != SessionStatus.ACTIVE:
        return reply
    final = latest_reading(req.transactionData or [])
    final["energy_wh"] = float(req.meterStop)
    await _feed_meter(cp, plug_id, tx.session_id, tx.meter_start_wh, final)
    async with _session_factory() as db:
        await finalize_charging_session(
            db, tx.session_id, reason=f"ocpp: {req.reason or 'Local'}", actuate=False,
        )
    logger.info(
        "OCPP transaction stopped",
        extra={"charge_point_id": cp.charge_point_id, "session_id": tx.session_id},
    )
    return reply


Handler = Callable[[ChargePointCtx, Any], Awaitable[Dict[str, Any]]]
HANDLERS: Dict[str, Tuple[Type[BaseModel], Handler]] = {
    "BootNotification": (BootNotificationReq, handle_boot),
    "Heartbeat": (HeartbeatReq, handle_heartbeat),
    "StatusNotification": (StatusNotificationReq, handle_status),
    "Authorize": (AuthorizeReq, handle_authorize),
    "StartTransaction": (StartTransactionReq, handle_start),
    "MeterValues": (MeterValuesReq, handle_meter_values),
    "StopTransaction": (StopTransactionReq, handle_stop),
}


async def dispatch(cp: ChargePointCtx, raw: str) -> Optional[str]:
    """Turn one inbound text frame into the reply frame (None = no reply)."""
    try:
        parsed = parse_frame(raw)
    except OcppError as err:
        return call_error(frame_unique_id(raw), err.code, err.description)
    if parsed is None:
        return None
    unique_id, action, payload = parsed
    entry = HANDLERS.get(action)
    if entry is None:
        return call_error(unique_id, "NotImplemented", "Action not supported.")
    model, handler = entry
    try:
        req = model.model_validate(payload)
    except ValidationError:
        # Never echo the validation error: it embeds the input (an idTag).
        return call_error(unique_id, "FormationViolation", "Payload failed validation.")
    try:
        return call_result(unique_id, await handler(cp, req))
    except OcppError as err:
        return call_error(unique_id, err.code, err.description)
    except Exception:
        logger.exception(
            "OCPP handler failed",
            extra={"charge_point_id": cp.charge_point_id, "action": action},
        )
        return call_error(unique_id, "InternalError", "Internal error.")
