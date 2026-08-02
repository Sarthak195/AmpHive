"""One threaded HTTP server per simulated plug, speaking exactly the wire
shape firmware/main/tapo_protocol.c's KLAP driver expects:

    POST /app/handshake1   body = local_seed(16)         -> 200, body = remote_seed(16) || server_hash(32), Set-Cookie: TP_SESSIONID=...
    POST /app/handshake2   body = SHA256(...)  (32 bytes) -> 200 (session established) or 403
    POST /app/request?seq=N  body = sig(32) || AES-CBC ciphertext -> 200, body = sig(32) || AES-CBC ciphertext (JSON-RPC request/response)

All three run on plain HTTP (no TLS) on whatever port the plug is bound to
— real P110s only ever listen on :80, but this emulator allows arbitrary
ports so several plugs can share one host (see the ":port" roster-addressing
support in firmware/main/session_nvs.h PLUG_IP_MAX_LEN).
"""

from __future__ import annotations

import hmac
import json
import logging
import random
import secrets
import socketserver
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional

from crypto import KlapSession, handshake1_server_hash, handshake2_expected
from plug import SimulatedPlug
from state import StateStore

TP_SESSIONID_RE = "TP_SESSIONID="


class _PendingHandshake:
    __slots__ = ("local_seed", "remote_seed", "created")

    def __init__(self, local_seed: bytes, remote_seed: bytes):
        self.local_seed = local_seed
        self.remote_seed = remote_seed
        self.created = time.monotonic()


class PlugHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], plug: SimulatedPlug, store: Optional[StateStore] = None):
        super().__init__(address, KlapRequestHandler)
        self.plug = plug
        self.store = store
        self.log = logging.getLogger(f"p110sim.{plug.cfg.label}")
        self.pending: dict[str, _PendingHandshake] = {}
        self.sessions: dict[str, KlapSession] = {}

    def reset_sessions(self) -> None:
        """Invalidate every live/pending KLAP session — mirrors what a real
        power-cycle of the physical plug does (its session state lives in
        RAM). Called on a scheduled --reset-counter event so the next poll
        forces a fresh handshake, exactly like the firmware's own
        `klap_request()` re-handshake-on-403 path."""
        self.pending.clear()
        self.sessions.clear()


def _extract_cookie_token(cookie_header: Optional[str]) -> Optional[str]:
    if not cookie_header:
        return None
    idx = cookie_header.find(TP_SESSIONID_RE)
    if idx < 0:
        return None
    rest = cookie_header[idx + len(TP_SESSIONID_RE) :]
    return rest.split(";", 1)[0].strip() or None


