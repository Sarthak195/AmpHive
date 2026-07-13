import { useState, useEffect, useCallback } from 'react';
import api from '../api/client';

const formatDate = (isoString) => {
  if (!isoString) return '-';
  return new Date(isoString).toLocaleString();
};

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
          <div className="glass-panel" style={{ overflowX: 'auto', padding: '1rem' }}>
            <table className="data-table" style={{ width: '100%', borderCollapse: 'collapse' }}>
              <thead>
                <tr style={{ borderBottom: '1px solid rgba(255,255,255,0.1)', textAlign: 'left' }}>
                  <th style={{ padding: '1rem' }}>Date</th>
                  <th style={{ padding: '1rem' }}>Plug ID</th>
                  <th style={{ padding: '1rem' }}>Energy (kWh)</th>
                  <th style={{ padding: '1rem' }}>Cost (Coins)</th>
                  <th style={{ padding: '1rem' }}>Status</th>
                </tr>
              </thead>
              <tbody>
                {sessions.map((s) => (
                  <tr key={s.id} style={{ borderBottom: '1px solid rgba(255,255,255,0.05)' }}>
                    <td style={{ padding: '1rem' }}>{formatDate(s.started_at)}</td>
                    <td style={{ padding: '1rem' }}>{s.plug_id}</td>
                    <td style={{ padding: '1rem' }}>{s.energy_kwh.toFixed(3)}</td>
                    <td style={{ padding: '1rem', color: 'var(--color-accent)' }}>{s.coins_spent.toFixed(2)}</td>
                    <td style={{ padding: '1rem' }}>
                      <span style={{
                        padding: '0.25rem 0.5rem',
                        borderRadius: '4px',
                        fontSize: '0.85rem',
                        backgroundColor: s.status === 'completed' || s.status === 'paid' ? 'rgba(198, 255, 0, 0.2)' : 'rgba(255, 165, 2, 0.2)',
                        color: s.status === 'completed' || s.status === 'paid' ? 'var(--color-success)' : 'var(--color-warning)',
                      }}>
                        {s.status}
                      </span>
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
        <div className="glass-panel" style={{ overflowX: 'auto', padding: '1rem' }}>
          <table className="data-table" style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead>
              <tr style={{ borderBottom: '1px solid rgba(255,255,255,0.1)', textAlign: 'left' }}>
                <th style={{ padding: '1rem' }}>Date</th>
                <th style={{ padding: '1rem' }}>Type</th>
                <th style={{ padding: '1rem' }}>Description</th>
                <th style={{ padding: '1rem', textAlign: 'right' }}>Amount</th>
                <th style={{ padding: '1rem', textAlign: 'right' }}>Balance</th>
              </tr>
            </thead>
            <tbody>
              {ledger.map((tx) => {
                const credit = tx.direction === 'credit';
                return (
                  <tr key={tx.id} style={{ borderBottom: '1px solid rgba(255,255,255,0.05)' }}>
                    <td style={{ padding: '1rem' }}>{formatDate(tx.created_at)}</td>
                    <td style={{ padding: '1rem' }}>{TX_LABEL[tx.transaction_type] || tx.transaction_type}</td>
                    <td style={{ padding: '1rem', color: 'var(--color-text-secondary)', fontSize: '0.9rem' }}>
                      {tx.description || (tx.session_id ? `Session #${tx.session_id}` : '—')}
                    </td>
                    <td style={{
                      padding: '1rem',
                      textAlign: 'right',
                      fontWeight: 600,
                      color: credit ? 'var(--color-success)' : 'var(--color-danger)',
                    }}>
                      {credit ? '+' : ''}{tx.amount.toFixed(2)}
                    </td>
                    <td style={{ padding: '1rem', textAlign: 'right', color: 'var(--color-text-secondary)' }}>
                      {tx.balance_after.toFixed(2)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
