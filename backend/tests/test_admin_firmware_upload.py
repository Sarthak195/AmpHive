"""
Tests for the raw-body firmware upload endpoint (POST
/api/admin/firmware-releases/upload) and the (retained, legacy) public image
host (GET /api/firmware/images/{filename}, routers/firmware_images.py).

As of the 2026-08-04 reliability backlog, the upload endpoint PUBLISHES the
image to the public GCS bucket gs://amphive-fw (keyless, via the VM service
account — services/firmware_publish.upload_firmware_image) and registers the
bucket URL, instead of self-hosting on the backend disk. These tests patch
`backend.routers.admin.upload_firmware_image` so they never touch the network,
assert the registered URL is the storage.googleapis.com bucket URL, and prove
nothing is written to local disk. The GET image host is retained for
back-compat with already-registered self-hosted URLs and is still covered.

DB-free direct-call style (same as test_admin_router.py): the endpoints are
imported and called directly with a mocked AsyncSession and a fake Request
whose async body() returns the crafted image bytes.
"""
import os
import struct
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from fastapi.responses import FileResponse

from backend.database.models import UserRole
from backend.routers.admin import admin_upload_firmware_release
from backend.routers.firmware_images import get_firmware_image
from backend.services.firmware_publish import FirmwarePublishError

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


def _patch_publish(monkeypatch):
    """Patch the GCS publisher the upload endpoint calls so no test hits the
    network. Returns the list of (filename, data) it was invoked with — empty
    if the endpoint 400s/413s before ever publishing."""
    calls = []

    def fake(filename, data):
        calls.append((filename, data))
        return f"https://storage.googleapis.com/amphive-fw/{filename}"

    monkeypatch.setattr("backend.routers.admin.upload_firmware_image", fake)
    return calls


def _inactive_release(release_id=42, version="2.5.0-direct"):
    r = MagicMock()
    r.id = release_id
    r.version = version
    r.url = "https://storage.googleapis.com/amphive-fw/old.bin"
    r.notes = "old notes"
    r.is_active = False
    r.created_at = datetime.now(timezone.utc)
    return r


# --- upload success (now publishes to GCS) ---------------------------------


@pytest.mark.asyncio
async def test_upload_success_publishes_to_gcs_and_registers(monkeypatch):
    calls = _patch_publish(monkeypatch)
    img = _make_image(version="2.4.0-direct")
    db = _db(_scalar_one_or_none(None))  # no duplicate version

    resp = await admin_upload_firmware_release(_request(img), _admin(), db, notes="hi")

    assert resp["version"] == "2.4.0-direct"
    assert resp["filename"] == "amphive-gateway-2.4.0-direct.bin"
    assert resp["url"] == (
        "https://storage.googleapis.com/amphive-fw/amphive-gateway-2.4.0-direct.bin"
    )
    assert resp["size_bytes"] == len(img)
    assert resp["is_active"] is True

    # The raw bytes were handed to the GCS publisher (not written to disk).
    assert calls == [("amphive-gateway-2.4.0-direct.bin", img)]

    release_added = db.add.call_args_list[0][0][0]
    assert type(release_added).__name__ == "FirmwareRelease"
    assert release_added.version == "2.4.0-direct"
    assert release_added.url == resp["url"]
    assert release_added.is_active is True
    audit_added = db.add.call_args_list[1][0][0]
    assert audit_added.action == "firmware_release.upload"
    assert audit_added.target_type == "firmware_release"


@pytest.mark.asyncio
async def test_upload_url_is_bucket_url_regardless_of_origin_env(monkeypatch):
    """The registered URL no longer derives from PUBLIC_API_ORIGIN/
    FRONTEND_ORIGIN — it's always the public GCS bucket URL."""
    calls = _patch_publish(monkeypatch)
    monkeypatch.setenv("PUBLIC_API_ORIGIN", "https://api.amphive.app")
    monkeypatch.setenv("FRONTEND_ORIGIN", "https://amphive.app")
    img = _make_image(version="3.0.0")
    db = _db(_scalar_one_or_none(None))

    resp = await admin_upload_firmware_release(_request(img), _admin(), db, notes=None)

    assert resp["url"] == "https://storage.googleapis.com/amphive-fw/amphive-gateway-3.0.0.bin"
    assert calls == [("amphive-gateway-3.0.0.bin", img)]


# --- re-upload of a DEACTIVATED version overwrites + reactivates ------------


