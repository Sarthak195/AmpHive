/**
 * Shared plug-availability classification for the driver Home page.
 * ===================================================================
 * The map legend, the availability filter, and the map marker colors all
 * key off this single helper so "what a marker/badge color means" lives in
 * exactly one place instead of being redefined (and drifting) in each spot.
 *
 * Four states — a simplification of the full `PlugStatus` enum for the
 * driver-facing map/list:
 * - 'available' — status is 'available', the gateway is reachable, and the
 *                 plug is drawing line power.
 * - 'in_use'    — status is 'occupied' (someone is charging right now).
 * - 'unpowered' — the gateway is reachable but the plug itself has no line
 *                 power (`plug_powered === false`). Distinct from 'offline':
 *                 the charger isn't broken, the mains feeding it are out, so
 *                 the driver can queue a charge to auto-start when power
 *                 returns (see the queued-charge feature).
 * - 'offline'   — the gateway is unreachable (`gateway_online === false`),
 *                 the plug's own status is 'offline', or any other status
 *                 (e.g. 'maintenance') a driver can't start anyway — folded
 *                 into "offline" rather than adding another bucket.
 */

export const AVAILABILITY_STATES = ['available', 'in_use', 'unpowered', 'offline'];

export const AVAILABILITY_LABELS = {
  available: 'Available',
  in_use: 'In use',
  unpowered: 'No power',
  offline: 'Offline',
};

// CSS custom-property names (see src/styles/global.css :root) — reused as-is
// so the legend and marker colors always match the existing badge palette
// (badge-success / badge-warning / badge-danger). "Unpowered" borrows the
// warning color: it's not startable now but isn't a dead charger either.
export const AVAILABILITY_CSS_VAR = {
  available: '--color-success',
  in_use: '--color-warning',
  unpowered: '--color-warning',
  offline: '--color-danger',
};

export function getPlugAvailability(plug) {
  if (plug?.gateway_online === false || plug?.status === 'offline') return 'offline';
  if (plug?.status === 'occupied') return 'in_use';
  if (plug?.status === 'available') {
    // Gateway reachable but the plug reports no line power → a distinct
    // "no power" state (queueable) rather than collapsing into "offline".
    // Older API data without `plug_powered` (undefined) stays 'available'.
    return plug?.plug_powered === false ? 'unpowered' : 'available';
  }
  // Unknown/other status (e.g. 'maintenance') — can't be started, group
  // with offline for this view.
  return 'offline';
}
