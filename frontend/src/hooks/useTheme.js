/**
 * useTheme — stamps the active theme (and optional accent) onto <html>.
 *
 * Layouts own the surface atmosphere: driver/marketing trees rely on the
 * index.html default (data-theme="day"), while console layouts call
 * useTheme('volt') and the admin layout useTheme('volt', 'admin').
 * Previous values are restored on unmount so navigating between surfaces
 * on the unsplit host never leaves a stale theme behind.
 *
 * Also syncs <meta name="theme-color"> to the surface's --bg so the
 * browser chrome (Android status bar / task switcher) matches the volt
 * console instead of index.html's hardcoded day-theme cream.
 */

import { useEffect } from 'react';

const THEME_COLOR = { day: '#FAF7EF', volt: '#11110D' };

export function useTheme(theme, accent) {
  useEffect(() => {
    const el = document.documentElement;
    const prevTheme = el.dataset.theme;
    const prevAccent = el.dataset.accent;

    el.dataset.theme = theme;
    if (accent) {
      el.dataset.accent = accent;
    } else {
      delete el.dataset.accent;
    }

    const meta = document.querySelector('meta[name="theme-color"]');
    const prevColor = meta?.getAttribute('content') ?? null;
    if (meta && THEME_COLOR[theme]) {
      meta.setAttribute('content', THEME_COLOR[theme]);
    }

    return () => {
      if (prevTheme !== undefined) {
        el.dataset.theme = prevTheme;
      } else {
        delete el.dataset.theme;
      }
      if (prevAccent !== undefined) {
        el.dataset.accent = prevAccent;
      } else {
        delete el.dataset.accent;
      }
      if (meta && prevColor !== null) {
        meta.setAttribute('content', prevColor);
      }
    };
  }, [theme, accent]);
}

export default useTheme;
