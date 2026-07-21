/**
 * CpoSessions (redesign v3, D5) — charging session history across the CPO's
 * chargers.
 *
 * GET /api/cpo/analytics/sessions?days&plug_id&status_filter&limit&offset —
 * paginated, with server-side `totals` {count, energy_kwh, revenue_coins}
 * computed over the FULL filtered set (not just the page). If an older
 * bare-array response ever comes back instead, this page still works: it
 * sums the fetched page itself and shows a visible "first N shown" caveat
 * rather than silently under-reporting.
 *
 * Row click opens a read-only detail modal via GET /api/sessions/{id} (the
 * same receipt shape the driver's Activity page renders). CSV export mirrors
 * the existing /api/cpo/analytics/sessions.csv endpoint — that's a file
 * download, so it goes through a raw authenticated fetch, not the JSON api
 * client.
 */

import { useCallback, useEffect, useState } from 'react';
import { BatteryCharging, Download } from 'lucide-react';

import CpoLayout from '../../components/CpoLayout';
import { PageHeader, DataTable, Modal, Money, useToast } from '../../components/ui';
import api from '../../api/client';
import { useConfig } from '../../contexts/ConfigContext';
import { formatKwh, formatKw, formatDuration } from '../../utils/money';
import { sessionStatusLabel, stopReasonCopy, apiErrorCopy } from '../../utils/statusCopy';

const SESSIONS_LIMIT = 20;
const DAYS_OPTIONS = [7, 14, 30, 90];

const STATUS_OPTIONS = [
  { value: '', label: 'All statuses' },
  { value: 'active', label: 'Charging' },
  { value: 'completed', label: 'Completed' },
  { value: 'paid', label: 'Paid' },
  { value: 'cancelled', label: 'Cancelled' },
];

const STATUS_TONE = {
  active: 'badge-info',
  completed: 'badge-ok',
  paid: 'badge-ok',
  cancelled: 'badge-danger',
};

const formatDateTime = (isoStr) =>
  isoStr
    ? new Date(isoStr).toLocaleString('en-IN', {
        month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
      })
    : '—';

function Row({ label, children }) {
  return (
    <div className="row-between">
      <span className="text-3 text-sm">{label}</span>
      <span>{children}</span>
    </div>
  );
}

