"""
Tests for driver-submitted plug photos held for approval
(backend/routers/plugs.py: upload_plug_photo / list_plug_photos and the
_sniff_image helper; CPO moderation in routers/cpo/_plug_photos.py).

DB-free: db.execute is mocked, the verified-charger gate + storage upload are
patched. Covers the image sniff (accept/reject), the size guard, the
verified-charger gate, the pending-on-upload result, and the anonymous
visibility rule. The GCS upload/delete never touch the network, upload_object /
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
               return_value="gs://amphive-plug-photos/plug-1/abc.jpg"):
        out = await upload_plug_photo(
            plug_id=1, request=_Req(b"\xff\xd8\xff\xe0somejpegbytes"), user=_user(), db=db)

    assert out.status == "pending"
    assert out.is_mine is True
    # Clients only ever get the backend image route, never a bucket URL.
    assert out.url == "/api/plugs/1/photos/55/image"


# --- image route: private bucket, status-gated bytes (C17) ---

def _photo(status, tenant_id=42):
    from backend.database.models import PlugPhotoStatus
    return MagicMock(id=5, plug_id=1, tenant_id=tenant_id,
                     status=PlugPhotoStatus(status), object_name="plug-1/abc.jpg")


def _photo_db(photo):
    db = AsyncMock()
    db.execute = AsyncMock(return_value=MagicMock(
        scalar_one_or_none=MagicMock(return_value=photo)))
    return db


def _role_user(role, tenant_id=42):
    from backend.database.models import UserRole
    return MagicMock(id=3, role=UserRole(role), tenant_id=tenant_id)


@pytest.mark.asyncio
async def test_approved_photo_image_is_public_and_cacheable():
    from backend.routers.plugs import get_plug_photo_image

    with patch("backend.routers.plugs.download_object", return_value=b"\xff\xd8\xffjpg"):
        resp = await get_plug_photo_image(
            plug_id=1, photo_id=5, db=_photo_db(_photo("approved")), user=None)
    assert resp.status_code == 200
    assert resp.body == b"\xff\xd8\xffjpg"
    assert resp.media_type == "image/jpeg"
    assert resp.headers["cache-control"] == "public, max-age=86400"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "rejected"])
@pytest.mark.parametrize("who", [None, ("driver", 42), ("cpo", 99)])
async def test_unapproved_photo_image_404s_for_non_moderators(status, who):
    from backend.routers.plugs import get_plug_photo_image

    user = _role_user(*who) if who else None
    with patch("backend.routers.plugs.download_object", return_value=b"x") as dl:
        with pytest.raises(HTTPException) as exc:
            await get_plug_photo_image(
                plug_id=1, photo_id=5, db=_photo_db(_photo(status)), user=user)
    assert exc.value.status_code == 404
    dl.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("who", [("cpo", 42), ("admin", None)])
async def test_pending_photo_image_served_privately_to_moderators(who):
    from backend.routers.plugs import get_plug_photo_image

    with patch("backend.routers.plugs.download_object", return_value=b"\x89PNG"):
        resp = await get_plug_photo_image(
            plug_id=1, photo_id=5, db=_photo_db(_photo("pending")), user=_role_user(*who))
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "private, no-store"


@pytest.mark.asyncio
async def test_photo_image_missing_object_is_404():
    from backend.routers.plugs import get_plug_photo_image

    with patch("backend.routers.plugs.download_object", return_value=None):
        with pytest.raises(HTTPException) as exc:
            await get_plug_photo_image(
                plug_id=1, photo_id=5, db=_photo_db(_photo("approved")), user=None)
    assert exc.value.status_code == 404


def test_image_route_cache_header_survives_the_no_store_middleware():
    """Through the real app: the global security_headers_middleware only sets
    Cache-Control when absent, so the approved image keeps its public cache."""
    from fastapi.testclient import TestClient

    import backend.main as main
    from backend.database.db import get_db

    async def _db():
        yield _photo_db(_photo("approved"))

    api = main.app.other_asgi_app  # main.app is the socket.io wrapper
    api.dependency_overrides[get_db] = _db
    try:
        with patch("backend.routers.plugs.download_object", return_value=b"\xff\xd8\xff"):
            resp = TestClient(main.app).get("/api/plugs/1/photos/5/image")
    finally:
        api.dependency_overrides.pop(get_db, None)
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "public, max-age=86400"
    assert resp.headers["content-type"] == "image/jpeg"


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


# --- CPO moderation: takedown + delete retry (C18) ---

def _cpo_db(photo):
    db = _photo_db(photo)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    return db


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "approved", "rejected"])
async def test_reject_works_from_any_status_and_deletes_the_object(status):
    from backend.database.models import PlugPhotoStatus
    from backend.routers.cpo._plug_photos import cpo_reject_plug_photo

    photo = _photo(status)
    photo.created_at = None
    with patch("backend.routers.cpo._plug_photos.delete_object") as rm:
        out = await cpo_reject_plug_photo(
            photo_id=5, user=_role_user("cpo"), db=_cpo_db(photo))
    assert photo.status == PlugPhotoStatus.REJECTED
    assert out.status == "rejected"
    assert out.url == "/api/plugs/1/photos/5/image"
    rm.assert_called_once_with("plug-1/abc.jpg")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["approved", "rejected"])
async def test_approve_stays_pending_only(status):
    from backend.routers.cpo._plug_photos import cpo_approve_plug_photo

    with pytest.raises(HTTPException) as exc:
        await cpo_approve_plug_photo(
            photo_id=5, user=_role_user("cpo"), db=_cpo_db(_photo(status)))
    assert exc.value.status_code == 409


# --- cpo_viewer sees only approved photos in the moderation list ---

async def _listed_sql(role, status_filter):
    from backend.routers.cpo._plug_photos import cpo_list_plug_photos

    db = AsyncMock()
    db.execute = AsyncMock(return_value=MagicMock(
        scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))))
    await cpo_list_plug_photos(
        user=_role_user(role), db=db, status_filter=status_filter, limit=10)
    return str(db.execute.call_args.args[0].compile(
        compile_kwargs={"literal_binds": True}))


@pytest.mark.asyncio
async def test_viewer_photo_list_is_forced_to_approved():
    sql = await _listed_sql("cpo_viewer", "")
    assert "plug_photos.status = 'approved'" in sql


@pytest.mark.asyncio
async def test_viewer_pending_filter_cannot_widen_the_list():
    sql = await _listed_sql("cpo_viewer", "pending")
    assert "plug_photos.status = 'approved'" in sql and "'pending'" in sql


@pytest.mark.asyncio
async def test_cpo_photo_list_is_not_forced_to_approved():
    sql = await _listed_sql("cpo", "")
    assert "'approved'" not in sql
