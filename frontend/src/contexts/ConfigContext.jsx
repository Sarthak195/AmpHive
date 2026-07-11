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
};

const ConfigContext = createContext(DEFAULTS);

export const ConfigProvider = ({ children }) => {
  const [config, setConfig] = useState(DEFAULTS);

  useEffect(() => {
    let cancelled = false;
    api.get('/api/config')
      .then((cfg) => { if (!cancelled && cfg) setConfig({ ...DEFAULTS, ...cfg }); })
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
