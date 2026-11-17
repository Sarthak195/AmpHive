/** Shared constants and helpers for the forwarded firmware log views. */

export const PAGE_SIZE = 50;
export const REFRESH_MS = 15_000;
// GATEWAY_LOGS_RETENTION_DAYS default (backend/services/session_reaper.py).
export const RETENTION_DAYS = 14;

const ANSI_RE = new RegExp(String.fromCharCode(27) + '[[][0-9;]*m', 'g');

/** Plain-text view of a stored line: drop ANSI colour escapes. */
export const cleanLogLine = (msg) => String(msg ?? '').replace(ANSI_RE, '');
