/**
 * ResetPassword — landing page for the emailed reset link
 * (/reset-password?token=...). New-password form → POST
 * /api/auth/reset-password. The backend enforces the same 8-72 char rule as
 * registration, revokes every existing session (token_version bump), and
 * consumes the single-use token — an expired/used/unknown token gets a
 * uniform 400 which is surfaced verbatim, with an inline "Request a new
 * link" button (the #1 stale-link path: reset emails sit unread for days).
 * Password mismatch is validated live as the driver types the confirmation.
 */

import { useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import AuthShell from '../components/AuthShell';
import api from '../api/client';
import { apiErrorCopy } from '../utils/statusCopy';

const MIN_LEN = 8;
const MAX_LEN = 72;

const ResetPassword = () => {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const token = searchParams.get('token') || '';

  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const [tokenFailed, setTokenFailed] = useState(false);

  const mismatch = confirm.length > 0 && password !== confirm;

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError('');
    setTokenFailed(false);

    if (password.length < MIN_LEN || password.length > MAX_LEN) {
      setError(`Password must be ${MIN_LEN}-${MAX_LEN} characters.`);
      return;
    }
    if (password !== confirm) {
      setError('Passwords do not match.');
      return;
    }

    setBusy(true);
    try {
      await api.post('/api/auth/reset-password', { token, password });
      setDone(true);
    } catch (err) {
      setError(apiErrorCopy(err));
      setTokenFailed(true);
    } finally {
      setBusy(false);
    }
  };

  if (!token) {
    return (
      <AuthShell title="Reset password">
        <div className="stack">
          <p className="auth-body">
            This reset link is missing its token. Please use the link from
            your email, or request a new one.
          </p>
          <Link to="/forgot-password" className="btn btn-primary btn-lg btn-full">
            Request a new link
          </Link>
        </div>
      </AuthShell>
    );
  }

  if (done) {
    return (
      <AuthShell title="Password updated">
        <div className="stack" role="status">
          <div className="banner banner-ok">
            <p>
              Password updated. All your existing sessions have been signed
              out — sign in with your new password.
            </p>
          </div>
          <button
            type="button"
            className="btn btn-primary btn-lg btn-full"
            onClick={() => navigate('/login', { replace: true })}
          >
            Go to sign in
          </button>
        </div>
      </AuthShell>
    );
  }

  return (
    <AuthShell title="Reset password">
      <form onSubmit={handleSubmit} className="stack">
        <div className="field">
          <label className="field-label" htmlFor="password">New password</label>
          <input
            id="password"
            type="password"
            className="input"
            placeholder="8-72 characters"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
            minLength={MIN_LEN}
            maxLength={MAX_LEN}
            autoComplete="new-password"
            autoFocus
          />
        </div>

        <div className="field">
          <label className="field-label" htmlFor="confirm">Confirm new password</label>
          <input
            id="confirm"
            type="password"
            className="input"
            placeholder="Repeat the new password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            required
            minLength={MIN_LEN}
            maxLength={MAX_LEN}
            autoComplete="new-password"
            aria-invalid={mismatch ? 'true' : undefined}
            aria-describedby={mismatch ? 'confirm-mismatch' : undefined}
          />
          {mismatch && (
            <p className="field-error" id="confirm-mismatch" aria-live="polite">
              Passwords do not match.
            </p>
          )}
        </div>

        {error && (
          <div className="banner banner-danger" role="alert">
            <div>
              <p>{error}</p>
              {tokenFailed && (
                <Link to="/forgot-password" className="btn btn-quiet btn-sm auth-inline-action">
                  Request a new link
                </Link>
              )}
            </div>
          </div>
        )}

        <button type="submit" className="btn btn-primary btn-lg btn-full" disabled={busy}>
          {busy ? 'Updating…' : 'Set new password'}
        </button>
      </form>
    </AuthShell>
  );
};

export default ResetPassword;
