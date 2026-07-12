/**
 * Shared plug-availability classification for the driver Home page.
 * ===================================================================
 * The map legend, the availability filter, and the map marker colors all
 * key off this single helper so "what a marker/badge color means" lives in
 * exactly one place instead of being redefined (and drifting) in each spot.
 *
 * Three states — a simplification of the full `PlugStatus` enum for the
 * driver-facing map/list:
 * - 'available' — status is 'available' AND the gateway is reachable.
 * - 'in_use'    — status is 'occupied' (someone is charging right now).
 * - 'offline'   — the gateway is unreachable (`gateway_online === false`),
 *                 the plug's own status is 'offline', or any other status
 *                 (e.g. 'maintenance') a driver can't start anyway — folded
 *                 into "offline" rather than adding a fourth bucket.
 */

export const AVAILABILITY_STATES = ['available', 'in_use', 'offline'];

export const AVAILABILITY_LABELS = {
  available: 'Available',
  in_use: 'In use',
  offline: 'Offline',
};

// CSS custom-property names (see src/styles/global.css :root) — reused as-is
// so the legend and marker colors always match the existing badge palette
// (badge-success / badge-warning / badge-danger).
export const AVAILABILITY_CSS_VAR = {
  available: '--color-success',
  in_use: '--color-warning',
  offline: '--color-danger',
};

export function getPlugAvailability(plug) {
  if (plug?.gateway_online === false || plug?.status === 'offline') return 'offline';
  if (plug?.status === 'occupied') return 'in_use';
  if (plug?.status === 'available') return 'available';
  // Unknown/other status (e.g. 'maintenance') — can't be started, group
  // with offline for this 3-state view.
  return 'offline';
}
