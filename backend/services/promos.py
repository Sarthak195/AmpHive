"""
[Promo codes] Code generation, normalization and hashing for CPO-funded promo
codes (models.PromoCode). Routes: routers/cpo/_promos.py (issue, list,
disable) and routers/payments.py redeem_promo_code (POST /api/wallet/redeem).

Storage: only sha256(normalized code) is kept, like password-reset tokens, so
the plain code is shown once at creation and a database read does not hand
out working codes. 16 symbols from a 31-symbol unambiguous alphabet is about
79 bits; the last 4 symbols are kept as a display hint, leaving about 59 bits
unknown, which is far beyond online guessing under the redeem rate limits.
"""
import hashlib
import re
import secrets
from datetime import datetime
from typing import Optional

# Same unambiguous alphabet as gateway claim codes (routers/admin.py): no
# 0/O, 1/I/L, uppercase only.
CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
CODE_LEN = 16
HINT_LEN = 4

_SEPARATORS = re.compile(r"[\s-]+")


def generate_code() -> str:
    """A fresh random code in canonical (normalized, unformatted) form."""
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))


def format_code(code: str) -> str:
    """Display form, in groups of 4: ABCD-EFGH-JKMN-PQRS."""
    return "-".join(code[i:i + 4] for i in range(0, len(code), 4))


def normalize_code(raw: str) -> Optional[str]:
    """Canonical form of a code a person typed (case, spaces and dashes are
    ignored), or None if it cannot be a valid code at all."""
    code = _SEPARATORS.sub("", raw or "").upper()
    if len(code) != CODE_LEN or any(c not in CODE_ALPHABET for c in code):
        return None
    return code


def hash_code(code: str) -> str:
    """SHA-256 hex digest of a canonical code (the stored column)."""
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def promo_status(
    *,
    disabled_at: Optional[datetime],
    expires_at: Optional[datetime],
    redemption_count: int,
    max_redemptions: int,
    now: datetime,
) -> str:
    """One of 'disabled', 'expired', 'used_up', 'active' (first match wins)."""
    if disabled_at is not None:
        return "disabled"
    if expires_at is not None and expires_at <= now:
        return "expired"
    if redemption_count >= max_redemptions:
        return "used_up"
    return "active"
