/**
 * useTheme — stamps the active theme (and optional accent) onto <html>.
 *
 * Layouts own the surface atmosphere: driver/marketing trees rely on the
 * index.html default (data-theme="day"), while console layouts call
 * useTheme('volt') and the admin layout useTheme('volt', 'admin').
 * Previous values are restored on unmount so navigating between surfaces
 * on the unsplit host never leaves a stale theme behind.
 */

import { useEffect } from 'react';

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
    };
  }, [theme, accent]);
}

export default useTheme;
