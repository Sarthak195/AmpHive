"""
Keyless plug-photo storage — upload/delete driver-submitted charger photos on a
public GCS bucket (``gs://amphive-plug-photos`` by default) using the VM's own
service-account identity.

Shaped exactly like ``services/firmware_publish.py`` (read its docstring for the
full rationale): keyless Application Default Credentials from the GCE metadata
server, the GCS JSON API over an ``AuthorizedSession``, and deliberately NO
``google-cloud-storage`` / ``boto3`` dependency — only ``google-auth`` +
``requests``, both already required. The bucket is public-read under uniform
bucket-level access, so there is no per-object ACL step: once an object exists
it is fetchable at ``https://storage.googleapis.com/<bucket>/<name>``.

Difference from firmware_publish: this stores arbitrary image bytes (so
``content_type`` is passed through, not hard-coded to octet-stream) and also
supports DELETE, because a CPO REJECTING a pending photo must remove the object,
not just hide the row.

INFRA PREREQUISITE (one-time, operator-gated): the compute service account needs
``roles/storage.objectAdmin`` (create + delete) on ``gs://amphive-plug-photos``.
See deploy/docs/plug_photo_publishing.md. Kept small, synchronous, and mockable:
routers offload it with ``asyncio.to_thread`` and tests patch these functions
without touching the network.
"""
import os
import urllib.parse

import google.auth
import google.auth.transport.requests

DEFAULT_BUCKET = "amphive-plug-photos"

# Create + delete both need the read_write scope.
_SCOPE = "https://www.googleapis.com/auth/devstorage.read_write"

_MAX_ERR_CHARS = 500
_TIMEOUT_SEC = 120


class PhotoPublishError(RuntimeError):
    """Uploading/deleting a plug photo to/from the storage bucket failed —
    credential acquisition, the HTTP call itself, or a non-2xx GCS response.
    The router maps this to a 502."""


def _bucket() -> str:
    return os.getenv("PLUG_PHOTO_GCS_BUCKET", DEFAULT_BUCKET)


def _session():
    try:
        credentials, _project = google.auth.default(scopes=[_SCOPE])
        return google.auth.transport.requests.AuthorizedSession(credentials)
    except Exception as exc:  # DefaultCredentialsError, RefreshError, ...
        raise PhotoPublishError(
            f"Could not obtain GCP credentials for gs://{_bucket()}: {exc}"
        ) from exc


def upload_object(filename: str, data: bytes, content_type: str) -> str:
    """Upload ``data`` to ``gs://<bucket>/<filename>`` with the given
    ``content_type`` and return its public https URL. Synchronous/blocking —
    call OFF the event loop (``asyncio.to_thread``). Raises ``PhotoPublishError``
    on any failure."""
    bucket = _bucket()
    session = _session()

    upload_url = (
        "https://storage.googleapis.com/upload/storage/v1/b/"
        f"{urllib.parse.quote(bucket, safe='')}/o"
        f"?uploadType=media&name={urllib.parse.quote(filename, safe='')}"
    )
    try:
        resp = session.post(
            upload_url,
            data=data,
            headers={"Content-Type": content_type},
            timeout=_TIMEOUT_SEC,
        )
    except Exception as exc:  # requests.RequestException (network/DNS/timeout)
        raise PhotoPublishError(
            f"Network error uploading photo to gs://{bucket}/{filename}: {exc}"
        ) from exc

    if not (200 <= resp.status_code < 300):
        body = (resp.text or "")[:_MAX_ERR_CHARS]
        raise PhotoPublishError(
            f"GCS upload of {filename} to gs://{bucket} failed "
            f"(HTTP {resp.status_code}): {body}"
        )

    return f"https://storage.googleapis.com/{bucket}/{filename}"


def delete_object(filename: str) -> None:
    """Delete ``gs://<bucket>/<filename>``. Synchronous/blocking — call OFF the
    event loop. A 404 is treated as success (already gone). Raises
    ``PhotoPublishError`` on any other failure."""
    bucket = _bucket()
    session = _session()

    delete_url = (
        "https://storage.googleapis.com/storage/v1/b/"
        f"{urllib.parse.quote(bucket, safe='')}/o/"
        f"{urllib.parse.quote(filename, safe='')}"
    )
    try:
        resp = session.delete(delete_url, timeout=_TIMEOUT_SEC)
    except Exception as exc:
        raise PhotoPublishError(
            f"Network error deleting photo gs://{bucket}/{filename}: {exc}"
        ) from exc

    if resp.status_code == 404:
        return  # already gone — deleting a rejected photo is idempotent
    if not (200 <= resp.status_code < 300):
        body = (resp.text or "")[:_MAX_ERR_CHARS]
        raise PhotoPublishError(
            f"GCS delete of {filename} from gs://{bucket} failed "
            f"(HTTP {resp.status_code}): {body}"
        )
