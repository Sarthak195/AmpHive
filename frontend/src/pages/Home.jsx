/**
 * AmpHive Home Page
 * =================
 * Dashboard showing wallet balance, plug ID entry, and available charger list.
 * Replaces the Phase 1 QR code flow with Plug ID entry + group-based access.
 */

import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import WalletCard from '../components/WalletCard';
import { useAuth } from '../contexts/AuthContext';
import { useSession } from '../contexts/SessionContext';
import api from '../api/client';
import MapComponent from '../components/MapComponent';

const Home = () => {
  const { user } = useAuth();
  const { startSession, activeSessions, switchSession, error: sessionError, socket } = useSession();
  const navigate = useNavigate();

  const [plugId, setPlugId] = useState('');
  const [plugs, setPlugs] = useState([]);
  const [loadingPlugs, setLoadingPlugs] = useState(false);
  const [startError, setStartError] = useState('');
  const [starting, setStarting] = useState(false);

  const fetchPlugs = async () => {
    setLoadingPlugs(true);
    try {
      const data = await api.get('/api/plugs/available');
      setPlugs(data);
    } catch (err) {
      console.error('Failed to fetch plugs:', err);
    } finally {
      setLoadingPlugs(false);
    }
  };

  // Fetch available plugs when user is logged in
  useEffect(() => {
    if (!user) return;
    fetchPlugs();
  }, [user]);

  // Live plug-availability: when any plug flips OCCUPIED/AVAILABLE (someone
  // else started/stopped a session), update its badge in place so the list
  // stays current without a manual refresh.
  useEffect(() => {
    if (!socket) return;
    const handlePlugStatus = ({ plug_id, status }) => {
      setPlugs((prev) =>
        prev.map((p) => (p.id === plug_id ? { ...p, status } : p))
      );
    };
    socket.on('plug_status', handlePlugStatus);
    return () => socket.off('plug_status', handlePlugStatus);
  }, [socket]);

  const handleStartSession = async (e, targetPlugId = null) => {
    if (e) e.preventDefault();
    const pid = targetPlugId || plugId.trim();
    if (!pid) return;

    setStartError('');
    setStarting(true);

    try {
      await startSession(pid);
      navigate('/session');
    } catch (err) {
      setStartError(err.message);
    } finally {
      setStarting(false);
    }
  };

  // Status badge color mapping
  const statusColor = (status) => {
    switch (status) {
      case 'available': return 'badge-success';
      case 'occupied': return 'badge-warning';
      case 'offline': return 'badge-danger';
      default: return 'badge-primary';
    }
  };

  return (
    <div className="page-container animate-fade-in">
      {/* Header */}
      <header className="text-center" style={{ marginBottom: '2rem' }}>
        <h1 style={{ color: 'var(--color-primary)', fontSize: '2.2rem', marginBottom: '0.25rem' }}>
          ⚡ AmpHive
        </h1>
        <p style={{ fontSize: '1.05rem' }}>Shared EV Charging Network</p>
      </header>


      {/* Active Session Banners — one per active session (max 2) */}
      {activeSessions.map((session) => (
        <div
          key={session.session_id}
          className="glass animate-slide-up"
          style={{
            padding: '1rem 1.25rem',
            background: 'rgba(235, 94, 40, 0.15)',
            border: '1px solid var(--color-primary)',
            borderRadius: 'var(--radius-md)',
            marginBottom: '1rem',
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center'
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
            <span style={{ fontSize: '1.25rem' }}>⚡</span>
            <div>
              <div style={{ fontWeight: 600 }}>Active charging session in progress</div>
              <div style={{ fontSize: '0.85rem', color: 'var(--color-text-secondary)' }}>
                Plug: {session.plug_name}
              </div>
            </div>
          </div>
          <button
            className="btn btn-accent btn-sm"
            style={{ whiteSpace: 'nowrap' }}
            onClick={() => {
              switchSession(session);
              navigate('/session');
            }}
          >
            Resume Session
          </button>
        </div>
      ))}

      {/* Wallet Card */}
      <div style={{ marginBottom: '1.5rem' }}>
        <WalletCard />
      </div>

      {/* Start Charging Section */}
      {user && (
        <div className="glass glass-panel animate-slide-up" style={{ marginBottom: '1.5rem' }}>
          <h3 style={{ marginBottom: '0.25rem' }}>Start Charging</h3>
          <p style={{ marginBottom: '1.25rem', fontSize: '0.9rem' }}>
            Enter a Plug ID to begin charging your vehicle.
          </p>

          <form onSubmit={handleStartSession} className="flex gap-3">
            <input
              type="text"
              className="input"
              placeholder="Enter Plug ID (e.g. 1)"
              value={plugId}
              onChange={(e) => setPlugId(e.target.value)}
              style={{ flex: 1 }}
            />
            <button
              type="submit"
              className="btn btn-accent"
              disabled={!plugId.trim() || starting}
            >
              {starting ? '...' : 'Start'}
            </button>
          </form>

          {(startError || sessionError) && (
            <div className="error-text mt-2">{startError || sessionError}</div>
          )}
        </div>
      )}

      {/* Available Chargers */}
      {user && (
        <div className="animate-slide-up" style={{ animationDelay: '0.15s' }}>
          <div className="flex justify-between items-center" style={{ marginBottom: '1rem' }}>
            <h3 style={{ margin: 0 }}>Available Chargers</h3>
            <button className="btn btn-ghost btn-sm" onClick={fetchPlugs}>
              Refresh
            </button>
          </div>

          {plugs.length > 0 && !loadingPlugs && (
            <MapComponent plugs={plugs} onPlugSelect={(id) => handleStartSession(null, id)} />
          )}

          {loadingPlugs ? (
            <div className="flex flex-col gap-3">
              {[1, 2, 3].map(i => (
                <div key={i} className="skeleton" style={{ height: '72px', width: '100%' }} />
              ))}
            </div>
          ) : plugs.length === 0 ? (
            <div className="glass glass-panel text-center" style={{ padding: '2.5rem 2rem' }}>
              <p style={{ fontSize: '1.5rem', marginBottom: '0.5rem' }}>🔌</p>
              <p>No chargers available yet.</p>
              <p style={{ fontSize: '0.85rem', marginTop: '0.5rem' }}>
                Join a charger group to see available plugs, or enter a Plug ID directly above.
              </p>
              <button
                className="btn btn-primary btn-sm mt-4"
                onClick={() => navigate('/groups')}
              >
                Browse Groups
              </button>
            </div>
          ) : (
            <div className="flex flex-col gap-3">
              {plugs.map((plug, index) => {
                // A plug is startable only if it is available AND its gateway is
                // reachable right now. gateway_online defaults true for older
                // API data; an unreachable charger is shown but not clickable so
                // the driver isn't sent into a 409 at start.
                const unreachable = plug.gateway_online === false;
                const startable = plug.status === 'available' && !unreachable;
                return (
                <div
                  key={plug.id}
                  className="glass glass-card flex justify-between items-center animate-slide-up"
                  style={{
                    animationDelay: `${index * 0.06}s`,
                    cursor: startable ? 'pointer' : 'default',
                    opacity: unreachable ? 0.6 : 1,
                  }}
                  onClick={() => {
                    if (startable) {
                      handleStartSession(null, plug.id);
                    }
                  }}
                >
                  <div>
                    <div className="flex items-center gap-2" style={{ marginBottom: '0.25rem' }}>
                      <span style={{ fontWeight: 600 }}>{plug.name}</span>
                      <span className={`badge ${unreachable ? 'badge-danger' : statusColor(plug.status)}`}>
                        {unreachable ? 'charger offline' : plug.status}
                      </span>
                    </div>
                    <div className="flex items-center gap-3" style={{ fontSize: '0.85rem', color: 'var(--color-text-secondary)' }}>
                      <span>ID: {plug.id}</span>
                      <span>•</span>
                      <span>{plug.plug_model}</span>
                      {plug.group_name && (
                        <>
                          <span>•</span>
                          <span>{plug.group_name}</span>
                        </>
                      )}
                    </div>
                  </div>
                  {startable ? (
                    <span style={{ color: 'var(--color-primary)', fontWeight: 600, fontSize: '0.9rem' }}>
                      Charge →
                    </span>
                  ) : unreachable ? (
                    <span style={{ color: 'var(--color-text-muted)', fontSize: '0.8rem' }}>
                      Unreachable
                    </span>
                  ) : null}
                </div>
                );
              })}
            </div>
          )}
        </div>
      )}

      {/* Not logged in */}
      {!user && (
        <div className="glass glass-panel text-center animate-slide-up" style={{ padding: '3rem 2rem' }}>
          <p style={{ fontSize: '1.5rem', marginBottom: '0.75rem' }}>🔐</p>
          <h2 style={{ marginBottom: '0.5rem' }}>Sign in to get started</h2>
          <p style={{ marginBottom: '1.5rem' }}>
            Create an account to top up your wallet and start charging.
          </p>
          <button className="btn btn-primary btn-lg" onClick={() => navigate('/login')}>
            Sign In
          </button>
        </div>
      )}
    </div>
  );
};

export default Home;
