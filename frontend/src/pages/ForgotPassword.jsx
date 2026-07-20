/**
 * AmpHive Forgot Password Page
 * ============================
 * Email form → POST /api/auth/forgot-password. The backend always answers the
 * same generic 200 (no account enumeration), so on success this page shows a
 * generic "check your inbox" message regardless of whether the address
 * matched an account. Styling mirrors Login (glass panel + shared form
 * classes — no new colors).
 */

import { useState } from 'react';
import { Link } from 'react-router-dom';
import api from '../api/client';

const ForgotPassword = () => {
  const [email, setEmail] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [sent, setSent] = useState(false);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError('');
    setLoading(true);
    try {
      await api.post('/api/auth/forgot-password', { email });
      setSent(true);
    } catch (err) {
      // Only rate limiting / network errors surface here — a non-matching
      // email still returns 200 by design.
      setError(err.message || 'Something went wrong. Please try again.');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="page-container animate-fade-in" style={{ maxWidth: '440px', marginTop: '4rem' }}>
      <div className="glass glass-panel animate-slide-up">
        <h2 style={{ marginBottom: '0.75rem', textAlign: 'center' }}>Forgot Password</h2>

        {sent ? (
          <>
            <p style={{ textAlign: 'center', marginBottom: '1.5rem' }}>
              If an account exists for that email, a password reset link has
              been sent. Check your inbox — the link expires after a short
              while.
            </p>
            <Link to="/login" className="btn btn-primary btn-lg btn-full" style={{ textAlign: 'center' }}>
              Back to Sign In
            </Link>
          </>
        ) : (
          <>
            <p style={{ textAlign: 'center', marginBottom: '1.5rem', fontSize: '0.95rem' }}>
              Enter your account email and we&apos;ll send you a link to reset
              your password.
            </p>
            <form onSubmit={handleSubmit} className="flex flex-col gap-4">
              <div className="input-group">
                <label htmlFor="email">Email Address</label>
                <input
                  id="email"
                  type="email"
                  className="input"
                  placeholder="you@example.com"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  required
                  autoComplete="email"
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
                {loading ? 'Please wait...' : 'Send Reset Link'}
              </button>
            </form>

            <div className="divider" />
            <p style={{ textAlign: 'center', fontSize: '0.95rem' }}>
              Remembered it?{' '}
              <Link to="/login" style={{ color: 'var(--color-primary)', fontWeight: 600 }}>
                Sign In
              </Link>
            </p>
          </>
        )}
      </div>
    </div>
  );
};

export default ForgotPassword;
