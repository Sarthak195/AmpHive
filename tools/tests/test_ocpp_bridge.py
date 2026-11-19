"""Tests for tools/ocpp_bridge: an in-process fake CSMS (websockets.serve on
127.0.0.1, random port) and a fake meter API (httpx.MockTransport). No real
network, no AmpHive access.
"""

import asyncio
import base64
import copy
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from ocpp_bridge import AuthError, Bridge, ChargePoint, Config, StateStore, ocpp_status
from websockets.asyncio.server import ServerConnection, serve

TOKEN = "amph_test-secret-123"
STARTED = "2026-11-17T10:00:00+00:00"


class FakeMeter:
    def __init__(self) -> None:
        self.plugs: list[dict] = []
        self.status_code = 200
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.url.path == "/api/me/meter"
        body = {"generated_at": "2026-11-17T10:00:05Z", "plugs": copy.deepcopy(self.plugs)}
        return httpx.Response(self.status_code, json=body)


class FakeCsms:
    def __init__(self, boot_statuses: tuple[str, ...] = ("Accepted",),
                 error_actions: tuple[str, ...] = (), heartbeat_s: int = 300,
                 close_after_boot: bool = False) -> None:
        self.boot_statuses = list(boot_statuses)
        self.error_actions = set(error_actions)  # answered with a CallError
        self.heartbeat_s = heartbeat_s
        self.close_after_boot = close_after_boot
        self.received: list[tuple[str, str, dict]] = []
        self.conns: dict[str, ServerConnection] = {}
        self.auth: dict[str, str | None] = {}
        self.replies: dict[str, list] = {}
        self.next_tx = 100

    def actions(self, action: str) -> list[dict]:
        return [p for _, a, p in self.received if a == action]

    def _result(self, action: str) -> dict:
        if action == "BootNotification":
            status = self.boot_statuses.pop(0) if len(self.boot_statuses) > 1 else self.boot_statuses[0]
            return {"status": status, "currentTime": "2026-11-17T10:00:00Z",
                    "interval": self.heartbeat_s if status == "Accepted" else 1}
        if action == "StartTransaction":
            self.next_tx += 1
            return {"transactionId": self.next_tx - 1, "idTagInfo": {"status": "Accepted"}}
        if action == "StopTransaction":
            return {"idTagInfo": {"status": "Accepted"}}
        if action == "Heartbeat":
            return {"currentTime": "2026-11-17T10:00:00Z"}
        return {}

    async def handler(self, ws: ServerConnection) -> None:
        assert ws.request is not None
        cp_id = ws.request.path.rsplit("/", 1)[-1]
        self.conns[cp_id] = ws
        self.auth[cp_id] = ws.request.headers.get("Authorization")
        async for raw in ws:
            msg = json.loads(raw)
            if msg[0] == 2:
                self.received.append((cp_id, msg[2], msg[3]))
                if msg[2] in self.error_actions:
                    await ws.send(json.dumps([4, msg[1], "InternalError", "boom", {}]))
                    continue
                await ws.send(json.dumps([3, msg[1], self._result(msg[2])]))
                if msg[2] == "BootNotification" and self.close_after_boot:
                    await ws.close()
            else:
                self.replies[msg[1]] = msg

    async def call(self, cp_id: str, action: str, payload: dict) -> list:
        msg_id = f"csms-{len(self.replies)}-{action}"
        await self.conns[cp_id].send(json.dumps([2, msg_id, action, payload]))
        await wait_until(lambda: msg_id in self.replies)
        return self.replies[msg_id]


async def wait_until(pred, timeout: float = 5.0) -> None:
    async def _loop() -> None:
        while not pred():
            await asyncio.sleep(0.01)
    await asyncio.wait_for(_loop(), timeout)


@asynccontextmanager
async def running(meter: FakeMeter, csms: FakeCsms, state_file: Path, **cfg_overrides):
    async with serve(csms.handler, "127.0.0.1", 0, subprotocols=["ocpp1.6"]) as server:
        port = server.sockets[0].getsockname()[1]
        cfg = Config(api_url="https://meter.test", token=TOKEN,
                     csms_url=f"ws://127.0.0.1:{port}/ocpp", poll_seconds=0.05,
                     state_file=state_file, **cfg_overrides)
        async with httpx.AsyncClient(transport=httpx.MockTransport(meter.handler)) as http:
            task = asyncio.create_task(Bridge(cfg, http, StateStore(state_file)).run())
            try:
                yield task
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


