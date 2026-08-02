"""Integration tests: a hand-rolled KLAP client (independent re-implementation
of the same formulas as tools/klap_probe.py, which was validated against
real P110 hardware fw 1.1.3) driving a live PlugHTTPServer over a real
localhost TCP socket. This is deliberately NOT sharing code with
tools/p110_sim/crypto.py's KlapSession — it re-derives everything from first
principles so a bug in crypto.py can't hide a matching bug on both sides of
the same test.
"""

import hashlib
import json
import secrets
import struct
import time

import pytest
import requests
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from conftest import DEFAULT_EMAIL, DEFAULT_PASSWORD

_PACK = struct.Struct(">i").pack


def _sha256(*parts: bytes) -> bytes:
    h = hashlib.sha256()
    for p in parts:
        h.update(p)
    return h.digest()


def _auth_hash(email: str, password: str) -> bytes:
    return _sha256(hashlib.sha1(email.encode()).digest(), hashlib.sha1(password.encode()).digest())


class _ReferenceKlapClient:
    """Independent client-side KLAP implementation for test purposes."""

    def __init__(self, base_url: str, email: str, password: str):
        self.base = base_url
        self.auth_hash = _auth_hash(email, password)
        self.cookies: dict[str, str] = {}
        self._key = self._iv = self._sig = None
        self._seq = 0

    def handshake(self) -> None:
        local_seed = secrets.token_bytes(16)
        r1 = requests.post(f"{self.base}/app/handshake1", data=local_seed, timeout=5)
        if r1.status_code != 200:
            raise AssertionError(f"handshake1 -> {r1.status_code}")
        remote_seed, server_hash = r1.content[:16], r1.content[16:]
        if _sha256(local_seed, remote_seed, self.auth_hash) != server_hash:
            raise AssertionError("server_hash mismatch")
        cookie = r1.cookies.get("TP_SESSIONID")
        self.cookies = {"TP_SESSIONID": cookie}

        hs2 = _sha256(remote_seed, local_seed, self.auth_hash)
        r2 = requests.post(f"{self.base}/app/handshake2", data=hs2, cookies=self.cookies, timeout=5)
        if r2.status_code != 200:
            raise AssertionError(f"handshake2 -> {r2.status_code}")

        s = local_seed + remote_seed + self.auth_hash
        self._key = _sha256(b"lsk", s)[:16]
        iv_seed = _sha256(b"iv", s)
        self._iv = iv_seed[:12]
        self._seq = struct.unpack(">i", iv_seed[28:32])[0]
        self._sig = _sha256(b"ldk", s)[:28]

    def call(self, method: str, params: dict | None = None) -> dict:
        inner = {"method": method}
        if params is not None:
            inner["params"] = params
        self._seq += 1
        full_iv = self._iv + _PACK(self._seq)

        padder = padding.PKCS7(128).padder()
        pt = padder.update(json.dumps(inner).encode()) + padder.finalize()
        enc = Cipher(algorithms.AES(self._key), modes.CBC(full_iv)).encryptor()
        ct = enc.update(pt) + enc.finalize()
        sig = _sha256(self._sig, _PACK(self._seq), ct)

        r = requests.post(
            f"{self.base}/app/request",
            params={"seq": self._seq},
            data=sig + ct,
            cookies=self.cookies,
            timeout=5,
        )
        if r.status_code != 200:
            raise AssertionError(f"{method} -> HTTP {r.status_code}")

        dec = Cipher(algorithms.AES(self._key), modes.CBC(full_iv)).decryptor()
        padded = dec.update(r.content[32:]) + dec.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        plaintext = unpadder.update(padded) + unpadder.finalize()
        return json.loads(plaintext)


def _client(live) -> _ReferenceKlapClient:
    c = _ReferenceKlapClient(f"http://{live.host}:{live.port}", DEFAULT_EMAIL, DEFAULT_PASSWORD)
    c.handshake()
    return c


def test_handshake_succeeds_with_correct_credentials(live_plug):
    live = live_plug()
    c = _client(live)
    resp = c.call("get_device_info")
    assert resp["error_code"] == 0
    assert resp["result"]["model"] == "P110"
    assert resp["result"]["device_on"] is False


def test_handshake1_rejects_wrong_credentials(live_plug):
    live = live_plug()
    bad = _ReferenceKlapClient(f"http://{live.host}:{live.port}", DEFAULT_EMAIL, "totally-wrong")
    local_seed = secrets.token_bytes(16)
    r1 = requests.post(f"http://{live.host}:{live.port}/app/handshake1", data=local_seed, timeout=5)
    remote_seed, server_hash = r1.content[:16], r1.content[16:]
    assert _sha256(local_seed, remote_seed, bad.auth_hash) != server_hash


def test_on_off_and_energy_flow(live_plug):
    live = live_plug(watts=2000.0, jitter=0.0)
    c = _client(live)

    on = c.call("set_device_info", {"device_on": True})
    assert on["error_code"] == 0
    assert live.plug.get_device_info()["device_on"] is True

    energy = c.call("get_energy_usage")
    assert energy["result"]["current_power"] == 2000000  # 2000 W in mW

    off = c.call("set_device_info", {"device_on": False})
    assert off["error_code"] == 0
    assert live.plug.get_device_info()["device_on"] is False


def test_request_without_session_gets_403(live_plug):
    live = live_plug()
    r = requests.post(
        f"http://{live.host}:{live.port}/app/request",
        params={"seq": 1},
        data=b"x" * 48,
        timeout=5,
    )
    assert r.status_code == 403


def test_drop_rate_one_drops_every_request(live_plug):
    live = live_plug(drop_rate=1.0)
    with pytest.raises(requests.exceptions.RequestException):
        requests.post(
            f"http://{live.host}:{live.port}/app/handshake1",
            data=secrets.token_bytes(16),
            timeout=2,
        )


def test_drop_rate_zero_never_drops(live_plug):
    live = live_plug(drop_rate=0.0)
    for _ in range(5):
        c = _client(live)
        assert c.call("get_device_info")["error_code"] == 0


def test_component_negotiation_probe_declines_with_1003(live_plug):
    """The `tapo` PyPI client always probes POST /app (unencrypted
    component_nego) before choosing a transport; a real P110 in "Third-Party
    Compatibility" mode answers 1003 there, which is what makes a real
    client fall back to KLAP instead of the AES/securePassthrough path. See
    server.py's do_POST "/app" branch and README.md."""
    live = live_plug()
    r = requests.post(
        f"http://{live.host}:{live.port}/app",
        data=json.dumps({"method": "component_nego", "params": None}),
        timeout=5,
    )
    assert r.status_code == 200
    assert r.json()["error_code"] == 1003


def test_reset_counter_invalidates_sessions_and_zeros_energy(live_plug, monkeypatch):
    t = {"v": 1000.0}
    monkeypatch.setattr(time, "time", lambda: t["v"])
    live = live_plug(start_kwh=8.0, reset_after_s=5.0, reset_value_kwh=1.0, watts=1000.0, jitter=0.0)
    live.plug.on_counter_reset = lambda plug: live.srv.reset_sessions()

    c = _client(live)
    assert c.call("get_device_info")["error_code"] == 0

    t["v"] += 10.0  # past the 5s reset deadline
    # Any request first calls plug.tick(), which applies the scheduled reset.
    with pytest.raises(AssertionError):
        c.call("get_device_info")  # old session was invalidated -> 403

    # A fresh handshake succeeds and sees the reset counter value.
    c2 = _client(live)
    energy = c2.call("get_energy_usage")
    assert energy["result"]["today_energy"] == pytest.approx(1000, abs=1)
