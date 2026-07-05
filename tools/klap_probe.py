"""
Throwaway KLAP protocol probe for the AmpHive ESP32 Tapo driver (Phase 0).

Talks *directly to the local Tapo P110* (same category as local_tapo_test.py) to
empirically confirm the exact KLAP flow before it's ported to C in
firmware/main/tapo_protocol.c.

It:
  1. tries the KLAP handshake on http://<ip>:80/app/handshake1
  2. auto-detects v2 (SHA1/SHA256) vs legacy v1 (MD5) by which auth hash the
     server_hash matches
  3. completes handshake2, derives the session key/iv/sig, and does an encrypted
     get_energy_usage + get_device_info round-trip
  4. prints the decrypted result JSON so we can lock the C field mapping

Deps: requests, cryptography (both confirmed present).
Usage: python tools/klap_probe.py [ip] [email] [password]
"""
import sys
import struct
import hashlib
import secrets
import json

import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding

# Credentials come from argv or the environment — never hardcoded.
import os
IP = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("TAPO_PLUG_IP", "192.168.1.4")
EMAIL = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("TAPO_EMAIL", "")
PASSWORD = sys.argv[3] if len(sys.argv) > 3 else os.environ.get("TAPO_PASSWORD", "")
if not EMAIL or not PASSWORD:
    sys.exit("Pass email/password as argv or set TAPO_EMAIL / TAPO_PASSWORD env vars.")

BASE = f"http://{IP}/app"
PACK_L = struct.Struct(">l").pack  # big-endian signed 4-byte


def sha1(b):
    return hashlib.sha1(b).digest()


def sha256(b):
    return hashlib.sha256(b).digest()


def md5(b):
    return hashlib.md5(b).digest()


def auth_hash_v2(email, pw):
    return sha256(sha1(email.encode()) + sha1(pw.encode()))


def auth_hash_v1(email, pw):
    return md5(md5(email.encode()) + md5(pw.encode()))


class KlapSession:
    """Mirror of python-kasa KlapEncryptionSession (key/iv/sig derivation)."""

    def __init__(self, local_seed, remote_seed, auth_hash):
        self._key = sha256(b"lsk" + local_seed + remote_seed + auth_hash)[:16]
        ivf = sha256(b"iv" + local_seed + remote_seed + auth_hash)
        self._iv = ivf[:12]
        self._seq = int.from_bytes(ivf[-4:], "big", signed=True)
        self._sig = sha256(b"ldk" + local_seed + remote_seed + auth_hash)[:28]

    def _full_iv(self, seq):
        return self._iv + PACK_L(seq)

    def encrypt(self, plaintext_bytes):
        self._seq += 1
        padder = padding.PKCS7(128).padder()
        data = padder.update(plaintext_bytes) + padder.finalize()
        enc = Cipher(algorithms.AES(self._key), modes.CBC(self._full_iv(self._seq))).encryptor()
        ct = enc.update(data) + enc.finalize()
        signature = sha256(self._sig + PACK_L(self._seq) + ct)
        return signature + ct, self._seq

    def decrypt(self, msg):
        ct = msg[32:]  # first 32 bytes are the response signature
        dec = Cipher(algorithms.AES(self._key), modes.CBC(self._full_iv(self._seq))).decryptor()
        data = dec.update(ct) + dec.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        return unpadder.update(data) + unpadder.finalize()


