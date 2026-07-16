/**
 * AmpHive Public Charger Map (pre-signup discovery)
 * =================================================
 * PUBLIC route (`/map`, NOT auth-gated): lets a visitor discover nearby PUBLIC
 * AmpHive chargers on a map before signing up. Data comes from the
 * unauthenticated `GET /api/plugs/public` (public-group plugs only — private/
 * society plugs are never exposed). Browse without an account; every action
 * (starting a charge) routes to sign-in. The authenticated Home map is untouched.
 */
import { useState, useEffect, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import api from '../api/client';
import MapComponent from '../components/MapComponent';
import { getPlugAvailability, AVAILABILITY_LABELS, AVAILABILITY_CSS_VAR } from '../utils/plugAvailability';

const LEGEND_STATES = ['available', 'in_use', 'offline'];

export default function PublicMap() {
  const navigate = useNavigate();
  const [plugs, setPlugs] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const fetchPlugs = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);
      setPlugs(await api.get('/api/plugs/public'));
    } catch (err) {
      setError(err.message || 'Failed to load chargers.');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { fetchPlugs(); }, [fetchPlugs]);

  // Every action pre-signup goes to sign-in (Login handles login + register).
  const goSignIn = () => navigate('/login');

  const availableCount = plugs.filter((p) => getPlugAvailability(p) === 'available').length;

  return (
    <div className="container" style={{ padding: '2rem 1rem' }}>
      <div className="flex justify-between items-center" style={{ flexWrap: 'wrap', gap: '1rem', marginBottom: '1rem' }}>
        <div>
          <h1 style={{ margin: 0, color: 'var(--color-primary)' }}>Nearby public chargers</h1>
          <p style={{ margin: '0.35rem 0 0', color: 'var(--color-text-secondary)' }}>
            Browse public AmpHive chargers near you. Sign in to start charging.
          </p>
        </div>
        <button className="btn btn-primary" onClick={goSignIn}>Sign in to charge</button>
      </div>

      {error && (
        <div className="glass-panel" style={{ color: 'var(--color-danger)', marginBottom: '1rem' }}>{error}</div>
      )}

      {!loading && plugs.length > 0 && (
        <div className="flex items-center" style={{ gap: '1.25rem', flexWrap: 'wrap', marginBottom: '0.75rem', fontSize: '0.85rem' }}>
          <span className="num" style={{ color: 'var(--color-text-secondary)' }}>
            {plugs.length} chargers · {availableCount} available
          </span>
          {LEGEND_STATES.map((s) => (
            <span key={s} className="flex items-center" style={{ gap: '0.4rem', color: 'var(--color-text-secondary)' }}>
              <span style={{
                background: `var(${AVAILABILITY_CSS_VAR[s]})`,
                width: 12, height: 12, borderRadius: '50%', display: 'inline-block',
              }} />
              {AVAILABILITY_LABELS[s]}
            </span>
          ))}
        </div>
      )}

      {loading ? (
        <div className="glass-panel shimmer" style={{ height: '460px' }} />
      ) : plugs.length === 0 ? (
        <div className="glass-panel" style={{ textAlign: 'center', padding: '2rem' }}>
          <p>No public chargers to show yet. Check back soon.</p>
        </div>
      ) : (
        <div style={{ height: '460px' }}>
          <MapComponent plugs={plugs} onPlugSelect={goSignIn} selectLabel="Sign up to charge" />
        </div>
      )}
    </div>
  );
}
