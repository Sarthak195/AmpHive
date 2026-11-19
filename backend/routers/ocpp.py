"""
OCPP 1.6J central system (feature C2): WebSocket endpoint + provisioning.

Mounted by main.py ONLY when OCPP_CSMS_ENABLED is on (off by default and in
production; no Caddy route exists). Importing this module imports
services/ocpp_adapter.py, which holds the message handlers.

WebSocket: /ocpp/{charge_point_id}, subprotocol "ocpp1.6", OCPP security
profile 1 (HTTP Basic, username = the charge point id, password checked
against a bcrypt hash). Before accept(): subprotocol check (400), per-IP
handshake limit (429), global connection cap (503), credentials (401). After
accept(): text frames only, a per-message size cap (close 1009), a
per-connection message rate limit (close 1008) and an idle timeout. A second
connection for the same charge point replaces the first.

Provisioning (plain JSON routes, same flag):
- POST /api/cpo/ocpp/charge-points  (cpo/admin with a tenant): create the
  charge point, its backing gateway and one plug per connector; the Basic
  password is generated here and returned exactly once.
- POST /api/ocpp/id-tags, DELETE /api/ocpp/id-tags/{id} (any signed-in
  user): bind or unbind an idTag on the caller's own account.
"""
import asyncio
import base64
import binascii
import logging
import os
import secrets
from typing import Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Response, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import and_, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.db import get_db
from backend.database.models import (
    Gateway,
    GatewayStatus,
    OcppChargePoint,
    OcppIdTag,
    Plug,
    PlugStatus,
    User,
)
from backend.routers.cpo._common import _require_tenant_id
from backend.services import ocpp_adapter as adapter
from backend.services.audit import try_record_audit
from backend.services.auth import get_current_user, hash_password
from backend.services.rate_limit import (
    SlidingWindowRateLimiter,
    _rule_from_env,
    client_ip_from_forwarded,
)
from backend.services.rbac import require_role

logger = logging.getLogger("amphive.ocpp")
router = APIRouter()

SUBPROTOCOL = "ocpp1.6"
MAX_CONNECTIONS = int(os.getenv("OCPP_MAX_CONNECTIONS") or "20")
MAX_MESSAGE_BYTES = int(os.getenv("OCPP_MAX_MESSAGE_BYTES") or "8192")
# Messages per connection: "<count>/<window seconds>".
_MESSAGE_RULE = _rule_from_env("OCPP_MESSAGE_RATE_LIMIT", "60/60")
# Handshakes (each one costs a bcrypt check) per client IP.
handshake_limiter = SlidingWindowRateLimiter(*_rule_from_env("OCPP_HANDSHAKE_RATE_LIMIT", "10/60"))
# A connection that sends nothing for this long is closed.
IDLE_TIMEOUT_SEC = float(os.getenv("OCPP_IDLE_TIMEOUT_SEC") or str(adapter.HEARTBEAT_INTERVAL_SEC * 3))

# charge_point_id -> its live WebSocket (one per charge point).
connections: Dict[str, WebSocket] = {}


def _basic_credentials(header: Optional[str]) -> Optional[tuple]:
    if not header or not header.lower().startswith("basic "):
        return None
    try:
        decoded = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    username, sep, password = decoded.partition(":")
    return (username, password) if sep else None


def _deny(status_code: int) -> Response:
    headers = {"WWW-Authenticate": 'Basic realm="OCPP"'} if status_code == 401 else None
    return Response(status_code=status_code, headers=headers)


@router.websocket("/ocpp/{charge_point_id}")
async def ocpp_websocket(websocket: WebSocket, charge_point_id: str):
    if SUBPROTOCOL not in websocket.scope.get("subprotocols", []):
        await websocket.send_denial_response(_deny(400))
        return
    ip = client_ip_from_forwarded(
        websocket.headers.get("x-forwarded-for", ""),
        websocket.client.host if websocket.client else None,
    )
    if handshake_limiter.check(ip) is not None:
        await websocket.send_denial_response(_deny(429))
        return
    if len(connections) >= MAX_CONNECTIONS and charge_point_id not in connections:
        await websocket.send_denial_response(_deny(503))
        return
    creds = _basic_credentials(websocket.headers.get("authorization"))
    cp = None
    if creds is not None and creds[0] == charge_point_id:
        cp = await adapter.authenticate(charge_point_id, creds[1])
    if cp is None:
        logger.warning("OCPP authentication failed", extra={"client_ip": ip})
        await websocket.send_denial_response(_deny(401))
        return

    await websocket.accept(subprotocol=SUBPROTOCOL)
    previous = connections.get(cp.charge_point_id)
    connections[cp.charge_point_id] = websocket
    if previous is not None:
        try:
            await previous.close(code=1000)
        except Exception:
            pass
    logger.info("OCPP charge point connected", extra={"charge_point_id": cp.charge_point_id})
    limiter = SlidingWindowRateLimiter(*_MESSAGE_RULE)
    try:
        while True:
            try:
                message = await asyncio.wait_for(websocket.receive(), timeout=IDLE_TIMEOUT_SEC)
            except asyncio.TimeoutError:
                await websocket.close(code=1000)
                break
            if message["type"] == "websocket.disconnect":
                break
            text = message.get("text")
            if text is None:
                await websocket.close(code=1003)  # binary frames are not OCPP-J
                break
            if len(text.encode("utf-8")) > MAX_MESSAGE_BYTES:
                await websocket.close(code=1009)
                break
            if limiter.check("c") is not None:
                await websocket.close(code=1008)
                break
            reply = await adapter.dispatch(cp, text)
            if reply is not None:
                await websocket.send_text(reply)
    except WebSocketDisconnect:
        pass
    finally:
        if connections.get(cp.charge_point_id) is websocket:
            del connections[cp.charge_point_id]
        logger.info("OCPP charge point disconnected", extra={"charge_point_id": cp.charge_point_id})


