import { useState, useEffect } from 'react';
import api from '../api/client';

export default function History() {
  const [sessions, setSessions] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const fetchHistory = async () => {
    try {
      setLoading(true);
      const data = await api.get('/api/sessions/history');
      setSessions(data);
    } catch (err) {
      setError(err.message || 'Failed to fetch history');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchHistory();
  }, []);

  const formatDate = (isoString) => {
    if (!isoString) return '-';
    return new Date(isoString).toLocaleString();
  };

  return (
    <div className="container" style={{ padding: '2rem 1rem' }}>
      <h1 style={{ marginBottom: '1.5rem', color: 'var(--color-primary)' }}>Charging History</h1>
      
      {error && (
        <div className="glass-panel" style={{ color: 'var(--color-danger)', marginBottom: '1rem' }}>
          {error}
        </div>
      )}

      {loading ? (
        <div className="glass-panel shimmer" style={{ height: '200px' }}></div>
      ) : sessions.length === 0 ? (
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
              {sessions.map(s => (
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
                      backgroundColor: s.status === 'completed' || s.status === 'paid' ? 'rgba(46, 213, 115, 0.2)' : 'rgba(255, 165, 2, 0.2)',
                      color: s.status === 'completed' || s.status === 'paid' ? 'var(--color-success)' : 'var(--color-warning)'
                    }}>
                      {s.status}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
