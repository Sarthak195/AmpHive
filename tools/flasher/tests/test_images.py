"""Unit tests for chip->image mapping and resolution (amphive_flasher.images).

Filesystem access is limited to pytest's tmp_path fixture; network access in
download tests is fully mocked (urllib.request.urlopen is patched) — nothing
here ever touches a real network or GitHub.
"""

import json
from io import BytesIO
from unittest.mock import patch

import pytest
from amphive_flasher.images import (
    FlasherError,
    download_latest_image,
    find_release_asset_url,
    image_filename_for_chip,
    known_chip_names,
    normalize_chip_name,
    resolve_image,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("ESP32-C3", "esp32c3"),
        ("esp32c3", "esp32c3"),
        ("esp32_c3", "esp32c3"),
        ("  ESP32  ", "esp32"),
        ("Esp32", "esp32"),
    ],
)
def test_normalize_chip_name(raw, expected):
    assert normalize_chip_name(raw) == expected


def test_known_chip_names_includes_both_current_families():
    assert set(known_chip_names()) == {"esp32", "esp32c3"}


def test_image_filename_for_chip_known():
    assert image_filename_for_chip("ESP32-C3") == "amphive-gateway-esp32c3-merged.bin"


def test_image_filename_for_chip_unknown_raises_friendly_error():
    with pytest.raises(FlasherError, match="esp32s3"):
        image_filename_for_chip("esp32s3")


def test_resolve_image_explicit_bin_path(tmp_path):
    custom = tmp_path / "custom.bin"
    custom.write_bytes(b"fake-image")
    resolved = resolve_image("esp32c3", images_dir=tmp_path / "unused", explicit_bin=custom)
    assert resolved.path == custom
    assert resolved.source == "explicit"


def test_resolve_image_explicit_bin_missing_raises(tmp_path):
    with pytest.raises(FlasherError, match="not found"):
        resolve_image("esp32c3", images_dir=tmp_path, explicit_bin=tmp_path / "nope.bin")


def test_resolve_image_finds_local_file_by_convention(tmp_path):
    (tmp_path / "amphive-gateway-esp32c3-merged.bin").write_bytes(b"fake-image")
    resolved = resolve_image("esp32c3", images_dir=tmp_path)
    assert resolved.source == "local"
    assert resolved.path.name == "amphive-gateway-esp32c3-merged.bin"


def test_resolve_image_missing_raises_with_actionable_message(tmp_path):
    with pytest.raises(FlasherError) as exc_info:
        resolve_image("esp32c3", images_dir=tmp_path)
    msg = str(exc_info.value)
    assert "--bin" in msg
    assert "download" in msg.lower()


def test_find_release_asset_url_match():
    release = {
        "tag_name": "v2.3.0",
        "assets": [
            {"name": "amphive-gateway-esp32c3-merged.bin", "browser_download_url": "https://x/1.bin"},
            {"name": "amphive-gateway-esp32-merged.bin", "browser_download_url": "https://x/2.bin"},
        ],
    }
    assert find_release_asset_url("esp32c3", release) == "https://x/1.bin"


def test_find_release_asset_url_no_match_raises():
    release = {"tag_name": "v2.3.0", "assets": []}
    with pytest.raises(FlasherError, match="v2.3.0"):
        find_release_asset_url("esp32c3", release)


class _FakeResponse:
    def __init__(self, payload: bytes):
        self._buf = BytesIO(payload)

    def read(self):
        return self._buf.read()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_download_latest_image_success(tmp_path):
    release_json = json.dumps(
        {
            "tag_name": "v2.3.0",
            "assets": [
                {
                    "name": "amphive-gateway-esp32c3-merged.bin",
                    "browser_download_url": "https://example.invalid/asset.bin",
                }
            ],
        }
    ).encode()

    responses = [_FakeResponse(release_json), _FakeResponse(b"fake-binary-content")]

    with patch("amphive_flasher.images.urllib.request.urlopen", side_effect=responses):
        resolved = download_latest_image("esp32c3", tmp_path)

    assert resolved.source == "downloaded"
    assert resolved.path.read_bytes() == b"fake-binary-content"


def test_download_latest_image_network_error_is_friendly(tmp_path):
    import urllib.error

    with patch(
        "amphive_flasher.images.urllib.request.urlopen",
        side_effect=urllib.error.URLError("no internet"),
    ):
        with pytest.raises(FlasherError, match="download"):
            download_latest_image("esp32c3", tmp_path)


def test_download_latest_image_no_matching_asset_is_friendly(tmp_path):
    release_json = json.dumps({"tag_name": "v2.3.0", "assets": []}).encode()
    with patch(
        "amphive_flasher.images.urllib.request.urlopen",
        return_value=_FakeResponse(release_json),
    ):
        with pytest.raises(FlasherError, match="v2.3.0"):
            download_latest_image("esp32c3", tmp_path)
