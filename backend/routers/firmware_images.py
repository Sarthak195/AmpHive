"""
Public (unauthenticated) OTA firmware image host —
GET /api/firmware/images/{filename}.

Public by design: OTA images were already world-readable on the public GCS
bucket (gs://amphive-fw) and are fetched by fielded gateways over plain https.
The device authenticates the IMAGE itself via its embedded ECDSA app signature
(signed-OTA / secure boot, firmware >= 1.4.0) — NOT the transport — so serving
the .bin without auth adds no new exposure while letting a gateway fetch an
admin-uploaded image (POST /api/admin/firmware-releases/upload) straight from
the backend's own origin instead of a hand-published bucket URL.

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
