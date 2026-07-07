"""
Tests for generate_unique_access_code (extracted from the 3× duplicated
inline loops in the CPO group routes — TD#10).
"""
import string
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.routers.cpo import generate_unique_access_code

ALPHABET = set(string.ascii_uppercase + string.digits)


def _lookup_result(found):
    r = MagicMock()
    r.scalar_one_or_none.return_value = found
    return r


@pytest.mark.asyncio
async def test_returns_8_char_uppercase_alnum_code():
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_lookup_result(None))

    code = await generate_unique_access_code(db)

    assert len(code) == 8
    assert set(code) <= ALPHABET


@pytest.mark.asyncio
async def test_retries_on_collision():
    """A code already taken by another group must be discarded and regenerated."""
    db = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[_lookup_result(MagicMock()), _lookup_result(None)]
    )

    code = await generate_unique_access_code(db)

    assert len(code) == 8
    assert db.execute.await_count == 2
