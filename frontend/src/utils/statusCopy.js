/**
 * statusCopy — the single machine-string → human-copy table.
 * ==========================================================
 * Every raw enum the backend emits (plug statuses, session statuses, ledger
 * transaction types, gateway event types, stop reasons, structured API error
 * codes) gets its driver/operator-facing wording here. Extend these tables
 * rather than writing ad-hoc strings in pages.
 */

const humanize = (s) => {
  if (!s) return '';
  const t = String(s).replace(/[_-]+/g, ' ').toLowerCase().trim();
  return t.charAt(0).toUpperCase() + t.slice(1);
};

/* ---- plug availability / status ------------------------------------------ */

export const PLUG_STATE_LABELS = {
  available: 'Available',
  occupied: 'In use', // raw PlugStatus enum
  in_use: 'In use', // plugAvailability bucket
  unpowered: 'No power',
  offline: 'Offline',
  maintenance: 'Under maintenance',
};

export function plugStateLabel(state) {
  return PLUG_STATE_LABELS[state] || humanize(state);
}

/* ---- charging session statuses ------------------------------------------- */

export const SESSION_STATUS_LABELS = {
  active: 'Charging',
  completed: 'Completed',
  paid: 'Paid',
  billed: 'Paid', // legacy alias
  cancelled: 'Cancelled',
  orphaned: 'Interrupted',
};

export function sessionStatusLabel(status) {
  return SESSION_STATUS_LABELS[status] || humanize(status);
}

/* ---- wallet ledger transaction types -------------------------------------- */

export const TX_TYPE_LABELS = {
  topup: 'Top-up',
  session_debit: 'Charging',
  refund: 'Refund',
  hold: 'Hold',
  hold_release: 'Hold released',
};

export function txTypeLabel(type) {
  return TX_TYPE_LABELS[type] || humanize(type);
}

/* ---- session stop reasons -------------------------------------------------
 * The backend finalizes with free-text reasons ("auto-stopped: wallet balance
 * exhausted", "safety cutoff: plug reported overheat", ...). Pattern-match to
 * friendly copy; unknown reasons fall back to the raw text minus the
 * "auto-stopped:" prefix. */

const STOP_REASON_PATTERNS = [
  [/hold exhausted/i, 'Stopped automatically — session hold used up'],
  [/balance exhausted/i, 'Stopped automatically — wallet balance used up'],
  [/energy limit/i, 'Stopped automatically — energy limit reached'],
  [/time limit/i, 'Stopped automatically — time limit reached'],
  [/overheat|thermal/i, 'Stopped for safety — the plug reported overheating'],
  [/over-current|overcurrent|current cap/i, 'Stopped for safety — the plug drew too much current'],
  [/telemetry lost/i, 'Stopped automatically — connection to the charger was lost'],
  [/energy\/duration limit|limit reached/i, 'Stopped automatically — session limit reached'],
];

export function stopReasonCopy(reason) {
  if (!reason) return '';
  for (const [re, copy] of STOP_REASON_PATTERNS) {
    if (re.test(reason)) return copy;
  }
  return String(reason).replace(/^auto-stopped:\s*/i, '');
}

/** True when a stop reason means the session ended without the driver asking. */
export function isAutoStopReason(reason) {
  return Boolean(reason) && /auto-stopped|telemetry lost|exhaust|safety cutoff|limit reached|current cap/i.test(reason);
}

/* ---- gateway / plug event types -------------------------------------------- */

export const EVENT_TYPE_COPY = {
  THERMAL_CUTOFF: 'Thermal safety cutoff',
  OVERCURRENT_CUTOFF: 'Current safety cutoff',
  OVERCURRENT_CAP: 'Current cap exceeded',
  LOCAL_LIMIT_CUTOFF: 'Session limit cutoff',
  UNAUTHORIZED_ON: 'Unauthorized power-on',
  OTA_STARTED: 'Firmware update started',
  OTA_OK_REBOOTING: 'Firmware updated — rebooting',
  OTA_FAILED: 'Firmware update failed',
  OTA_REFUSED_SESSION_ACTIVE: 'Firmware update deferred — session active',
  OTA_START_FAILED: 'Firmware update failed to start',
};

export function eventTypeCopy(type) {
  return EVENT_TYPE_COPY[type] || humanize(type);
}

/* ---- structured API error codes ---------------------------------------------
 * api/client.js surfaces the backend's { code, message } detail as err.code /
 * err.message. Known codes get friendly copy; everything else falls back to
 * the server message, then a generic line. */

export const API_ERROR_COPY = {
  circuit_full: 'This circuit is at capacity right now',
  gateway_offline: "This charger can't be reached right now",
  already_queued: 'You already have a queued charge for this charger',
  queue_disabled: "Queued charging isn't available for this charger",
  plug_powered: 'This charger has power now — start charging directly',
  queue_cap: "You've reached the limit of queued charges",
  balance_too_low: 'Add money to your wallet first',
};

const GENERIC_ERROR = 'Something went wrong. Please try again.';

export function apiErrorCopy(err) {
  if (!err) return GENERIC_ERROR;
  if (typeof err === 'string') return err;
  if (err.code && API_ERROR_COPY[err.code]) return API_ERROR_COPY[err.code];
  return err.message || GENERIC_ERROR;
}
