/**
 * useTheme tests: stamps data-theme/data-accent onto <html>, syncs
 * <meta name="theme-color"> to match, and restores both on unmount.
 */
import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { renderHook } from '@testing-library/react';

import useTheme from './useTheme';

let meta;
beforeEach(() => {
  meta = document.createElement('meta');
  meta.setAttribute('name', 'theme-color');
  meta.setAttribute('content', '#FAF7EF');
  document.head.appendChild(meta);
});

afterEach(() => {
  meta.remove();
  delete document.documentElement.dataset.theme;
  delete document.documentElement.dataset.accent;
});

describe('useTheme', () => {
  it('stamps data-theme and syncs theme-color to match', () => {
    renderHook(() => useTheme('volt'));
    expect(document.documentElement.dataset.theme).toBe('volt');
    expect(meta.getAttribute('content')).toBe('#11110D');
  });

  it('stamps data-accent when given one', () => {
    renderHook(() => useTheme('volt', 'admin'));
    expect(document.documentElement.dataset.accent).toBe('admin');
  });

  it('restores the previous theme, accent, and theme-color on unmount', () => {
    document.documentElement.dataset.theme = 'day';
    meta.setAttribute('content', '#FAF7EF');

    const { unmount } = renderHook(() => useTheme('volt', 'admin'));
    expect(meta.getAttribute('content')).toBe('#11110D');

    unmount();
    expect(document.documentElement.dataset.theme).toBe('day');
    expect(document.documentElement.dataset.accent).toBeUndefined();
    expect(meta.getAttribute('content')).toBe('#FAF7EF');
  });

  it('does nothing to theme-color when no meta tag is present', () => {
    meta.remove();
    expect(() => renderHook(() => useTheme('volt'))).not.toThrow();
  });
});
