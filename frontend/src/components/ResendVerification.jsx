/**
 * ResendVerification — shared "resend the email-verification link" affordance.
 * =============================================================================
 * Used by three surfaces after the registration flow gained email
 * verification: Signup's "check your email" confirmation, the /verify-email
 * invalid/expired-token state, and Login's 403 "please verify" state.
 *
 * POSTs { email } to /api/auth/resend-verification. That endpoint always
 * answers a generic 200 (no account enumeration), so success shows a generic
 * "if an account exists…" line regardless of whether the address matched. A
 * 429 rate-limit (or network error) surfaces inline via apiErrorCopy.
 *
 * The button self-debounces: after a send it disables for a short cooldown so
 * repeated taps don't hammer the server-side rate limit. When `email` is
 * supplied (Signup / Login already know it) the address is fixed and only the
 * button renders; otherwise an email input is shown (the /verify-email
 * bad-token case, where the visitor's address is unknown).
 */

import { useState, useRef, useEffect } from 'react';
import api from '../api/client';
import { apiErrorCopy } from '../utils/statusCopy';

const COOLDOWN_MS = 30_000;

export default function ResendVerification({ email: fixedEmail, label = 'Resend verification email' }) {
  const hasFixedEmail = Boolean(fixedEmail);
  const [email, setEmail] = useState(fixedEmail || '');
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState(false);
  const [error, setError] = useState('');
  const [cooling, setCooling] = useState(false);
  const timer = useRef(null);

  useEffect(() => () => clearTimeout(timer.current), []);

  const send = async (e) => {
    e?.preventDefault();
    setError('');

    const addr = (hasFixedEmail ? fixedEmail : email).trim();
    if (!addr) {
      setError('Enter your email address.');
      return;
    }

    setBusy(true);
    try {
      await api.post('/api/auth/resend-verification', { email: addr });
      setSent(true);
      // Debounce: briefly disable to respect the server-side rate limit.
      setCooling(true);
      timer.current = setTimeout(() => setCooling(false), COOLDOWN_MS);
    } catch (err) {
      // Only a 429 rate-limit / network error reaches here — a non-matching
      // address still returns 200 by design.
      setError(apiErrorCopy(err));
    } finally {
      setBusy(false);
    }
  };

  const disabled = busy || cooling;
  const buttonLabel = busy ? 'Sending…' : cooling ? 'Sent — check your inbox' : label;

  return (
    <form onSubmit={send} className="stack">
      {!hasFixedEmail && (
        <div className="field">
          <label className="field-label" htmlFor="resend-email">Email address</label>
          <input
            id="resend-email"
            type="email"
            className="input"
            placeholder="you@example.com"
            value={email}
            onChange={(ev) => setEmail(ev.target.value)}
            required
            autoComplete="email"
          />
        </div>
      )}

      {sent && (
        <div className="banner banner-ok" role="status">
          <p>
            If an account exists for that email and still needs verifying, a
            new verification link is on its way. The link expires after a
            short while.
          </p>
        </div>
      )}

      {error && (
        <div className="banner banner-danger" role="alert">
          <p>{error}</p>
        </div>
      )}

      <button type="submit" className="btn btn-quiet btn-lg btn-full" disabled={disabled}>
        {buttonLabel}
      </button>
    </form>
  );
}
