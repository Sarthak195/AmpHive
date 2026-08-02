"""Pure unit tests for the KLAP v2 crypto primitives — no network. These pin
down the exact byte-level formulas firmware/main/tapo_protocol.c implements
(auth_hash, session key/iv/sig derivation, seal/unseal), independent of any
HTTP transport.
"""

import hashlib

import crypto
import pytest


def test_auth_hash_matches_reference_formula():
    # auth_hash = SHA256(SHA1(email) || SHA1(password)) — tapo_protocol.c tapo_init().
    email, password = "a@b.com", "hunter2"
    expected = hashlib.sha256(
        hashlib.sha1(email.encode()).digest() + hashlib.sha1(password.encode()).digest()
    ).digest()
    assert crypto.auth_hash(email, password) == expected
    assert len(crypto.auth_hash(email, password)) == 32


def test_auth_hash_is_deterministic_and_credential_sensitive():
    h1 = crypto.auth_hash("a@b.com", "pw1")
    h2 = crypto.auth_hash("a@b.com", "pw1")
    h3 = crypto.auth_hash("a@b.com", "pw2")
    assert h1 == h2
    assert h1 != h3


def test_session_derive_matches_reference_formula():
    local_seed = b"\x01" * 16
    remote_seed = b"\x02" * 16
    ah = crypto.auth_hash("a@b.com", "pw")
    s = local_seed + remote_seed + ah

    session = crypto.KlapSession.derive(local_seed, remote_seed, ah)

    assert session.key == hashlib.sha256(b"lsk" + s).digest()[:16]
    iv_seed = hashlib.sha256(b"iv" + s).digest()
    assert session.iv == iv_seed[:12]
    assert session.sig == hashlib.sha256(b"ldk" + s).digest()[:28]


def test_session_derive_rejects_wrong_seed_length():
    ah = crypto.auth_hash("a@b.com", "pw")
    with pytest.raises(ValueError):
        crypto.KlapSession.derive(b"short", b"\x02" * 16, ah)


def test_handshake1_server_hash_is_what_a_real_client_verifies():
    local_seed = b"\x03" * 16
    remote_seed = b"\x04" * 16
    ah = crypto.auth_hash("a@b.com", "pw")
    server_hash = crypto.handshake1_server_hash(local_seed, remote_seed, ah)
    # A real client independently computes the exact same thing to verify —
    # see klap_probe.py's `sha256(local_seed + remote_seed + ah2)`.
    assert server_hash == hashlib.sha256(local_seed + remote_seed + ah).digest()


def test_handshake2_expected_matches_reference_formula():
    local_seed = b"\x05" * 16
    remote_seed = b"\x06" * 16
    ah = crypto.auth_hash("a@b.com", "pw")
    expected = crypto.handshake2_expected(local_seed, remote_seed, ah)
    assert expected == hashlib.sha256(remote_seed + local_seed + ah).digest()


def test_seal_then_verify_and_open_round_trips():
    ah = crypto.auth_hash("a@b.com", "pw")
    session = crypto.KlapSession.derive(b"\x07" * 16, b"\x08" * 16, ah)
    plaintext = b'{"method":"get_device_info"}'

    wire = session.seal(session.seq0 + 1, plaintext)
    opened = session.verify_and_open(session.seq0 + 1, wire)

    assert opened == plaintext


def test_two_directions_produce_identical_bytes_for_same_seq_and_plaintext():
    """The whole point of a single KlapSession class serving both directions:
    whoever calls seal() for a given seq gets byte-identical output — this is
    what lets tapo_protocol.c decrypt a response with the exact same full_iv
    it built to encrypt the matching request."""
    ah = crypto.auth_hash("a@b.com", "pw")
    client_session = crypto.KlapSession.derive(b"\x09" * 16, b"\x0a" * 16, ah)
    server_session = crypto.KlapSession.derive(b"\x09" * 16, b"\x0a" * 16, ah)

    assert client_session == server_session
    assert client_session.seal(5, b"payload") == server_session.seal(5, b"payload")


def test_verify_and_open_rejects_tampered_signature():
    ah = crypto.auth_hash("a@b.com", "pw")
    session = crypto.KlapSession.derive(b"\x0b" * 16, b"\x0c" * 16, ah)
    wire = bytearray(session.seal(1, b'{"method":"get_device_info"}'))
    wire[0] ^= 0xFF  # corrupt one byte of the signature
    with pytest.raises(ValueError):
        session.verify_and_open(1, bytes(wire))


def test_verify_and_open_rejects_wrong_seq():
    ah = crypto.auth_hash("a@b.com", "pw")
    session = crypto.KlapSession.derive(b"\x0d" * 16, b"\x0e" * 16, ah)
    wire = session.seal(1, b'{"method":"get_device_info"}')
    with pytest.raises(ValueError):
        session.verify_and_open(2, wire)  # signature was computed for seq=1


def test_verify_and_open_rejects_malformed_body():
    ah = crypto.auth_hash("a@b.com", "pw")
    session = crypto.KlapSession.derive(b"\x0f" * 16, b"\x10" * 16, ah)
    with pytest.raises(ValueError):
        session.verify_and_open(1, b"too short")


def test_wrong_credentials_produce_a_different_session():
    local_seed, remote_seed = b"\x11" * 16, b"\x12" * 16
    right = crypto.KlapSession.derive(local_seed, remote_seed, crypto.auth_hash("a@b.com", "pw"))
    wrong = crypto.KlapSession.derive(local_seed, remote_seed, crypto.auth_hash("a@b.com", "WRONG"))
    assert right.key != wrong.key
    assert right.seal(1, b"x") != wrong.seal(1, b"x")
