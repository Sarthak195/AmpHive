"""
Semver-aware ordering for firmware release version strings
(feat/ota-version-picker).

Firmware versions in this repo look like "2.3.0" or "2.3.0-direct"
(`firmware/CMakeLists.txt` PROJECT_VER — see docs/FIRMWARE.md). Sorting them
as plain strings is wrong: "2.9.0" > "2.10.0" lexicographically (the char
'9' sorts above '1'), so the newest release wouldn't reliably land first in
a descending list. `version_sort_key` parses the numeric (major, minor,
patch) tuple so comparisons are numeric, not lexical.
"""
import re
from typing import Tuple

# Major.minor.patch, with an optional "-suffix" (e.g. "-direct", "-rc1").
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-(.*))?$")


def version_sort_key(version: str) -> Tuple[int, int, int, str]:
    """Sort key for a firmware version string: ascending (major, minor,
    patch) as integers, then the optional "-suffix" as a string tiebreaker
    (so "2.3.0" and "2.3.0-direct" order deterministically against each
    other, though in practice a fleet uses one suffix convention at a time).

    Malformed/unparseable strings (missing, non-numeric, wrong shape) sort
    BELOW every well-formed version — this feeds list ordering for an admin/
    CPO picker, which must never raise on a bad row; it just floats junk to
    the bottom instead.

    Usage: ``sorted(releases, key=lambda r: version_sort_key(r.version), reverse=True)``
    for descending (newest-first) order.
    """
    if not version:
        return (-1, -1, -1, "")
    match = _VERSION_RE.match(version.strip())
    if not match:
        return (-1, -1, -1, version)
    major, minor, patch, suffix = match.groups()
    return (int(major), int(minor), int(patch), suffix or "")


def is_newer_version(candidate: str, baseline: str) -> bool:
    """True if `candidate` sorts strictly above `baseline` (e.g. to flag a
    release as newer than a gateway's currently-reported firmware_version
    in the CPO picker). Two malformed strings both sort to the same
    "unknown" key, so this returns False rather than an arbitrary guess."""
    return version_sort_key(candidate) > version_sort_key(baseline)
