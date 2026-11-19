"""Read-only OCPP 1.6J virtual charge-point bridge for AmpHive plugs.

Each plug the meter token can see becomes one OCPP 1.6J charge point
("<prefix><plug_id>", connector 1) that dials OUT to a CSMS. Data comes only
from GET /api/me/meter, polled every few seconds; nothing the CSMS sends can
change AmpHive (every remote command is refused). See docs/OCPP_BRIDGE.md.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

import httpx
from websockets.asyncio.client import ClientConnection, connect
from websockets.typing import Subprotocol

log = logging.getLogger("ocpp_bridge")

CALL, CALLRESULT, CALLERROR = 2, 3, 4
CALL_TIMEOUT = 30.0
CHARGING_MIN_W = 10.0
SUBPROTOCOL = "ocpp1.6"
VENDOR, MODEL = "AmpHive", "Tapo P110 bridge"
DEFAULT_HEARTBEAT_S = 300
DEFAULT_BOOT_RETRY_S = 30
BACKOFF_BASE_S = 1.0
BACKOFF_MAX_S = 60.0
STOP_GIVE_UP = 3  # consecutive CallErrors on one StopTransaction before it is dropped locally
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

# CSMS-initiated calls the bridge answers with a refusal. It is a read-only
# mirror: none of these may ever reach AmpHive.
REJECTED_CALLS = frozenset({
    "RemoteStartTransaction", "RemoteStopTransaction", "ChangeAvailability", "Reset",
    "ChangeConfiguration", "ClearCache", "DataTransfer", "TriggerMessage",
})


class AuthError(Exception):
    """The meter API rejected the token (401). Fatal: the bridge stops."""


class OcppCallError(Exception):
    def __init__(self, code: str, description: str = "") -> None:
        super().__init__(f"{code}: {description}")
        self.code = code
        self.description = description


def utc_iso(value: str | None = None) -> str:
    """ISO-8601 UTC with a Z suffix. Accepts Z, +00:00 or naive (taken as UTC)."""
    if value:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    else:
        dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def ocpp_status(plug: Mapping[str, Any]) -> str:
    """Map one meter-endpoint plug to an OCPP 1.6 ChargePointStatus.

    Precedence: offline/unreachable, then maintenance, then an active session,
    then available/reserved; anything else is Unavailable.
    """
    status = str(plug.get("status") or "").lower()
    if status == "offline" or not plug.get("online"):
        return "Unavailable"
    if status == "maintenance":
        return "Unavailable"
    if plug.get("session"):
        power = plug.get("power_w")
        return "Charging" if power is not None and power >= CHARGING_MIN_W else "SuspendedEV"
    if status == "available":
        return "Available"
    if status == "reserved":
        return "Reserved"
    return "Unavailable"


def _is_local(url: str) -> bool:
    return (urlsplit(url).hostname or "") in LOCAL_HOSTS


@dataclass
class Config:
    api_url: str
    token: str
    csms_url: str
    prefix: str = "AMPHIVE-"
    poll_seconds: float = 10.0
    id_tag: str = "AMPHIVE"
    basic_user: str | None = None
    basic_password: str | None = None
    state_file: Path = Path.home() / ".amphive-ocpp-bridge.json"

    def __repr__(self) -> str:  # never print the token or the password
        return f"Config(api_url={self.api_url!r}, csms_url={self.csms_url!r}, prefix={self.prefix!r})"

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Config:
        token = env.get("AMPHIVE_METER_TOKEN", "").strip()
        csms = env.get("OCPP_CSMS_URL", "").strip()
        if not token:
            raise ValueError("AMPHIVE_METER_TOKEN is not set")
        if not csms:
            raise ValueError("OCPP_CSMS_URL is not set")
        cfg = cls(
            api_url=env.get("AMPHIVE_API_URL", "https://amphive.app").strip(),
            token=token,
            csms_url=csms,
            prefix=env.get("OCPP_CP_PREFIX", "AMPHIVE-"),
            poll_seconds=float(env.get("OCPP_POLL_SECONDS", "10")),
            id_tag=env.get("OCPP_ID_TAG", "AMPHIVE"),
            basic_user=env.get("OCPP_BASIC_USER") or None,
            basic_password=env.get("OCPP_BASIC_PASSWORD") or None,
        )
        if env.get("OCPP_STATE_FILE"):
            cfg.state_file = Path(env["OCPP_STATE_FILE"])
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if not self.api_url.startswith("https://") and not _is_local(self.api_url):
            raise ValueError("AMPHIVE_API_URL must be https:// (plain http only for localhost)")
        if not self.csms_url.startswith(("ws://", "wss://")):
            raise ValueError("OCPP_CSMS_URL must start with ws:// or wss://")
        if self.poll_seconds <= 0:
            raise ValueError("OCPP_POLL_SECONDS must be positive")
        if self.basic_password and self.csms_url.startswith("ws://") and not _is_local(self.csms_url):
            log.warning("Basic auth over plain ws:// sends the password in clear; prefer wss://")


class StateStore:
    """Per charge point: virtual register base (Wh) and the open transaction.

    Written atomically (temp file + replace) so a crash leaves the old or the
    new state, never a torn file.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, dict[str, Any]] = {}
        if path.exists():
            self.data = json.loads(path.read_text(encoding="utf-8"))

    def get(self, cp_id: str, plug_id: int) -> dict[str, Any]:
        return self.data.setdefault(cp_id, {
            "plug_id": plug_id, "base_wh": 0.0,
            "session_id": None, "transaction_id": None, "last_energy_wh": 0.0,
        })

    def open_transactions(self) -> list[tuple[str, int]]:
        return [(cp, int(s["plug_id"])) for cp, s in self.data.items()
                if s.get("transaction_id") is not None]

    def save(self) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)


