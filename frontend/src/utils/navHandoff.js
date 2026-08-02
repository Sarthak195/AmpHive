/**
 * navHandoff — hand off "get me there" to the device's own maps app instead
 * of building turn-by-turn directions ourselves. Just a URL builder; callers
 * `window.open(url, '_blank', 'noopener')` it (map-list rows, the
 * MapComponent popup).
 */

/**
 * Google Maps' documented "universal" directions link — works on desktop and
 * mobile (opens the installed app on iOS/Android when present, falls back to
 * the web UI otherwise). No API key required for this deep-link form.
 */
export function googleMapsDirUrl(lat, lng) {
  return `https://www.google.com/maps/dir/?api=1&destination=${lat},${lng}`;
}
