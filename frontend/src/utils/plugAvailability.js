/**
 * Shared plug-availability classification for driver-facing surfaces.
 * ===================================================================
 * The map legend, the availability filter, marker colors and StatusDot all
 * key off this single helper so "what a marker/badge color means" lives in
 * exactly one place instead of being redefined (and drifting) in each spot.
 *
 * Five states — a simplification of the full `PlugStatus` enum:
 * - 'available'   — status is 'available', the gateway is reachable, and the
 *                   plug is drawing line power.
 * - 'in_use'      — status is 'occupied' (someone is charging right now).
 * - 'unpowered'   — the gateway is reachable but the plug itself has no line
 *                   power (`plug_powered === false`). Distinct from 'offline':
 *                   the charger isn't broken, the mains feeding it are out, so
 *                   the driver can queue a charge to auto-start when power
 *                   returns (see the queued-charge feature).
 * - 'offline'     — the gateway is unreachable (`gateway_online === false`)
 *                   or the plug's own status is 'offline'.
 * - 'maintenance' — the operator (or an auto safety cutoff) has taken the
 *                   plug out of service. Split from 'offline' since v3:
 *                   deliberate downtime, not a connectivity fault.
 *
 * Colors reference the `--state-*` custom properties in styles/tokens.css;
 * labels come from utils/statusCopy.js.
 */

import { plugStateLabel } from './statusCopy';

export const AVAILABILITY_STATES = ['available', 'in_use', 'unpowered', 'offline', 'maintenance'];

export const AVAILABILITY_LABELS = Object.fromEntries(
  AVAILABILITY_STATES.map((s) => [s, plugStateLabel(s)])
);

// CSS custom-property names (see src/styles/tokens.css) — one per state, so
// the legend, markers and dots always agree with the design tokens.
export const AVAILABILITY_CSS_VAR = {
  available: '--state-available',
  in_use: '--state-in-use',
  unpowered: '--state-unpowered',
  offline: '--state-offline',
  maintenance: '--state-maintenance',
};

export function getPlugAvailability(plug) {
  if (plug?.gateway_online === false || plug?.status === 'offline') return 'offline';
  if (plug?.status === 'maintenance') return 'maintenance';
  if (plug?.status === 'occupied') return 'in_use';
  if (plug?.status === 'available') {
    // Gateway reachable but the plug reports no line power → a distinct
    // "no power" state (queueable) rather than collapsing into "offline".
    // Older API data without `plug_powered` (undefined) stays 'available'.
    return plug?.plug_powered === false ? 'unpowered' : 'available';
  }
  // Unknown/other status — can't be started, group with offline.
  return 'offline';
}
