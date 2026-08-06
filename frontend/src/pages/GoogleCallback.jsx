/**
 * GoogleCallback — landing page for the Google sign-in redirect
 * (/auth/google/callback#code=...).
 *
 * The backend (routers/auth.py google_callback) already did the entire OAuth
 * round-trip server-side — state check, code exchange, ID-token verification,
 * find-or-create/link — before ever sending the browser here. It does NOT put
 * the app JWT straight in the fragment though: an unbound bearer token in a
 * shareable URL is a login-CSRF / session-fixation vector (M5). Instead the
 * fragment carries a single-use, browser-bound CODE (`#code=`), and the real
 * binding is an httpOnly nonce cookie set on the redirect. This page's job is
 * to POST that code to /api/auth/google/exchange (which sends the nonce cookie
 * back) to obtain the JWT, then hand it to AuthContext.loginWithToken(). The
 * code rides in the fragment, never a query string, so it never reaches this
 * app's own access logs or any proxy in front of it.
 *
 * A missing code means something upstream broke (or someone bookmarked/shared
 * this URL without its fragment, which browsers never send to a server
 * anyway); a code with no matching nonce cookie (e.g. a planted link opened in
 * a different browser) is refused by the exchange endpoint. Either way we
 * render the same error-state look as Login instead of silently redirecting,
 * so it's obvious sign-in didn't complete.
 */

import { useEffect, useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import AuthShell from '../components/AuthShell';
import api from '../api/client';
import { useAuth } from '../contexts/AuthContext';

const GoogleCallback = () => {
  const { loginWithToken } = useAuth();
  const navigate = useNavigate();
  const [error, setError] = useState('');
  // When the failure is "already signed in as someone else", the way out is
  // back to the app, not the sign-in screen — track it so the banner can
  // offer the right link.
  const [alreadySignedIn, setAlreadySignedIn] = useState(false);
  const ran = useRef(false);

  useEffect(() => {
    // StrictMode/dev double-invokes effects — the exchange code is strictly
    // single-use server-side, so a double run would burn it and fail the
    // second pass; the ref guard keeps this to exactly one attempt.
    if (ran.current) return;
    ran.current = true;

    const hash = new URLSearchParams(window.location.hash.replace(/^#/, ''));
    const code = hash.get('code');

    // Scrub the code out of the address bar/history immediately — do this
    // unconditionally (before the async work below) so it happens whether
    // that work succeeds, throws, or never runs (no code at all). Leaving it
    // in place would keep a (single-use) sign-in code recoverable from browser
    // history on a shared machine even after an error.
    window.history.replaceState(null, '', window.location.pathname + window.location.search);

    if (!code) {
      setError('Google sign-in did not complete. Please try again.');
      return;
    }

    (async () => {
      try {
        // Trade the single-use code for the real app JWT. The httpOnly nonce
        // cookie set on the callback redirect rides along automatically
        // (same-origin request) and is what binds this exchange to the browser
        // that actually finished the flow.
        const { token } = await api.post('/api/auth/google/exchange', { code });
        await loginWithToken(token);
        navigate('/', { replace: true });
      } catch (err) {
        if (err.code === 'already_signed_in') setAlreadySignedIn(true);
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
          {alreadySignedIn ? (
            // We refused to swap the active account out from under the user —
            // send them back into the app they're already signed into rather
            // than to the sign-in screen.
            <Link to="/" className="btn btn-primary btn-lg btn-full">
              Return to app
            </Link>
          ) : (
            <Link to="/login" className="btn btn-primary btn-lg btn-full">
              Back to sign in
            </Link>
          )}
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
