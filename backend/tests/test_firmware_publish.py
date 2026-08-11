"""
Unit tests for services/firmware_publish.upload_firmware_image — the keyless
GCS uploader the admin firmware-upload endpoint uses.

These stub out google.auth.default (the metadata-server ADC lookup) and the
AuthorizedSession so nothing hits the network or needs GCP credentials: the
point is to pin the auth wiring, the JSON-upload URL, the octet-stream header,
and the error mapping.
"""
import pytest

from backend.services import firmware_publish
from backend.services.firmware_publish import FirmwarePublishError, upload_firmware_image


class _Resp:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


class _Session:
    """Records the single .post the uploader makes and returns a canned resp."""
    def __init__(self, resp):
        self._resp = resp
        self.calls = []

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append({"url": url, "data": data, "headers": headers, "timeout": timeout})
        return self._resp


def _stub_auth(monkeypatch, session):
    monkeypatch.setattr(
        firmware_publish.google.auth, "default", lambda scopes=None: ("creds", "proj")
    )
    monkeypatch.setattr(
        firmware_publish.google.auth.transport.requests,
        "AuthorizedSession",
        lambda credentials: session,
    )


def test_upload_posts_to_gcs_json_media_api(monkeypatch):
    session = _Session(_Resp(status_code=200))
    _stub_auth(monkeypatch, session)

    out = upload_firmware_image("amphive-gateway-2.4.0-direct.bin", b"\xe9payload")

    assert out == (
        "https://storage.googleapis.com/amphive-fw/amphive-gateway-2.4.0-direct.bin"
    )
    assert len(session.calls) == 1
    call = session.calls[0]
    assert call["url"] == (
        "https://storage.googleapis.com/upload/storage/v1/b/amphive-fw/o"
        "?uploadType=media&name=amphive-gateway-2.4.0-direct.bin"
    )
    assert call["data"] == b"\xe9payload"
    assert call["headers"]["Content-Type"] == "application/octet-stream"


def test_upload_honors_bucket_override(monkeypatch):
    session = _Session(_Resp(status_code=200))
    _stub_auth(monkeypatch, session)
    monkeypatch.setenv("FIRMWARE_GCS_BUCKET", "my-other-bucket")

    out = upload_firmware_image("amphive-gateway-9.9.9.bin", b"x")

    assert out == "https://storage.googleapis.com/my-other-bucket/amphive-gateway-9.9.9.bin"
    assert "/b/my-other-bucket/o" in session.calls[0]["url"]


def test_upload_non_2xx_raises_with_status_and_body(monkeypatch):
    session = _Session(_Resp(status_code=403, text="Forbidden: objectCreator missing"))
    _stub_auth(monkeypatch, session)

    with pytest.raises(FirmwarePublishError) as exc:
        upload_firmware_image("amphive-gateway-2.4.0-direct.bin", b"x")
    msg = str(exc.value)
    assert "403" in msg
    assert "objectCreator missing" in msg


def test_upload_credential_error_raises_firmware_publish_error(monkeypatch):
    def _boom(scopes=None):
        raise RuntimeError("metadata server unreachable")

    monkeypatch.setattr(firmware_publish.google.auth, "default", _boom)

    with pytest.raises(FirmwarePublishError) as exc:
        upload_firmware_image("amphive-gateway-2.4.0-direct.bin", b"x")
    assert "credentials" in str(exc.value).lower()


def test_upload_network_error_raises_firmware_publish_error(monkeypatch):
    class _DeadSession:
        def post(self, *a, **k):
            raise OSError("connection reset")

    _stub_auth(monkeypatch, _DeadSession())

    with pytest.raises(FirmwarePublishError) as exc:
        upload_firmware_image("amphive-gateway-2.4.0-direct.bin", b"x")
    assert "network error" in str(exc.value).lower()
