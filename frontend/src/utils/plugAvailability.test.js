/**
 * plugAvailability tests: 5-state classification (maintenance now split from
 * offline), the state list/labels staying in lockstep, and colors pointing
 * at the --state-* design tokens.
 */
import { describe, it, expect } from 'vitest';

import {
  AVAILABILITY_STATES,
  AVAILABILITY_LABELS,
  AVAILABILITY_CSS_VAR,
  getPlugAvailability,
} from './plugAvailability';

describe('getPlugAvailability', () => {
  it('classifies the five states', () => {
    expect(getPlugAvailability({ status: 'available', gateway_online: true, plug_powered: true }))
      .toBe('available');
    expect(getPlugAvailability({ status: 'occupied', gateway_online: true })).toBe('in_use');
    expect(getPlugAvailability({ status: 'available', gateway_online: true, plug_powered: false }))
      .toBe('unpowered');
    expect(getPlugAvailability({ status: 'offline', gateway_online: true })).toBe('offline');
    expect(getPlugAvailability({ status: 'maintenance', gateway_online: true })).toBe('maintenance');
  });

  it('an unreachable gateway wins over everything (including maintenance)', () => {
    expect(getPlugAvailability({ status: 'available', gateway_online: false })).toBe('offline');
    expect(getPlugAvailability({ status: 'occupied', gateway_online: false })).toBe('offline');
    expect(getPlugAvailability({ status: 'maintenance', gateway_online: false })).toBe('offline');
  });

  it('older API data without plug_powered stays available', () => {
    expect(getPlugAvailability({ status: 'available', gateway_online: true })).toBe('available');
  });

  it('unknown/missing statuses group with offline', () => {
    expect(getPlugAvailability({ status: 'weird_future_state', gateway_online: true })).toBe('offline');
    expect(getPlugAvailability({})).toBe('offline');
    expect(getPlugAvailability(null)).toBe('offline');
  });
});

describe('state tables', () => {
  it('lists exactly the five states', () => {
    expect(AVAILABILITY_STATES).toEqual([
      'available',
      'in_use',
      'unpowered',
      'offline',
      'maintenance',
    ]);
  });

  it('has a label and a --state-* token for every state', () => {
    for (const s of AVAILABILITY_STATES) {
      expect(AVAILABILITY_LABELS[s]).toBeTruthy();
      expect(AVAILABILITY_CSS_VAR[s]).toMatch(/^--state-/);
    }
    expect(AVAILABILITY_LABELS.maintenance).toBe('Under maintenance');
    expect(AVAILABILITY_LABELS.unpowered).toBe('No power');
    expect(AVAILABILITY_CSS_VAR.in_use).toBe('--state-in-use');
  });
});