export default function CpoSessions() {
  const { coin_inr_rate: rate = 1 } = useConfig();
  const toast = useToast();

  const [days, setDays] = useState(30);
  const [plugId, setPlugId] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [plugs, setPlugs] = useState([]);

  const [sessions, setSessions] = useState([]);
  const [total, setTotal] = useState(null); // null → legacy bare array, no pager
  const [totals, setTotals] = useState(null); // null → sum the page client-side
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const [exporting, setExporting] = useState(false);

  const fetchSessions = useCallback(async (nextOffset = 0) => {
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams();
      params.set('days', days);
      params.set('limit', SESSIONS_LIMIT);
      params.set('offset', nextOffset);
      if (plugId) params.set('plug_id', plugId);
      if (statusFilter) params.set('status_filter', statusFilter);

      const data = await api.get(`/api/cpo/analytics/sessions?${params.toString()}`);
      if (Array.isArray(data)) {
        setSessions(data);
        setTotal(null);
        setTotals(null);
        setOffset(0);
      } else {
        setSessions(data?.items || data?.sessions || []);
        setTotal(typeof data?.total === 'number' ? data.total : null);
        setTotals(data?.totals || null);
        setOffset(nextOffset);
      }
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, [days, plugId, statusFilter]);

  useEffect(() => {
    fetchSessions(0);
  }, [fetchSessions]);

  // Best-effort — only feeds the charger filter dropdown, never blocks the
  // page and never shows its own error state.
  useEffect(() => {
    let cancelled = false;
    api.get('/api/cpo/plugs')
      .then((data) => { if (!cancelled) setPlugs(Array.isArray(data) ? data : []); })
      .catch(() => {});
    return () => { cancelled = true; };
  }, []);

  const clientTotals = {
    count: sessions.length,
    energy_kwh: sessions.reduce((sum, s) => sum + (s.energy_kwh || 0), 0),
    revenue_coins: sessions.reduce((sum, s) => sum + (s.coins_spent || 0), 0),
  };
  const displayTotals = totals || clientTotals;
  const totalsAreClientSide = !totals;

  const exportCsv = async () => {
    setExporting(true);
    try {
      const params = new URLSearchParams();
      params.set('days', days);
      if (plugId) params.set('plug_id', plugId);
      if (statusFilter) params.set('status_filter', statusFilter);

      const base = import.meta.env.VITE_API_URL || '';
      const token = localStorage.getItem('amphive_token');
      const res = await fetch(`${base}/api/cpo/analytics/sessions.csv?${params.toString()}`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      if (!res.ok) throw new Error(`Export failed (${res.status})`);

      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `amphive-sessions-${new Date().toISOString().slice(0, 10)}.csv`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      toast.ok('Sessions exported.');
    } catch (err) {
      toast.error(apiErrorCopy(err));
    } finally {
      setExporting(false);
    }
  };

  // ---- Detail modal ---------------------------------------------------
  const [detail, setDetail] = useState(null);
  const [detailRow, setDetailRow] = useState(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);

  const openDetail = useCallback(async (row) => {
    if (detailLoading) return;
    setDetailLoading(true);
    setDetailRow(row);
    try {
      const data = await api.get(`/api/sessions/${row.id}`);
      setDetail(data);
      setDetailOpen(true);
    } catch {
      toast.error('Details unavailable.');
    } finally {
      setDetailLoading(false);
    }
  }, [detailLoading, toast]);

  const closeDetail = () => setDetailOpen(false);

  const columns = [
    { key: 'started_at', label: 'Started', render: (s) => formatDateTime(s.started_at) },
    { key: 'plug_name', label: 'Charger', render: (s) => s.plug_name || `Plug #${s.plug_id}` },
    { key: 'user_email', label: 'Driver' },
    {
      key: 'duration_minutes',
      label: 'Duration',
      render: (s) => formatDuration(s.duration_minutes != null ? s.duration_minutes * 60 : null),
    },
    { key: 'energy_kwh', label: 'Energy', num: true, render: (s) => formatKwh(s.energy_kwh) },
    {
      key: 'coins_spent',
      label: 'Revenue',
      num: true,
      render: (s) => <Money coins={s.coins_spent} rate={rate} />,
    },
    {
      key: 'status',
      label: 'Status',
      render: (s) => (
        <span className={`badge ${STATUS_TONE[s.status] || 'badge-info'}`}>
          {sessionStatusLabel(s.status)}
        </span>
      ),
    },
  ];

  return (
    <CpoLayout>
      <PageHeader
        eyebrow="Console"
        title="Sessions"
        sub="Charging session history across your chargers."
        actions={
          <button
            type="button"
            className="btn btn-quiet"
            onClick={exportCsv}
            disabled={exporting || sessions.length === 0}
          >
            <Download size={16} aria-hidden="true" />
            {exporting ? 'Exporting…' : 'Export CSV'}
          </button>
        }
      />

      <div className="filter-bar">
        <select
          className="select"
          value={days}
          onChange={(e) => setDays(Number(e.target.value))}
          aria-label="Date range"
        >
          {DAYS_OPTIONS.map((d) => (
            <option key={d} value={d}>Last {d} days</option>
          ))}
        </select>

        <select
          className="select"
          value={plugId}
          onChange={(e) => setPlugId(e.target.value)}
          aria-label="Charger"
        >
          <option value="">All chargers</option>
          {plugs.map((p) => (
            <option key={p.id} value={p.id}>{p.name}</option>
          ))}
        </select>

        <select
          className="select"
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          aria-label="Status"
        >
          {STATUS_OPTIONS.map((opt) => (
            <option key={opt.value} value={opt.value}>{opt.label}</option>
          ))}
        </select>
      </div>

      <div className="kpi-grid">
        <div className="kpi">
          <span className="kpi-label">Sessions</span>
          <span className="kpi-value">{displayTotals.count}</span>
        </div>
        <div className="kpi">
          <span className="kpi-label">Energy</span>
          <span className="kpi-value">{formatKwh(displayTotals.energy_kwh)}</span>
        </div>
        <div className="kpi">
          <span className="kpi-label">Revenue</span>
          <span className="kpi-value"><Money coins={displayTotals.revenue_coins} rate={rate} /></span>
        </div>
      </div>
      {totalsAreClientSide && sessions.length > 0 && (
        <p className="text-3 text-sm">
          Totals computed from the first {sessions.length} sessions shown, not the full filtered range.
        </p>
      )}

      <DataTable
        columns={columns}
        rows={sessions}
        loading={loading}
        error={error}
        onRetry={() => fetchSessions(offset)}
        emptyIcon={BatteryCharging}
        emptyTitle="No sessions found"
        emptyBody={
          plugId || statusFilter
            ? 'No sessions match these filters. Try widening them.'
            : 'Sessions will appear here once drivers start charging on your chargers.'
        }
        onRowClick={openDetail}
        pagination={
          total != null
            ? { total, offset, limit: SESSIONS_LIMIT, onPage: (o) => fetchSessions(o) }
            : undefined
        }
        collapse
      />

      <Modal
        open={detailOpen}
        onClose={closeDetail}
        title="Session detail"
        size="sm"
        footer={
          <button type="button" className="btn btn-quiet" onClick={closeDetail}>
            Close
          </button>
        }
      >
        {detail && (
          <div className="stack-sm">
            <Row label="Charger">{detail.plug_name || detailRow?.plug_name}</Row>
            <Row label="Driver">{detailRow?.user_email || '—'}</Row>
            <Row label="Status">
              <span className={`badge ${STATUS_TONE[detail.status] || 'badge-info'}`}>
                {sessionStatusLabel(detail.status)}
              </span>
            </Row>
            <Row label="Started"><span className="num">{formatDateTime(detail.started_at)}</span></Row>
            <Row label="Ended"><span className="num">{formatDateTime(detail.ended_at)}</span></Row>
            <Row label="Duration"><span className="num">{formatDuration(detail.duration_sec)}</span></Row>
            <Row label="Energy"><span className="num">{formatKwh(detail.energy_kwh)}</span></Row>
            {detail.peak_power_w != null && (
              <Row label="Peak power"><span className="num">{formatKw(detail.peak_power_w)}</span></Row>
            )}
            <Row label="Revenue"><span className="num"><Money coins={detail.coins_spent} rate={rate} /></span></Row>
            {detail.shortfall_coins > 0 && (
              <Row label="Uncollected">
                <span className="num"><Money coins={detail.shortfall_coins} rate={rate} /></span>
              </Row>
            )}
            {detail.reason && <p className="text-3 text-sm">{stopReasonCopy(detail.reason)}</p>}
          </div>
        )}
      </Modal>
    </CpoLayout>
  );
}