@pytest.mark.asyncio
async def test_upload_reactivates_deactivated_version(monkeypatch):
    calls = _patch_publish(monkeypatch)
    img = _make_image(version="2.5.0-direct")
    existing = _inactive_release(release_id=42, version="2.5.0-direct")
    db = _db(_scalar_one_or_none(existing))  # duplicate found — but INACTIVE

    resp = await admin_upload_firmware_release(_request(img), _admin(), db, notes="fresh")

    assert resp["id"] == 42
    assert resp["is_active"] is True
    assert resp["url"] == (
        "https://storage.googleapis.com/amphive-fw/amphive-gateway-2.5.0-direct.bin"
    )
    # The in-place row was overwritten + reactivated.
    assert existing.is_active is True
    assert existing.url == resp["url"]
    assert existing.notes == "fresh"
    # Bytes were re-published to GCS.
    assert calls == [("amphive-gateway-2.5.0-direct.bin", img)]
    # No new FirmwareRelease row inserted — only the audit row.
    assert db.add.call_count == 1
    audit_added = db.add.call_args_list[0][0][0]
    assert audit_added.action == "firmware_release.reupload_reactivate"


@pytest.mark.asyncio
async def test_upload_active_duplicate_version_400_publishes_nothing(monkeypatch):
    calls = _patch_publish(monkeypatch)
    img = _make_image(version="2.4.0-direct")
    active = MagicMock()
    active.is_active = True
    active.version = "2.4.0-direct"
    db = _db(_scalar_one_or_none(active))  # duplicate found — ACTIVE

    with pytest.raises(HTTPException) as exc:
        await admin_upload_firmware_release(_request(img), _admin(), db, notes=None)
    assert exc.value.status_code == 400
    assert "already registered" in exc.value.detail
    # An active duplicate is rejected BEFORE any GCS publish or DB write.
    assert calls == []
    db.add.assert_not_called()


# --- GCS publish failure surfaces as 502 -----------------------------------


@pytest.mark.asyncio
async def test_upload_gcs_publish_failure_502(monkeypatch):
    def boom(filename, data):
        raise FirmwarePublishError("bucket denied: objectCreator missing")

    monkeypatch.setattr("backend.routers.admin.upload_firmware_image", boom)
    img = _make_image(version="2.4.0-direct")
    db = _db(_scalar_one_or_none(None))

    with pytest.raises(HTTPException) as exc:
        await admin_upload_firmware_release(_request(img), _admin(), db, notes=None)
    assert exc.value.status_code == 502
    assert "storage bucket" in exc.value.detail.lower()
    assert "bucket denied" in exc.value.detail
    # A publish failure means nothing gets registered.
    db.add.assert_not_called()


# --- upload guards (all fail BEFORE the publish) ---------------------------


@pytest.mark.asyncio
async def test_upload_not_esp32_image_400(monkeypatch):
    calls = _patch_publish(monkeypatch)
    img = bytearray(_make_image())
    img[0] = 0x00  # not the 0xE9 image magic byte
    db = _db()
    with pytest.raises(HTTPException) as exc:
        await admin_upload_firmware_release(_request(bytes(img)), _admin(), db, notes=None)
    assert exc.value.status_code == 400
    assert "esp32 app image" in exc.value.detail.lower()
    assert calls == []


@pytest.mark.asyncio
async def test_upload_bad_descriptor_magic_400(monkeypatch):
    calls = _patch_publish(monkeypatch)
    img = _make_image(desc_magic=0x00000000)
    db = _db()
    with pytest.raises(HTTPException) as exc:
        await admin_upload_firmware_release(_request(img), _admin(), db, notes=None)
    assert exc.value.status_code == 400
    assert "descriptor magic" in exc.value.detail.lower()
    assert calls == []


@pytest.mark.asyncio
async def test_upload_wrong_project_name_400(monkeypatch):
    calls = _patch_publish(monkeypatch)
    img = _make_image(project_name="some-other-app")
    db = _db()
    with pytest.raises(HTTPException) as exc:
        await admin_upload_firmware_release(_request(img), _admin(), db, notes=None)
    assert exc.value.status_code == 400
    assert "some-other-app" in exc.value.detail
    assert calls == []  # nothing published


@pytest.mark.asyncio
async def test_upload_malformed_version_in_image_400(monkeypatch):
    """A version parsed from the image that fails the reused
    AdminFirmwareReleaseCreateRequest version regex is a 400 — surfaced before
    any GCS publish."""
    calls = _patch_publish(monkeypatch)
    img = _make_image(version="2.4")  # not X.Y.Z
    db = _db()
    with pytest.raises(HTTPException) as exc:
        await admin_upload_firmware_release(_request(img), _admin(), db, notes=None)
    assert exc.value.status_code == 400
    assert "2.4" in exc.value.detail
    assert calls == []


@pytest.mark.asyncio
async def test_upload_oversized_413(monkeypatch):
    calls = _patch_publish(monkeypatch)
    big = b"\xe9" + b"\x00" * (4 * 1024 * 1024 + 1)
    db = _db()
    with pytest.raises(HTTPException) as exc:
        await admin_upload_firmware_release(_request(big), _admin(), db, notes=None)
    assert exc.value.status_code == 413
    assert calls == []


# --- retained public image host (legacy back-compat) -----------------------


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