class KlapRequestHandler(BaseHTTPRequestHandler):
    server: PlugHTTPServer  # narrows the type for the methods below
    protocol_version = "HTTP/1.1"

    # --- plumbing ---------------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 (BaseHTTPRequestHandler API)
        self.server.log.debug("%s - %s", self.address_string(), fmt % args)

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0") or "0")
        return self.rfile.read(length) if length > 0 else b""

    def _send(
        self,
        status: int,
        body: bytes = b"",
        extra_headers: Optional[dict] = None,
        content_type: str = "application/octet-stream",
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_json(self, status: int, obj: dict, extra_headers: Optional[dict] = None) -> None:
        self._send(status, json.dumps(obj).encode("utf-8"), extra_headers, "application/json")

    # --- dispatch -----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        self.server.log.warning("Unhandled GET %s (headers=%s)", self.path, dict(self.headers))
        self._send(404)

    def do_POST(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        self.server.log.debug("POST %s (headers=%s)", self.path, dict(self.headers))
        plug = self.server.plug
        if plug.cfg.drop_rate > 0 and random.random() < plug.cfg.drop_rate:
            # Simulate an unreachable/flaky plug: drop the connection with no
            # response at all, rather than a well-formed HTTP error. This is
            # what a real dead plug looks like to esp_http_client (transport
            # error -> klap_request() fails -> that poll cycle is skipped,
            # see firmware/main/main.c telemetry_task).
            self.server.log.info("DROP %s (simulated flaky plug)", self.path)
            self.close_connection = True
            return

        plug.tick()  # advance energy integration; may fire a scheduled reset
        if self.server.store is not None:
            snap = plug.snapshot()
            if snap["device_on"]:
                # Only worth the disk write while actually accruing energy;
                # relay-state changes persist immediately via on_state_changed.
                self.server.store.set(plug.cfg.plug_id, snap)

        # Read the body exactly once, unconditionally, before any branch can
        # return early. With protocol_version="HTTP/1.1" the connection is
        # kept alive across requests; leaving unread body bytes in the
        # socket corrupts the NEXT request's status line (observed in
        # practice against the real `tapo` client, which pipelines a
        # component-negotiation probe on the same connection as the KLAP
        # handshake that follows it).
        body = self._body()

        path = urllib.parse.urlsplit(self.path).path
        try:
            if path == "/app/handshake1":
                self._handshake1(body)
            elif path == "/app/handshake2":
                self._handshake2(body)
            elif path == "/app/request":
                self._request(body)
            elif path == "/app":
                # The real `tapo` client library (mihai-dinculescu/tapo,
                # tapo/src/api/protocol/tapo_protocol.rs::is_aes_supported)
                # always probes the newer unencrypted AES/"securePassthrough"
                # transport first via an unauthenticated `component_nego`
                # call to POST /app, before deciding whether to fall back to
                # KLAP. Its fallback rule is exactly:
                #   error_code == 0        -> AES supported, use it
                #   error_code == 1003     -> AES NOT supported, use KLAP
                #   any other error_code   -> fatal (raises, no fallback)
                # A real P110 in "Third-Party Compatibility" (KLAP-only) mode
                # returns 1003 here — that's what makes a real client (and
                # this emulator) settle on KLAP, the only transport
                # firmware/main/tapo_protocol.c speaks. This code path only
                # matters for the `tapo` PyPI client's auto-detection; the
                # firmware itself never touches bare "/app".
                self._send_json(200, {"error_code": 1003})
            else:
                self.server.log.warning("Unhandled POST %s body=%r", self.path, body[:200])
                self._send(404)
        except Exception:
            self.server.log.exception("Unhandled error on %s", self.path)
            self._send(500)

    # --- KLAP handshake -------------------------------------------------------

    def _handshake1(self, local_seed: bytes) -> None:
        if len(local_seed) != 16:
            self.server.log.warning("handshake1: bad local_seed length %d", len(local_seed))
            self._send(400)
            return
        remote_seed = secrets.token_bytes(16)
        server_hash = handshake1_server_hash(local_seed, remote_seed, self.server.plug.auth_hash)
        token = secrets.token_hex(16)
        self.server.pending[token] = _PendingHandshake(local_seed, remote_seed)
        self._send(
            200,
            remote_seed + server_hash,
            {"Set-Cookie": f"TP_SESSIONID={token}; TIMEOUT=1440"},
        )

    def _handshake2(self, body: bytes) -> None:
        token = _extract_cookie_token(self.headers.get("Cookie"))
        pending = self.server.pending.pop(token, None) if token else None
        if not pending or len(body) != 32:
            self.server.log.warning("handshake2: no/expired pending handshake")
            self._send(403)
            return
        expected = handshake2_expected(
            pending.local_seed, pending.remote_seed, self.server.plug.auth_hash
        )
        if not hmac.compare_digest(expected, body):
            self.server.log.warning("handshake2: hash mismatch (wrong credentials?)")
            self._send(403)
            return
        session = KlapSession.derive(
            pending.local_seed, pending.remote_seed, self.server.plug.auth_hash
        )
        self.server.sessions[token] = session
        self.server.log.info("KLAP session established (token=%s...)", token[:8])
        self._send(200)

    # --- KLAP request/response -------------------------------------------------

    def _request(self, body: bytes) -> None:
        token = _extract_cookie_token(self.headers.get("Cookie"))
        session = self.server.sessions.get(token) if token else None
        if not session:
            self._send(403)  # no/unknown session -> client re-handshakes
            return

        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        try:
            seq = int(qs["seq"][0])
        except (KeyError, IndexError, ValueError):
            self._send(400)
            return

        try:
            plaintext = session.verify_and_open(seq, body)
        except ValueError as e:
            self.server.log.warning("request seq=%d rejected: %s", seq, e)
            self._send(403)
            return

        try:
            req = json.loads(plaintext)
        except json.JSONDecodeError:
            self._send(400)
            return

        method = req.get("method", "")
        params = req.get("params") or {}
        result = self.server.plug.dispatch(method, params)
        self.server.log.debug("%s(%s) -> %s", method, params, result)

        resp_body = session.seal(seq, json.dumps(result).encode("utf-8"))
        self._send(200, resp_body)


def make_server(plug: SimulatedPlug, store: Optional[StateStore] = None) -> PlugHTTPServer:
    return PlugHTTPServer((plug.cfg.host, plug.cfg.port), plug, store)
