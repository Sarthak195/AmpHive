/**
 * AmpHive CPO Sessions Page
 * ===========================
 * Session history view for the CPO with filters (plug, status, date range)
 * and a summary stats bar showing totals for the filtered range.
 *
 * Data source: /api/cpo/analytics/sessions, /api/cpo/plugs
 */

import { useState, useEffect, useCallback } from 'react';
import CpoLayout from '../../components/CpoLayout';
import CpoSummary from '../../components/CpoSummary';
import api from '../../api/client';

/**
 * Session status options for filtering.
 */
const STATUS_OPTIONS = [
  { value: '', label: 'All Statuses' },
  { value: 'active', label: 'Active' },
  { value: 'completed', label: 'Completed' },
  { value: 'paid', label: 'Paid' },
  { value: 'cancelled', label: 'Cancelled' },
];

/**
 * Date range options.
 */
const DATE_RANGE_OPTIONS = [
  { value: 7, label: 'Last 7 days' },
  { value: 14, label: 'Last 14 days' },
  { value: 30, label: 'Last 30 days' },
  { value: 90, label: 'Last 90 days' },
];

/**
 * Map session status to badge class.
 */
const STATUS_BADGE = {
  active: 'badge-success',
  completed: 'badge-primary',
  paid: 'badge-primary',
  cancelled: 'badge-danger',
};

const CpoSessions = () => {
  const [sessions, setSessions] = useState([]);
  const [plugs, setPlugs] = useState([]);
  const [tenantName, setTenantName] = useState('');
  const [loading, setLoading] = useState(true);

  // Filter state
  const [plugFilter, setPlugFilter] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [daysFilter, setDaysFilter] = useState(30);

  const fetchData = useCallback(async () => {
    setLoading(true);
    try {
      // Build query string for sessions
      const params = new URLSearchParams();
      params.set('days', daysFilter);
      params.set('limit', '100');
      if (plugFilter) params.set('plug_id', plugFilter);
      if (statusFilter) params.set('status_filter', statusFilter);

      const [sessionsRes, plugsRes, profileRes] = await Promise.all([
        api.get(`/api/cpo/analytics/sessions?${params.toString()}`),
        api.get('/api/cpo/plugs'),
        api.get('/api/cpo/profile'),
      ]);
      setSessions(sessionsRes);
      setPlugs(plugsRes);
      setTenantName(profileRes.tenant?.name || '');
    } catch (err) {
      console.error('Failed to load sessions:', err);
    } finally {
      setLoading(false);
    }
  }, [plugFilter, statusFilter, daysFilter]);

  useEffect(() => { fetchData(); }, [fetchData]);

  const [exporting, setExporting] = useState(false);

  // Download the current filtered session history as CSV. Uses a raw
  // authenticated fetch (the JSON api client can't hand back a file blob),
  // then triggers a browser download of the text/csv attachment.
  const exportCsv = async () => {
    setExporting(true);
    try {
      const params = new URLSearchParams();
      params.set('days', daysFilter);
      if (plugFilter) params.set('plug_id', plugFilter);
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
    } catch (err) {
      console.error('CSV export failed:', err);
    } finally {
      setExporting(false);
    }
  };

  /**
   * Calculate summary stats from the current sessions list.
   */
  const stats = {
    total: sessions.length,
    totalEnergy: sessions.reduce((sum, s) => sum + (s.energy_kwh || 0), 0),
    totalRevenue: sessions.reduce((sum, s) => sum + (s.coins_spent || 0), 0),
  };

  /**
   * Format an ISO timestamp to a readable datetime string.
   */
  const formatDateTime = (isoStr) => {
    if (!isoStr) return '—';
    const d = new Date(isoStr);
    return d.toLocaleString('en-IN', {
      month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit',
    });
  };

  /**
   * Format duration in minutes to a human-readable string.
   */
  const formatDuration = (minutes) => {
    if (minutes == null) return '—';
    const h = Math.floor(minutes / 60);
    const m = Math.round(minutes % 60);
    if (h === 0) return `${m}m`;
    return `${h}h ${m}m`;
  };

  return (
    <CpoLayout tenantName={tenantName}>
      {/* Page Header */}
      <div className="cpo-page-header">
        <h1>Sessions</h1>
        <p>Charging session history across all your plugs</p>
      </div>

      {/* Filter Bar */}
      <div className="filter-bar">
        <select value={daysFilter} onChange={(e) => setDaysFilter(parseInt(e.target.value))}>
          {DATE_RANGE_OPTIONS.map((opt) => (
            <option key={opt.value} value={opt.value}>{opt.label}</option>
          ))}
        </select>

        <select value={plugFilter} onChange={(e) => setPlugFilter(e.target.value)}>
          <option value="">All Plugs</option>
          {plugs.map((p) => (
            <option key={p.id} value={p.id}>{p.name}</option>
          ))}
        </select>

        <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
          {STATUS_OPTIONS.map((opt) => (
            <option key={opt.value} value={opt.value}>{opt.label}</option>
          ))}
        </select>

        <button
          className="btn btn-ghost btn-sm"
          onClick={exportCsv}
          disabled={exporting || sessions.length === 0}
          title="Download the filtered sessions as CSV"
          style={{ marginLeft: 'auto' }}
        >
          {exporting ? 'Exporting…' : '⬇ Export CSV'}
        </button>
      </div>

      {/* Summary Stats */}
      <CpoSummary
        items={[
          { label: 'Sessions', value: stats.total },
          { label: 'Total Energy', value: `${stats.totalEnergy.toFixed(3)} kWh` },
          { label: 'Total Revenue', value: `🪙 ${stats.totalRevenue.toFixed(2)}`, tone: 'accent' },
        ]}
      />

      {/* Sessions Table */}
      {loading ? (
        <div className="data-table-container">
          {[1, 2, 3, 4, 5].map((i) => (
            <div key={i} style={{ padding: '1rem', borderBottom: '1px solid hsla(0,0%,100%,0.04)' }}>
              <div className="skeleton" style={{ width: '100%', height: '18px' }} />
            </div>
          ))}
        </div>
      ) : sessions.length > 0 ? (
        <div className="data-table-container animate-fade-in">
          <table className="data-table">
            <thead>
              <tr>
                <th>ID</th>
                <th>Plug</th>
                <th>User</th>
                <th>Started</th>
                <th>Ended</th>
                <th>Duration</th>
                <th>Energy</th>
                <th>Revenue</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {sessions.map((session) => (
                <tr key={session.id}>
                  <td style={{ color: 'var(--color-text-muted)' }}>#{session.id}</td>
                  <td style={{ color: 'var(--color-text-primary)', fontWeight: 500 }}>
                    {session.plug_name}
                  </td>
                  <td>{session.user_email}</td>
                  <td>{formatDateTime(session.started_at)}</td>
                  <td>{formatDateTime(session.ended_at)}</td>
                  <td>{formatDuration(session.duration_minutes)}</td>
                  <td>{session.energy_kwh} kWh</td>
                  <td style={{ color: 'var(--color-accent)', fontWeight: 600 }}>
                    🪙 {session.coins_spent}
                  </td>
                  <td>
                    <span className={`badge ${STATUS_BADGE[session.status] || 'badge-warning'}`}>
                      {session.status}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="empty-state glass">
          <div className="empty-state-icon">⚡</div>
          <h3>No sessions found</h3>
          <p>
            {(plugFilter || statusFilter)
              ? 'No sessions match the selected filters. Try adjusting your criteria.'
              : 'Sessions will appear here once drivers start charging on your plugs.'}
          </p>
        </div>
      )}
    </CpoLayout>
  );
};

export default CpoSessions;