def main():
    print(f"== KLAP probe -> {IP}  (account: {EMAIL}) ==\n")
    local_seed = secrets.token_bytes(16)

    # ---- handshake1 ----
    r1 = requests.post(f"{BASE}/handshake1", data=local_seed, timeout=5)
    print(f"handshake1: HTTP {r1.status_code}, {len(r1.content)} bytes body")
    if r1.status_code != 200 or len(r1.content) != 48:
        print("  !! Not a 48-byte KLAP handshake. Plug may use RSA passthrough or HTTPS:4433.")
        print(f"  body[:80]={r1.content[:80]!r}")
        sys.exit(1)

    remote_seed = r1.content[:16]
    server_hash = r1.content[16:]

    # ---- detect protocol variant ----
    ah2 = auth_hash_v2(EMAIL, PASSWORD)
    ah1 = auth_hash_v1(EMAIL, PASSWORD)
    if sha256(local_seed + remote_seed + ah2) == server_hash:
        variant, auth_hash = "v2 (SHA1/SHA256)", ah2
        hs2_body = sha256(remote_seed + local_seed + auth_hash)
    elif sha256(local_seed + ah1) == server_hash:
        variant, auth_hash = "v1 (MD5)", ah1
        hs2_body = sha256(remote_seed + auth_hash)
    else:
        print("  !! server_hash matched NEITHER v2 nor v1 -> credentials wrong or")
        print("     unknown variant. Check Tapo email/password + Third-Party Compatibility.")
        sys.exit(1)
    print(f"  -> credentials OK, protocol = KLAP {variant}")

    session_cookie = r1.cookies.get("TP_SESSIONID")
    timeout_cookie = r1.cookies.get("TIMEOUT")
    print(f"  -> TP_SESSIONID={session_cookie}  TIMEOUT={timeout_cookie}")
    cookies = {"TP_SESSIONID": session_cookie}  # deliberately NOT echoing TIMEOUT

    # ---- handshake2 ----
    r2 = requests.post(f"{BASE}/handshake2", data=hs2_body, cookies=cookies, timeout=5)
    print(f"handshake2: HTTP {r2.status_code}")
    if r2.status_code != 200:
        print("  !! handshake2 rejected."); sys.exit(1)

    sess = KlapSession(local_seed, remote_seed, auth_hash)

    def klap_request(method, params=None):
        inner = {"method": method}
        if params is not None:
            inner["params"] = params
        payload, seq = sess.encrypt(json.dumps(inner).encode())
        rr = requests.post(f"{BASE}/request", params={"seq": seq}, data=payload,
                           cookies=cookies, timeout=5)
        if rr.status_code != 200:
            raise RuntimeError(f"request '{method}' HTTP {rr.status_code}")
        return json.loads(sess.decrypt(rr.content))

    # ---- real telemetry round-trips ----
    print("\n-- get_device_info (FULL — hunting for the real thermal/overheat field) --")
    info = klap_request("get_device_info")
    print("  result:", json.dumps(info.get("result", {}), indent=2))

    print("\n-- get_current_power (does this method exist? what scale?) --")
    try:
        cp = klap_request("get_current_power")
        print("  result:", json.dumps(cp.get("result", {}), indent=2), " error_code=", cp.get("error_code"))
    except Exception as e:
        print("  not available:", e)

    print("\n-- get_energy_usage --")
    energy = klap_request("get_energy_usage")
    print(f"  error_code={energy.get('error_code')}")
    print("  result:", json.dumps(energy.get("result", {}), indent=2))

    # ---- WRITE PATH: toggle ON, read power, toggle back OFF ----
    start_state = info.get("result", {}).get("device_on")
    print(f"\n-- WRITE PATH test (start device_on={start_state}) --")
    print("  set_device_info device_on=True ...")
    on = klap_request("set_device_info", {"device_on": True})
    print(f"    error_code={on.get('error_code')}")
    import time
    time.sleep(2)
    di = klap_request("get_device_info").get("result", {})
    eu = klap_request("get_energy_usage").get("result", {})
    print(f"    -> device_on now = {di.get('device_on')}   current_power(energy_usage) = {eu.get('current_power')}")
    try:
        cp2 = klap_request("get_current_power").get("result", {})
        print(f"    -> get_current_power = {cp2}")
    except Exception:
        pass
    print("  set_device_info device_on=False (restoring) ...")
    off = klap_request("set_device_info", {"device_on": False})
    print(f"    error_code={off.get('error_code')}")

    print("\nKLAP probe SUCCESS. Field mapping for the C port is confirmed above.")


if __name__ == "__main__":
    main()
