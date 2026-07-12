/**
 * AmpHive CPO Earnings & Payouts Page
 * ====================================
 * Shows the tenant's lifetime + unsettled earnings (gross / platform fee /
 * net) and the payout ledger, and lets the CPO snapshot its unsettled
 * earnings into a payout request.
 *
 * Record-keeping only: the actual money transfer happens OUTSIDE the app
 * (bank transfer / UPI). The platform operator (admin) marks a payout PAID
 * once that transfer has happened — the admin-only "Mark paid" action shows
 * up here when the logged-in user's role is `admin`.
 *
 * Coins are ₹-equivalent (1 coin = ₹1), labelled as such throughout.
 *
 * Data source: /api/cpo/earnings, /api/cpo/payouts, /api/cpo/profile
 */

import { useState, useEffect, useCallback } from 'react';
import CpoLayout from '../../components/CpoLayout';
import api from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';

/**
 * Map payout status to badge class.
 */
const STATUS_BADGE = {
  requested: 'badge-warning',
  paid: 'badge-success',
  cancelled: 'badge-danger',
};

/**
 * Format a coin amount (₹-equivalent) to two decimals.
 */
const formatCoins = (value) => Number(value || 0).toFixed(2);

/**
 * Format an ISO timestamp to a readable date (period bounds span months,
 * so include the year).
 */
const formatDate = (isoStr) => {
  if (!isoStr) return '—';
  return new Date(isoStr).toLocaleDateString('en-IN', {
    year: 'numeric', month: 'short', day: 'numeric',
  });
};

/**
 * Format an ISO timestamp to a readable datetime.
 */
const formatDateTime = (isoStr) => {
  if (!isoStr) return '—';
  return new Date(isoStr).toLocaleString('en-IN', {
    year: 'numeric', month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit',
  });
};

/**
 * One gross / platform-fee / net summary block (used for both the
 * "Lifetime" and "Unsettled" legs of GET /api/cpo/earnings).
 */
const EarningsSummaryCard = ({ title, subtitle, leg, feePct, highlightNet }) => (
  <div className="glass glass-card" style={{ cursor: 'default', flex: '1 1 280px' }}>
    <div style={{ marginBottom: '0.75rem' }}>
      <h3 style={{ margin: 0, fontSize: '1.05rem' }}>{title}</h3>
      {subtitle && (
        <p style={{ color: 'var(--color-text-muted)', fontSize: '0.78rem', margin: '0.25rem 0 0' }}>
          {subtitle}
        </p>
      )}
    </div>
    <div className="flex gap-4" style={{ flexWrap: 'wrap' }}>
      <div>
        <span style={{ color: 'var(--color-text-muted)', fontSize: '0.78rem' }}>Gross</span>
        <p style={{ color: 'var(--color-text-primary)', fontWeight: 600, margin: '0.1rem 0 0', fontSize: '1.1rem' }}>
          🪙 {formatCoins(leg?.gross_coins)}
        </p>
      </div>
      <div>
        <span style={{ color: 'var(--color-text-muted)', fontSize: '0.78rem' }}>
          Platform fee{feePct != null ? ` (${feePct}%)` : ''}
        </span>
        <p style={{ color: 'var(--color-text-secondary)', fontWeight: 600, margin: '0.1rem 0 0', fontSize: '1.1rem' }}>
          🪙 {formatCoins(leg?.platform_fee_coins)}
        </p>
      </div>
      <div>
        <span style={{ color: 'var(--color-text-muted)', fontSize: '0.78rem' }}>Net (yours)</span>
        <p style={{
          color: highlightNet ? 'var(--color-accent)' : 'var(--color-text-primary)',
          fontWeight: 600, margin: '0.1rem 0 0', fontSize: '1.1rem',
        }}>
          🪙 {formatCoins(leg?.net_coins)}
        </p>
      </div>
    </div>
  </div>
);

