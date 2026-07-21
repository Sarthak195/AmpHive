/**
 * ForgotPassword — email form → POST /api/auth/forgot-password.
 * ================================================================
 * The backend always answers the same generic 200 (no account enumeration),
 * so on success this page shows a generic "check your inbox" message
 * regardless of whether the address matched an account. Rate-limit/network
 * errors surface inline via apiErrorCopy and keep the form on screen.
 */

import { useState } from 'react';
import { Link } from 'react-router-dom';
import AuthShell from '../components/AuthShell';
import api from '../api/client';
import { apiErrorCopy } from '../utils/statusCopy';

const ForgotPassword = () => {
  const [email, setEmail] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState(false);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError('');
    setBusy(true);
    try {
      await api.post('/api/auth/forgot-password', { email });
      setSent(true);
    } catch (err) {
      // Only rate limiting / network errors surface here — a non-matching
      // email still returns 200 by design.
      setError(apiErrorCopy(err));
    } finally {
      setBusy(false);
    }
  };

  if (sent) {
    return (
      <AuthShell title="Check your inbox">
        <div className="stack" role="status">
          <p className="auth-body">
            If an account exists for that email, a password reset link has
            been sent. The link expires after a short while.
          </p>
          <Link to="/login" className="btn btn-primary btn-lg btn-full">
            Back to sign in
          </Link>
        </div>
      </AuthShell>
    );
  }

  return (
    <AuthShell
      title="Forgot password"
      sub="Enter your account email and we'll send you a link to reset your password."
      footer={
        <>
          Remembered it? <Link to="/login">Sign in</Link>
        </>
      }
    >
      <form onSubmit={handleSubmit} className="stack">
        <div className="field">
          <label className="field-label" htmlFor="email">Email address</label>
          <input
            id="email"
            type="email"
            className="input"
            placeholder="you@example.com"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
            autoComplete="email"
            autoFocus
          />
        </div>

        {error && (
          <div className="banner banner-danger" role="alert">
            <p>{error}</p>
          </div>
        )}

        <button type="submit" className="btn btn-primary btn-lg btn-full" disabled={busy}>
          {busy ? 'Sending…' : 'Send reset link'}
        </button>
      </form>
    </AuthShell>
  );
};

export default ForgotPassword;