def plug(plug_id: int = 7, status: str = "available", online: bool = True,
         power_w: float | None = None, session: dict | None = None) -> dict:
    return {"plug_id": plug_id, "name": "Garage", "status": status, "online": online,
            "power_w": power_w, "current_a": 9.1 if power_w else None,
            "voltage_v": 231.0 if power_w else None,
            "reading_at": "2026-11-17T10:00:04Z" if power_w else None, "session": session}


def session(energy_kwh: float, sid: int = 55) -> dict:
    return {"id": sid, "started_at": STARTED, "energy_kwh": energy_kwh}


def samples(meter_values: dict) -> dict[str, str]:
    return {s["measurand"]: s["value"] for s in meter_values["meterValue"][0]["sampledValue"]}


@pytest.mark.parametrize("p, expected", [
    (plug(status="available"), "Available"),
    (plug(status="reserved"), "Reserved"),
    (plug(status="occupied", power_w=2000, session=session(1)), "Charging"),
    (plug(status="occupied", power_w=9.9, session=session(1)), "SuspendedEV"),
    (plug(status="occupied", power_w=None, session=session(1)), "SuspendedEV"),
    (plug(status="occupied"), "Unavailable"),
    (plug(status="maintenance"), "Unavailable"),
    (plug(status="maintenance", power_w=2000, session=session(1)), "Unavailable"),
    (plug(status="offline"), "Unavailable"),
    (plug(status="available", online=False), "Unavailable"),
    (plug(status="occupied", online=False, power_w=2000, session=session(1)), "Unavailable"),
    (plug(status="something-new"), "Unavailable"),
])
def test_status_mapping(p, expected):
    assert ocpp_status(p) == expected


def test_boot_retries_pending_then_reports_status(tmp_path):
    meter, csms = FakeMeter(), FakeCsms(boot_statuses=("Pending", "Accepted"))
    meter.plugs = [plug()]

    async def scenario():
        async with running(meter, csms, tmp_path / "s.json", basic_password="cp-secret"):
            await wait_until(lambda: csms.actions("StatusNotification"), timeout=8)
        boots = csms.actions("BootNotification")
        assert len(boots) == 2
        assert boots[0] == {"chargePointVendor": "AmpHive", "chargePointModel": "Tapo P110 bridge"}
        # nothing but BootNotification until the CSMS accepted it
        assert [a for _, a, _ in csms.received[:2]] == ["BootNotification", "BootNotification"]
        assert csms.actions("StatusNotification")[0]["status"] == "Available"
        assert csms.actions("StatusNotification")[0]["errorCode"] == "NoError"
        assert set(csms.conns) == {"AMPHIVE-7"}
        expected = "Basic " + base64.b64encode(b"AMPHIVE-7:cp-secret").decode()
        assert csms.auth["AMPHIVE-7"] == expected
        assert meter.requests[0].headers["Authorization"] == f"Bearer {TOKEN}"

    asyncio.run(scenario())


