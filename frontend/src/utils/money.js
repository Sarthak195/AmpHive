/**
 * Money + measurement formatting — the single source for how ₹, kWh, kW and
 * durations render everywhere. The app is ₹-first (coins are demoted to
 * secondary copy); the 1:1 coin↔₹ rate comes from ConfigContext at call
 * sites, never hardcoded here.
 */

const inrFormatter = new Intl.NumberFormat('en-IN', {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

/** "₹1,240.50" (en-IN grouping: "₹1,24,050.50"). Null/NaN → "—". */
export function formatINR(n) {
  const v = Number(n);
  if (n == null || n === '' || Number.isNaN(v)) return '—';
  const sign = v < 0 ? '-' : '';
  return `${sign}₹${inrFormatter.format(Math.abs(v))}`;
}

/** Convert coins to ₹ at the given rate (₹ per coin, from ConfigContext). */
export function coinsToINR(coins, rate = 1) {
  const c = Number(coins);
  const r = Number(rate);
  if (Number.isNaN(c) || Number.isNaN(r)) return 0;
  return c * r;
}

/** "1.23 kWh". Null/NaN → "—". */
export function formatKwh(n) {
  const v = Number(n);
  if (n == null || Number.isNaN(v)) return '—';
  return `${v.toFixed(2)} kWh`;
}

/** Watts in, human power out: 2300 → "2.3 kW", 640 → "640 W". Null/NaN → "—". */
export function formatKw(w) {
  const v = Number(w);
  if (w == null || Number.isNaN(v)) return '—';
  if (Math.abs(v) >= 1000) return `${(v / 1000).toFixed(1)} kW`;
  return `${Math.round(v)} W`;
}

/** Seconds in, "1h 24m" / "24m 30s" / "45s" out. Null/NaN/negative → "—". */
export function formatDuration(secs) {
  const v = Number(secs);
  if (secs == null || Number.isNaN(v) || v < 0) return '—';
  const h = Math.floor(v / 3600);
  const m = Math.floor((v % 3600) / 60);
  const s = Math.floor(v % 60);
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return s > 0 ? `${m}m ${s}s` : `${m}m`;
  return `${s}s`;
}
