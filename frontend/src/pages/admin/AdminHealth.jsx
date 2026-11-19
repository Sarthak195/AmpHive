/**
 * AdminHealth: host and process health for the platform admin.
 * Data: GET /api/admin/health (admin only), polled every 30 s while the tab is
 * visible (usePoll pauses on hidden tabs). Any source the backend could not
 * read arrives as null and renders as "unknown", never as a fake zero.
 */

import { useCallback, useState } from 'react';
import PageHeader from '../../components/ui/PageHeader';
import ErrorState from '../../components/ui/ErrorState';
import Skeleton from '../../components/ui/Skeleton';
import api from '../../api/client';
import usePoll from '../../hooks/usePoll';

const POLL_MS = 30_000;

const mb = (v) => (v == null ? 'unknown' : `${Math.round(v)} MB`);

const fmtDuration = (s) => {
  if (s == null) return 'unknown';
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
};

const fmtAgo = (iso) => {
  if (!iso) return 'not run yet';
  const s = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  return s < 120 ? `${s}s ago` : `${Math.round(s / 60)} min ago`;
};

/** One gauge: label, value text and a meter bar (pct null hides the bar). */
function Gauge({ label, value, sub, pct }) {
  const tone = pct == null ? '' : pct >= 90 ? ' is-danger' : pct >= 75 ? ' is-warn' : '';
  return (
    <div className="kpi">
      <span className="kpi-label">{label}</span>
      <span className="kpi-value">{value}</span>
      {pct != null && (
        <div className="meter" role="img" aria-label={`${label} ${Math.round(pct)}% used`}>
          <div className={`meter-fill${tone}`} style={{ width: `${Math.min(100, pct)}%` }} />
        </div>
      )}
      <span className="kpi-sub">{sub}</span>
    </div>
  );
}

const pctOf = (used, total) => (used != null && total ? (100 * used) / total : null);

export default function AdminHealth() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    try {
      setData(await api.get('/api/admin/health'));
      setError(null);
    } catch (err) {
      setError(err);
    }
  }, []);

  usePoll(load, POLL_MS);

  const header = (
    <PageHeader eyebrow="Platform" title="Health" sub="Memory, disk and database on the host. Refreshes every 30 s." />
  );

  if (error && !data) {
    return (
      <>
        {header}
        <ErrorState error={error} onRetry={load} title="Couldn't load health data" />
      </>
    );
  }
  if (!data) {
    return (
      <>
        {header}
        <p className="sr-only" role="status">Loading health data…</p>
        <Skeleton lines={4} />
      </>
    );
  }

  const { memory, cgroup, disk, database, last_runs: runs } = data;
  const memUsed = memory ? memory.total_mb - memory.available_mb : null;
  const swapPct = memory ? pctOf(memory.swap_used_mb, memory.swap_total_mb) : null;

  return (
    <>
      {header}
      {error && <ErrorState error={error} onRetry={load} title="Refresh failed, showing the last reading" />}
      <div className="kpi-grid">
        <Gauge
          label="Host memory"
          value={memory ? `${mb(memory.available_mb)} free` : 'unknown'}
          sub={memory ? `${mb(memUsed)} used of ${mb(memory.total_mb)}` : 'No /proc/meminfo'}
          pct={memory ? pctOf(memUsed, memory.total_mb) : null}
        />
        <Gauge
          label="Swap"
          value={memory ? mb(memory.swap_used_mb) : 'unknown'}
          sub={memory ? `of ${mb(memory.swap_total_mb)}` : 'No /proc/meminfo'}
          pct={swapPct}
        />
        <Gauge
          label="Container memory"
          value={cgroup ? mb(cgroup.current_mb) : 'unknown'}
          sub={cgroup ? (cgroup.limit_mb == null ? 'No limit set' : `limit ${mb(cgroup.limit_mb)}`) : 'No cgroup v2 data'}
          pct={cgroup ? pctOf(cgroup.current_mb, cgroup.limit_mb) : null}
        />
        <Gauge
          label="Backend memory"
          value={mb(data.backend_rss_mb)}
          sub="Resident set size of the API process"
          pct={null}
        />
        <Gauge
          label="Disk /"
          value={disk ? `${disk.free_gb} GB free` : 'unknown'}
          sub={disk ? `${disk.used_pct}% of ${disk.total_gb} GB used` : 'Unavailable'}
          pct={disk?.used_pct ?? null}
        />
        <Gauge
          label="Database"
          value={database?.size_mb == null ? 'unknown' : mb(database.size_mb)}
          sub={database?.connections == null ? 'Connections unknown' : `${database.connections} connections`}
          pct={null}
        />
        <Gauge
          label="Uptime"
          value={fmtDuration(data.process_uptime_s)}
          sub={`API process. Host up ${fmtDuration(data.host_uptime_s)}`}
          pct={null}
        />
        <Gauge
          label="Background jobs"
          value={`Reaper ${fmtAgo(runs?.session_reaper)}`}
          sub={`Telemetry flush ${fmtAgo(runs?.telemetry_flush)}`}
          pct={null}
        />
      </div>
    </>
  );
}
