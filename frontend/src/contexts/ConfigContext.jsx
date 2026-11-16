/**
 * AmpHive Config Context
 * ======================
 * Fetches public pricing/config from the backend (`GET /api/config`) once, so
 * the UI shows the same tariff and minimum-balance the server actually
 * enforces instead of hardcoding them. Falls back to sensible defaults if the
 * request fails (the app still renders).
 */

import { createContext, useContext, useState, useEffect } from 'react';
import api from '../api/client';

const DEFAULTS = {
  coins_per_kwh: 5.0,
  min_start_balance_coins: 50,
  coin_inr_rate: 1.0,
  currency: 'INR',
  // "Continue with Google" is hidden until the backend confirms it's
  // configured (GOOGLE_CLIENT_ID set) — see backend/main.py public_config().
  google_login_enabled: false,
};

const ConfigContext = createContext(DEFAULTS);

// Public config is cached so money renders at the real coin rate on an
// instant open, before /api/config answers. Not personal: kept on logout.
const CONFIG_KEY = 'amphive_config';
const cachedConfig = () => {
  try {
    const c = JSON.parse(localStorage.getItem(CONFIG_KEY));
    return c && typeof c === 'object' && !Array.isArray(c) ? { ...DEFAULTS, ...c } : DEFAULTS;
  } catch {
    return DEFAULTS;
  }
};

export const ConfigProvider = ({ children }) => {
  const [config, setConfig] = useState(cachedConfig);

  useEffect(() => {
    let cancelled = false;
    api.get('/api/config')
      .then((cfg) => {
        if (cancelled || !cfg) return;
        setConfig({ ...DEFAULTS, ...cfg });
        try {
          localStorage.setItem(CONFIG_KEY, JSON.stringify(cfg));
        } catch {
          // Storage full or blocked: defaults next time.
        }
      })
      .catch(() => { /* keep defaults */ });
    return () => { cancelled = true; };
  }, []);

  return (
    <ConfigContext.Provider value={config}>
      {children}
    </ConfigContext.Provider>
  );
};

export const useConfig = () => useContext(ConfigContext);