def test_full_transaction_and_failed_poll_keeps_it_open(tmp_path):
    meter, csms = FakeMeter(), FakeCsms()
    meter.plugs = [plug()]

    async def scenario():
        async with running(meter, csms, tmp_path / "s.json"):
            await wait_until(lambda: csms.actions("StatusNotification"))
            meter.plugs = [plug(status="occupied", power_w=2000.0, session=session(0.5))]
            await wait_until(lambda: csms.actions("MeterValues"))
            start = csms.actions("StartTransaction")[0]
            assert start["meterStart"] == 0
            assert start["idTag"] == "AMPHIVE"
            assert start["timestamp"] == "2026-11-17T10:00:00.000Z"
            assert csms.actions("StatusNotification")[-1]["status"] == "Charging"
            mv = csms.actions("MeterValues")[0]
            assert mv["transactionId"] == 100
            assert samples(mv) == {"Energy.Active.Import.Register": "500",
                                   "Power.Active.Import": "2000.00",
                                   "Current.Import": "9.10", "Voltage": "231.00"}

            meter.plugs = [plug(status="occupied", power_w=2000.0, session=session(1.2))]
            await wait_until(lambda: samples(csms.actions("MeterValues")[-1])
                             ["Energy.Active.Import.Register"] == "1200")

            meter.status_code = 500  # a failed poll must not end the transaction
            seen = len(meter.requests)
            await wait_until(lambda: len(meter.requests) >= seen + 4)
            assert not csms.actions("StopTransaction")
            meter.status_code = 200

            meter.plugs = [plug(status="available")]
            await wait_until(lambda: csms.actions("StopTransaction"))
            stop = csms.actions("StopTransaction")[0]
            assert stop == {**stop, "transactionId": 100, "meterStop": 1200, "reason": "Local"}
            await wait_until(lambda: csms.actions("StatusNotification")[-1]["status"] == "Available")

            # the next session starts where the last one stopped
            meter.plugs = [plug(status="occupied", power_w=1500.0, session=session(0.3, sid=56))]
            await wait_until(lambda: len(csms.actions("StartTransaction")) == 2)
            assert csms.actions("StartTransaction")[1]["meterStart"] == 1200
            await wait_until(lambda: samples(csms.actions("MeterValues")[-1])
                             ["Energy.Active.Import.Register"] == "1500")
        assert all(r.method == "GET" for r in meter.requests)

    asyncio.run(scenario())


def test_csms_commands_are_refused(tmp_path):
    meter, csms = FakeMeter(), FakeCsms()
    meter.plugs = [plug(status="occupied", power_w=2000.0, session=session(0.5))]

    async def scenario():
        async with running(meter, csms, tmp_path / "s.json"):
            await wait_until(lambda: csms.actions("MeterValues"))
            cp = "AMPHIVE-7"
            r = await csms.call(cp, "RemoteStartTransaction", {"idTag": "X", "connectorId": 1})
            assert r[0] == 3 and r[2] == {"status": "Rejected"}
            r = await csms.call(cp, "RemoteStopTransaction", {"transactionId": 100})
            assert r[2] == {"status": "Rejected"}
            r = await csms.call(cp, "UnlockConnector", {"connectorId": 1})
            assert r[2] == {"status": "NotSupported"}
            r = await csms.call(cp, "GetConfiguration", {"key": ["HeartbeatInterval", "Nope"]})
            assert r[2]["configurationKey"] == [
                {"key": "HeartbeatInterval", "readonly": True, "value": "300"}]
            assert r[2]["unknownKey"] == ["Nope"]
            r = await csms.call(cp, "SetChargingProfile", {})
            assert r[0] == 4 and r[2] == "NotImplemented"
            # the session is untouched: still a transaction, never a stop
            await asyncio.sleep(0.2)
            assert not csms.actions("StopTransaction")
        assert all(r.method == "GET" for r in meter.requests)

    asyncio.run(scenario())


def test_restart_continues_transaction_without_double_count(tmp_path):
    state_file = tmp_path / "s.json"
    meter, csms = FakeMeter(), FakeCsms()
    meter.plugs = [plug(status="occupied", power_w=2000.0, session=session(0.8))]

    async def scenario():
        async with running(meter, csms, state_file):
            await wait_until(lambda: csms.actions("MeterValues"))
        saved = json.loads(state_file.read_text())["AMPHIVE-7"]
        assert saved["transaction_id"] == 100 and saved["session_id"] == 55

        meter.plugs = [plug(status="occupied", power_w=2000.0, session=session(1.0))]
        async with running(meter, csms, state_file):
            await wait_until(lambda: samples(csms.actions("MeterValues")[-1])
                             ["Energy.Active.Import.Register"] == "1000")
            assert len(csms.actions("StartTransaction")) == 1
            assert csms.actions("MeterValues")[-1]["transactionId"] == 100
            meter.plugs = []  # driver token: the plug vanishes with its session
            await wait_until(lambda: csms.actions("StopTransaction"))
        assert csms.actions("StopTransaction")[0]["meterStop"] == 1000
        assert json.loads(state_file.read_text())["AMPHIVE-7"]["base_wh"] == 1000.0

    asyncio.run(scenario())


