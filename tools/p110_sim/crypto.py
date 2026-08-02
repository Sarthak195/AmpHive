"""KLAP v2 crypto primitives — a server-side mirror of the AmpHive firmware's
Tapo driver (firmware/main/tapo_protocol.c) and the already-hardware-validated
probe script (tools/klap_probe.py).

KLAP ("Krypto for Local Area Protocol") is TP-Link's local encryption scheme
for Tapo/Kasa devices: SHA1/SHA256 + AES-128-CBC, no RSA. This module
implements only the "v2" variant (SHA1/SHA256 auth hash) — the one
tapo_protocol.c speaks and klap_probe.py confirmed a real P110 (fw 1.1.3)
uses when "Third-Party Compatibility" is enabled. The legacy v1 (MD5) variant
is out of scope: AmpHive's firmware never speaks it.

Both sides of a KLAP session — the real client (firmware or the `tapo`
library) and this emulator acting as the plug — derive IDENTICAL key
material from the same (local_seed, remote_seed, auth_hash) triple, so a
single `KlapSession` implementation works for both directions: whichever
side calls `seal()` for a given seq produces the same bytes the other side
would if it played the opposite role. That symmetry is what tapo_protocol.c
implicitly relies on when it decrypts a response using the exact same
`full_iv` it built to encrypt the matching request (see klap_request_once()).
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# Big-endian SIGNED 4-byte seq, matching firmware's be32() and
# klap_probe.py's `PACK_L = struct.Struct(">l").pack` (both are the standard
# 4-byte size once a byte-order prefix is given to Python's struct module).
_PACK_SEQ = struct.Struct(">i").pack
_UNPACK_SEQ = struct.Struct(">i").unpack


def sha256(*parts: bytes) -> bytes:
    h = hashlib.sha256()
    for p in parts:
        h.update(p)
    return h.digest()


def sha1(data: bytes) -> bytes:
    return hashlib.sha1(data).digest()


def auth_hash(email: str, password: str) -> bytes:
    """auth_hash = SHA256(SHA1(email) || SHA1(password)).

    Mirrors tapo_init() in tapo_protocol.c and auth_hash_v2() in
    klap_probe.py. This is the shared secret both the emulator (standing in
    for the plug's registered Tapo account) and the real client must derive
    identically for the handshake1 server_hash check to pass.
    """
    return sha256(sha1(email.encode("utf-8")), sha1(password.encode("utf-8")))


@dataclass(frozen=True)
class KlapSession:
    """Derived per-handshake session material.

    Mirrors tapo_protocol.c's klap_handshake() key/iv/sig derivation
    exactly:
        S   = local_seed || remote_seed || auth_hash
        key = SHA256("lsk" || S)[0:16]
        ivf = SHA256("iv"  || S); iv = ivf[0:12]; seq0 = be32_signed(ivf[28:32])
        sig = SHA256("ldk" || S)[0:28]
    """

    key: bytes
    iv: bytes
    sig: bytes
    seq0: int

    @classmethod
    def derive(cls, local_seed: bytes, remote_seed: bytes, auth_hash_: bytes) -> "KlapSession":
        if len(local_seed) != 16 or len(remote_seed) != 16:
            raise ValueError("KLAP seeds must be exactly 16 bytes")
        s = local_seed + remote_seed + auth_hash_
        key = sha256(b"lsk", s)[:16]
        iv_seed = sha256(b"iv", s)
        iv = iv_seed[:12]
        (seq0,) = _UNPACK_SEQ(iv_seed[28:32])
        sig = sha256(b"ldk", s)[:28]
        return cls(key=key, iv=iv, sig=sig, seq0=seq0)

    def full_iv(self, seq: int) -> bytes:
        return self.iv + _PACK_SEQ(seq)

    def seal(self, seq: int, plaintext: bytes) -> bytes:
        """AES-128-CBC/PKCS7-encrypt `plaintext` at `seq`, return the full
        wire body: signature(32) || ciphertext. Used for BOTH directions —
        a request encrypted by a client and a response encrypted by this
        emulator use the exact same formula (see module docstring)."""
        padder = padding.PKCS7(128).padder()
        padded = padder.update(plaintext) + padder.finalize()
        encryptor = Cipher(algorithms.AES(self.key), modes.CBC(self.full_iv(seq))).encryptor()
        ct = encryptor.update(padded) + encryptor.finalize()
        signature = sha256(self.sig, _PACK_SEQ(seq), ct)
        return signature + ct

    def verify_and_open(self, seq: int, body: bytes) -> bytes:
        """Split `body` into signature(32) || ciphertext, verify the
        signature, decrypt, and strip PKCS7 padding. Raises ValueError on
        any mismatch (bad signature, bad padding, truncated body)."""
        if len(body) < 32 or (len(body) - 32) % 16 != 0:
            raise ValueError(f"malformed KLAP request body ({len(body)} bytes)")
        sig_in, ct = body[:32], body[32:]
        expected_sig = sha256(self.sig, _PACK_SEQ(seq), ct)
        # Constant-time compare avoids a timing oracle on the signature check.
        if not _consteq(expected_sig, sig_in):
            raise ValueError("KLAP request signature mismatch")
        decryptor = Cipher(algorithms.AES(self.key), modes.CBC(self.full_iv(seq))).decryptor()
        padded = decryptor.update(ct) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        return unpadder.update(padded) + unpadder.finalize()


def _consteq(a: bytes, b: bytes) -> bool:
    import hmac

    return hmac.compare_digest(a, b)


def handshake1_server_hash(local_seed: bytes, remote_seed: bytes, auth_hash_: bytes) -> bytes:
    """What the emulator (as the plug) returns from POST /app/handshake1:
    SHA256(local_seed || remote_seed || auth_hash) — the client recomputes
    this itself and only proceeds if it matches, proving both sides agree
    on the Tapo account credentials."""
    return sha256(local_seed, remote_seed, auth_hash_)


def handshake2_expected(local_seed: bytes, remote_seed: bytes, auth_hash_: bytes) -> bytes:
    """What the emulator expects the client's POST /app/handshake2 body to
    equal: SHA256(remote_seed || local_seed || auth_hash)."""
    return sha256(remote_seed, local_seed, auth_hash_)
