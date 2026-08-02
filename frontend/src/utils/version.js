/**
 * version — semver-aware comparison for firmware version strings
 * (feat/ota-version-picker).
 *
 * Firmware versions look like "2.3.0" or "2.3.0-direct"
 * (`firmware/CMakeLists.txt` PROJECT_VER — see docs/FIRMWARE.md). Sorting
 * them as plain strings is wrong: '2.9.0' > '2.10.0' lexicographically
 * (the char '9' sorts above '1'), so the newest release wouldn't reliably
 * land first in a descending list (fleet "behind" badges, the OTA version
 * picker). `compareVersions` parses the numeric (major, minor, patch) parts
 * so comparisons are numeric, not lexical. Mirrors
 * backend/services/versioning.py's `version_sort_key` — keep the two in
 * sync if the version format ever changes.
 */

const VERSION_RE = /^(\d+)\.(\d+)\.(\d+)(?:-(.*))?$/;

/** Parse a version string into a [major, minor, patch, suffix] sort key.
 * Malformed/missing strings sort as [-1, -1, -1, raw] — below every
 * well-formed version, never throwing. */
function versionKey(version) {
  const v = String(version || '').trim();
  const match = VERSION_RE.exec(v);
  if (!match) return [-1, -1, -1, v];
  const [, major, minor, patch, suffix] = match;
  return [Number(major), Number(minor), Number(patch), suffix || ''];
}

/** Three-way compare: negative if `a` < `b`, positive if `a` > `b`, 0 if
 * equal. Use `.sort(compareVersions)` for ascending, or negate/reverse for
 * descending (newest-first) order. */
export function compareVersions(a, b) {
  const ka = versionKey(a);
  const kb = versionKey(b);
  for (let i = 0; i < 3; i++) {
    if (ka[i] !== kb[i]) return ka[i] - kb[i];
  }
  if (ka[3] === kb[3]) return 0;
  return ka[3] > kb[3] ? 1 : -1;
}

/** True if `candidate` is a strictly newer version than `baseline` (e.g. to
 * flag a release as newer than a gateway's currently-reported
 * firmware_version). Two malformed strings compare equal ([-1,-1,-1,'']
 * plus their own raw suffix would clash by string), so this returns false
 * rather than an arbitrary guess when either side can't be parsed. */
export function isNewerVersion(candidate, baseline) {
  if (!candidate || !baseline) return false;
  const kc = versionKey(candidate);
  const kb = versionKey(baseline);
  if (kc[0] === -1 || kb[0] === -1) return false;
  return compareVersions(candidate, baseline) > 0;
}

/** Sort an array of version strings descending (newest first). Does not
 * mutate the input. */
export function sortVersionsDescending(versions) {
  return [...versions].sort((a, b) => compareVersions(b, a));
}
