/**
 * AmpHive CPO Reservations Page
 * =============================
 * Tenant-wide view of plug bookings: who reserved which plug, when, and the
 * current state (booked / fulfilled / cancelled / expired). Operators can
 * cancel a still-BOOKED slot (the driver is notified), e.g. to free a plug
 * for maintenance.
 *
 * Data source: GET /api/cpo/reservations (status + upcoming_only filters),
 * POST /api/reservations/{id}/cancel (operator cancel, tenant-scoped).
 */

import { useState, useEffect, useCallback } from 'react';
import CpoLayout from '../../components/CpoLayout';
import api from '../../api/client';

const STATUS_OPTIONS = [
  { value: '', label: 'All Statuses' },
  { value: 'booked', label: 'Booked' },
  { value: 'fulfilled', label: 'Fulfilled' },
  { value: 'cancelled', label: 'Cancelled' },
  { value: 'expired', label: 'Expired' },
];

const STATUS_BADGE = {
  booked: 'badge-success',
  fulfilled: 'badge-primary',
  cancelled: 'badge-danger',
  expired: 'badge-warning',
};

const CpoReservations = () => {
  const [reservations, setReservations] = useState([]);
  const [tenantName, setTenantName] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [cancellingId, setCancellingId] = useState(null);

  // Filter state
  const [statusFilter, setStatusFilter] = useState('');
  const [upcomingOnly, setUpcomingOnly] = useState(false);

  const fetchData = useCallback(async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams();
      params.set('limit', '100');
      if (statusFilter) params.set('status', statusFilter);
      if (upcomingOnly) params.set('upcoming_only', 'true');

      const [resvRes, profileRes] = await Promise.all([
        api.get(`/api/cpo/reservations?${params.toString()}`),
        api.get('/api/cpo/profile'),
      ]);
      setReservations(resvRes);
      setTenantName(profileRes.tenant?.name || '');
    } catch (err) {
      console.error('Failed to load reservations:', err);
      setError(err.message || 'Failed to load reservations.');
    } finally {
      setLoading(false);
    }
  }, [statusFilter, upcomingOnly]);

  useEffect(() => { fetchData(); }, [fetchData]);

  // Cancel a still-BOOKED slot as the operator. The backend notifies the
  // driver (they'd otherwise show up to a pulled reservation), so confirm
  // first. A 409 (already fulfilled/cancelled/expired) surfaces inline.
  const cancelReservation = async (id) => {
    if (!window.confirm('Cancel this reservation? The driver will be notified.')) return;
    setCancellingId(id);
    setError('');
    try {
      await api.post(`/api/reservations/${id}/cancel`, {});
      await fetchData();
    } catch (err) {
      console.error('Failed to cancel reservation:', err);
      setError(err.message || 'Failed to cancel the reservation.');
    } finally {
      setCancellingId(null);
    }
  };

  // Render a booking window in the viewer's local timezone. Collapses the end
  // to a bare time when it shares the start's calendar day.
  const formatWindow = (startIso, endIso) => {
    if (!startIso || !endIso) return '—';
    const s = new Date(startIso);
    const e = new Date(endIso);
    const dateOpts = { month: 'short', day: 'numeric' };
    const timeOpts = { hour: '2-digit', minute: '2-digit' };
    const sameDay = s.toDateString() === e.toDateString();
    const startStr = `${s.toLocaleDateString('en-IN', dateOpts)}, ${s.toLocaleTimeString('en-IN', timeOpts)}`;
    const endStr = sameDay
      ? e.toLocaleTimeString('en-IN', timeOpts)
      : `${e.toLocaleDateString('en-IN', dateOpts)}, ${e.toLocaleTimeString('en-IN', timeOpts)}`;
    return `${startStr} – ${endStr}`;
  };

  const bookedCount = reservations.filter((r) => r.status === 'booked').length;

  return (
    <CpoLayout tenantName={tenantName}>
      {/* Page Header */}
      <div className="cpo-page-header">
        <h1>Reservations</h1>
        <p>Plug bookings across your network — who reserved what, and when</p>
      </div>

      {/* Filter Bar */}
      <div className="filter-bar">
        <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
          {STATUS_OPTIONS.map((opt) => (
            <option key={opt.value} value={opt.value}>{opt.label}</option>
          ))}
        </select>

        <label style={{ display: 'flex', alignItems: 'center', gap: '0.4rem', fontSize: '0.85rem', color: 'var(--color-text-secondary)' }}>
          <input
            type="checkbox"
            checked={upcomingOnly}
            onChange={(e) => setUpcomingOnly(e.target.checked)}
          />
          Upcoming only
        </label>

        <span style={{ marginLeft: 'auto', color: 'var(--color-text-muted)', fontSize: '0.82rem' }}>
          {reservations.length} shown · {bookedCount} active
        </span>
      </div>

      {error && (
        <div className="glass" style={{
          padding: '0.75rem 1rem', marginBottom: '1rem',
          borderRadius: 'var(--radius-md)',
          border: '1px solid var(--color-danger)',
          color: 'var(--color-danger)', fontSize: '0.85rem',
        }}>
          {error}
        </div>
      )}

      {/* Reservations Table */}
      {loading ? (
        <div className="data-table-container">
          {[1, 2, 3, 4, 5].map((i) => (
            <div key={i} style={{ padding: '1rem', borderBottom: '1px solid hsla(0,0%,100%,0.04)' }}>
              <div className="skeleton" style={{ width: '100%', height: '18px' }} />
            </div>
          ))}
        </div>
      ) : reservations.length > 0 ? (
        <div className="data-table-container animate-fade-in">
          <table className="data-table">
            <thead>
              <tr>
                <th>ID</th>
                <th>Plug</th>
                <th>Driver</th>
                <th>Window (local)</th>
                <th>Status</th>
                <th>Session</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {reservations.map((r) => (
                <tr key={r.id}>
                  <td style={{ color: 'var(--color-text-muted)' }}>#{r.id}</td>
                  <td style={{ color: 'var(--color-text-primary)', fontWeight: 500 }}>
                    {r.plug_name || `Plug ${r.plug_id}`}
                  </td>
                  <td>
                    <div style={{ color: 'var(--color-text-primary)' }}>{r.user_name || '—'}</div>
                    <div style={{ color: 'var(--color-text-muted)', fontSize: '0.78rem' }}>{r.user_email}</div>
                  </td>
                  <td>{formatWindow(r.start_at, r.end_at)}</td>
                  <td>
                    <span className={`badge ${STATUS_BADGE[r.status] || 'badge-warning'}`}>
                      {r.status}
                    </span>
                  </td>
                  <td style={{ color: 'var(--color-text-muted)' }}>
                    {r.session_id ? `#${r.session_id}` : '—'}
                  </td>
                  <td>
                    {r.status === 'booked' ? (
                      <button
                        className="btn btn-ghost btn-sm"
                        onClick={() => cancelReservation(r.id)}
                        disabled={cancellingId === r.id}
                      >
                        {cancellingId === r.id ? 'Cancelling…' : 'Cancel'}
                      </button>
                    ) : (
                      <span style={{ color: 'var(--color-text-muted)' }}>—</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="empty-state glass">
          <div className="empty-state-icon">📅</div>
          <h3>No reservations found</h3>
          <p>
            {(statusFilter || upcomingOnly)
              ? 'No reservations match the selected filters. Try adjusting your criteria.'
              : 'Bookings will appear here once drivers reserve time slots on your plugs.'}
          </p>
        </div>
      )}
    </CpoLayout>
  );
};

export default CpoReservations;
