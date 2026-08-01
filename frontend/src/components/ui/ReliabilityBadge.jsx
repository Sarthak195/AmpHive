/**
 * ReliabilityBadge — a plug's rolling REACHABILITY (gateway + mains power),
 * NOT availability-to-charge — see backend/services/reliability.py's
 * docstring for the full definition. Renders "98% online · 7d" plus a
 * "seen Xm ago" trailer from `last_seen_at`.
 *
 * Fetch-on-mount only (GET /api/plugs/{plugId}/reliability) — no polling,
 * this is a slow-moving historical stat, not live telemetry. Deliberately
 * NOT wired into the bulk plug-list endpoints (cost — see the backend
 * route's docstring), so every mount of this badge is its own request;
 * callers should mount it lazily (e.g. inside a Leaflet popup, which only
 * renders its children once opened) rather than in a dense always-visible
 * grid.
 *
 * Quiet-fail (mirrors Dashboard's month-stats hide-on-error convention):
 * renders a skeleton while loading, then either the stat or NOTHING — never
 * an error banner, since this is a secondary signal, not core plug info. A
 * plug too young to have a meaningful reading (uptime_pct: null from the
 * backend) is treated the same as an error: nothing renders.
 */
import { useEffect, useState } from 'react';
import { SignalHigh } from 'lucide-react';
import api from '../../api/client';
import './ui.css';

const timeAgo = (iso) => {
  if (!iso) return '';
  const secs = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (secs < 60) return 'just now';
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
};

// sample_window_days -> "7d" (or "18h" for a young plug's short window).
const windowLabel = (days) => {
  if (days == null) return '';
  return days >= 1 ? `${Math.round(days)}d` : `${Math.round(days * 24)}h`;
};

export default function ReliabilityBadge({ plugId }) {
  const [state, setState] = useState({ status: 'loading' });

  useEffect(() => {
    if (plugId == null) {
      setState({ status: 'hidden' });
      return undefined;
    }
    let cancelled = false;
    setState({ status: 'loading' });
    api
      .get(`/api/plugs/${plugId}/reliability`)
      .then((res) => {
        if (cancelled) return;
        if (res && res.uptime_pct != null) setState({ status: 'ready', data: res });
        else setState({ status: 'hidden' });
      })
      .catch(() => {
        if (!cancelled) setState({ status: 'hidden' });
      });
    return () => {
      cancelled = true;
    };
  }, [plugId]);

  if (state.status === 'hidden') return null;

  if (state.status === 'loading') {
    return (
      <span className="reliability-badge" aria-hidden="true">
        <span className="skeleton skeleton-text reliability-badge-skeleton" />
      </span>
    );
  }

  const { data } = state;
  return (
    <span className="reliability-badge text-3 text-xs">
      <SignalHigh size={12} aria-hidden="true" />
      {Math.round(data.uptime_pct)}% online · {windowLabel(data.sample_window_days)}
      {data.last_seen_at && ` · seen ${timeAgo(data.last_seen_at)}`}
    </span>
  );
}
