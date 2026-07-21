/**
 * statusCopy tests: every machine-string table (plug states, session
 * statuses, ledger tx types, event types, stop reasons, API error codes)
 * maps to its human copy, with sensible humanized fallbacks for unknowns.
 */
import { describe, it, expect } from 'vitest';

import {
  plugStateLabel,
  sessionStatusLabel,
  txTypeLabel,
  stopReasonCopy,
  isAutoStopReason,
  eventTypeCopy,
  apiErrorCopy,
} from './statusCopy';

describe('plugStateLabel', () => {
  it('covers the raw enum and the availability buckets', () => {
    expect(plugStateLabel('available')).toBe('Available');
    expect(plugStateLabel('occupied')).toBe('In use');
    expect(plugStateLabel('in_use')).toBe('In use');
    expect(plugStateLabel('unpowered')).toBe('No power');
    expect(plugStateLabel('offline')).toBe('Offline');
    expect(plugStateLabel('maintenance')).toBe('Under maintenance');
  });

  it('humanizes unknown states instead of leaking raw enums', () => {
    expect(plugStateLabel('some_new_state')).toBe('Some new state');
    expect(plugStateLabel(null)).toBe('');
  });
});

describe('sessionStatusLabel', () => {
  it('covers the SessionStatus enum + legacy aliases', () => {
    expect(sessionStatusLabel('active')).toBe('Charging');
    expect(sessionStatusLabel('completed')).toBe('Completed');
    expect(sessionStatusLabel('paid')).toBe('Paid');
    expect(sessionStatusLabel('billed')).toBe('Paid');
    expect(sessionStatusLabel('cancelled')).toBe('Cancelled');
    expect(sessionStatusLabel('orphaned')).toBe('Interrupted');
  });
});

describe('txTypeLabel', () => {
  it('covers the ledger transaction types', () => {
    expect(txTypeLabel('topup')).toBe('Top-up');
    expect(txTypeLabel('session_debit')).toBe('Charging');
    expect(txTypeLabel('refund')).toBe('Refund');
    expect(txTypeLabel('hold')).toBe('Hold');
    expect(txTypeLabel('hold_release')).toBe('Hold released');
    expect(txTypeLabel('cpo_topup')).toBe('Top-up by operator (cash)');
  });
});

describe('stopReasonCopy', () => {
  it('maps the backend finalize reasons to friendly copy', () => {
    expect(stopReasonCopy('auto-stopped: wallet balance exhausted')).toBe(
      'Stopped automatically — wallet balance used up'
    );
    expect(stopReasonCopy('auto-stopped: session hold exhausted')).toBe(
      'Stopped automatically — session hold used up'
    );
    expect(stopReasonCopy('auto-stopped: energy limit reached')).toBe(
      'Stopped automatically — energy limit reached'
    );
    expect(stopReasonCopy('auto-stopped: time limit reached')).toBe(
      'Stopped automatically — time limit reached'
    );
    expect(stopReasonCopy('safety cutoff: plug reported overheat')).toBe(
      'Stopped for safety — the plug reported overheating'
    );
    expect(stopReasonCopy('safety cutoff: plug reported over-current')).toBe(
      'Stopped for safety — the plug drew too much current'
    );
    expect(stopReasonCopy('current cap exceeded: plug drew over its configured limit')).toBe(
      'Stopped for safety — the plug drew too much current'
    );
    expect(stopReasonCopy('limit reached: session hit its energy/duration limit')).toBe(
      'Stopped automatically — session limit reached'
    );
    expect(stopReasonCopy('auto-stopped: telemetry lost')).toBe(
      'Stopped automatically — connection to the charger was lost'
    );
  });

  it('falls back to the raw reason minus the auto-stopped prefix', () => {
    expect(stopReasonCopy('auto-stopped: mystery condition')).toBe('mystery condition');
    expect(stopReasonCopy('')).toBe('');
    expect(stopReasonCopy(null)).toBe('');
  });

  it('flags auto-stop reasons', () => {
    expect(isAutoStopReason('auto-stopped: wallet balance exhausted')).toBe(true);
    expect(isAutoStopReason('safety cutoff: plug reported overheat')).toBe(true);
    expect(isAutoStopReason(null)).toBe(false);
  });
});

describe('eventTypeCopy', () => {
  it('covers safety, unauthorized and OTA event types', () => {
    expect(eventTypeCopy('THERMAL_CUTOFF')).toBe('Thermal safety cutoff');
    expect(eventTypeCopy('OVERCURRENT_CUTOFF')).toBe('Current safety cutoff');
    expect(eventTypeCopy('OVERCURRENT_CAP')).toBe('Current cap exceeded');
    expect(eventTypeCopy('LOCAL_LIMIT_CUTOFF')).toBe('Session limit cutoff');
    expect(eventTypeCopy('UNAUTHORIZED_ON')).toBe('Unauthorized power-on');
    expect(eventTypeCopy('OTA_STARTED')).toBe('Firmware update started');
    expect(eventTypeCopy('OTA_OK_REBOOTING')).toBe('Firmware updated — rebooting');
    expect(eventTypeCopy('OTA_FAILED')).toBe('Firmware update failed');
    expect(eventTypeCopy('OTA_REFUSED_SESSION_ACTIVE')).toBe(
      'Firmware update deferred — session active'
    );
    expect(eventTypeCopy('OTA_START_FAILED')).toBe('Firmware update failed to start');
  });

  it('humanizes unknown event types', () => {
    expect(eventTypeCopy('SOME_NEW_EVENT')).toBe('Some new event');
  });
});

describe('apiErrorCopy', () => {
  it('maps structured error codes to friendly copy', () => {
    expect(apiErrorCopy({ code: 'circuit_full', message: 'raw' })).toBe(
      'This circuit is at capacity right now'
    );
    expect(apiErrorCopy({ code: 'gateway_offline' })).toBe(
      "This charger can't be reached right now"
    );
    expect(apiErrorCopy({ code: 'queue_disabled' })).toBe(
      "Queued charging isn't available for this charger"
    );
  });

  it('falls back to the server message, then a generic line', () => {
    expect(apiErrorCopy(new Error('Plug not found'))).toBe('Plug not found');
    expect(apiErrorCopy({ code: 'unmapped_code', message: 'server said so' })).toBe(
      'server said so'
    );
    expect(apiErrorCopy('plain string')).toBe('plain string');
    expect(apiErrorCopy(null)).toBe('Something went wrong. Please try again.');
    expect(apiErrorCopy({})).toBe('Something went wrong. Please try again.');
  });
});
