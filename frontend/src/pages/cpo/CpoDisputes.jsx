/**
 * AmpHive CPO Disputes Page
 * =========================
 * Operators review driver-filed disputes on finished charging sessions and
 * resolve them: an "approve" refunds the driver's wallet (defaulting to the
 * full session cost) and writes a REFUND ledger row; a "reject" just records a
 * note. A dispute is resolved exactly once — the backend returns 409 if it is
 * already closed, or 400 if a refund exceeds the session cost.
 *
 * Data: GET /api/cpo/disputes[?status_filter=open|approved|rejected],
 *       POST /api/cpo/disputes/{id}/resolve { action, refund_coins?, note? }.
 */

import { useState, useEffect, useCallback } from 'react';
import CpoLayout from '../../components/CpoLayout';
import api from '../../api/client';

// Dispute status -> badge class. Rejected has no dedicated tone (plain badge).
const STATUS_BADGE = {
  open: 'badge-warning',
  approved: 'badge-success',
  rejected: '',
};

// Status filter options for the dropdown. '' = no query param (All).
const STATUS_OPTIONS = [
  { value: '', label: 'All' },
  { value: 'open', label: 'Open' },
  { value: 'approved', label: 'Approved' },
  { value: 'rejected', label: 'Rejected' },
];

const CpoDisputes = () => {
  const [disputes, setDisputes] = useState([]);
  const [tenantName, setTenantName] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [statusFilter, setStatusFilter] = useState('');

  const fetchDisputes = useCallback(async () => {
    setLoading(true);
    try {
      const url = statusFilter
        ? `/api/cpo/disputes?status_filter=${statusFilter}`
        : '/api/cpo/disputes';
      const [disputesRes, profileRes] = await Promise.all([
        api.get(url),
        api.get('/api/cpo/profile'),
      ]);
      setDisputes(disputesRes);
      setTenantName(profileRes.tenant?.name || '');
    } catch (err) {
      console.error('Failed to load disputes:', err);
      setError(err.message || 'Failed to load disputes.');
    } finally {
      setLoading(false);
    }
  }, [statusFilter]);

  useEffect(() => { fetchDisputes(); }, [fetchDisputes]);

  // Resolve an open dispute. Approve omits refund_coins so the backend credits
  // the full session cost; reject just records the (empty) note. Either way the
  // list is refetched so the row reflects its new status. A 409/400 surfaces
  // inline via the shared error block.
  const resolve = async (dispute, action) => {
    const prompt = action === 'approve'
      ? 'Approve this dispute and refund the driver?'
      : 'Reject this dispute? The driver will not be refunded.';
    if (!window.confirm(prompt)) return;
    setError('');
    try {
      await api.post(`/api/cpo/disputes/${dispute.id}/resolve`, { action });
      await fetchDisputes();
    } catch (err) {
      setError(err.message || 'Failed to resolve the dispute.');
    }
  };

  return (
    <CpoLayout tenantName={tenantName}>
      <div className="cpo-page-header">
        <h1>Disputes</h1>
        <p>Review and resolve driver disputes on finished sessions</p>
      </div>

      {/* Status filter */}
      <div className="filter-bar">
        <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
          {STATUS_OPTIONS.map((opt) => (
            <option key={opt.value} value={opt.value}>{opt.label}</option>
          ))}
        </select>
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

      {loading ? (
        <div className="data-table-container">
          {[1, 2, 3].map((i) => (
            <div key={i} style={{ padding: '1rem', borderBottom: '1px solid hsla(0,0%,100%,0.04)' }}>
              <div className="skeleton" style={{ width: '100%', height: '18px' }} />
            </div>
          ))}
        </div>
      ) : disputes.length > 0 ? (
        <div className="data-table-container animate-fade-in">
          <table className="data-table">
            <thead>
              <tr>
                <th>Session</th>
                <th>Driver</th>
                <th>Reason</th>
                <th>Status</th>
                <th>Filed</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {disputes.map((d) => (
                <tr key={d.id}>
                  <td style={{ color: 'var(--color-text-muted)' }}>#{d.session_id}</td>
                  <td>User #{d.driver_user_id}</td>
                  <td>{d.reason}</td>
                  <td>
                    <span className={`badge ${STATUS_BADGE[d.status] || ''}`.trim()}>
                      {d.status}
                    </span>
                    {d.status !== 'open' && (d.refund_coins || d.resolution_note) && (
                      <div style={{ marginTop: '0.25rem', fontSize: '0.72rem', color: 'var(--color-text-muted)' }}>
                        {d.refund_coins ? `refunded ${d.refund_coins}` : ''}
                        {d.refund_coins && d.resolution_note ? ' — ' : ''}
                        {d.resolution_note || ''}
                      </div>
                    )}
                  </td>
                  <td>{new Date(d.created_at).toLocaleString()}</td>
                  <td>
                    {d.status === 'open' ? (
                      <>
                        <button className="btn btn-primary btn-sm" onClick={() => resolve(d, 'approve')}>
                          Approve
                        </button>{' '}
                        <button className="btn btn-ghost btn-sm" onClick={() => resolve(d, 'reject')}>
                          Reject
                        </button>
                      </>
                    ) : (
                      <span style={{ color: 'var(--color-text-muted)', fontSize: '0.82rem' }}>—</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="empty-state glass">
          <div className="empty-state-icon">🛡️</div>
          <h3>No disputes</h3>
          <p>Driver disputes on finished sessions show up here.</p>
        </div>
      )}
    </CpoLayout>
  );
};

export default CpoDisputes;
