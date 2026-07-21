"""
Password hashing after the passlib -> pyca/bcrypt swap (audit L6: passlib is
abandoned upstream and incompatible with bcrypt>=4.1).

All DB-free. The load-bearing guarantee is COMPATIBILITY: every hash passlib
wrote before the swap ($2b$12$...) must keep verifying, including passlib's
silent truncation of passwords at bcrypt's 72-byte limit — pyca/bcrypt raises
there instead, so services/auth.py truncates explicitly to match.
"""
from backend.services.auth import hash_password, verify_password

# Generated ONCE with the removed stack (passlib 1.7.4 CryptContext(["bcrypt"])
# + bcrypt 4.0.1) for the password "correct-horse-battery" — a frozen sample of
# what every pre-swap row in users.hashed_password looks like.
_PASSLIB_ERA_HASH = "$2b$12$W4Jvi4jWkAN1QxcwlmU0Re.L1qitq8Ur7Danri4HBj5NxRdCE9Faa"


def test_round_trip_and_reject():
    h = hash_password("s3cret-pw")
    assert h.startswith("$2b$12$")            # same prefix/cost passlib produced
    assert verify_password("s3cret-pw", h) is True
    assert verify_password("wrong-pw", h) is False


def test_passlib_era_hash_still_verifies():
    assert verify_password("correct-horse-battery", _PASSLIB_ERA_HASH) is True
    assert verify_password("wrong-horse", _PASSLIB_ERA_HASH) is False


def test_long_passwords_truncate_at_72_bytes_like_passlib():
    # 100 chars: hashing must not raise (pyca/bcrypt alone would), and — the
    # bcrypt property passlib exposed — bytes past 72 don't participate, so a
    # password differing only after byte 72 still verifies. A user who
    # registered a >72-byte password under passlib can still log in.
    long_pw = "x" * 100
    h = hash_password(long_pw)
    assert verify_password(long_pw, h) is True
    assert verify_password("x" * 72, h) is True
    assert verify_password("x" * 72 + "different-tail", h) is True
    assert verify_password("x" * 71, h) is False


def test_malformed_stored_hash_is_no_match_not_error():
    # A corrupt/non-bcrypt DB value must fail the login, not 500 it.
    assert verify_password("anything", "not-a-bcrypt-hash") is False
    assert verify_password("anything", "") is False
