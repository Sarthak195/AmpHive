/**
 * plugSearch tests: the shared client-side search predicate — matches on
 * name / id / group_name, case- and whitespace-insensitive, empty query
 * matches everything — plus the shared power/price bucket filters and the
 * distinct-connector helper the Dashboard + MapPage filter bars use.
 */
import { describe, it, expect } from 'vitest';
import {
  distinctConnectors,
  matchesPowerBucket,
  matchesPriceBucket,
  matchesQuery,
} from './plugSearch';

const PLUG = { id: 12, name: 'Lobby Charger', group_name: 'Sunrise Apartments' };

describe('matchesQuery', () => {
  it('matches on a name substring, case-insensitively', () => {
    expect(matchesQuery(PLUG, 'lobby')).toBe(true);
    expect(matchesQuery(PLUG, 'LOBBY')).toBe(true);
    expect(matchesQuery(PLUG, 'charger')).toBe(true);
  });

  it('matches on the numeric id as a string', () => {
    expect(matchesQuery(PLUG, '12')).toBe(true);
    expect(matchesQuery(PLUG, '1')).toBe(true); // substring match, like name
  });

  it('matches on group_name when present', () => {
    expect(matchesQuery(PLUG, 'sunrise')).toBe(true);
  });

  it('is whitespace-insensitive (trims the query)', () => {
    expect(matchesQuery(PLUG, '  lobby  ')).toBe(true);
  });

  it('an empty or whitespace-only query matches everything', () => {
    expect(matchesQuery(PLUG, '')).toBe(true);
    expect(matchesQuery(PLUG, '   ')).toBe(true);
    expect(matchesQuery(PLUG, undefined)).toBe(true);
  });

  it('rejects a non-matching query', () => {
    expect(matchesQuery(PLUG, 'basement')).toBe(false);
  });

  it('is safe when group_name is absent', () => {
    const ungrouped = { id: 3, name: 'Public Plug' };
    expect(matchesQuery(ungrouped, 'sunrise')).toBe(false);
    expect(matchesQuery(ungrouped, 'public')).toBe(true);
  });

  it('is safe against a null/undefined plug', () => {
    expect(matchesQuery(null, 'lobby')).toBe(false);
    expect(matchesQuery(undefined, '')).toBe(true); // empty query short-circuits first
  });
});

describe('matchesPowerBucket', () => {
  it('the empty ("Any") bucket matches everything, including unset specs', () => {
    expect(matchesPowerBucket({ rated_power_w: 3300 }, '')).toBe(true);
    expect(matchesPowerBucket({ rated_power_w: null }, '')).toBe(true);
    expect(matchesPowerBucket({}, '')).toBe(true);
  });

  it('buckets on the boundaries: ≤3.3 kW inclusive, 3.3–7.4 kW half-open, 7.4 kW+ exclusive-low', () => {
    expect(matchesPowerBucket({ rated_power_w: 3300 }, 'lte3300')).toBe(true);
    expect(matchesPowerBucket({ rated_power_w: 3301 }, 'lte3300')).toBe(false);
    expect(matchesPowerBucket({ rated_power_w: 3301 }, '3300to7400')).toBe(true);
    expect(matchesPowerBucket({ rated_power_w: 7400 }, '3300to7400')).toBe(true);
    expect(matchesPowerBucket({ rated_power_w: 7401 }, 'gt7400')).toBe(true);
    expect(matchesPowerBucket({ rated_power_w: 7400 }, 'gt7400')).toBe(false);
  });

  it('an unset rated_power_w only matches "Any" — never a specific bucket', () => {
    expect(matchesPowerBucket({ rated_power_w: null }, 'lte3300')).toBe(false);
    expect(matchesPowerBucket({}, 'gt7400')).toBe(false);
  });
});

describe('matchesPriceBucket', () => {
  it('buckets price_per_kwh into under-10 / 10-20 / 20-plus', () => {
    expect(matchesPriceBucket({ price_per_kwh: 8 }, 'lt10')).toBe(true);
    expect(matchesPriceBucket({ price_per_kwh: 10 }, 'lt10')).toBe(false);
    expect(matchesPriceBucket({ price_per_kwh: 10 }, '10to20')).toBe(true);
    expect(matchesPriceBucket({ price_per_kwh: 20 }, '10to20')).toBe(true);
    expect(matchesPriceBucket({ price_per_kwh: 21 }, 'gt20')).toBe(true);
  });

  it('"Any" matches everything; a missing price only matches "Any"', () => {
    expect(matchesPriceBucket({ price_per_kwh: null }, '')).toBe(true);
    expect(matchesPriceBucket({ price_per_kwh: null }, 'lt10')).toBe(false);
  });
});

describe('distinctConnectors', () => {
  it('collects sorted distinct non-null connector types', () => {
    const plugs = [
      { connector_type: 'Type 2' },
      { connector_type: '3-pin 16A' },
      { connector_type: 'Type 2' },
      { connector_type: null },
      {},
    ];
    expect(distinctConnectors(plugs)).toEqual(['3-pin 16A', 'Type 2']);
  });

  it('is safe on empty/undefined input', () => {
    expect(distinctConnectors([])).toEqual([]);
    expect(distinctConnectors(undefined)).toEqual([]);
  });
});
