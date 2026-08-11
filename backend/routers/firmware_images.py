"""
Public (unauthenticated) OTA firmware image host —
GET /api/firmware/images/{filename}.

LEGACY / BACK-COMPAT ONLY (2026-08-04). New admin uploads (POST
/api/admin/firmware-releases/upload) now publish the image to the public bucket
gs://amphive-fw and register the bucket URL — the self-hosted path this router
serves proved unreliable for real devices (the ESP32's TLS download breaks
mid-stream) and non-persistent (deploy.ps1 doesn't ship the firmware_images
volume, so uploads vanished on redeploy). See services/firmware_publish.py and
docs/TODO.md "2026-08-04 reliability & integration backlog". This endpoint is
retained so any release row still pointing at an already-registered self-hosted
`/api/firmware/images/...` URL keeps resolving; nothing new is written here.

Public by design: OTA images were already world-readable on the public GCS
bucket (gs://amphive-fw) and are fetched by fielded gateways over plain https.
The device authenticates the IMAGE itself via its embedded ECDSA app signature
(signed-OTA / secure boot, firmware >= 1.4.0) — NOT the transport — so serving
the .bin without auth adds no new exposure.

This router deliberately lives OUTSIDE routers/admin.py: everything in that
module is gated by require_role("admin"), and this endpoint must be reachable
by an unauthenticated gateway.
"""
import os
import re

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

router = APIRouter()

# The exact filename shape the upload endpoint mints
# (amphive-gateway-<version>.bin). Constraining the charset here is the first
# line of defense against path traversal — no slashes, no "..".
_FILENAME_RE = re.compile(r"amphive-gateway-[0-9A-Za-z._-]+\.bin")


@router.get("/api/firmware/images/{filename}")
async def get_firmware_image(filename: str):
    """Stream a stored OTA image by filename. 404 for a name that doesn't match
    the minted shape, escapes the store dir, or has no backing file."""
    if not _FILENAME_RE.fullmatch(filename):
        raise HTTPException(status_code=404, detail="Firmware image not found.")

    # Resolve both the store dir and the target, then require the target to sit
    # DIRECTLY in the store dir (defense in depth on top of the regex — catches
    # any symlink/normalization surprise).
    store_dir = os.path.realpath(os.getenv("FIRMWARE_IMAGE_DIR", "data/firmware-images"))
    path = os.path.realpath(os.path.join(store_dir, filename))
    if os.path.dirname(path) != store_dir or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Firmware image not found.")

    return FileResponse(path, media_type="application/octet-stream")
