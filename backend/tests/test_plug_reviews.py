"""
Tests for verified-charger plug reviews (backend/routers/plugs.py:
create_or_update_plug_review / list_plug_reviews / delete_my_plug_review, and
the _author_display / _has_completed_charge helpers).

DB-free: db.execute is mocked and the collaborators (ensure_plug_group_access,
_has_completed_charge, _plug_is_public) are patched. This covers the
verified-charger gate, the anonymous-visibility rule, the PII-masking of the
public author label, and the request-schema validation — the parts that live in
Python. The SQL (unique-index upsert, GROUP BY aggregate) is exercised by the
DB-gated integration tests in CI.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from backend.database.models import PlugStatus


def _plug(id=1, group_id=None, gateway_id="gw-1"):
    p = MagicMock(id=id, group_id=group_id, gateway_id=gateway_id,
                  status=PlugStatus.AVAILABLE)
    p.name = "Test Plug"
    return p


def _user(id=7, full_name="Ada Lovelace", email="ada@example.com"):
    u = MagicMock(id=id, full_name=full_name, email=email)
    return u


# --- _author_display: never leak a full email or user id ---

def test_author_display_prefers_first_name():
    from backend.routers.plugs import _author_display
    assert _author_display("Ada Lovelace", "ada@example.com") == "Ada"


def test_author_display_masks_email_when_no_name():
    from backend.routers.plugs import _author_display
    out = _author_display("", "sarthak@example.com")
    assert out == "sar***"
    assert "@" not in out and "example.com" not in out


def test_author_display_masks_short_local_part():
    from backend.routers.plugs import _author_display
    assert _author_display(None, "ab@x.com") == "a***"


# --- verified-charger gate ---

@pytest.mark.asyncio
async def test_review_rejected_without_completed_charge():
    from backend.routers.plugs import create_or_update_plug_review
    from backend.schemas import PlugReviewCreateRequest

    db = AsyncMock()
    db.execute = AsyncMock(return_value=MagicMock(
        scalar_one_or_none=MagicMock(return_value=_plug())))

    with patch("backend.routers.plugs.ensure_plug_group_access",
               AsyncMock(return_value=("Grp", False))), \
         patch("backend.routers.plugs._has_completed_charge",
               AsyncMock(return_value=False)):
        with pytest.raises(HTTPException) as exc:
            await create_or_update_plug_review(
                plug_id=1,
                req=PlugReviewCreateRequest(rating=5, body="Great charger"),
                user=_user(), db=db,
            )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_review_created_for_verified_charger_is_mine():
    from backend.routers.plugs import create_or_update_plug_review
    from backend.schemas import PlugReviewCreateRequest

    plug = _plug()
    gateway = MagicMock(id="gw-1", tenant_id=42)
    # First execute -> plug lookup; second -> gateway lookup.
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        MagicMock(scalar_one_or_none=MagicMock(return_value=plug)),
        MagicMock(scalar_one_or_none=MagicMock(return_value=gateway)),
    ])
    db.add = MagicMock()  # sync in real code
    db.commit = AsyncMock()
    # Prod's refresh assigns the SERIAL id; emulate that so the response
    # (which requires a non-null id) validates.
    async def _refresh(obj):
        obj.id = 123
    db.refresh = AsyncMock(side_effect=_refresh)

    with patch("backend.routers.plugs.ensure_plug_group_access",
               AsyncMock(return_value=("Grp", False))), \
         patch("backend.routers.plugs._has_completed_charge",
               AsyncMock(return_value=True)):
        out = await create_or_update_plug_review(
            plug_id=1,
            req=PlugReviewCreateRequest(rating=4, body="Solid"),
            user=_user(), db=db,
        )

    assert out.rating == 4
    assert out.is_mine is True
    assert out.author_display == "Ada"  # never the raw email


# --- anonymous visibility rule on list ---

@pytest.mark.asyncio
async def test_anon_cannot_list_reviews_of_private_plug():
    from backend.routers.plugs import list_plug_reviews

    db = AsyncMock()
    db.execute = AsyncMock(return_value=MagicMock(
        scalar_one_or_none=MagicMock(return_value=_plug(group_id=99))))

    with patch("backend.routers.plugs._plug_is_public",
               AsyncMock(return_value=False)):
        with pytest.raises(HTTPException) as exc:
            await list_plug_reviews(plug_id=1, db=db, user=None)
    # 404 (not 403) — never reveal a private plug's existence to anon.
    assert exc.value.status_code == 404


# --- request schema validation ---

def test_review_request_rejects_out_of_range_rating():
    import pydantic

    from backend.schemas import PlugReviewCreateRequest

    for bad in (0, 6, -1):
        with pytest.raises(pydantic.ValidationError):
            PlugReviewCreateRequest(rating=bad)


def test_review_request_allows_rating_only():
    from backend.schemas import PlugReviewCreateRequest
    r = PlugReviewCreateRequest(rating=5)
    assert r.rating == 5 and r.body is None
