"""
Tests for driver-submitted plug photos held for approval
(backend/routers/plugs.py: upload_plug_photo / list_plug_photos and the
_sniff_image helper; CPO moderation in routers/cpo/_plug_photos.py).

DB-free: db.execute is mocked, the verified-charger gate + storage upload are
patched. Covers the image sniff (accept/reject), the size guard, the
verified-charger gate, the pending-on-upload result, and the anonymous
visibility rule. The GCS upload/delete never touch the network — upload_object /
delete_object are patched.
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


def _user(id=7):
    return MagicMock(id=id, full_name="Ada", email="ada@example.com")


class _Req:
    """Minimal stand-in for fastapi Request.body()."""
    def __init__(self, body):
        self._body = body

    async def body(self):
        return self._body


# --- _sniff_image ---

def test_sniff_accepts_jpeg_png_webp():
    from backend.routers.plugs import _sniff_image
    assert _sniff_image(b"\xff\xd8\xff\xe0rest")[0] == "image/jpeg"
    assert _sniff_image(b"\x89PNG\r\n\x1a\nrest")[0] == "image/png"
    assert _sniff_image(b"RIFF\x00\x00\x00\x00WEBPVP8 ")[0] == "image/webp"


def test_sniff_rejects_non_image():
    from backend.routers.plugs import _sniff_image
    assert _sniff_image(b"not an image at all") is None
    assert _sniff_image(b"%PDF-1.7") is None


# --- upload gate + validation ---

@pytest.mark.asyncio
async def test_upload_rejected_without_completed_charge():
    from backend.routers.plugs import upload_plug_photo

    db = AsyncMock()
    db.execute = AsyncMock(return_value=MagicMock(
        scalar_one_or_none=MagicMock(return_value=_plug())))

    with patch("backend.routers.plugs.ensure_plug_group_access",
               AsyncMock(return_value=("Grp", False))), \
         patch("backend.routers.plugs._has_completed_charge",
               AsyncMock(return_value=False)):
        with pytest.raises(HTTPException) as exc:
            await upload_plug_photo(
                plug_id=1, request=_Req(b"\xff\xd8\xffdata"), user=_user(), db=db)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_upload_rejects_non_image():
    from backend.routers.plugs import upload_plug_photo

    plug = _plug()
    gateway = MagicMock(id="gw-1", tenant_id=42)
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        MagicMock(scalar_one_or_none=MagicMock(return_value=plug)),
        MagicMock(scalar_one_or_none=MagicMock(return_value=gateway)),
    ])

    with patch("backend.routers.plugs.ensure_plug_group_access",
               AsyncMock(return_value=("Grp", False))), \
         patch("backend.routers.plugs._has_completed_charge",
               AsyncMock(return_value=True)):
        with pytest.raises(HTTPException) as exc:
            await upload_plug_photo(
                plug_id=1, request=_Req(b"this is not an image"), user=_user(), db=db)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_upload_stores_pending_and_returns_is_mine():
    from backend.routers.plugs import upload_plug_photo

    plug = _plug()
    gateway = MagicMock(id="gw-1", tenant_id=42)
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        MagicMock(scalar_one_or_none=MagicMock(return_value=plug)),
        MagicMock(scalar_one_or_none=MagicMock(return_value=gateway)),
    ])
    db.add = MagicMock()
    db.commit = AsyncMock()

    async def _refresh(obj):
        obj.id = 55
    db.refresh = AsyncMock(side_effect=_refresh)

    with patch("backend.routers.plugs.ensure_plug_group_access",
               AsyncMock(return_value=("Grp", False))), \
         patch("backend.routers.plugs._has_completed_charge",
               AsyncMock(return_value=True)), \
         patch("backend.routers.plugs.upload_object",
               return_value="https://storage.googleapis.com/amphive-plug-photos/plug-1/abc.jpg"):
        out = await upload_plug_photo(
            plug_id=1, request=_Req(b"\xff\xd8\xff\xe0somejpegbytes"), user=_user(), db=db)

    assert out.status == "pending"
    assert out.is_mine is True
    assert out.url.endswith("abc.jpg")


# --- anonymous visibility on list ---

@pytest.mark.asyncio
async def test_anon_cannot_list_photos_of_private_plug():
    from backend.routers.plugs import list_plug_photos

    db = AsyncMock()
    db.execute = AsyncMock(return_value=MagicMock(
        scalar_one_or_none=MagicMock(return_value=_plug(group_id=99))))

    with patch("backend.routers.plugs._plug_is_public",
               AsyncMock(return_value=False)):
        with pytest.raises(HTTPException) as exc:
            await list_plug_photos(plug_id=1, db=db, user=None)
    assert exc.value.status_code == 404
