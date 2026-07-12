/**
 * Charging-limit helpers, shared by ChargeLimitControl (the Home start-flow
 * control) and Home itself (which converts the raw spec into the start
 * payload at send time — see ChargeLimitControl's module docstring for why
 * the conversion doesn't live inside the control).
 */

// Backend SessionStartRequest bounds (backend/schemas.py): 0.1–100 kWh and
// 60 s–24 h. Clamp client-side so a typo can't 422 the start call.
export const clampKwh = (k) => Math.min(100, Math.max(0.1, Math.round(k * 100) / 100));
export const clampSeconds = (s) => Math.min(86400, Math.max(60, s));

// spec: { mode: 'kwh'|'time'|'coins', value: string } | null
// rate: coins per kWh used for the coins→kWh conversion.
// Returns { max_kwh } | { max_duration_seconds } | null (no/invalid limit).
export const computeChargeLimits = (spec, rate) => {
  if (!spec) return null;
  const v = parseFloat(spec.value);
  if (!Number.isFinite(v) || v <= 0) return null;
  if (spec.mode === 'kwh') return { max_kwh: clampKwh(v) };
  if (spec.mode === 'coins') {
    const r = Number(rate) > 0 ? Number(rate) : 5;
    return { max_kwh: clampKwh(v / r) };
  }
  // time — value is in hours (decimals fine: 0.5 = 30 min)
  return { max_duration_seconds: clampSeconds(Math.round(v * 3600)) };
};

export const formatDuration = (totalSeconds) => {
  const h = Math.floor(totalSeconds / 3600);
  const m = Math.round((totalSeconds % 3600) / 60);
  if (h > 0 && m > 0) return `${h} h ${m} min`;
  if (h > 0) return `${h} h`;
  return `${m} min`;
};
