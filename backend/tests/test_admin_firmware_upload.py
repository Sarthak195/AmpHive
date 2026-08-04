"""
Tests for the raw-body firmware upload endpoint (POST
/api/admin/firmware-releases/upload) and its public image host
(GET /api/firmware/images/{filename}, routers/firmware_images.py) —
feat/admin-dashboard completion.

DB-free direct-call style (same as test_admin_router.py): the endpoints are
imported and called directly with a mocked AsyncSession and a fake Request
whose async body() returns the crafted image bytes.
"""
import os
import struct
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from fastapi.responses import FileResponse

from backend.database.models import UserRole
from backend.routers.admin import admin_upload_firmware_release
from backend.routers.firmware_images import get_firmware_image

# esp_app_desc_t magic word (little-endian uint32 @ offset 32 of an app image).
_DESC_MAGIC = 0xABCD5432


def _admin(user_id=1, email="admin@amphive.test"):
    u = MagicMock()
    u.id = user_id
    u.email = email
    u.role = UserRole.ADMIN
    u.tenant_id = None
    return u


def _scalar_one_or_none(value):
    r = MagicMock()
    r.scalar_one_or_none.return_value = value
    return r


def _db(*results):
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=list(results))
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.rollback = AsyncMock()
    return db


def _request(body_bytes):
    req = MagicMock()
    req.body = AsyncMock(return_value=body_bytes)
    return req


def _make_image(version="2.4.0-direct", project_name="amphive-gateway",
                desc_magic=_DESC_MAGIC, first_byte=0xE9, total_len=320):
    """Craft a minimal ESP-IDF app image: 0xE9 magic byte, the esp_app_desc_t
    descriptor magic (LE uint32) at offset 32, then NUL-padded version[32] @48
    and project_name[32] @80, padded out past the 288-byte minimum."""
    buf = bytearray(total_len)
    if total_len >= 1:
        buf[0] = first_byte
    if total_len >= 36:
        struct.pack_into("<I", buf, 32, desc_magic)
    vb = version.encode("ascii")
    buf[48:48 + len(vb)] = vb
    pb = project_name.encode("ascii")
    buf[80:80 + len(pb)] = pb
    return bytes(buf)


def _std_env(monkeypatch, tmp_path, origin="https://amphive.app"):
    monkeypatch.setenv("FIRMWARE_IMAGE_DIR", str(tmp_path))
    monkeypatch.delenv("PUBLIC_API_ORIGIN", raising=False)
    if origin is None:
        monkeypatch.delenv("FRONTEND_ORIGIN", raising=False)
    else:
        monkeypatch.setenv("FRONTEND_ORIGIN", origin)


# --- upload success ---------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_success_writes_file_and_registers(monkeypatch, tmp_path):
    _std_env(monkeypatch, tmp_path)
    img = _make_image(version="2.4.0-direct")
    db = _db(_scalar_one_or_none(None))  # no duplicate version

    resp = await admin_upload_firmware_release(_request(img), _admin(), db, notes="hi")

    assert resp["version"] == "2.4.0-direct"
    assert resp["filename"] == "amphive-gateway-2.4.0-direct.bin"
    assert resp["url"] == (
        "https://amphive.app/api/firmware/images/amphive-gateway-2.4.0-direct.bin"
    )
    assert resp["size_bytes"] == len(img)
    assert resp["is_active"] is True

    written = tmp_path / "amphive-gateway-2.4.0-direct.bin"
    assert written.is_file()
    assert written.read_bytes() == img
    # No stray .tmp left behind (atomic write did os.replace).
    assert not (tmp_path / "amphive-gateway-2.4.0-direct.bin.tmp").exists()

    release_added = db.add.call_args_list[0][0][0]
    assert type(release_added).__name__ == "FirmwareRelease"
    assert release_added.version == "2.4.0-direct"
    assert release_added.url == resp["url"]
    assert release_added.is_active is True
    audit_added = db.add.call_args_list[1][0][0]
    assert audit_added.action == "firmware_release.upload"
    assert audit_added.target_type == "firmware_release"


@pytest.mark.asyncio
async def test_upload_prefers_public_api_origin(monkeypatch, tmp_path):
    monkeypatch.setenv("FIRMWARE_IMAGE_DIR", str(tmp_path))
    monkeypatch.setenv("PUBLIC_API_ORIGIN", "https://api.amphive.app")
    monkeypatch.setenv("FRONTEND_ORIGIN", "https://amphive.app")
    img = _make_image(version="3.0.0")
    db = _db(_scalar_one_or_none(None))

    resp = await admin_upload_firmware_release(_request(img), _admin(), db, notes=None)

    assert resp["url"].startswith("https://api.amphive.app/api/firmware/images/")


# --- upload guards ----------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_not_esp32_image_400(monkeypatch, tmp_path):
    _std_env(monkeypatch, tmp_path)
    img = bytearray(_make_image())
    img[0] = 0x00  # not the 0xE9 image magic byte
    db = _db()
    with pytest.raises(HTTPException) as exc:
        await admin_upload_firmware_release(_request(bytes(img)), _admin(), db, notes=None)
    assert exc.value.status_code == 400
    assert "esp32 app image" in exc.value.detail.lower()