# ---------------------------------------------------------------------------
# Provisioning
# ---------------------------------------------------------------------------

class ChargePointCreateRequest(BaseModel):
    charge_point_id: str = Field(pattern=adapter.CHARGE_POINT_ID_RE.pattern)
    name: str = Field(min_length=1, max_length=90)
    connectors: int = Field(default=1, ge=1, le=adapter.MAX_CONNECTORS)


class IdTagCreateRequest(BaseModel):
    id_tag: str = Field(min_length=1, max_length=20)


@router.post("/api/cpo/ocpp/charge-points", status_code=201)
async def create_charge_point(
    req: ChargePointCreateRequest,
    user: User = Depends(require_role("cpo", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """Register a charge point for the caller's tenant. The response carries
    the generated Basic password once; only its bcrypt hash is stored."""
    tenant_id = _require_tenant_id(user)
    gateway_id = adapter.gateway_id_for(req.charge_point_id)
    taken = (await db.execute(
        select(OcppChargePoint.id).where(OcppChargePoint.charge_point_id == req.charge_point_id)
    )).scalar_one_or_none() or (await db.execute(
        select(Gateway.id).where(Gateway.id == gateway_id)
    )).scalar_one_or_none()
    if taken is not None:
        raise HTTPException(status_code=409, detail="That charge point id is already registered.")

    password = secrets.token_urlsafe(18)
    password_hash = await asyncio.to_thread(hash_password, password)
    # Never marked ONLINE (see OcppChargePoint): the app's own start paths
    # refuse these plugs; only the charger's StartTransaction starts them.
    db.add(Gateway(
        id=gateway_id, tenant_id=tenant_id, name=req.name, vpn_ip=gateway_id,
        status=GatewayStatus.OFFLINE,
    ))
    await db.flush()
    plugs = [
        Plug(
            gateway_id=gateway_id, name=f"{req.name} #{n}", local_ip="ocpp",
            plug_model="ocpp1.6j",
            unique_id=adapter.connector_unique_id(req.charge_point_id, n),
            status=PlugStatus.AVAILABLE,
        )
        for n in range(1, req.connectors + 1)
    ]
    db.add_all(plugs)
    db.add(OcppChargePoint(
        charge_point_id=req.charge_point_id, tenant_id=tenant_id,
        gateway_id=gateway_id, password_hash=password_hash,
    ))
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="That charge point id is already registered.")
    connectors = [{"connector_id": n, "plug_id": p.id} for n, p in enumerate(plugs, start=1)]
    await try_record_audit(
        db, tenant_id=tenant_id, actor_user_id=user.id,
        action="ocpp.charge_point.create", target_type="gateway", target_id=gateway_id,
        detail=f"connectors={req.connectors}",
    )
    return {
        "charge_point_id": req.charge_point_id,
        "gateway_id": gateway_id,
        "websocket_path": f"/ocpp/{req.charge_point_id}",
        "subprotocol": SUBPROTOCOL,
        "password": password,
        "connectors": connectors,
    }


@router.post("/api/ocpp/id-tags", status_code=201)
async def create_id_tag(
    req: IdTagCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Bind an idTag (an RFID card UID) to the caller's own account. Only its
    SHA-256 digest is stored."""
    row = OcppIdTag(user_id=user.id, tag_hash=adapter.hash_id_tag(req.id_tag))
    db.add(row)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="That tag is already registered.")
    return {"id": row.id}


@router.delete("/api/ocpp/id-tags/{tag_id}", status_code=204)
async def delete_id_tag(
    tag_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        delete(OcppIdTag).where(and_(OcppIdTag.id == tag_id, OcppIdTag.user_id == user.id))
    )
    await db.commit()
    if not getattr(result, "rowcount", 0):
        raise HTTPException(status_code=404, detail="Tag not found.")
    return Response(status_code=204)
