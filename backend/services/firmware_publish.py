"""
Keyless firmware-image publisher — upload an OTA `.bin` to the public GCS
bucket ``gs://amphive-fw`` using the VM's own service-account identity.

Why this module exists / why it's shaped this way
-------------------------------------------------
The admin firmware-UPLOAD endpoint (POST /api/admin/firmware-releases/upload)
used to self-host the image on the backend's disk and serve it from
GET /api/firmware/images/{file}. That path is unreliable for real devices — the
ESP32's TLS download breaks mid-stream — and non-persistent: deploy.ps1 doesn't
ship the ``firmware_images`` volume/env, so an uploaded image vanishes on the
next redeploy. Every historically-successful OTA fetched from ``gs://amphive-fw``,
so uploads now publish there instead (docs/TODO.md "2026-08-04 reliability &
integration backlog").

Auth is KEYLESS. The backend runs on a GCE VM whose attached compute service
account can mint access tokens straight from the instance metadata server
(Application Default Credentials). This is the SAME identity
``deploy/scripts/backup_db.sh`` already uses to write DB backups — no key file,
no ``GOOGLE_APPLICATION_CREDENTIALS``, nothing mounted. We deliberately do NOT
add ``google-cloud-storage`` (a heavy dependency with a large transitive tree —
a poor fit for the RAM-constrained e2-micro and the pinned lockfile); instead we
POST the bytes to the GCS JSON upload API over an ``AuthorizedSession`` built
from those ADC credentials, using only ``google-auth`` + ``requests`` — both
already required by backend/requirements.txt for "Sign in with Google".

The bucket is public-read under uniform bucket-level access (see
deploy/docs/ota_image_publishing.md), so there is NO per-object ACL step: once
the object exists it is fetchable at
``https://storage.googleapis.com/amphive-fw/<name>`` by any device.

INFRA PREREQUISITE (one-time): the compute service account needs
``roles/storage.objectCreator`` on ``gs://amphive-fw`` — today it only holds
that on ``gs://amphive-db-backups``. The exact gcloud command is in
deploy/docs/ota_image_publishing.md. The VM's ``devstorage.read_write`` access
scope is already set.

Kept small, synchronous, and easily mockable: the admin router imports
``upload_firmware_image`` and offloads it with ``asyncio.to_thread`` so the
blocking HTTP call doesn't stall the event loop, and tests patch
``backend.routers.admin.upload_firmware_image`` (or this module's function)
without ever hitting the network.
"""
import os
import urllib.parse

import google.auth
import google.auth.transport.requests

# Default target bucket (overridable via FIRMWARE_GCS_BUCKET). Public-read under
# uniform bucket-level access — see the module docstring.
DEFAULT_BUCKET = "amphive-fw"

# Read/write scope for the metadata-server token: object create needs write.
_UPLOAD_SCOPE = "https://www.googleapis.com/auth/devstorage.read_write"

# Truncate the GCS error body we echo into the exception/HTTP detail so a huge
# or hostile response can't blow up a log line or a 502 detail string.
_MAX_ERR_CHARS = 500

# Bound the upload so a stalled connection can't hang the worker forever.
_UPLOAD_TIMEOUT_SEC = 120


class FirmwarePublishError(RuntimeError):
    """Publishing a firmware image to the storage bucket failed — credential
    acquisition, the HTTP POST itself, or a non-2xx GCS response. The upload
    endpoint maps this to a 502."""


def upload_firmware_image(filename: str, data: bytes) -> str:
    """Upload ``data`` to ``gs://<bucket>/<filename>`` and return its public
    https URL (``https://storage.googleapis.com/<bucket>/<filename>``).

    Synchronous and blocking — call it OFF the event loop (e.g. via
    ``asyncio.to_thread``). Uses keyless Application Default Credentials from
    the GCE metadata server; no key file, nothing mounted. Raises
    ``FirmwarePublishError`` on any failure (missing/invalid ADC, a network
    error, or a non-2xx GCS status), with the GCS status + a truncated response
    body included for diagnosis.
    """
    bucket = os.getenv("FIRMWARE_GCS_BUCKET", DEFAULT_BUCKET)

    try:
        credentials, _project = google.auth.default(scopes=[_UPLOAD_SCOPE])
        session = google.auth.transport.requests.AuthorizedSession(credentials)
    except Exception as exc:  # DefaultCredentialsError, RefreshError, ...
        raise FirmwarePublishError(
            f"Could not obtain GCP credentials to publish to gs://{bucket}: {exc}"
        ) from exc

    # GCS JSON API "media" upload: the object body IS the request body, its
    # name is the `name` query param. quote(safe="") so any odd char in the
    # bucket/name is percent-encoded rather than breaking out of the path/query.
    upload_url = (
        "https://storage.googleapis.com/upload/storage/v1/b/"
        f"{urllib.parse.quote(bucket, safe='')}/o"
        f"?uploadType=media&name={urllib.parse.quote(filename, safe='')}"
    )

    try:
        resp = session.post(
            upload_url,
            data=data,
            headers={"Content-Type": "application/octet-stream"},
            timeout=_UPLOAD_TIMEOUT_SEC,
        )
    except Exception as exc:  # requests.RequestException (network/DNS/timeout)
        raise FirmwarePublishError(
            f"Network error uploading firmware to gs://{bucket}/{filename}: {exc}"
        ) from exc

    if not (200 <= resp.status_code < 300):
        body = (resp.text or "")[:_MAX_ERR_CHARS]
        raise FirmwarePublishError(
            f"GCS upload of {filename} to gs://{bucket} failed "
            f"(HTTP {resp.status_code}): {body}"
        )

    return f"https://storage.googleapis.com/{bucket}/{filename}"
