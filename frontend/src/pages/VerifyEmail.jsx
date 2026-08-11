/**
 * VerifyEmail — landing page for the emailed verification link
 * (/verify-email?token=...). Mirrors ResetPassword: the single-use token is
 * read from the query string and POSTed to /api/auth/verify-email on mount.
 *
 * On 200 the backend returns a full AuthResponse { token, user } — the same
 * shape login mints — so we adopt it through the app's normal loginWithToken
 * path (persist JWT + optimistic user + /me refresh) and drop the freshly
 * verified driver straight onto Home.
 *
 * On 400 (invalid / expired / already-used token — surfaced uniformly by the
 * backend) we show a clear error with an inline "resend verification email"
 * control (ResendVerification, email unknown → shows an input). A missing/
 * empty token short-circuits to the same resend affordance instead of a
 * doomed request.
 */

import { useState, useEffect, useRef } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import AuthShell from '../components/AuthShell';
import ResendVerification from '../components/ResendVerification';
import { useAuth } from '../contexts/AuthContext';
import api from '../api/client';
import { apiErrorCopy } from '../utils/statusCopy';

const VerifyEmail = () => {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const { loginWithToken } = useAuth();
  const token = searchParams.get('token') || '';

  // 'verifying' while the POST is in flight, 'error' once it fails. Success
  // never renders — we navigate away — so no 'done' state is needed.
  const [status, setStatus] = useState(token ? 'verifying' : 'missing');
  const [error, setError] = useState('');

  // Verify exactly once on mount (StrictMode double-invoke guard) — a
  // single-use token must not be spent twice.
  const ranRef = useRef(false);

  useEffect(() => {
    if (!token || ranRef.current) return;
    ranRef.current = true;

    (async () => {
      try {
        const data = await api.post('/api/auth/verify-email', { token });
        // AuthResponse { token, user } — adopt it via the same path Google
        // sign-in uses, then land on Home.
        await loginWithToken(data.token);
        navigate('/', { replace: true });
      } catch (err) {
        setError(apiErrorCopy(err));
        setStatus('error');
      }
    })();
  }, [token, loginWithToken, navigate]);

  if (status === 'missing') {
    return (
      <AuthShell title="Verify your email">
        <div className="stack">
          <p className="auth-body">
            This verification link is missing its token. Please use the link
            from your email, or request a new one below.
          </p>
          <ResendVerification />
        </div>
      </AuthShell>
    );
  }

  if (status === 'error') {
    return (
      <AuthShell title="Verification link invalid">
        <div className="stack">
          <div className="banner banner-danger" role="alert">
            <p>{error || 'This link is invalid or expired.'}</p>
          </div>
          <p className="auth-body">
            Enter your email address and we'll send a fresh verification link.
          </p>
          <ResendVerification />
        </div>
      </AuthShell>
    );
  }

  return (
    <AuthShell title="Verifying your email">
      <div className="stack" role="status">
        <p className="auth-body">One moment while we verify your email…</p>
      </div>
    </AuthShell>
  );
};

export default VerifyEmail;
