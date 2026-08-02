/**
 * version tests: semver-aware ordering for firmware version strings — the
 * regression this guards is a plain string sort putting "2.9.0" above
 * "2.10.0" (see version.js's module docstring).
 */
import { describe, it, expect } from 'vitest';
import { compareVersions, isNewerVersion, sortVersionsDescending } from './version';

describe('compareVersions', () => {
  it('orders numerically, not lexically ("2.10.0" above "2.9.0")', () => {
    expect(compareVersions('2.10.0', '2.9.0')).toBeGreaterThan(0);
    expect(compareVersions('2.9.0', '2.10.0')).toBeLessThan(0);
  });

  it('treats equal versions as equal', () => {
    expect(compareVersions('2.3.0', '2.3.0')).toBe(0);
  });

  it('compares major/minor/patch in priority order', () => {
    expect(compareVersions('10.0.0', '2.99.99')).toBeGreaterThan(0);
    expect(compareVersions('2.1.0', '2.0.99')).toBeGreaterThan(0);
    expect(compareVersions('2.0.5', '2.0.4')).toBeGreaterThan(0);
  });

  it('breaks ties on the -suffix', () => {
    expect(compareVersions('2.3.0-direct', '2.3.0')).toBeGreaterThan(0);
  });

  it('sorts malformed/empty strings below well-formed ones', () => {
    const sorted = sortVersionsDescending(['2.3.0', 'not-a-version', '', '2.10.0']);
    expect(sorted[0]).toBe('2.10.0');
    expect(sorted[1]).toBe('2.3.0');
  });
});

describe('sortVersionsDescending', () => {
  it('returns newest-first without mutating the input', () => {
    const input = ['2.9.0', '2.10.0', '2.2.0', '2.10.0-direct'];
    const sorted = sortVersionsDescending(input);
    expect(sorted).toEqual(['2.10.0-direct', '2.10.0', '2.9.0', '2.2.0']);
    expect(input).toEqual(['2.9.0', '2.10.0', '2.2.0', '2.10.0-direct']); // unmutated
  });
});

describe('isNewerVersion', () => {
  it('flags a strictly newer version', () => {
    expect(isNewerVersion('2.10.0', '2.9.0')).toBe(true);
    expect(isNewerVersion('2.9.0', '2.10.0')).toBe(false);
  });

  it('is false for an equal version', () => {
    expect(isNewerVersion('2.3.0', '2.3.0')).toBe(false);
  });

  it('is false when either side is missing/malformed (no arbitrary guess)', () => {
    expect(isNewerVersion('2.3.0', '')).toBe(false);
    expect(isNewerVersion('', '2.3.0')).toBe(false);
    expect(isNewerVersion('2.3.0', 'unknown')).toBe(false);
  });
});
