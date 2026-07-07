/**
 * AmpHive Groups Page
 * ====================
 * Allows users to:
 * 1. Join a private charger group by entering an access code.
 * 2. View all groups they have access to (public + joined private).
 * 3. See plug count per group.
 */

import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';
import api from '../api/client';

const Groups = () => {
  const { user } = useAuth();
  const navigate = useNavigate();

  const [groups, setGroups] = useState([]);
  const [accessCode, setAccessCode] = useState('');
  const [loading, setLoading] = useState(true);
  const [joining, setJoining] = useState(false);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');

  const fetchGroups = async () => {
    try {
      const data = await api.get('/api/groups/my');
      setGroups(data);
    } catch (err) {
      console.error('Failed to fetch groups:', err);
    } finally {
      setLoading(false);
    }
  };

  // Fetch user's groups on mount
  useEffect(() => {
    if (!user) return;
    fetchGroups();
  }, [user]);

  const handleJoinGroup = async (e) => {
    e.preventDefault();
    if (!accessCode.trim()) return;

    setJoining(true);
    setError('');
    setSuccess('');

    try {
      const result = await api.post('/api/groups/join', { access_code: accessCode.trim() });
      setSuccess(`Joined "${result.group_name}" successfully!`);
      setAccessCode('');
      // Refresh the groups list
      await fetchGroups();
    } catch (err) {
      setError(err.message);
    } finally {
      setJoining(false);
    }
  };

  if (!user) {
    return (
      <div className="page-container text-center animate-fade-in">
        <h2>Sign in to manage groups</h2>
        <p>You need an account to join and browse charger groups.</p>
        <button className="btn btn-primary mt-4" onClick={() => navigate('/login')}>Sign In</button>
      </div>
    );
  }

  return (
    <div className="page-container animate-fade-in">
      <h1 style={{ marginBottom: '0.25rem' }}>Charger Groups</h1>
      <p style={{ marginBottom: '2rem' }}>
        Join private groups using an access code, or browse public charger networks.
      </p>

      {/* Join Group Form */}
      <div className="glass glass-panel animate-slide-up" style={{ marginBottom: '2rem' }}>
        <h3 style={{ marginBottom: '0.5rem' }}>🔑 Join a Private Group</h3>
        <p style={{ marginBottom: '1.25rem', fontSize: '0.9rem' }}>
          Enter the access code shared by the charger operator.
        </p>

        <form onSubmit={handleJoinGroup} className="flex gap-3">
          <input
            type="text"
            className="input"
            placeholder="Enter access code (e.g. SUNRISE2024)"
            value={accessCode}
            onChange={(e) => setAccessCode(e.target.value.toUpperCase())}
            style={{ flex: 1, letterSpacing: '0.05em', fontWeight: 600 }}
          />
          <button
            type="submit"
            className="btn btn-primary"
            disabled={joining || !accessCode.trim()}
          >
            {joining ? 'Joining...' : 'Join'}
          </button>
        </form>

        {error && <div className="error-text mt-2">{error}</div>}
        {success && (
          <div style={{ color: 'var(--color-success)', fontSize: '0.9rem', marginTop: '0.5rem', fontWeight: 500 }}>
            ✓ {success}
          </div>
        )}
      </div>

      {/* Groups List */}
      <h3 style={{ marginBottom: '1rem' }}>Your Groups</h3>

      {loading ? (
        <div className="flex flex-col gap-3">
          {[1, 2, 3].map(i => (
            <div key={i} className="skeleton" style={{ height: '80px', width: '100%' }} />
          ))}
        </div>
      ) : groups.length === 0 ? (
        <div className="glass glass-panel text-center" style={{ padding: '3rem 2rem' }}>
          <p style={{ fontSize: '2rem', marginBottom: '0.5rem' }}>📭</p>
          <p>You haven't joined any groups yet.</p>
          <p style={{ fontSize: '0.9rem', marginTop: '0.5rem' }}>
            Enter an access code above, or browse public chargers on the Home page.
          </p>
        </div>
      ) : (
        <div className="flex flex-col gap-3">
          {groups.map((group, index) => (
            <div
              key={group.id}
              className="glass glass-card flex justify-between items-center animate-slide-up"
              style={{ animationDelay: `${index * 0.08}s` }}
              onClick={() => navigate('/')}
            >
              <div>
                <div className="flex items-center gap-2" style={{ marginBottom: '0.25rem' }}>
                  <span style={{ fontWeight: 600, fontSize: '1.05rem' }}>{group.name}</span>
                  <span className={`badge ${group.is_public ? 'badge-success' : 'badge-primary'}`}>
                    {group.is_public ? 'Public' : 'Private'}
                  </span>
                </div>
                <span style={{ color: 'var(--color-text-secondary)', fontSize: '0.9rem' }}>
                  {group.plug_count} {group.plug_count === 1 ? 'charger' : 'chargers'}
                </span>
              </div>
              <span style={{ color: 'var(--color-text-muted)', fontSize: '1.25rem' }}>→</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
};

export default Groups;
