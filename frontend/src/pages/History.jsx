import { useState, useEffect, useCallback } from 'react';
import api from '../api/client';
import DisputeModal from '../components/DisputeModal';

const formatDate = (isoString) => {
  if (!isoString) return '-';
  return new Date(isoString).toLocaleString();
};

// The invoice endpoint 400s on a zero-cost session, so only offer it for a
// completed/paid session that actually billed something.
const canInvoice = (s) =>
  (s.status === 'completed' || s.status === 'paid') && Number(s.coins_spent) > 0;

// Human labels for ledger transaction types.
const TX_LABEL = {
  topup: 'Top-up',
  session_debit: 'Charging',
  refund: 'Refund',
};

export default function History() {
  const [tab, setTab] = useState('sessions'); // 'sessions' | 'ledger'
  const [sessions, setSessions] = useState([]);
  const [ledger, setLedger] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [disputeFor, setDisputeFor] = useState(null); // session id the dispute modal is open for

  const fetchData = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);
      // Fetch both up front so switching tabs is instant.
      const [sessionsData, ledgerData] = await Promise.all([
        api.get('/api/sessions/history'),
        api.get('/api/wallet/ledger'),
      ]);
      setSessions(sessionsData);
      setLedger(ledgerData);
    } catch (err) {
      setError(err.message || 'Failed to fetch history');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  // The GST invoice endpoint returns printable HTML (not JSON) and needs the
  // Bearer header, so it can't go through the api client — raw-fetch the blob
  // and open it in a new tab. Issues the invoice server-side on first view.
  const viewInvoice = async (sessionId) => {
    try {
      const base = import.meta.env.VITE_API_URL || '';
      const token = localStorage.getItem('amphive_token');
      const res = await fetch(`${base}/api/sessions/${sessionId}/invoice?format=html`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) throw new Error('Could not load the invoice.');
      const blob = await res.blob();
      window.open(URL.createObjectURL(blob), '_blank');
    } catch (err) {
      setError(err.message || 'Could not load the invoice.');
    }
  };

  const TABS = [
    { id: 'sessions', label: 'Charging Sessions' },
    { id: 'ledger', label: 'Wallet Ledger' },
  ];

  return (
    <div className="container" style={{ padding: '2rem 1rem' }}>
      <h1 style={{ marginBottom: '1rem', color: 'var(--color-primary)' }}>History</h1>

      <div className="flex gap-2" style={{ marginBottom: '1.5rem', flexWrap: 'wrap' }}>
        {TABS.map((t) => (
          <button
            key={t.id}
            className={`btn btn-sm ${tab === t.id ? 'btn-primary' : 'btn-ghost'}`}
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>

      {error && (
        <div className="glass-panel" style={{ color: 'var(--color-danger)', marginBottom: '1rem' }}>
          {error}
        </div>
      )}

      {loading ? (
        <div className="glass-panel shimmer" style={{ height: '200px' }}></div>
      ) : tab === 'sessions' ? (
        sessions.length === 0 ? (
          <div className="glass-panel" style={{ textAlign: 'center', padding: '2rem' }}>
            <p>No charging sessions found.</p>
          </div>
        ) : (
          <div className="data-table-container animate-fade-in">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Date</th>
                  <th>Plug ID</th>
                  <th>Energy (kWh)</th>
                  <th>Cost (Coins)</th>
                  <th>Status</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {sessions.map((s) => (
                  <tr key={s.id}>
                    <td>{formatDate(s.started_at)}</td>
                    <td>{s.plug_id}</td>
                    <td className="num">{Number(s.energy_kwh ?? 0).toFixed(3)}</td>
                    <td className="num" style={{ color: 'var(--color-accent)' }}>{Number(s.coins_spent ?? 0).toFixed(2)}</td>
                    <td>
                      <span className={`badge ${s.status === 'completed' || s.status === 'paid' ? 'badge-success' : 'badge-warning'}`}>
                        {s.status}
                      </span>
                    </td>
                    <td>
                      <div className="flex gap-2" style={{ flexWrap: 'wrap' }}>
                        {canInvoice(s) && (
                          <button className="btn btn-ghost btn-sm" onClick={() => viewInvoice(s.id)}>
                            Invoice
                          </button>
                        )}
                        <button className="btn btn-ghost btn-sm" onClick={() => setDisputeFor(s.id)}>
                          Report an issue
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      ) : ledger.length === 0 ? (
        <div className="glass-panel" style={{ textAlign: 'center', padding: '2rem' }}>
          <p>No wallet activity yet. Top up to add coins.</p>
        </div>
      ) : (
        <div className="data-table-container animate-fade-in">
          <table className="data-table">
            <thead>
              <tr>
                <th>Date</th>
                <th>Type</th>
                <th>Description</th>
                <th style={{ textAlign: 'right' }}>Amount</th>
                <th style={{ textAlign: 'right' }}>Balance</th>
              </tr>
            </thead>
            <tbody>
              {ledger.map((tx) => {
                const credit = tx.direction === 'credit';
                return (
                  <tr key={tx.id}>
                    <td>{formatDate(tx.created_at)}</td>
                    <td>{TX_LABEL[tx.transaction_type] || tx.transaction_type}</td>
                    <td style={{ color: 'var(--color-text-secondary)' }}>
                      {tx.description || (tx.session_id ? `Session #${tx.session_id}` : '—')}
                    </td>
                    <td className="num" style={{
                      textAlign: 'right',
                      fontWeight: 600,
                      color: credit ? 'var(--color-success)' : 'var(--color-danger)',
                    }}>
                      {credit ? '+' : ''}{Number(tx.amount ?? 0).toFixed(2)}
                    </td>
                    <td className="num" style={{ textAlign: 'right', color: 'var(--color-text-secondary)' }}>
                      {Number(tx.balance_after ?? 0).toFixed(2)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {disputeFor != null && (
        <DisputeModal
          sessionId={disputeFor}
          onClose={() => setDisputeFor(null)}
          onSubmitted={fetchData}
        />
      )}
    </div>
  );
}
