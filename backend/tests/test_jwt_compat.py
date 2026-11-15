"""Regression: tokens minted by the retired python-jose library must still decode.

Production switched from python-jose to PyJWT. Every live session token was
signed by python-jose, so the new decode path has to accept them or the deploy
signs every user out. FIXTURE_TOKEN was minted once with python-jose (HS256,
claims sub, role, email, tv, iat, exp; no auth_time) and is checked in verbatim.
"""

import backend.services.auth as auth

FIXTURE_SECRET = "fixture-secret-for-jose-compat-test-0123456789"  # gitleaks:allow
# exp is in the year 2100 so this never expires.
FIXTURE_TOKEN = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJzdWIiOiI0MiIsInJvbGUiOiJkcml2ZXIiLCJlbWFpbCI6ImZpeHR1cmVAYW1waGl2ZS50ZXN0IiwidHYiOjAsImlhdCI6MTc2NzIyNTYwMCwiZXhwIjo0MTAyNDQ0ODAwfQ."
    "FNJ1YX9_ffXp_3Dhri55kjTx-L4TEwiCYw17fGEH6ho"
)


def test_python_jose_token_still_decodes(monkeypatch):
    monkeypatch.setattr(auth, "JWT_SECRET_KEY", FIXTURE_SECRET)
    payload = auth.decode_access_token(FIXTURE_TOKEN)
    assert payload is not None
    assert payload["sub"] == "42"
    assert payload["role"] == "driver"
    assert payload["tv"] == 0
    assert payload["exp"] == 4102444800


def test_python_jose_token_rejected_with_wrong_secret(monkeypatch):
    monkeypatch.setattr(auth, "JWT_SECRET_KEY", "a-different-secret-that-is-long-enough-0123456789")
    assert auth.decode_access_token(FIXTURE_TOKEN) is None
