/**
 * NetworkStatus: a collapsed "Network status" panel on the public /map page.
 * Fetches GET /api/status (public, cached 60 s server-side) only once opened,
 * then re-polls every 60 s while open. Inline CSS bars only (no recharts: the
 * map is a startup page).
 */
import { useCallback, useState } from 'react';
import api from '../api/client';
import usePoll from '../hooks/usePoll';
import './NetworkStatus.css';

const OVERALL_LABEL = {
  operational: 'All systems operational',
  degraded: 'Some chargers unreachable',
  down: 'Service down',
};

function StatusBody({ data }) {
  const hourly = data.tariff_today?.hourly ?? [];
  const max = Math.max(...hourly, 0.01);
  const nowHour = new Date().getHours();
  const { gateways, plugs } = data;
  return (
    <div className="net-status-body">
      <p className={`net-status-overall net-status-${data.status}`}>
        {OVERALL_LABEL[data.status] ?? data.status}
      </p>
      <p className="net-status-line">
        Gateways online: {gateways.online}/{gateways.total}
      </p>
      <p className="net-status-line">
        Chargers: {plugs.available} free, {plugs.occupied} in use, {plugs.offline} offline
        {plugs.maintenance > 0 ? `, ${plugs.maintenance} in maintenance` : ''}
      </p>
      {data.reachability.length > 0 && (
        <ul className="net-status-reach" aria-label="Seven-day reachability">
          {data.reachability.map((p) => (
            <li key={p.plug_id}>
              <span>{p.name}</span>
              <span>{p.uptime_7d_pct == null ? 'n/a' : `${p.uptime_7d_pct}% reachable, 7 d`}</span>
            </li>
          ))}
        </ul>
      )}
      {hourly.length === 24 && (
        <div className="net-status-tariff">
          <span className="net-status-line">Today's price per kWh, by hour</span>
          <div className="net-status-strip" role="img" aria-label={`Hourly price today, ${Math.min(...hourly)} to ${Math.max(...hourly)} per kWh`}>
            {hourly.map((rate, h) => (
              <span
                key={h}
                className={h === nowHour ? 'net-status-bar is-now' : 'net-status-bar'}
                style={{ height: `${Math.max(12, (rate / max) * 100)}%` }}
                title={`${String(h).padStart(2, '0')}:00  ${rate}`}
              />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

export default function NetworkStatus() {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState(null);
  const [failed, setFailed] = useState(false);

  const load = useCallback(async () => {
    if (!open) return;
    try {
      setData(await api.get('/api/status'));
      setFailed(false);
    } catch {
      setFailed(true);
    }
  }, [open]);

  usePoll(load, 60000, [open]);

  return (
    <section className="net-status">
      <button
        type="button"
        className="btn btn-ghost btn-sm net-status-toggle"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        Network status
      </button>
      {open && (
        <div aria-live="polite">
          {data ? <StatusBody data={data} /> : failed ? null : <p className="net-status-line">Loading…</p>}
          {failed && <p className="net-status-line">Status unavailable right now.</p>}
        </div>
      )}
    </section>
  );
}
