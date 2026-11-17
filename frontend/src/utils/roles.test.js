import { describe, it, expect } from 'vitest';
import { hasCpoConsole, isReadOnlyViewer } from './roles';

describe('role helpers (F10)', () => {
  it('lets cpo, admin and cpo_viewer into the CPO console, nobody else', () => {
    for (const role of ['cpo', 'admin', 'cpo_viewer']) {
      expect(hasCpoConsole({ role })).toBe(true);
    }
    for (const user of [{ role: 'driver' }, { role: undefined }, null, undefined]) {
      expect(hasCpoConsole(user)).toBe(false);
    }
  });

  it('marks only the cpo_viewer as read-only', () => {
    expect(isReadOnlyViewer({ role: 'cpo_viewer' })).toBe(true);
    for (const role of ['cpo', 'admin', 'driver']) {
      expect(isReadOnlyViewer({ role })).toBe(false);
    }
    expect(isReadOnlyViewer(null)).toBe(false);
  });
});