class MeterClient:
    def __init__(self, http: httpx.AsyncClient, api_url: str, token: str) -> None:
        self._http = http
        self._url = api_url.rstrip("/") + "/api/me/meter"
        self._headers = {"Authorization": f"Bearer {token}"}

    async def fetch(self) -> dict[int, dict[str, Any]]:
        resp = await self._http.get(self._url, headers=self._headers, timeout=15.0)
        if resp.status_code == 401:
            raise AuthError("meter API returned 401 (missing, invalid or revoked token)")
        resp.raise_for_status()
        return {int(p["plug_id"]): p for p in resp.json()["plugs"]}


class ChargePoint:
    """One virtual OCPP 1.6J charge point (connector 1) for one AmpHive plug."""

    def __init__(self, cp_id: str, plug_id: int, cfg: Config, state: StateStore) -> None:
        self.cp_id = cp_id
        self.plug_id = plug_id
        self.cfg = cfg
        self.state = state
        self.snapshot: dict[str, Any] | None = None
        self.changed = asyncio.Event()
        self.heartbeat_interval = DEFAULT_HEARTBEAT_S
        self._ws: ClientConnection | None = None
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._call_lock = asyncio.Lock()
        self._last_status: str | None = None
        self._booted = False  # set once a connection's BootNotification was accepted
        self._stop_errors = 0  # consecutive CallErrors on the open transaction's StopTransaction
        self._sleep = asyncio.sleep  # reconnect delay; tests replace it

    def update(self, snapshot: dict[str, Any]) -> None:
        self.snapshot = snapshot
        self.changed.set()

    async def run(self) -> None:
        delay = BACKOFF_BASE_S
        while True:
            self._booted = False
            try:
                await self._connection()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # reconnect on any transport or protocol failure
                log.warning("%s: connection lost (%s: %s)", self.cp_id, type(exc).__name__, exc)
            if self._booted:  # the link was healthy once: start the backoff over
                delay = BACKOFF_BASE_S
            await self._sleep(delay)
            delay = min(delay * 2, BACKOFF_MAX_S)

    def _headers(self) -> dict[str, str]:
        if not self.cfg.basic_password:
            return {}
        user = self.cfg.basic_user or self.cp_id
        raw = f"{user}:{self.cfg.basic_password}".encode()
        return {"Authorization": "Basic " + base64.b64encode(raw).decode()}

    async def _connection(self) -> None:
        url = f"{self.cfg.csms_url.rstrip('/')}/{self.cp_id}"
        async with connect(url, subprotocols=[Subprotocol(SUBPROTOCOL)],
                           additional_headers=self._headers(), proxy=None) as ws:
            if ws.subprotocol != SUBPROTOCOL:
                raise RuntimeError(f"CSMS did not accept subprotocol {SUBPROTOCOL}")
            self._ws = ws
            reader = asyncio.create_task(self._reader(ws))
            heartbeat: asyncio.Task[None] | None = None
            try:
                await self._boot()
                self._booted = True
                log.info("%s: boot accepted, heartbeat %ss", self.cp_id, self.heartbeat_interval)
                heartbeat = asyncio.create_task(self._heartbeat())
                self._last_status = None  # re-send status after every (re)connect
                while True:
                    self.changed.clear()
                    if self.snapshot is not None:
                        try:
                            await self._sync()
                        except OcppCallError as exc:
                            # The CSMS refused one message: retry on the next poll
                            # rather than reconnecting in a tight loop.
                            log.warning("%s: CSMS returned CallError %s", self.cp_id, exc)
                    waiter = asyncio.create_task(self.changed.wait())
                    done, _ = await asyncio.wait({waiter, reader, heartbeat},
                                                 return_when=asyncio.FIRST_COMPLETED)
                    if waiter not in done:
                        waiter.cancel()
                        for finished in done:
                            finished.result()  # surfaces the failure
                        raise ConnectionError("connection closed")
            finally:
                for task in (reader, heartbeat):
                    if task is not None:
                        task.cancel()
                self._fail_pending()
                self._ws = None

    async def call(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Send one CALL and wait for its result. One outstanding CALL at a time."""
        async with self._call_lock:
            ws = self._ws
            if ws is None:
                raise ConnectionError("not connected")
            msg_id = str(uuid.uuid4())
            fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
            self._pending[msg_id] = fut
            try:
                await ws.send(json.dumps([CALL, msg_id, action, payload]))
                # asyncio.timeout, not wait_for: on Python 3.11 wait_for can
                # swallow a task cancel that lands as the reply arrives
                # (cpython gh-86296), and the charge point then never stops.
                async with asyncio.timeout(CALL_TIMEOUT):
                    result: dict[str, Any] = await fut
                return result
            except asyncio.TimeoutError:
                await ws.close()  # an unanswered call means a broken link: reconnect
                raise
            finally:
                self._pending.pop(msg_id, None)

    def _fail_pending(self) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("connection closed"))

    async def _reader(self, ws: ClientConnection) -> None:
        try:
            async for raw in ws:
                await self._dispatch(ws, raw)
        finally:
            self._fail_pending()  # a drop mid-call fails it now, not after 30 s

    async def _dispatch(self, ws: ClientConnection, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
            kind, msg_id = msg[0], msg[1]
            if kind == CALL:
                action, payload = msg[2], (msg[3] if len(msg) > 3 else {})
        except (ValueError, IndexError, KeyError, TypeError):
            log.warning("%s: dropped malformed frame", self.cp_id)
            return
        if kind == CALL:
            await ws.send(json.dumps(self._answer(msg_id, action, payload)))
        elif kind in (CALLRESULT, CALLERROR):
            fut = self._pending.get(msg_id)
            if fut is None or fut.done():
                return
            if kind == CALLRESULT:
                fut.set_result(msg[2] if len(msg) > 2 else {})
            else:
                code = str(msg[2]) if len(msg) > 2 else "GenericError"
                fut.set_exception(OcppCallError(code, str(msg[3]) if len(msg) > 3 else ""))

    def _answer(self, msg_id: str, action: str, payload: Any) -> list[Any]:
        """Reply to a CSMS-initiated CALL. Read-only: nothing here touches AmpHive."""
        log.info("%s: refused CSMS call %s", self.cp_id, action)
        if action in REJECTED_CALLS:
            return [CALLRESULT, msg_id, {"status": "Rejected"}]
        if action == "UnlockConnector":
            return [CALLRESULT, msg_id, {"status": "NotSupported"}]
        if action == "GetConfiguration":
            known = {
                "HeartbeatInterval": str(self.heartbeat_interval),
                "MeterValueSampleInterval": str(int(self.cfg.poll_seconds)),
                "NumberOfConnectors": "1",
                "MeterValuesSampledData": "Energy.Active.Import.Register,Power.Active.Import,"
                                          "Current.Import,Voltage",
                "AuthorizeRemoteTxRequests": "false",
            }
            asked = payload.get("key") if isinstance(payload, dict) else None
            keys = list(known) if not asked else [k for k in asked if k in known]
            unknown = [k for k in (asked or []) if k not in known]
            return [CALLRESULT, msg_id, {
                "configurationKey": [{"key": k, "readonly": True, "value": known[k]} for k in keys],
                "unknownKey": unknown,
            }]
        return [CALLERROR, msg_id, "NotImplemented",
                f"{action} is not supported by a read-only bridge", {}]

    async def _boot(self) -> None:
        while True:
            res = await self.call("BootNotification", {
                "chargePointVendor": VENDOR, "chargePointModel": MODEL,
            })
            interval = int(res.get("interval") or 0)
            if res.get("status") == "Accepted":
                self.heartbeat_interval = interval if interval > 0 else DEFAULT_HEARTBEAT_S
                return
            log.info("%s: boot %s, retrying in %ss", self.cp_id, res.get("status"), interval)
            await asyncio.sleep(interval if interval > 0 else DEFAULT_BOOT_RETRY_S)

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_interval)
            try:
                await self.call("Heartbeat", {})
            except OcppCallError as exc:  # the CSMS refused one heartbeat: the link is fine
                log.warning("%s: Heartbeat CallError %s", self.cp_id, exc)

    async def _sync(self) -> None:
        plug = self.snapshot
        assert plug is not None
        st = self.state.get(self.cp_id, self.plug_id)
        session = plug.get("session") or None
        session_id = int(session["id"]) if session else None

        if st["transaction_id"] is not None and st["session_id"] != session_id:
            try:
                await self._stop(st)
                self._stop_errors = 0
            except OcppCallError as exc:
                self._stop_errors += 1
                if self._stop_errors < STOP_GIVE_UP:
                    raise
                # The CSMS keeps refusing this stop (for example it does not know the
                # transactionId): treat it as acked so the charge point is not wedged.
                log.warning("%s: StopTransaction refused %s times (%s); closing transaction "
                            "%s locally", self.cp_id, self._stop_errors, exc, st["transaction_id"])
                self._stop_errors = 0
                self._close_locally(st)

        energy_wh = float(session["energy_kwh"]) * 1000.0 if session else 0.0
        if session is not None and st["transaction_id"] is None:
            res = await self.call("StartTransaction", {
                "connectorId": 1, "idTag": self.cfg.id_tag,
                "meterStart": round(st["base_wh"]),
                "timestamp": utc_iso(session["started_at"]),
            })
            tag_status = (res.get("idTagInfo") or {}).get("status")
            if tag_status != "Accepted":
                log.warning("%s: CSMS answered idTag %s; still mirroring (read-only)",
                            self.cp_id, tag_status)
            st.update(session_id=session_id, transaction_id=int(res["transactionId"]),
                      last_energy_wh=0.0)
            self.state.save()
            log.info("%s: transaction %s started for session %s",
                     self.cp_id, st["transaction_id"], session_id)

        status = ocpp_status(plug)
        if status != self._last_status:
            await self.call("StatusNotification", {
                "connectorId": 1, "errorCode": "NoError", "status": status,
                "timestamp": utc_iso(),
            })
            self._last_status = status  # only after the CSMS answered

        if session is None:
            return
        st["last_energy_wh"] = max(float(st["last_energy_wh"]), energy_wh)
        self.state.save()
        await self.call("MeterValues", {
            "connectorId": 1, "transactionId": st["transaction_id"],
            "meterValue": [{
                "timestamp": utc_iso(plug.get("reading_at")),
                "sampledValue": self._samples(plug, st),
            }],
        })

    def _samples(self, plug: Mapping[str, Any], st: Mapping[str, Any]) -> list[dict[str, str]]:
        register = round(float(st["base_wh"]) + float(st["last_energy_wh"]))
        out = [{"value": str(register), "measurand": "Energy.Active.Import.Register",
                "unit": "Wh", "context": "Sample.Periodic"}]
        for key, measurand, unit in (("power_w", "Power.Active.Import", "W"),
                                     ("current_a", "Current.Import", "A"),
                                     ("voltage_v", "Voltage", "V")):
            if plug.get(key) is not None:
                out.append({"value": f"{float(plug[key]):.2f}", "measurand": measurand,
                            "unit": unit, "context": "Sample.Periodic"})
        return out

    async def _stop(self, st: dict[str, Any]) -> None:
        meter_stop = round(float(st["base_wh"]) + float(st["last_energy_wh"]))
        await self.call("StopTransaction", {
            "transactionId": st["transaction_id"], "meterStop": meter_stop,
            "timestamp": utc_iso(), "reason": "Local", "idTag": self.cfg.id_tag,
        })
        log.info("%s: transaction %s stopped at %s Wh", self.cp_id, st["transaction_id"], meter_stop)
        self._close_locally(st)  # only after the CSMS acked

    def _close_locally(self, st: dict[str, Any]) -> None:
        """Advance the register and drop the transaction mapping."""
        st.update(base_wh=float(st["base_wh"]) + float(st["last_energy_wh"]),
                  session_id=None, transaction_id=None, last_energy_wh=0.0)
        self.state.save()


class Bridge:
    def __init__(self, cfg: Config, http: httpx.AsyncClient, state: StateStore) -> None:
        self.cfg = cfg
        self.meter = MeterClient(http, cfg.api_url, cfg.token)
        self.state = state
        self.cps: dict[int, ChargePoint] = {}
        self._tasks: list[asyncio.Task[None]] = []

    def _cp(self, plug_id: int, cp_id: str | None = None) -> ChargePoint:
        if plug_id not in self.cps:
            cp = ChargePoint(cp_id or f"{self.cfg.prefix}{plug_id}", plug_id, self.cfg, self.state)
            self.cps[plug_id] = cp
            self._tasks.append(asyncio.create_task(cp.run()))
        return self.cps[plug_id]

    async def run(self) -> None:
        try:
            # A transaction left open by a previous run must still be closed even
            # if its plug no longer shows up (e.g. a driver token after the session).
            for cp_id, plug_id in self.state.open_transactions():
                self._cp(plug_id, cp_id)
            while True:
                await self.poll_once()
                await asyncio.sleep(self.cfg.poll_seconds)
        finally:
            for task in self._tasks:
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def poll_once(self) -> None:
        try:
            plugs = await self.meter.fetch()
        except AuthError:
            raise
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            # Only a successful response may end a transaction; a failed poll changes nothing.
            log.warning("meter poll failed (%s)", type(exc).__name__)
            return
        for plug_id, plug in plugs.items():
            self._cp(plug_id).update(plug)
        for plug_id, cp in self.cps.items():
            if plug_id not in plugs:
                cp.update({"plug_id": plug_id, "status": "unknown", "online": False,
                           "session": None})


async def amain(cfg: Config) -> None:
    async with httpx.AsyncClient() as http:
        await Bridge(cfg, http, StateStore(cfg.state_file)).run()
