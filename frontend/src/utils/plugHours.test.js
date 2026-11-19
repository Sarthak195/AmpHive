import { describe, it, expect } from 'vitest';

import { hoursFormFromPlug, hoursLabel, hoursPatch, matchesHours } from './plugHours';

describe('hoursLabel', () => {
  it('says nothing for a 24x7 plug', () => {
    expect(hoursLabel({ open_now: null })).toBe('');
  });

  it('renders the closing and opening time in the tenant zone, without an em-dash', () => {
    const soon = new Date(Date.now() + 3600 * 1000);
    soon.setUTCMinutes(30, 0, 0);
    const iso = soon.toISOString();
    const hhmm = soon.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', timeZone: 'UTC' });
    const open = hoursLabel({ open_now: true, hours_changes_at: iso, tenant_timezone: 'UTC' });
    const closed = hoursLabel({ open_now: false, hours_changes_at: iso, tenant_timezone: 'UTC' });
    expect(open).toBe(`Open now, closes ${hhmm}`);
    expect(closed).toBe(`Closed, opens ${hhmm}`);
    expect(open + closed).not.toMatch(new RegExp(String.fromCharCode(0x2014)));
  });
});

describe('matchesHours', () => {
  it('open-now keeps open and 24x7 plugs; 24x7 keeps only plugs with no hours', () => {
    const always = { open_now: null, opening_hours: null };
    const open = { open_now: true, opening_hours: [{ days: 127, start: 480, end: 1320 }] };
    const closed = { open_now: false, opening_hours: [{ days: 1, start: 0, end: 60 }] };
    expect([always, open, closed].filter((p) => matchesHours(p, 'open'))).toEqual([always, open]);
    expect([always, open, closed].filter((p) => matchesHours(p, '24x7'))).toEqual([always]);
    expect([always, open, closed].filter((p) => matchesHours(p, ''))).toHaveLength(3);
  });
});

describe('CPO form helpers', () => {
  it('round-trips a split overnight window with no change, and sends only what changed', () => {
    const plug = {
      // Key order as Postgres JSONB returns it (end, days, start).
      opening_hours: [{ end: 1440, days: 1, start: 1320 }, { end: 360, days: 2, start: 0 }],
      max_session_minutes: null,
      amenities: null,
      access_note: null,
    };
    const form = hoursFormFromPlug(plug);
    expect(form.windows[0]).toEqual({ days: 1, start: '22:00', end: '00:00' });
    expect(hoursPatch(form, plug)).toEqual({});

    const edited = { ...form, windows: [], amenities: ['wifi'], access_note: 'Gate B' };
    expect(hoursPatch(edited, plug)).toEqual({
      opening_hours: null, amenities: ['wifi'], access_note: 'Gate B',
    });
  });

  it('rejects a window with no day or a zero length', () => {
    const plug = { opening_hours: null };
    const base = hoursFormFromPlug(plug);
    expect(() => hoursPatch({ ...base, windows: [{ days: 0, start: '08:00', end: '09:00' }] }, plug))
      .toThrow(/day/);
    expect(() => hoursPatch({ ...base, windows: [{ days: 127, start: '08:00', end: '08:00' }] }, plug))
      .toThrow(/same time/);
  });

  it('00:00 to 00:00 is a whole day, and a stored whole-day window saves unchanged', () => {
    const plug = { opening_hours: [{ end: 1440, days: 63, start: 0 }] };
    const form = hoursFormFromPlug(plug);
    expect(form.windows[0]).toEqual({ days: 63, start: '00:00', end: '00:00' });
    expect(hoursPatch(form, plug)).toEqual({});
    const fresh = { opening_hours: null };
    expect(hoursPatch({ ...hoursFormFromPlug(fresh), windows: [{ days: 63, start: '00:00', end: '00:00' }] }, fresh))
      .toEqual({ opening_hours: [{ days: 63, start: 0, end: 0 }] });
  });
});
