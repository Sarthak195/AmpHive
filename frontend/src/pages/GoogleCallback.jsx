/**
 * GoogleCallback — landing page for the Google sign-in redirect
 * (/auth/google/callback#token=...).
 *
 * The backend (routers/auth.py google_callback) already did the entire OAuth
 * round-trip server-side — state check, code exchange, ID-token verification,
 * find-or-create/link — before ever sending the browser here; this page's
 * only job is to pick the app JWT out of the URL fragment and hand it to
 * AuthContext.loginWithToken(). The token rides in the fragment (`#token=`),
 * never a query string, so it never reaches this app's own access logs or
 * any proxy in front of it either.
 *
 * A missing/malformed token means something upstream broke (or someone
 * bookmarked/shared this URL without its fragment, which browsers never send
 * to a server anyway) — render the same error-state look as Login instead of
 * silently redirecting, so it's obvious sign-in didn't complete.
 */

import { useEffect, useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import AuthShell from '../components/AuthShell';
import { useAuth } from '../contexts/AuthContext';

const GoogleCallback = () => {
  const { loginWithToken } = useAuth();
  const navigate = useNavigate();
  const [error, setError] = useState('');
  const ran = useRef(false);

  useEffect(() => {
    // StrictMode/dev double-invokes effects — a token is single-use only in
    // spirit (the JWT itself is fine to reuse), but running loginWithToken
    // twice is still wasted work worth skipping.
    if (ran.current) return;
    ran.current = true;

    const hash = new URLSearchParams(window.location.hash.replace(/^#/, ''));
    const token = hash.get('token');

    if (!token) {
      setError('Google sign-in did not complete. Please try again.');
      return;
    }

    (async () => {
      try {
        await loginWithToken(token);
        navigate('/', { replace: true });
      } catch (err) {
        setError(err.message || 'Google sign-in did not complete. Please try again.');
      }
    })();
  }, [loginWithToken, navigate]);

  if (error) {
    return (
      <AuthShell
        title="Sign in"
        footer={
          <>
            New here? <Link to="/signup">Create an account</Link>
          </>
        }
      >
        <div className="stack">
          <div className="banner banner-danger" role="alert">
            <p>{error}</p>
          </div>
          <Link to="/login" className="btn btn-primary btn-lg btn-full">
            Back to sign in
          </Link>
        </div>
      </AuthShell>
    );
  }

  return (
    <AuthShell title="Signing in…">
      <p className="auth-body">Finishing sign-in with Google…</p>
    </AuthShell>
  );
};

export default GoogleCallback;
