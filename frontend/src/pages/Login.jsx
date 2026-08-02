/**
 * Login — email + password sign-in (day theme, AuthShell frame).
 * ================================================================
 * Register mode has moved to its own page (Signup) — this is sign-in only,
 * with a footer link to "Create an account".
 *
 * On success, returns the driver to wherever they were headed: the `?next=`
 * query param (the api client's 401 handler / Marketing's plug-ID funnel
 * both append it) wins when it's a safe same-app path, else router
 * state.from (ProtectedRoute / the Home QR/deep-link guard) — falling back
 * to Home. This keeps the printed-QR path intact: /login?next=/?plug=7 lands
 * back on / with the plug param preserved. `next` is validated via
 * isSafeInternalPath() so an attacker-controlled query string can never
 * bounce the driver off-origin (open-redirect guard).
 */

import { useState } from 'react';
import { Link, useNavigate, useLocation } from 'react-router-dom';
import { Eye, EyeOff } from 'lucide-react';
import AuthShell from '../components/AuthShell';
import GoogleSignInButton from '../components/GoogleSignInButton';
import { useAuth } from '../contexts/AuthContext';
import { apiErrorCopy } from '../utils/statusCopy';
import { isSafeInternalPath } from '../utils/safePath';

const Login = () => {
  const { login } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();

  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError('');
    setBusy(true);

    try {
      await login(email, password);
      // Return to the original destination: a safe ?next= param wins, else
      // router state.from (ProtectedRoute / the QR-deep-link guard), else Home.
      const next = new URLSearchParams(location.search).get('next');
      const from = location.state?.from;
      const fromTarget = from ? `${from.pathname}${from.search || ''}${from.hash || ''}` : null;
      const target = (next && isSafeInternalPath(next) ? next : null) || fromTarget || '/';
      navigate(target, { replace: true });
    } catch (err) {
      setError(apiErrorCopy(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <AuthShell
      title="Sign in"
      footer={
        <>
          New here? <Link to="/signup">Create an account</Link>
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

        <div className="field">
          <label className="field-label" htmlFor="password">Password</label>
          <div className="auth-password-row">
            <input
              id="password"
              type={showPassword ? 'text' : 'password'}
              className="input"
              placeholder="Enter your password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              autoComplete="current-password"
            />
            <button
              type="button"
              className="btn btn-ghost btn-icon auth-password-toggle"
              onClick={() => setShowPassword((v) => !v)}
              aria-label={showPassword ? 'Hide password' : 'Show password'}
              aria-pressed={showPassword}
            >
              {showPassword ? <EyeOff size={18} aria-hidden="true" /> : <Eye size={18} aria-hidden="true" />}
            </button>
          </div>
        </div>

        <p className="auth-forgot-link">
          <Link to="/forgot-password">Forgot password?</Link>
        </p>

        {error && (
          <div className="banner banner-danger" role="alert">
            <p>{error}</p>
          </div>
        )}

        <button type="submit" className="btn btn-primary btn-lg btn-full" disabled={busy}>
          {busy ? 'Signing in…' : 'Sign in'}
        </button>
      </form>

      <GoogleSignInButton />
    </AuthShell>
  );
};

export default Login;