const CpoEarnings = () => {
  const { user } = useAuth();
  const isAdmin = user?.role === 'admin';

  const [earnings, setEarnings] = useState(null);
  const [payouts, setPayouts] = useState([]);
  const [tenantName, setTenantName] = useState('');
  const [loading, setLoading] = useState(true);

  // Request-payout modal state
  const [showRequestConfirm, setShowRequestConfirm] = useState(false);
  const [requestError, setRequestError] = useState('');
  const [requestLoading, setRequestLoading] = useState(false);

  // Row-action (cancel / mark paid) confirm state
  const [confirmAction, setConfirmAction] = useState(null); // { type: 'cancel'|'mark_paid', payout }
  const [actionError, setActionError] = useState('');
  const [actionLoading, setActionLoading] = useState(false);

  const fetchData = useCallback(async () => {
    try {
      const [earningsRes, payoutsRes, profileRes] = await Promise.all([
        api.get('/api/cpo/earnings'),
        api.get('/api/cpo/payouts'),
        api.get('/api/cpo/profile'),
      ]);
      setEarnings(earningsRes);
      setPayouts(payoutsRes);
      setTenantName(profileRes.tenant?.name || '');
    } catch (err) {
      console.error('Failed to load earnings:', err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { fetchData(); }, [fetchData]);

  /**
   * Snapshot the unsettled window into a REQUESTED payout.
   * The backend answers 400 ("No unsettled earnings to pay out.") or
   * 409 ("A payout request is already pending for this tenant.") — both
   * are surfaced inline in the confirm dialog.
   */
  const handleRequestPayout = async () => {
    setRequestLoading(true);
    setRequestError('');
    try {
      await api.post('/api/cpo/payouts', {});
      setShowRequestConfirm(false);
      await fetchData();
    } catch (err) {
      setRequestError(err.message || 'Failed to request payout.');
    } finally {
      setRequestLoading(false);
    }
  };

  /**
   * Cancel (owner/admin) or mark paid (admin only) a REQUESTED payout.
   */
  const handleConfirmAction = async () => {
    if (!confirmAction) return;
    const { type, payout } = confirmAction;
    setActionLoading(true);
    setActionError('');
    try {
      await api.post(`/api/cpo/payouts/${payout.id}/${type === 'cancel' ? 'cancel' : 'mark_paid'}`, {});
      setConfirmAction(null);
      await fetchData();
    } catch (err) {
      setActionError(err.message || 'Action failed.');
    } finally {
      setActionLoading(false);
    }
  };

  const unsettled = earnings?.unsettled;
  const hasUnsettled = Number(unsettled?.net_coins || 0) > 0;

  return (
    <CpoLayout tenantName={tenantName}>
      {/* Page Header */}
      <div className="cpo-page-header flex justify-between items-center" style={{ flexWrap: 'wrap', gap: '1rem' }}>
        <div>
          <h1>Earnings &amp; Payouts</h1>
          <p>What your chargers have earned, and settlement of what you're owed</p>
        </div>
        <button
          className="btn btn-cpo"
          onClick={() => { setRequestError(''); setShowRequestConfirm(true); }}
          disabled={loading}
        >
          Request payout
        </button>
      </div>

      {/* Earnings Summary Cards */}
      {loading ? (
        <div className="flex gap-4" style={{ flexWrap: 'wrap', marginBottom: '1.5rem' }}>
          {[1, 2].map((i) => (
            <div key={i} className="glass glass-card" style={{ cursor: 'default', flex: '1 1 280px' }}>
              <div className="skeleton" style={{ width: '50%', height: '18px', marginBottom: '1rem' }} />
              <div className="skeleton" style={{ width: '80%', height: '28px' }} />
            </div>
          ))}
        </div>
      ) : earnings && (
        <div className="flex gap-4 animate-fade-in" style={{ flexWrap: 'wrap', marginBottom: '1.5rem' }}>
          <EarningsSummaryCard
            title="Lifetime earnings"
            subtitle="All completed sessions to date"
            leg={earnings.lifetime}
            feePct={earnings.platform_fee_pct}
          />
          <EarningsSummaryCard
            title="Unsettled earnings"
            subtitle={`Window: ${formatDate(unsettled?.period_start)} → ${formatDate(unsettled?.period_end)}`}
            leg={unsettled}
            feePct={earnings.platform_fee_pct}
            highlightNet
          />
        </div>
      )}

      {/* Coin denomination note */}
      <p style={{ color: 'var(--color-text-muted)', fontSize: '0.8rem', margin: '0 0 1.5rem' }}>
        Amounts are in coins — ₹-equivalent (1 coin = ₹1).
      </p>

      {/* Payout History Table */}
      <div className="cpo-page-header" style={{ marginBottom: '0.75rem' }}>
        <h2 style={{ fontSize: '1.2rem', margin: 0 }}>Payout history</h2>
      </div>

      {actionError && <p className="error-text" style={{ marginBottom: '0.75rem' }}>{actionError}</p>}

      {loading ? (
        <div className="data-table-container">
          {[1, 2, 3].map((i) => (
            <div key={i} style={{ padding: '1rem', borderBottom: '1px solid hsla(0,0%,100%,0.04)' }}>
              <div className="skeleton" style={{ width: '100%', height: '18px' }} />
            </div>
          ))}
        </div>
      ) : payouts.length > 0 ? (
        <div className="data-table-container animate-fade-in">
          <table className="data-table">
            <thead>
              <tr>
                <th>ID</th>
                <th>Period</th>
                <th>Gross</th>
                <th>Fee</th>
                <th>Net</th>
                <th>Status</th>
                <th>Requested</th>
                <th>Paid</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {payouts.map((payout) => (
                <tr key={payout.id}>
                  <td style={{ color: 'var(--color-text-muted)' }}>#{payout.id}</td>
                  <td>
                    {formatDate(payout.period_start)} → {formatDate(payout.period_end)}
                  </td>
                  <td>🪙 {formatCoins(payout.gross_coins)}</td>
                  <td>🪙 {formatCoins(payout.platform_fee_coins)}</td>
                  <td style={{ color: 'var(--color-accent)', fontWeight: 600 }}>
                    🪙 {formatCoins(payout.net_coins)}
                  </td>
                  <td>
                    <span className={`badge ${STATUS_BADGE[payout.status] || 'badge-warning'}`}>
                      {payout.status}
                    </span>
                  </td>
                  <td>{formatDateTime(payout.requested_at)}</td>
                  <td>{formatDateTime(payout.paid_at)}</td>
                  <td>
                    {payout.status === 'requested' && (
                      <div className="flex gap-2">
                        <button
                          className="btn btn-ghost btn-sm"
                          style={{ color: 'var(--color-danger)' }}
                          onClick={() => { setActionError(''); setConfirmAction({ type: 'cancel', payout }); }}
                        >
                          Cancel
                        </button>
                        {isAdmin && (
                          <button
                            className="btn btn-ghost btn-sm"
                            style={{ color: 'var(--color-success)' }}
                            onClick={() => { setActionError(''); setConfirmAction({ type: 'mark_paid', payout }); }}
                          >
                            Mark paid
                          </button>
                        )}
                      </div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="empty-state glass">
          <div className="empty-state-icon">🪙</div>
          <h3>No payouts yet</h3>
          <p>
            Once your chargers have earned unsettled coins, request a payout to
            snapshot what you're owed. The transfer itself happens outside the app.
          </p>
        </div>
      )}

      {/* Out-of-band settlement footnote */}
      <p style={{ color: 'var(--color-text-muted)', fontSize: '0.8rem', marginTop: '1rem' }}>
        Note: payment itself happens outside the app (bank transfer / UPI).
        Requesting a payout records what you're owed; the platform operator marks
        it paid once the transfer has been made.
      </p>

      {/* Request Payout Confirm Modal */}
      {showRequestConfirm && (
        <div className="modal-overlay" onClick={() => setShowRequestConfirm(false)}>
          <div className="modal-content" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2>Request payout</h2>
              <button className="modal-close" onClick={() => setShowRequestConfirm(false)}>✕</button>
            </div>

            <p style={{ marginBottom: '0.5rem' }}>
              This snapshots your unsettled earnings
              {hasUnsettled && (
                <> — net <strong style={{ color: 'var(--color-accent)' }}>🪙 {formatCoins(unsettled?.net_coins)}</strong> (₹-equivalent)</>
              )}
              {' '}into a payout request for the platform operator to settle.
            </p>
            <p style={{ fontSize: '0.85rem', color: 'var(--color-text-muted)' }}>
              The transfer happens outside the app (bank/UPI). Only one payout
              request can be pending at a time; you can cancel it later if needed.
            </p>

            {requestError && <p className="error-text mt-4">{requestError}</p>}

            <div className="modal-actions">
              <button type="button" className="btn btn-ghost" onClick={() => setShowRequestConfirm(false)}>
                Cancel
              </button>
              <button
                type="button"
                className="btn btn-cpo"
                onClick={handleRequestPayout}
                disabled={requestLoading}
              >
                {requestLoading ? 'Requesting...' : 'Request payout'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Cancel / Mark-Paid Confirm Modal */}
      {confirmAction && (
        <div className="modal-overlay" onClick={() => setConfirmAction(null)}>
          <div className="modal-content" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2>{confirmAction.type === 'cancel' ? 'Cancel payout' : 'Mark payout paid'}</h2>
              <button className="modal-close" onClick={() => setConfirmAction(null)}>✕</button>
            </div>

            {confirmAction.type === 'cancel' ? (
              <p style={{ marginBottom: '0.5rem' }}>
                Cancel payout <strong>#{confirmAction.payout.id}</strong> (net
                🪙 {formatCoins(confirmAction.payout.net_coins)})? Its earnings
                window is freed and will be covered by your next payout request.
              </p>
            ) : (
              <p style={{ marginBottom: '0.5rem' }}>
                Mark payout <strong>#{confirmAction.payout.id}</strong> (net
                🪙 {formatCoins(confirmAction.payout.net_coins)}) as paid? Only do
                this after the bank/UPI transfer has actually been made — this
                records the settlement, it does not move money.
              </p>
            )}

            {actionError && <p className="error-text mt-4">{actionError}</p>}

            <div className="modal-actions">
              <button className="btn btn-ghost" onClick={() => setConfirmAction(null)}>
                Keep as is
              </button>
              <button
                className={`btn ${confirmAction.type === 'cancel' ? 'btn-danger' : 'btn-cpo'}`}
                onClick={handleConfirmAction}
                disabled={actionLoading}
              >
                {actionLoading
                  ? 'Working...'
                  : confirmAction.type === 'cancel' ? 'Cancel payout' : 'Mark paid'}
              </button>
            </div>
          </div>
        </div>
      )}
    </CpoLayout>
  );
};

export default CpoEarnings;
