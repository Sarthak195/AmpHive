/**
 * AmpHive CPO Setup Page
 * =======================
 * One-time onboarding page for users who want to become a Charge Point
 * Operator. Creates a new tenant (organization) and promotes the user's
 * role from 'driver' to 'cpo'.
 *
 * Shown when a user navigates to /cpo but doesn't have the 'cpo' role yet.
 * After successful setup, refreshes the user profile and redirects to
 * the CPO dashboard.
 */

import React, { useState } from 'react';
import { useNavigate, Navigate } from 'react-router-dom';
import { useAuth } from '../../contexts/AuthContext';
import api from '../../api/client';

const CpoSetup = () => {
  const [tenantName, setTenantName] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const navigate = useNavigate();
  const { user, refreshUser } = useAuth();

  // If the user is already a CPO, redirect to the dashboard. Use the
  // declarative <Navigate> element instead of calling navigate() in the render
  // body — an imperative navigate during render updates the router mid-render,
  // which React warns about and can loop under StrictMode.
  if (user?.role === 'cpo') {
    return <Navigate to="/cpo/dashboard" replace />;
  }

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!tenantName.trim()) {
      setError('Please enter your organization name.');
      return;
    }

    setLoading(true);
    setError('');

    try {
      await api.post('/api/cpo/setup', { tenant_name: tenantName.trim() });
      // Refresh user to get updated role and tenant_id
      await refreshUser();
      navigate('/cpo/dashboard', { replace: true });
    } catch (err) {
      setError(err.message || 'Setup failed. Please try again.');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="page-container" style={{ maxWidth: '520px' }}>
      <div className="glass glass-panel animate-fade-in" style={{ textAlign: 'center' }}>
        {/* Header Icon */}
        <div style={{ fontSize: '3.5rem', marginBottom: '1rem' }}>🏢</div>

        <h1 style={{ marginBottom: '0.5rem' }}>Become a Host</h1>
        <p style={{ marginBottom: '2rem', maxWidth: '380px', margin: '0 auto 2rem' }}>
          Set up your organization to start managing EV charging plugs,
          create charger groups, and earn revenue from charging sessions.
        </p>

        <form onSubmit={handleSubmit}>
          <div className="input-group" style={{ textAlign: 'left', marginBottom: '1.5rem' }}>
            <label htmlFor="tenant-name">Organization Name</label>
            <input
              id="tenant-name"
              type="text"
              className="input"
              placeholder="e.g. Sunrise Apartments, TechPark Office"
              value={tenantName}
              onChange={(e) => setTenantName(e.target.value)}
              maxLength={100}
              autoFocus
            />
          </div>

          {error && (
            <p className="error-text" style={{ marginBottom: '1rem' }}>
              {error}
            </p>
          )}

          <button
            type="submit"
            className="btn btn-cpo btn-lg btn-full"
            disabled={loading || !tenantName.trim()}
          >
            {loading ? 'Setting up...' : '🚀 Create Organization'}
          </button>
        </form>

        {/* Info cards */}
        <div style={{ marginTop: '2rem', textAlign: 'left' }}>
          <div className="divider" />
          <h3 style={{ fontSize: '0.9rem', color: 'var(--color-text-secondary)', marginBottom: '1rem' }}>
            What you'll get:
          </h3>
          <div className="flex flex-col gap-3">
            {[
              { icon: '🔌', text: 'Register and manage smart plug chargers' },
              { icon: '👥', text: 'Create public or private charger groups' },
              { icon: '📊', text: 'View revenue and energy analytics' },
              { icon: '⚡', text: 'Monitor live charging sessions' },
            ].map((item, i) => (
              <div
                key={i}
                className="flex items-center gap-3"
                style={{ fontSize: '0.9rem', color: 'var(--color-text-secondary)' }}
              >
                <span style={{ fontSize: '1.2rem' }}>{item.icon}</span>
                {item.text}
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
};

export default CpoSetup;
