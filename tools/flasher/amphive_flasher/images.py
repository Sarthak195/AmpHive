"""Chip -> firmware image resolution.

The flasher never compiles firmware (see README). It only ever writes a
prebuilt, pre-merged single-binary image, produced ahead of time by a
maintainer with the full ESP-IDF toolchain (see ``scripts/build-merged-image.ps1``)
and either shipped in a ``firmware-images/`` folder next to the EXE, pointed
at explicitly with ``--bin``, or fetched from a GitHub release.

Kept free of network/filesystem side effects except where explicitly noted,
so the mapping and path-resolution logic is unit-testable without touching
the network or a real firmware-images directory.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# Chip identifiers as esptool reports them (ESPLoader.CHIP_NAME, e.g.
# "ESP32-C3") normalized to lowercase/no-hyphen -> the merged image filename
# a maintainer is expected to produce for that chip. See
# scripts/build-merged-image.ps1.
CHIP_IMAGE_FILENAMES: dict[str, str] = {
    "esp32": "amphive-gateway-esp32-merged.bin",
    "esp32c3": "amphive-gateway-esp32c3-merged.bin",
}

GITHUB_REPO = "Sarthak195/AmpHive"
GITHUB_API_LATEST_RELEASE = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"


class FlasherError(Exception):
    """A user-facing error: message is meant to be printed as-is, no traceback."""


def normalize_chip_name(name: str) -> str:
    """"ESP32-C3" / "esp32-c3" / "esp32c3" -> "esp32c3"."""
    return name.strip().lower().replace("-", "").replace("_", "")


def known_chip_names() -> list[str]:
    return sorted(CHIP_IMAGE_FILENAMES)


def image_filename_for_chip(chip: str) -> str:
    """Raises FlasherError with a friendly message for an unsupported chip."""
    key = normalize_chip_name(chip)
    try:
        return CHIP_IMAGE_FILENAMES[key]
    except KeyError:
        supported = ", ".join(known_chip_names())
        raise FlasherError(
            f"The connected board reports chip type '{chip}', which this tool doesn't have "
            f"an AmpHive image for yet (supported: {supported}). If this is a new AmpHive "
            f"gateway revision, ask the maintainer to build and publish an image for it."
        ) from None


@dataclass(frozen=True)
class ResolvedImage:
    path: Path
    source: str  # "explicit" | "local" | "downloaded"


def resolve_image(
    chip: str,
    images_dir: Path,
    explicit_bin: Path | None = None,
) -> ResolvedImage:
    """Find the merged image to flash for ``chip``, *without* downloading.

    Order: explicit --bin override, then <images_dir>/<expected filename>.
    Raises FlasherError (with next-step guidance) if neither exists.
    """
    if explicit_bin is not None:
        if not explicit_bin.is_file():
            raise FlasherError(f"--bin file not found: {explicit_bin}")
        return ResolvedImage(path=explicit_bin, source="explicit")

    filename = image_filename_for_chip(chip)
    candidate = images_dir / filename
    if candidate.is_file():
        return ResolvedImage(path=candidate, source="local")

    raise FlasherError(
        f"No firmware image found for {chip} at {candidate}.\n"
        f"  - Place '{filename}' in {images_dir}, or\n"
        f"  - Re-run with --bin <path-to-image.bin>, or\n"
        f"  - Let this tool try to download the latest release (unless you passed --no-download)."
    )


def _http_get_json(url: str, timeout: float = 10.0) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https GitHub API URL
        return json.load(resp)


def find_release_asset_url(chip: str, release_json: dict) -> str:
    """Pick the asset matching this chip's expected filename out of a GitHub
    'latest release' API response. Raises FlasherError if not present."""
    filename = image_filename_for_chip(chip)
    for asset in release_json.get("assets", []):
        if asset.get("name") == filename:
            url = asset.get("browser_download_url")
            if url:
                return url
    tag = release_json.get("tag_name", "latest")
    raise FlasherError(
        f"The GitHub release '{tag}' doesn't have an asset named '{filename}'. "
        f"A maintainer needs to attach it - see tools/flasher/README.md."
    )


def download_latest_image(chip: str, images_dir: Path) -> ResolvedImage:
    """Best-effort download of the latest release's image for ``chip``.

    Any failure (network down, repo private/unreachable, no releases yet,
    no matching asset) raises FlasherError with a plain-language message -
    never lets an urllib traceback reach the user.
    """
    filename = image_filename_for_chip(chip)
    try:
        release = _http_get_json(GITHUB_API_LATEST_RELEASE)
        asset_url = find_release_asset_url(chip, release)
        images_dir.mkdir(parents=True, exist_ok=True)
        dest = images_dir / filename
        req = urllib.request.Request(asset_url, headers={"Accept": "application/octet-stream"})
        with urllib.request.urlopen(req, timeout=60.0) as resp:  # noqa: S310 - GitHub release asset URL
            dest.write_bytes(resp.read())
        return ResolvedImage(path=dest, source="downloaded")
    except FlasherError:
        raise
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
        raise FlasherError(
            "Couldn't download a firmware image from GitHub "
            f"({GITHUB_REPO}). This is expected if the repo is private, there's no "
            "internet connection right now, or no release has been published yet.\n"
            f"  Details: {exc}\n"
            "  Instead, ask the maintainer for the image file and either drop it in "
            f"{images_dir} or point this tool at it with --bin <path>."
        ) from exc
