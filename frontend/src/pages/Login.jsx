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
 *
 * This page is registered on BOTH hosts (driver + operator console), which is
 * why the Google button is host-gated below — see the comment at its call
 * site.
 */

import { useState } from 'react';
import { Link, useNavigate, useLocation } from 'react-router-dom';
import { Eye, EyeOff } from 'lucide-react';
import AuthShell from '../components/AuthShell';
import GoogleSignInButton from '../components/GoogleSignInButton';
import ResendVerification from '../components/ResendVerification';
import { useAuth } from '../contexts/AuthContext';
import useDocumentMeta from '../hooks/useDocumentMeta';
import { apiErrorCopy } from '../utils/statusCopy';
import { isSafeInternalPath } from '../utils/safePath';
import { isCpoHost } from '../utils/appHost';

const Login = () => {
  const { login } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();

  // Distinct title so the tab, the back-history entry and the screen-reader
  // route announcement identify the page. noindex by default — a sign-in form
  // has nothing to rank for.
  useDocumentMeta({
    title: 'Sign in',
    description: 'Sign in to AmpHive to start a charge, top up your charging credit and see your activity.',
  });

  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  // A 403 means the account exists but its email is unverified — a distinct
  // state from the 401 "wrong credentials" error, with a resend affordance.
  const [unverified, setUnverified] = useState(false);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError('');
    setUnverified(false);
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
      if (err?.status === 403) {
        // Unverified email — show the verify/resend state, not a red error.
        setUnverified(true);
      } else {
        setError(apiErrorCopy(err));
      }
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
          {/* eslint-disable jsx-a11y/no-autofocus -- this page exists solely
              for this one form, so focusing its first field on arrival is the
              expected behaviour rather than focus being stolen from other
              content. Pre-existing; kept deliberately when jsx-a11y was
              switched on. */}
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
          {/* eslint-enable jsx-a11y/no-autofocus */}
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

        {unverified && (
          <div className="banner banner-warn" role="alert">
            <p>
              Please verify your email address before signing in. Check your
              inbox for the verification link, or resend it below.
            </p>
          </div>
        )}

        <button type="submit" className="btn btn-primary btn-lg btn-full" disabled={busy}>
          {busy ? 'Signing in…' : 'Sign in'}
        </button>
      </form>

      {unverified && <ResendVerification email={email} label="Resend verification email" />}

      {/* TEMPORARY host gate — NOT a design decision.
          "Sign in with Google" is broken on the console host in two places at
          once: /auth/google/callback isn't registered in CpoHostRoutes
          (App.jsx), and the backend hardcodes the DRIVER origin as the final
          redirect target (routers/auth.py, `auth/google/callback`). So an
          operator who clicks this on cpo.<domain> is landed on the driver
          origin, has their token written to localStorage for THAT origin, and
          is never signed in to the console — a dead end that looks like a
          login failure.
          The real fix is to make the OAuth redirect_uri host-aware (carry the
          originating host through `state` and validate it against an
          allowlist of the driver + console origins) and register the callback
          route on both hosts. Until then, hiding the button is better than
          offering one that cannot work. */}
      {!isCpoHost() && <GoogleSignInButton />}
    </AuthShell>
  );
};

export default Login;
