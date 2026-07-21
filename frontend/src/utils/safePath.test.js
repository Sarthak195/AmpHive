/**
 * safePath tests: the open-redirect regression guard for ?next=/state.from
 * targets — same-app paths pass, anything that could point off-origin is
 * rejected.
 */
import { describe, it, expect } from 'vitest';
import { isSafeInternalPath } from './safePath';

describe('isSafeInternalPath', () => {
  it('rejects protocol-relative paths', () => {
    expect(isSafeInternalPath('//evil.com')).toBe(false);
  });

  it('rejects absolute URLs with a scheme', () => {
    expect(isSafeInternalPath('https://evil.com')).toBe(false);
    expect(isSafeInternalPath('javascript:alert(1)')).toBe(false);
  });

  it('rejects the backslash variant', () => {
    expect(isSafeInternalPath('/\\evil.com')).toBe(false);
  });

  it('rejects non-string and empty input', () => {
    expect(isSafeInternalPath(undefined)).toBe(false);
    expect(isSafeInternalPath(null)).toBe(false);
    expect(isSafeInternalPath('')).toBe(false);
  });

  it('accepts a plain same-app path', () => {
    expect(isSafeInternalPath('/wallet')).toBe(true);
  });

  it('accepts a same-app path with a query string', () => {
    expect(isSafeInternalPath('/?plug=7')).toBe(true);
  });
});
