/**
 * Signup — name/email/password account creation (day theme, AuthShell frame).
 * =============================================================================
 * Split out of the old combined Login/register toggle. Password is enforced
 * 8-72 chars client-side (mirrors the backend rule — see ResetPassword) with
 * a live hint under the field. No terms/consent checkbox — none exist to
 * link to, so none is invented here. On success: toast "Account created",
 * then straight in (register() already logs the driver in) honoring the same
 * from/next return-to-origin logic as Login, so a QR deep-link funnel
 * (`/signup?next=/?plug=7`) still lands back where it should.
 */

import { useState } from 'react';
import { Link, useNavigate, useLocation } from 'react-router-dom';
import { Eye, EyeOff } from 'lucide-react';
import AuthShell from '../components/AuthShell';
import { useToast } from '../components/ui';
import { useAuth } from '../contexts/AuthContext';
import { apiErrorCopy } from '../utils/statusCopy';
import { isSafeInternalPath } from '../utils/safePath';

const MIN_LEN = 8;
const MAX_LEN = 72;

const Signup = () => {
  const { register } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const toast = useToast();

  const [fullName, setFullName] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const passwordTouched = password.length > 0;
  const passwordValid = password.length >= MIN_LEN && password.length <= MAX_LEN;

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError('');

    if (!fullName.trim()) {
      setError('Please enter your full name.');
      return;
    }
    if (!passwordValid) {
      setError(`Password must be ${MIN_LEN}-${MAX_LEN} characters.`);
      return;
    }

    setBusy(true);
    try {
      await register(email, password, fullName);
      toast.ok('Account created.');
      const from = location.state?.from;
      const next = new URLSearchParams(location.search).get('next');
      const target = from
        ? `${from.pathname}${from.search || ''}${from.hash || ''}`
        : (next && isSafeInternalPath(next) ? next : '/');
      navigate(target, { replace: true });
    } catch (err) {
      setError(apiErrorCopy(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <AuthShell
      title="Create your account"
      footer={
        <>
          Already have an account? <Link to="/login">Sign in</Link>
        </>
      }
    >
      <form onSubmit={handleSubmit} className="stack">
        <div className="field">
          <label className="field-label" htmlFor="fullName">Full name</label>
          <input
            id="fullName"
            type="text"
            className="input"
            placeholder="Enter your full name"
            value={fullName}
            onChange={(e) => setFullName(e.target.value)}
            required
            autoComplete="name"
            autoFocus
          />
        </div>

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
          />
        </div>

        <div className="field">
          <label className="field-label" htmlFor="password">Password</label>
          <div className="auth-password-row">
            <input
              id="password"
              type={showPassword ? 'text' : 'password'}
              className="input"
              placeholder="8-72 characters"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              minLength={MIN_LEN}
              maxLength={MAX_LEN}
              autoComplete="new-password"
              aria-invalid={passwordTouched && !passwordValid ? 'true' : undefined}
              aria-describedby="password-hint"
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
          <p
            id="password-hint"
            className={`auth-hint ${passwordTouched ? (passwordValid ? 'is-valid' : 'is-invalid') : ''}`}
            aria-live="polite"
          >
            {password.length}/{MAX_LEN} characters — at least {MIN_LEN} required
          </p>
        </div>

        {error && (
          <div className="banner banner-danger" role="alert">
            <p>{error}</p>
          </div>
        )}

        <button type="submit" className="btn btn-primary btn-lg btn-full" disabled={busy}>
          {busy ? 'Creating account…' : 'Create account'}
        </button>
      </form>
    </AuthShell>
  );
};

export default Signup;
