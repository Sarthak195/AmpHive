/**
 * AmpHive Reset Password Page
 * ===========================
 * Landing page for the emailed reset link (/reset-password?token=...).
 * New-password form → POST /api/auth/reset-password. The backend enforces the
 * same 8-72 char rule as registration, revokes every existing session
 * (token_version bump), and consumes the single-use token — an expired/used/
 * unknown token gets a uniform 400 which is surfaced verbatim. On success the
 * driver is pointed back to Sign In. Styling mirrors Login (glass panel +
 * shared form classes — no new colors).
 */

import { useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import api from '../api/client';

const ResetPassword = () => {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const token = searchParams.get('token') || '';

  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [done, setDone] = useState(false);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError('');
    if (password !== confirm) {
      setError('Passwords do not match.');
      return;
    }
    setLoading(true);
    try {
      await api.post('/api/auth/reset-password', { token, password });
      setDone(true);
    } catch (err) {
      setError(err.message || 'Something went wrong. Please try again.');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="page-container animate-fade-in" style={{ maxWidth: '440px', marginTop: '4rem' }}>
      <div className="glass glass-panel animate-slide-up">
        <h2 style={{ marginBottom: '0.75rem', textAlign: 'center' }}>Reset Password</h2>

        {!token ? (
          <>
            <p style={{ textAlign: 'center', marginBottom: '1.5rem' }}>
              This reset link is missing its token. Please use the link from
              your email, or request a new one.
            </p>
            <Link to="/forgot-password" className="btn btn-primary btn-lg btn-full" style={{ textAlign: 'center' }}>
              Request a New Link
            </Link>
          </>
        ) : done ? (
          <>
            <p style={{ textAlign: 'center', marginBottom: '1.5rem', color: 'var(--color-success)' }}>
              Password updated. All your existing sessions have been signed
              out — sign in with your new password.
            </p>
            <button
              type="button"
              className="btn btn-primary btn-lg btn-full"
              onClick={() => navigate('/login', { replace: true })}
            >
              Go to Sign In
            </button>
          </>
        ) : (
          <form onSubmit={handleSubmit} className="flex flex-col gap-4">
            <div className="input-group">
              <label htmlFor="password">New Password</label>
              <input
                id="password"
                type="password"
                className="input"
                placeholder="8-72 characters"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
                minLength={8}
                maxLength={72}
                autoComplete="new-password"
              />
            </div>

            <div className="input-group">
              <label htmlFor="confirm">Confirm New Password</label>
              <input
                id="confirm"
                type="password"
                className="input"
                placeholder="Repeat the new password"
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
                required
                minLength={8}
                maxLength={72}
                autoComplete="new-password"
              />
            </div>

            {error && (
              <div className="error-text" style={{ textAlign: 'center', padding: '0.5rem' }}>
                {error}
              </div>
            )}

            <button
              type="submit"
              className="btn btn-primary btn-lg btn-full"
              disabled={loading}
              style={{ marginTop: '0.5rem' }}
            >
              {loading ? 'Please wait...' : 'Set New Password'}
            </button>
          </form>
        )}
      </div>
    </div>
  );
};

export default ResetPassword;