@pytest.mark.asyncio
async def test_upload_bad_descriptor_magic_400(monkeypatch, tmp_path):
    _std_env(monkeypatch, tmp_path)
    img = _make_image(desc_magic=0x00000000)
    db = _db()
    with pytest.raises(HTTPException) as exc:
        await admin_upload_firmware_release(_request(img), _admin(), db, notes=None)
    assert exc.value.status_code == 400
    assert "descriptor magic" in exc.value.detail.lower()


@pytest.mark.asyncio
async def test_upload_wrong_project_name_400(monkeypatch, tmp_path):
    _std_env(monkeypatch, tmp_path)
    img = _make_image(project_name="some-other-app")
    db = _db()
    with pytest.raises(HTTPException) as exc:
        await admin_upload_firmware_release(_request(img), _admin(), db, notes=None)
    assert exc.value.status_code == 400
    assert "some-other-app" in exc.value.detail
    # nothing was stored
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_upload_duplicate_version_400_writes_nothing(monkeypatch, tmp_path):
    _std_env(monkeypatch, tmp_path)
    img = _make_image(version="2.4.0-direct")
    db = _db(_scalar_one_or_none(MagicMock()))  # duplicate found
    with pytest.raises(HTTPException) as exc:
        await admin_upload_firmware_release(_request(img), _admin(), db, notes=None)
    assert exc.value.status_code == 400
    assert "already registered" in exc.value.detail
    # dup check precedes the file write — store dir stays empty
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_upload_oversized_413(monkeypatch, tmp_path):
    _std_env(monkeypatch, tmp_path)
    big = b"\xe9" + b"\x00" * (4 * 1024 * 1024 + 1)
    db = _db()
    with pytest.raises(HTTPException) as exc:
        await admin_upload_firmware_release(_request(big), _admin(), db, notes=None)
    assert exc.value.status_code == 413


@pytest.mark.asyncio
async def test_upload_unset_origin_500(monkeypatch, tmp_path):
    _std_env(monkeypatch, tmp_path, origin=None)
    img = _make_image()
    db = _db()
    with pytest.raises(HTTPException) as exc:
        await admin_upload_firmware_release(_request(img), _admin(), db, notes=None)
    assert exc.value.status_code == 500
    assert "FRONTEND_ORIGIN" in exc.value.detail


@pytest.mark.asyncio
async def test_upload_http_dev_origin_rejected_400(monkeypatch, tmp_path):
    """A plain http:// origin fails the reused AdminFirmwareReleaseCreateRequest
    https-only URL rule — the endpoint must surface it as a 400."""
    _std_env(monkeypatch, tmp_path, origin="http://localhost:8000")
    img = _make_image(version="2.4.0-direct")
    db = _db()
    with pytest.raises(HTTPException) as exc:
        await admin_upload_firmware_release(_request(img), _admin(), db, notes=None)
    assert exc.value.status_code == 400
    assert "https" in exc.value.detail.lower()


# --- public image host ------------------------------------------------------


@pytest.mark.asyncio
async def test_firmware_image_served(monkeypatch, tmp_path):
    monkeypatch.setenv("FIRMWARE_IMAGE_DIR", str(tmp_path))
    fname = "amphive-gateway-2.4.0-direct.bin"
    (tmp_path / fname).write_bytes(b"\xe9firmware-bytes")

    resp = await get_firmware_image(fname)

    assert isinstance(resp, FileResponse)
    assert resp.media_type == "application/octet-stream"
    assert os.path.isfile(resp.path)
    assert os.path.basename(resp.path) == fname


@pytest.mark.asyncio
async def test_firmware_image_missing_file_404(monkeypatch, tmp_path):
    monkeypatch.setenv("FIRMWARE_IMAGE_DIR", str(tmp_path))
    with pytest.raises(HTTPException) as exc:
        await get_firmware_image("amphive-gateway-9.9.9.bin")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_firmware_image_bad_name_404(monkeypatch, tmp_path):
    monkeypatch.setenv("FIRMWARE_IMAGE_DIR", str(tmp_path))
    # Names that don't match the minted shape are refused outright.
    for bad in ["passwd", "amphive-gateway-2.4.0.txt", "evil.bin", "../secret.bin"]:
        with pytest.raises(HTTPException) as exc:
            await get_firmware_image(bad)
        assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_firmware_image_traversal_name_404(monkeypatch, tmp_path):
    """A regex-matching name that can't resolve to a real file in the store
    dir still 404s (the charset forbids '/', and the dirname guard holds)."""
    monkeypatch.setenv("FIRMWARE_IMAGE_DIR", str(tmp_path))
    with pytest.raises(HTTPException) as exc:
        await get_firmware_image("amphive-gateway-..-..-etc-passwd.bin")
    assert exc.value.status_code == 404
