/**
 * CpoDashboard (redesign v3, D1) — the console's landing analytics overview.
 *
 * Five independently-loaded sections so one flaky endpoint never blanks the
 * whole page: KPI row (GET /api/cpo/analytics/overview), revenue (GET
 * /api/cpo/analytics/revenue?days=30), energy (GET
 * /api/cpo/analytics/energy?days=30), 24h load (GET
 * /api/cpo/analytics/telemetry?days=1&bucket=hour) and recent sessions (GET
 * /api/cpo/analytics/sessions?limit=8). Each section renders its own
 * skeleton -> ErrorState-with-retry -> content/EmptyState; a failure in one
 * card never hides the others. Polls every 30s while the tab is visible.
 *
 * CpoLayout (mounted by this page, per file ownership) already renders
 * CpoAlerts and provides TenantContext — this page no longer fetches the
 * profile or passes tenantName anywhere.
 */

import { useCallback, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  AreaChart, Area, BarChart, Bar, Line, ComposedChart,
  XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from 'recharts';
import { Banknote, Zap, BatteryCharging, Radio } from 'lucide-react';

import CpoLayout from '../../components/CpoLayout';
import { PageHeader, DataTable, Skeleton, SkeletonTitle, ErrorState, EmptyState, Money } from '../../components/ui';
import api from '../../api/client';
import usePoll from '../../hooks/usePoll';
import { useConfig } from '../../contexts/ConfigContext';
import { formatKwh, formatINR, coinsToINR } from '../../utils/money';
import { sessionStatusLabel } from '../../utils/statusCopy';
import './CpoDashboard.css';

const POLL_MS = 30_000;
const RECENT_SESSIONS_LIMIT = 8;

const SESSION_STATUS_TONE = {
  active: 'badge-info',
  completed: 'badge-ok',
  paid: 'badge-ok',
  billed: 'badge-ok',
  cancelled: 'badge-danger',
  orphaned: 'badge-warn',
};

const formatDateShort = (dateStr) =>
  dateStr ? new Date(dateStr).toLocaleDateString('en-IN', { month: 'short', day: 'numeric' }) : '';

const formatDateTime = (isoStr) =>
  isoStr
    ? new Date(isoStr).toLocaleString('en-IN', {
        month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
      })
    : '—';

const formatHour = (isoStr) =>
  isoStr ? new Date(isoStr).toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', hour12: false }) : '';

/** Empty async section state: not yet loaded, no error, nothing fetched. */
const initSection = () => ({ data: null, error: null, loading: true });

/** Shared card tooltip — surface, border, ₹/kWh/W formatting per series. */
function ChartTooltip({ active, payload, label, labelFormatter, valueFormatter }) {
  if (!active || !payload?.length) return null;
  return (
    <div className="dash-tooltip">
      <p className="dash-tooltip-label">{labelFormatter ? labelFormatter(label) : label}</p>
      {payload.map((entry) => (
        <p key={entry.dataKey} className="dash-tooltip-row" style={{ color: entry.color }}>
          {entry.name}: {valueFormatter ? valueFormatter(entry.value) : entry.value}
        </p>
      ))}
    </div>
  );
}

const axisTick = { fill: 'var(--ink-3)', fontSize: 11, fontFamily: 'var(--font-data)' };

const CpoDashboard = () => {
  const { coin_inr_rate: rate = 1 } = useConfig();

  const [overview, setOverview] = useState(initSection);
  const [revenue, setRevenue] = useState(initSection);
  const [energy, setEnergy] = useState(initSection);
  const [load, setLoad] = useState(initSection);
  const [sessions, setSessions] = useState(initSection);

  const loadOverview = useCallback(async () => {
    try {
      const data = await api.get('/api/cpo/analytics/overview');
      setOverview({ data, error: null, loading: false });
    } catch (err) {
      setOverview((s) => ({ ...s, error: err, loading: false }));
    }
  }, []);

  const loadRevenue = useCallback(async () => {
    try {
      const data = await api.get('/api/cpo/analytics/revenue?days=30');
      setRevenue({ data, error: null, loading: false });
    } catch (err) {
      setRevenue((s) => ({ ...s, error: err, loading: false }));
    }
  }, []);

  const loadEnergy = useCallback(async () => {
    try {
      const data = await api.get('/api/cpo/analytics/energy?days=30');
      setEnergy({ data, error: null, loading: false });
    } catch (err) {
      setEnergy((s) => ({ ...s, error: err, loading: false }));
    }
  }, []);

  const loadLoad = useCallback(async () => {
    try {
      const data = await api.get('/api/cpo/analytics/telemetry?days=1&bucket=hour');
      setLoad({ data, error: null, loading: false });
    } catch (err) {
      setLoad((s) => ({ ...s, error: err, loading: false }));
    }
  }, []);

  const loadSessions = useCallback(async () => {
    try {
      const res = await api.get(`/api/cpo/analytics/sessions?limit=${RECENT_SESSIONS_LIMIT}`);
      const items = Array.isArray(res) ? res : res?.items || [];
      setSessions({ data: items, error: null, loading: false });
    } catch (err) {
      setSessions((s) => ({ ...s, error: err, loading: false }));
    }
  }, []);

  const loadAll = useCallback(() => {
    loadOverview();
    loadRevenue();
    loadEnergy();
    loadLoad();
    loadSessions();
  }, [loadOverview, loadRevenue, loadEnergy, loadLoad, loadSessions]);

  usePoll(loadAll, POLL_MS);

  const ov = overview.data;
  const gatewaysTotal = ov?.gateways?.total ?? 0;
  const gatewaysOnline = ov?.gateways?.online ?? 0;
  const gatewaysGap = Math.max(0, gatewaysTotal - gatewaysOnline);

  const sessionColumns = [
    { key: 'plug_name', label: 'Charger' },
    { key: 'user_email', label: 'Driver' },
    { key: 'started_at', label: 'Started', render: (row) => formatDateTime(row.started_at) },
    { key: 'energy_kwh', label: 'Energy', num: true, render: (row) => formatKwh(row.energy_kwh) },
    {
      key: 'coins_spent',
      label: 'Revenue',
      num: true,
      render: (row) => <Money coins={row.coins_spent} rate={rate} />,
    },
    {
      key: 'status',
      label: 'Status',
      render: (row) => (
        <span className={`badge ${SESSION_STATUS_TONE[row.status] || 'badge'}`}>
          {sessionStatusLabel(row.status)}
        </span>
      ),
    },
  ];

  const peakW = load.data?.length ? Math.max(...load.data.map((d) => d.max_power_w || 0)) : null;
  const peakA = load.data?.length ? Math.max(...load.data.map((d) => d.max_current_a || 0)) : null;

  return (
    <CpoLayout>
      <PageHeader
        eyebrow="Console"
        title="Dashboard"
        sub="How your charging network is doing right now."
      />

      <div className="stack">
      {/* ---- KPI row -------------------------------------------------- */}
      {overview.loading ? (
        <div className="kpi-grid" aria-hidden="true">
          {Array.from({ length: 4 }, (_, i) => (
            <div className="kpi" key={i}>
              <SkeletonTitle />
              <Skeleton lines={1} />
            </div>
          ))}
        </div>
      ) : overview.error ? (
        <ErrorState error={overview.error} onRetry={loadOverview} title="Couldn't load your stats" />
      ) : (
        <div className="kpi-grid" aria-live="polite">
          <Link to="/cpo/sessions" className="kpi">
            <span className="kpi-label dash-kpi-label">
              <Banknote size={14} aria-hidden="true" />
              Today&rsquo;s revenue
            </span>
            <span className="kpi-value"><Money coins={ov?.today?.revenue_coins} rate={rate} /></span>
            <span className="kpi-sub">
              All-time <Money coins={ov?.all_time?.revenue_coins} rate={rate} />
            </span>
          </Link>

          <Link to="/cpo/sessions" className="kpi">
            <span className="kpi-label dash-kpi-label">
              <Zap size={14} aria-hidden="true" />
              Today&rsquo;s energy
            </span>
            <span className="kpi-value">{formatKwh(ov?.today?.energy_kwh)}</span>
            <span className="kpi-sub">All-time {formatKwh(ov?.all_time?.energy_kwh)}</span>
          </Link>

          <Link to="/cpo/sessions?status=active" className="kpi">
            <span className="kpi-label dash-kpi-label">
              <BatteryCharging size={14} aria-hidden="true" />
              Active sessions
            </span>
            <span className="kpi-value">{ov?.active_sessions ?? 0}</span>
            <span className="kpi-sub">{ov?.today?.sessions ?? 0} today &middot; {ov?.all_time?.sessions ?? 0} all-time</span>
          </Link>

          <Link to="/cpo/gateways" className="kpi">
            <span className="kpi-label dash-kpi-label">
              <Radio size={14} aria-hidden="true" />
              Gateways online
            </span>
            <span className="kpi-value">
              {gatewaysOnline} / {gatewaysTotal}
              {gatewaysGap > 0 && (
                <span className="badge badge-danger dash-kpi-badge">{gatewaysGap} offline</span>
              )}
            </span>
            <span className="kpi-sub">{ov?.plugs?.total ?? 0} chargers total</span>
          </Link>
        </div>
      )}

      {/* ---- Revenue + energy charts ------------------------------------ */}
      <div className="dash-charts-row">
        <section className="card dash-chart">
          <h2>Revenue (last 30 days)</h2>
          {revenue.loading ? (
            <Skeleton lines={5} />
          ) : revenue.error ? (
            <ErrorState error={revenue.error} onRetry={loadRevenue} title="Couldn't load revenue" />
          ) : !revenue.data?.length ? (
            <EmptyState icon={Banknote} title="No revenue yet" body="Billed sessions will chart here." />
          ) : (
            <ResponsiveContainer width="100%" height={220}>
              <AreaChart data={revenue.data}>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
                <XAxis dataKey="date" tickFormatter={formatDateShort} tick={axisTick} axisLine={{ stroke: 'var(--border)' }} tickLine={false} />
                <YAxis tick={axisTick} axisLine={{ stroke: 'var(--border)' }} tickLine={false} />
                <Tooltip
                  content={
                    <ChartTooltip
                      labelFormatter={formatDateShort}
                      valueFormatter={(v) => formatINR(coinsToINR(v, rate))}
                    />
                  }
                />
                <Area
                  type="monotone"
                  dataKey="revenue_coins"
                  name="Revenue"
                  stroke="var(--brand)"
                  strokeWidth={2}
                  fill="var(--brand)"
                  fillOpacity={0.16}
                />
              </AreaChart>
            </ResponsiveContainer>
          )}
        </section>

        <section className="card dash-chart">
          <h2>Energy (last 30 days)</h2>
          {energy.loading ? (
            <Skeleton lines={5} />
          ) : energy.error ? (
            <ErrorState error={energy.error} onRetry={loadEnergy} title="Couldn't load energy" />
          ) : !energy.data?.length ? (
            <EmptyState icon={Zap} title="No energy yet" body="Charging sessions will chart here." />
          ) : (
            <ResponsiveContainer width="100%" height={220}>
              <BarChart data={energy.data}>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
                <XAxis dataKey="date" tickFormatter={formatDateShort} tick={axisTick} axisLine={{ stroke: 'var(--border)' }} tickLine={false} />
                <YAxis tick={axisTick} axisLine={{ stroke: 'var(--border)' }} tickLine={false} />
                <Tooltip
                  content={
                    <ChartTooltip labelFormatter={formatDateShort} valueFormatter={(v) => formatKwh(v)} />
                  }
                />
                <Bar dataKey="energy_kwh" name="Energy" fill="var(--info)" radius={[4, 4, 0, 0]} maxBarSize={24} />
              </BarChart>
            </ResponsiveContainer>
          )}
        </section>
      </div>

      {/* ---- Load (last 24h) --------------------------------------------- */}
      <section className="card dash-chart">
        <div className="row-between dash-chart-head">
          <h2>Load (last 24h)</h2>
          {peakW != null && (
            <span className="text-3 text-sm num">
              Peak {peakW.toFixed(0)} W &middot; {peakA.toFixed(1)} A
            </span>
          )}
        </div>
        {load.loading ? (
          <Skeleton lines={5} />
        ) : load.error ? (
          <ErrorState error={load.error} onRetry={loadLoad} title="Couldn't load telemetry" />
        ) : !load.data?.length ? (
          <EmptyState title="No telemetry in the last 24h" body="Load readings will chart here once a charger reports telemetry." />
        ) : (
          <ResponsiveContainer width="100%" height={220}>
            <ComposedChart data={load.data}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
              <XAxis dataKey="timestamp" tickFormatter={formatHour} tick={axisTick} axisLine={{ stroke: 'var(--border)' }} tickLine={false} />
              <YAxis tick={axisTick} axisLine={{ stroke: 'var(--border)' }} tickLine={false} />
              <Tooltip
                content={
                  <ChartTooltip
                    labelFormatter={formatHour}
                    valueFormatter={(v) => `${Number(v).toFixed(0)} W`}
                  />
                }
              />
              <Area
                type="monotone"
                dataKey="avg_power_w"
                name="Avg power"
                stroke="var(--brand)"
                strokeWidth={2}
                fill="var(--brand)"
                fillOpacity={0.14}
              />
              <Line
                type="monotone"
                dataKey="max_power_w"
                name="Peak power"
                stroke="var(--warn)"
                strokeWidth={1.5}
                strokeDasharray="4 3"
                dot={false}
              />
            </ComposedChart>
          </ResponsiveContainer>
        )}
      </section>

      {/* ---- Recent sessions ---------------------------------------------- */}
      <section className="card">
        <div className="row-between dash-chart-head">
          <h2>Recent sessions</h2>
          <Link to="/cpo/sessions" className="btn btn-quiet btn-sm">View all</Link>
        </div>
        <DataTable
          columns={sessionColumns}
          rows={sessions.data || []}
          loading={sessions.loading}
          error={sessions.error}
          onRetry={loadSessions}
          emptyIcon={BatteryCharging}
          emptyTitle="No sessions yet"
          emptyBody="Sessions will appear here once drivers start charging on your chargers."
          collapse
        />
      </section>
      </div>
    </CpoLayout>
  );
};

export default CpoDashboard;