def test_restart_closes_orphaned_transaction(tmp_path):
    state_file = tmp_path / "s.json"
    meter, csms = FakeMeter(), FakeCsms()
    meter.plugs = [plug(status="occupied", power_w=2000.0, session=session(0.8))]

    async def scenario():
        async with running(meter, csms, state_file):
            await wait_until(lambda: csms.actions("MeterValues"))
        meter.plugs = []  # the session ended while the bridge was down
        async with running(meter, csms, state_file):
            await wait_until(lambda: csms.actions("StopTransaction"))
        stop = csms.actions("StopTransaction")[0]
        assert stop["transactionId"] == 100 and stop["meterStop"] == 800
        saved = json.loads(state_file.read_text())["AMPHIVE-7"]
        assert saved["transaction_id"] is None and saved["base_wh"] == 800.0

    asyncio.run(scenario())


def test_revoked_token_stops_bridge_without_logging_it(tmp_path, caplog):
    meter, csms = FakeMeter(), FakeCsms()
    meter.status_code = 401
    caplog.set_level(logging.DEBUG)

    async def scenario():
        async with running(meter, csms, tmp_path / "s.json") as task:
            with pytest.raises(AuthError):
                await asyncio.wait_for(asyncio.shield(task), 5)

    asyncio.run(scenario())
    assert TOKEN not in caplog.text


def test_config_requires_token_and_safe_urls():
    base = {"OCPP_CSMS_URL": "ws://127.0.0.1:8180/steve/websocket/CentralSystemService"}
    with pytest.raises(ValueError, match="AMPHIVE_METER_TOKEN"):
        Config.from_env(base)
    with pytest.raises(ValueError, match="https"):
        Config.from_env({**base, "AMPHIVE_METER_TOKEN": TOKEN,
                         "AMPHIVE_API_URL": "http://example.test"})
    cfg = Config.from_env({**base, "AMPHIVE_METER_TOKEN": TOKEN})
    assert cfg.prefix == "AMPHIVE-" and cfg.poll_seconds == 10.0 and cfg.id_tag == "AMPHIVE"
    assert TOKEN not in repr(cfg)


def test_backoff_resets_after_a_healthy_connection(tmp_path):
    csms = FakeCsms(close_after_boot=True)
    delays: list[float] = []

    async def scenario():
        async with serve(csms.handler, "127.0.0.1", 0, subprotocols=["ocpp1.6"]) as server:
            port = server.sockets[0].getsockname()[1]
            cfg = Config(api_url="https://meter.test", token=TOKEN,
                         csms_url=f"ws://127.0.0.1:{port}/ocpp", state_file=tmp_path / "s.json")
            cp = ChargePoint("AMPHIVE-7", 7, cfg, StateStore(cfg.state_file))

            async def fake_sleep(seconds: float) -> None:
                delays.append(seconds)
                await asyncio.sleep(0.01)

            cp._sleep = fake_sleep
            task = asyncio.create_task(cp.run())
            try:
                await wait_until(lambda: len(delays) >= 4)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert delays[:4] == [1.0, 1.0, 1.0, 1.0]  # each connection booted, so no doubling


def test_persistent_stop_call_error_is_given_up_after_three(tmp_path):
    state_file = tmp_path / "s.json"
    meter, csms = FakeMeter(), FakeCsms(error_actions=("StopTransaction",))
    meter.plugs = [plug(status="occupied", power_w=2000.0, session=session(0.5))]

    async def scenario():
        async with running(meter, csms, state_file):
            await wait_until(lambda: csms.actions("MeterValues"))
            # the next session replaces the open one: the stop is refused, not wedging
            meter.plugs = [plug(status="occupied", power_w=1500.0, session=session(0.2, sid=56))]
            await wait_until(lambda: len(csms.actions("StartTransaction")) == 2)
            assert len(csms.actions("StopTransaction")) == 3
            assert csms.actions("StartTransaction")[1]["meterStart"] == 500
            await wait_until(lambda: samples(csms.actions("MeterValues")[-1])
                             ["Energy.Active.Import.Register"] == "700")
            meter.plugs = [plug(status="available")]  # and a later session end is not stuck
            await wait_until(lambda: csms.actions("StatusNotification")[-1]["status"] == "Available")
        saved = json.loads(state_file.read_text())["AMPHIVE-7"]
        assert saved["transaction_id"] is None and saved["base_wh"] == 700.0

    asyncio.run(scenario())


