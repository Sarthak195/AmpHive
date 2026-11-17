/**
 * SessionPowerCurve: the power-over-time curve of one session, drawn as an
 * inline SVG (no chart library, so driver pages keep recharts out of the
 * first-load bundle).
 *
 * Fetches GET /api/sessions/{id}/telemetry (server-bucketed to ~120 points).
 * With `live` ({ power_w, energy_kwh, at }) the series is refetched at most
 * every 60 s (replacing the server points, which fills any socket gaps) and
 * the client's own telemetry frames are drawn after the last server point in
 * between. Live frames carry power only, so the energy line always uses the
 * server points as its single basis. When the session has a kWh limit,
 * cumulative energy is drawn as a thin line against a dashed limit marker.
 * `endedAt` (receipt use): a session that ended seconds ago may still have
 * readings in the 10 s DB batch flush, so a short curve is refetched once.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import api from '../api/client';
import { formatKw, formatKwh, formatDuration } from '../utils/money';
import './SessionPowerCurve.css';

const W = 320;
const H = 110;
const PAD = { l: 4, r: 4, t: 8, b: 4 };
const MIN_LIVE_GAP_SEC = 5;
const REFETCH_MS = 60_000;
const RETRY_WINDOW_MS = 15_000;
const RETRY_DELAY_MS = 12_000;
const END_SLACK_SEC = 5;

const line = (pts) => pts.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(' ');

function buildCurve(points, serverPoints, limitKwh) {
  const maxT = Math.max(1, ...points.map((p) => p.t));
  const maxP = Math.max(1, ...points.map((p) => p.power_w));
  const maxE = Math.max(0, ...serverPoints.map((p) => p.energy_kwh || 0));
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
    energy: eTop && serverPoints.length >= 2
      ? line(serverPoints.map((p) => [x(p.t), ey(p.energy_kwh || 0)]))
      : null,
    limitY: eTop ? ey(Number(limitKwh)) : null,
  };
}

const SessionPowerCurve = ({ sessionId, limitKwh = null, live = null, endedAt = null }) => {
  const [state, setState] = useState({ loading: true, failed: false, data: null, server: [] });
  const [livePts, setLivePts] = useState([]);
  const reqRef = useRef(0);
  const serverLastRef = useRef(-1);
  const isLive = live != null;

  // Latest request wins; a failure keeps whatever was already drawn.
  const load = useCallback(async () => {
    const req = ++reqRef.current;
    try {
      const data = await api.get(`/api/sessions/${sessionId}/telemetry`);
      if (req !== reqRef.current) return null;
      const server = (data.points || []).map((p) => ({
        t: p.offset_sec,
        power_w: p.power_w,
        peak_w: p.peak_power_w ?? p.power_w,
        current_a: p.current_a,
        energy_kwh: p.energy_kwh,
      }));
      const lastT = server.length ? server[server.length - 1].t : -1;
      serverLastRef.current = lastT;
      setState({ loading: false, failed: false, data, server });
      // Live frames at or before the last server offset are now covered.
      setLivePts((prev) => prev.filter((p) => p.t > lastT));
      return { data, server };
    } catch {
      if (req === reqRef.current) setState((prev) => ({ ...prev, loading: false, failed: true }));
      return null;
    }
  }, [sessionId]);

  useEffect(() => {
    let timer;
    serverLastRef.current = -1;
    setState({ loading: true, failed: false, data: null, server: [] });
    setLivePts([]);
    if (sessionId == null) return undefined;
    const first = load();
    if (endedAt) {
      // Readings reach the DB on a 10 s batch flush: a session that ended
      // moments ago can still be missing its tail, so look once more.
      first.then((res) => {
        if (!res) return;
        const endMs = new Date(endedAt).getTime();
        const startMs = new Date(res.data.started_at).getTime();
        if (!(Date.now() - endMs < RETRY_WINDOW_MS)) return;
        const last = res.server[res.server.length - 1];
        const spanSec = (endMs - startMs) / 1000;
        const short = !last || last.t + (res.data.bucket_seconds || 1) + END_SLACK_SEC < spanSec;
        if (short) timer = setTimeout(load, RETRY_DELAY_MS);
      });
    }
    return () => {
      reqRef.current += 1;
      clearTimeout(timer);
    };
  }, [sessionId, endedAt, load]);

  // Live: pull the server series again every minute (a failed fetch retries
  // on the next tick).
  useEffect(() => {
    if (!isLive || sessionId == null) return undefined;
    const id = setInterval(() => {
      if (!document.hidden) load();
    }, REFETCH_MS);
    return () => clearInterval(id);
  }, [isLive, sessionId, load]);

  const startedMs = state.data?.started_at ? new Date(state.data.started_at).getTime() : null;
  const liveAt = live?.at ?? null;
  const livePower = live?.power_w;
  useEffect(() => {
    if (state.loading || startedMs == null || liveAt == null) return;
    const t = Math.round((liveAt - startedMs) / 1000);
    if (t <= serverLastRef.current || Number.isNaN(Number(livePower))) return;
    setLivePts((prev) => {
      const last = prev[prev.length - 1];
      if (last && t - last.t < MIN_LIVE_GAP_SEC) return prev;
      return [...prev, { t, power_w: Number(livePower) || 0 }];
    });
  }, [liveAt, livePower, startedMs, state.loading]);

  const limit = limitKwh ?? state.data?.max_kwh ?? null;
  const { server } = state;
  const points = useMemo(() => [...server, ...livePts], [server, livePts]);
  const curve = useMemo(
    () => (points.length >= 2 ? buildCurve(points, server, limit) : null),
    [points, server, limit],
  );
  // The real peak (per-bucket maxima / live frames), not the max of averages,
  // so it matches the receipt's "Peak power" row.
  const peakW = useMemo(
    () => Math.max(0, ...server.map((p) => p.peak_w), ...livePts.map((p) => p.power_w)),
    [server, livePts],
  );
  const avgA = useMemo(() => {
    const amps = server.map((p) => Number(p.current_a)).filter((a) => a > 0);
    return amps.length ? amps.reduce((a, b) => a + b, 0) / amps.length : null;
  }, [server]);

  if (state.loading) return null;

  if (!curve) {
    return (
      <p className="power-curve-empty text-3 text-sm">
        {state.failed
          ? 'Power curve unavailable'
          : points.length === 1 || isLive
            ? 'Collecting power readings…'
            : 'No power readings were recorded for this session.'}
      </p>
    );
  }

  const label = `Power over time, peak ${formatKw(peakW)} over ${formatDuration(curve.maxT)}` +
    (avgA != null ? `, average current ${avgA.toFixed(1)} A` : '') +
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
        <span>Peak <span className="num">{formatKw(peakW)}</span></span>
        {avgA != null && <span>Avg <span className="num">{avgA.toFixed(1)} A</span></span>}
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
