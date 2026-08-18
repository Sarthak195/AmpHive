/**
 * Signup — name/email/password account creation (day theme, AuthShell frame).
 * =============================================================================
 * Split out of the old combined Login/register toggle. Password is enforced
 * 8-72 chars client-side (mirrors the backend rule — see ResetPassword) with
 * a live hint under the field.
 *
 * Consent: the Terms of Service and Privacy Policy now exist (/terms,
 * /privacy), so the submit button carries a stated-consent line linking both.
 * Deliberately NOT a tick-box: a stated consent at the point of submission is
 * the norm, is equally binding, and doesn't add a failure mode (a form that
 * can silently refuse to submit because an unnoticed box is unticked).
 *
 * Registration no longer logs the driver in: the backend now returns
 * 200 { status: 'verification_sent', email } and emails a verification link.
 * On success this page flips to a "check your email" confirmation with a
 * ResendVerification control — the account stays inert until the emailed
 * /verify-email?token=... link is opened (that page mints the JWT).
 */

import { useState } from 'react';
import { Link } from 'react-router-dom';
import { Eye, EyeOff } from 'lucide-react';
import AuthShell from '../components/AuthShell';
import GoogleSignInButton from '../components/GoogleSignInButton';
import ResendVerification from '../components/ResendVerification';
import { useAuth } from '../contexts/AuthContext';
import useDocumentMeta from '../hooks/useDocumentMeta';
import { apiErrorCopy } from '../utils/statusCopy';

const MIN_LEN = 8;
const MAX_LEN = 72;

const Signup = () => {
  const { register } = useAuth();

  // Distinct title so the tab, the back-history entry and the screen-reader
  // route announcement say "Create your account", not the generic site title
  // every other route used to share. noindex by default: the sign-up form is
  // reachable from the indexed marketing page, which is the page that should
  // rank for it.
  useDocumentMeta({
    title: 'Create your account',
    description: 'Create an AmpHive account to find a charger nearby, plug in and pay from your charging credit.',
  });

  const [fullName, setFullName] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  // Set to the registered address once the backend accepts the sign-up —
  // flips the page to its "check your email" confirmation.
  const [sentTo, setSentTo] = useState('');

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
      const data = await register(email, password, fullName);
      // Prefer the email the backend echoes back; fall back to what was typed.
      setSentTo(data?.email || email);
    } catch (err) {
      setError(apiErrorCopy(err));
    } finally {
      setBusy(false);
    }
  };

  if (sentTo) {
    return (
      <AuthShell
        title="Check your email"
        footer={
          <>
            Already verified? <Link to="/login">Sign in</Link>
          </>
        }
      >
        <div className="stack">
          <div className="banner banner-ok" role="status">
            <p>
              We've sent a verification link to <strong>{sentTo}</strong>. Open
              it to activate your account and sign in. The link expires after a
              short while.
            </p>
          </div>
          <p className="auth-body">
            Didn't get it? Check your spam folder, or resend the email.
          </p>
          <ResendVerification email={sentTo} label="Resend email" />
        </div>
      </AuthShell>
    );
  }

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
          {/* eslint-disable jsx-a11y/no-autofocus -- this page exists solely
              for this one form, so focusing its first field on arrival is the
              expected behaviour rather than focus being stolen from other
              content. Pre-existing; kept deliberately when jsx-a11y was
              switched on. */}
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
          {/* eslint-enable jsx-a11y/no-autofocus */}
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

        {/* Stated consent at the point of submission — sits with the button
            that performs the act it refers to, not buried in a footer. */}
        <p className="auth-body">
          By creating an account you agree to our{' '}
          <Link to="/terms">Terms of Service</Link> and{' '}
          <Link to="/privacy">Privacy Policy</Link>.
        </p>
      </form>

      <GoogleSignInButton />
    </AuthShell>
  );
};

export default Signup;