def test_stop_call_error_counter_resets_on_success(tmp_path):
    meter, csms = FakeMeter(), FakeCsms()
    meter.plugs = [plug(status="occupied", power_w=2000.0, session=session(0.5))]

    async def scenario():
        async with running(meter, csms, tmp_path / "s.json"):
            await wait_until(lambda: csms.actions("MeterValues"))
            csms.error_actions.add("StopTransaction")
            meter.plugs = [plug(status="available")]
            await wait_until(lambda: len(csms.actions("StopTransaction")) == 2)
            csms.error_actions.clear()  # the third attempt is acked: nothing is dropped
            await wait_until(lambda: csms.actions("StatusNotification")[-1]["status"] == "Available")
            assert len(csms.actions("StopTransaction")) == 3
            # a fresh refused stop starts counting again from zero (needs 3 more)
            meter.plugs = [plug(status="occupied", power_w=2000.0, session=session(0.4, sid=57))]
            await wait_until(lambda: len(csms.actions("StartTransaction")) == 2)
            csms.error_actions.add("StopTransaction")
            meter.plugs = [plug(status="available")]
            await wait_until(lambda: len(csms.actions("StopTransaction")) == 6)

    asyncio.run(scenario())


def test_heartbeat_call_error_keeps_the_connection(tmp_path):
    meter, csms = FakeMeter(), FakeCsms(error_actions=("Heartbeat",), heartbeat_s=1)
    meter.plugs = [plug()]

    async def scenario():
        async with running(meter, csms, tmp_path / "s.json"):
            await wait_until(lambda: len(csms.actions("Heartbeat")) >= 2, timeout=8)
        assert len(csms.actions("BootNotification")) == 1  # never reconnected

    asyncio.run(scenario())


def test_call_propagates_a_cancel_that_races_the_reply(tmp_path):
    # The reply and the cancel land in the same loop tick. Python 3.11's
    # wait_for returned the reply and dropped the cancel (cpython gh-86296),
    # so a stopping bridge kept running and CI hung in teardown.
    cfg = Config(api_url="https://meter.test", token=TOKEN, csms_url="ws://127.0.0.1:1/ocpp",
                 poll_seconds=0.05, state_file=tmp_path / "s.json")
    cp = ChargePoint("AMPHIVE-7", 7, cfg, StateStore(cfg.state_file))

    class SilentWs:
        async def send(self, data: str) -> None:
            pass

        async def close(self) -> None:
            pass

    cp._ws = SilentWs()

    async def scenario():
        task = asyncio.create_task(cp.call("Heartbeat", {}))
        await wait_until(lambda: cp._pending)
        next(iter(cp._pending.values())).set_result({})
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_start_transaction_precedes_the_charging_status(tmp_path):
    meter, csms = FakeMeter(), FakeCsms()
    meter.plugs = [plug()]

    async def scenario():
        async with running(meter, csms, tmp_path / "s.json"):
            await wait_until(lambda: csms.actions("StatusNotification"))
            meter.plugs = [plug(status="occupied", power_w=2000.0, session=session(0.5))]
            await wait_until(lambda: csms.actions("MeterValues"))
        order = [a for _, a, _ in csms.received if a != "BootNotification"]
        assert order == ["StatusNotification", "StartTransaction",
                         "StatusNotification", "MeterValues"]
        assert csms.actions("StatusNotification")[-1]["status"] == "Charging"

    asyncio.run(scenario())


def test_failed_start_never_announces_charging(tmp_path):
    meter, csms = FakeMeter(), FakeCsms(error_actions=("StartTransaction",))
    meter.plugs = [plug(status="occupied", power_w=2000.0, session=session(0.5))]

    async def scenario():
        async with running(meter, csms, tmp_path / "s.json"):
            await wait_until(lambda: len(csms.actions("StartTransaction")) >= 3)
        assert not csms.actions("StatusNotification")
        assert not csms.actions("MeterValues")

    asyncio.run(scenario())
