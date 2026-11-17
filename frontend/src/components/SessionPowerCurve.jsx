/**
 * SessionPowerCurve — the power-over-time curve of one session, drawn as an
 * inline SVG (no chart library, so driver pages keep recharts out of the
 * first-load bundle).
 *
 * Fetches GET /api/sessions/{id}/telemetry once (server-bucketed to ~120
 * points). With `live` ({ power_w, energy_kwh, at }) it appends points as the
 * client's own telemetry frames arrive instead of refetching. When the session
 * has a kWh limit, cumulative energy is drawn as a thin line against a dashed
 * limit marker.
 */

import { useEffect, useMemo, useState } from 'react';

import api from '../api/client';
import { formatKw, formatKwh, formatDuration } from '../utils/money';
import './SessionPowerCurve.css';

const W = 320;
const H = 110;
const PAD = { l: 4, r: 4, t: 8, b: 4 };
const MIN_LIVE_GAP_SEC = 5;
const MAX_POINTS = 720;

const line = (pts) => pts.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(' ');

function buildCurve(points, limitKwh) {
  const maxT = Math.max(1, ...points.map((p) => p.t));
  const maxP = Math.max(1, ...points.map((p) => p.power_w));
  const maxE = Math.max(0, ...points.map((p) => p.energy_kwh || 0));
  const eTop = limitKwh > 0 ? Math.max(Number(limitKwh), maxE) : 0;
  const iw = W - PAD.l - PAD.r;
  const ih = H - PAD.t - PAD.b;
  const x = (t) => PAD.l + (t / maxT) * iw;
  const py = (v) => PAD.t + ih - (v / maxP) * ih;
  const ey = (v) => PAD.t + ih - (v / eTop) * ih;
  const power = points.map((p) => [x(p.t), py(p.power_w)]);
  const area = `${PAD.l},${PAD.t + ih} ${line(power)} ${x(points[points.length - 1].t).toFixed(1)},${PAD.t + ih}`;
  return {
    maxT,
    maxP,
    power: line(power),
    area,
    energy: eTop ? line(points.map((p) => [x(p.t), ey(p.energy_kwh || 0)])) : null,
    limitY: eTop ? ey(Number(limitKwh)) : null,
  };
}

const SessionPowerCurve = ({ sessionId, limitKwh = null, live = null }) => {
  const [state, setState] = useState({ loading: true, data: null, points: [] });

  useEffect(() => {
    let cancelled = false;
    setState({ loading: true, data: null, points: [] });
    if (sessionId == null) return undefined;
    api
      .get(`/api/sessions/${sessionId}/telemetry`)
      .then((data) => {
        if (cancelled) return;
        const points = (data.points || []).map((p) => ({
          t: p.offset_sec,
          power_w: p.power_w,
          energy_kwh: p.energy_kwh,
        }));
        // Live frames that arrived while the fetch was in flight are kept.
        const lastT = points.length ? points[points.length - 1].t : -1;
        setState((prev) => ({
          loading: false,
          data,
          points: [...points, ...prev.points.filter((p) => p.t >= lastT + MIN_LIVE_GAP_SEC)],
        }));
      })
      .catch(() => { if (!cancelled) setState({ loading: false, data: null, points: [] }); });
    return () => { cancelled = true; };
  }, [sessionId]);

  const startedMs = state.data?.started_at ? new Date(state.data.started_at).getTime() : null;
  const liveAt = live?.at ?? null;
  const livePower = live?.power_w;
  const liveEnergy = live?.energy_kwh;
  useEffect(() => {
    if (state.loading || startedMs == null || liveAt == null) return;
    const t = Math.round((liveAt - startedMs) / 1000);
    if (t < 0 || Number.isNaN(Number(livePower))) return;
    setState((prev) => {
      const last = prev.points[prev.points.length - 1];
      if (last && t - last.t < MIN_LIVE_GAP_SEC) return prev;
      const next = [...prev.points, {
        t, power_w: Number(livePower) || 0, energy_kwh: Number(liveEnergy) || 0,
      }];
      return { ...prev, points: next.length > MAX_POINTS ? next.slice(-MAX_POINTS) : next };
    });
  }, [liveAt, livePower, liveEnergy, startedMs, state.loading]);

  const limit = limitKwh ?? state.data?.max_kwh ?? null;
  const curve = useMemo(
    () => (state.points.length >= 2 ? buildCurve(state.points, limit) : null),
    [state.points, limit],
  );

  if (state.loading) return null;

  if (!curve) {
    return (
      <p className="power-curve-empty text-3 text-sm">
        {state.points.length === 1 || live
          ? 'Collecting power readings…'
          : 'No power readings were recorded for this session.'}
      </p>
    );
  }

  const label = `Power over time, peak ${formatKw(curve.maxP)} over ${formatDuration(curve.maxT)}` +
    (limit ? `, energy limit ${formatKwh(Number(limit))}` : '');

  return (
    <figure className="power-curve">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={label} preserveAspectRatio="none">
        <polygon className="power-curve-area" points={curve.area} />
        <polyline className="power-curve-line" points={curve.power} />
        {curve.energy && <polyline className="power-curve-energy" points={curve.energy} />}
        {curve.limitY != null && (
          <line className="power-curve-limit" x1={PAD.l} x2={W - PAD.r} y1={curve.limitY} y2={curve.limitY} />
        )}
      </svg>
      <figcaption className="power-curve-caption text-3 text-xs">
        <span>Peak <span className="num">{formatKw(curve.maxP)}</span></span>
        {curve.energy && (
          <span className="power-curve-key">
            Energy vs <span className="num">{formatKwh(Number(limit))}</span> limit (dashed)
          </span>
        )}
        <span className="num">{formatDuration(curve.maxT)}</span>
      </figcaption>
    </figure>
  );
};

export default SessionPowerCurve;
