/**
 * [Scheduled start] Tenant-zone time helpers: the next occurrence of a
 * wall-clock time in the tenant's IANA zone, independent of the runner's TZ,
 * including the DST short and long days.
 */
import { describe, it, expect } from 'vitest';

import { fmtTimeInZone, nextOccurrenceInZone } from './reservationTime';

const at = (iso) => Date.parse(iso);

describe('nextOccurrenceInZone', () => {
  it('22:00 in Asia/Kolkata is later today when it has not passed', () => {
    const r = nextOccurrenceInZone('22:00', 'Asia/Kolkata', at('2026-11-15T16:00:00Z'));
    expect(r).toEqual({ iso: '2026-11-15T16:30:00.000Z', tomorrow: false });
  });

  it('rolls to tomorrow once the time has passed', () => {
    const r = nextOccurrenceInZone('22:00', 'Asia/Kolkata', at('2026-11-15T17:00:00Z'));
    expect(r).toEqual({ iso: '2026-11-16T16:30:00.000Z', tomorrow: true });
  });

  it('spring-forward eve (23 h day) lands on the next local date, not day + 2', () => {
    // Sat 2027-03-27 23:30 GMT; Sunday 28 Mar is 23 h long in Europe/London.
    const now = at('2027-03-27T23:30:00Z');
    expect(nextOccurrenceInZone('00:30', 'Europe/London', now).iso).toBe('2027-03-28T00:30:00.000Z');
    // 23:00 BST on Sunday the 28th, within 24 h (now + 24 h would hit Monday).
    expect(nextOccurrenceInZone('23:00', 'Europe/London', now).iso).toBe('2027-03-28T22:00:00.000Z');
  });

  it('an autumn-overlap time resolves to one instant showing that wall-clock', () => {
    const r = nextOccurrenceInZone('01:30', 'Europe/London', at('2027-10-30T12:00:00Z'));
    expect(['2027-10-31T00:30:00.000Z', '2027-10-31T01:30:00.000Z']).toContain(r.iso);
    expect(fmtTimeInZone(r.iso, 'Europe/London')).toMatch(/01:30|1:30/);
  });
});

describe('fmtTimeInZone', () => {
  it("renders the tenant's wall-clock, not the viewer's", () => {
    expect(fmtTimeInZone('2026-11-15T16:30:00Z', 'Asia/Kolkata')).toMatch(/22:00|10:00\s?PM/i);
    expect(fmtTimeInZone('2026-11-15T16:30:00Z', 'UTC')).toMatch(/16:30|04:30\s?PM/i);
  });
});
