/**
 * GoogleSignInButton — the "Continue with Google" affordance shared by Login
 * and Signup. Config-gated (useConfig().google_login_enabled — see
 * ConfigContext/backend GET /api/config): renders nothing when the backend
 * hasn't reported GOOGLE_CLIENT_ID configured, so there's no dead button
 * pointing at a 503.
 *
 * Deliberately a plain `<a href="/api/auth/google/login">`, not a click
 * handler calling the api client — this has to be a real top-level browser
 * navigation (it ends in a 302 to accounts.google.com), which fetch/XHR
 * cannot follow cross-origin. The G mark is drawn inline (official brand
 * colors) so no external asset/font icon is needed.
 */

import { useConfig } from '../contexts/ConfigContext';

const GoogleMark = () => (
  <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true" focusable="false">
    <path fill="#4285F4" d="M17.64 9.2c0-.64-.06-1.25-.16-1.84H9v3.48h4.84a4.14 4.14 0 0 1-1.8 2.72v2.26h2.9c1.7-1.57 2.68-3.87 2.68-6.62Z" />
    <path fill="#34A853" d="M9 18c2.43 0 4.47-.8 5.96-2.18l-2.9-2.26c-.8.54-1.83.86-3.06.86-2.35 0-4.34-1.59-5.05-3.72H.98v2.33A9 9 0 0 0 9 18Z" />
    <path fill="#FBBC05" d="M3.95 10.7A5.4 5.4 0 0 1 3.68 9c0-.59.1-1.17.27-1.7V4.97H.98A9 9 0 0 0 0 9c0 1.45.35 2.83.98 4.03l2.97-2.33Z" />
    <path fill="#EA4335" d="M9 3.58c1.32 0 2.51.45 3.44 1.35l2.58-2.58C13.46.89 11.43 0 9 0A9 9 0 0 0 .98 4.97l2.97 2.33C4.66 5.17 6.65 3.58 9 3.58Z" />
  </svg>
);

const GoogleSignInButton = () => {
  const { google_login_enabled: googleLoginEnabled } = useConfig();
  if (!googleLoginEnabled) return null;

  return (
    <>
      <div className="auth-divider" role="separator">
        <span>or</span>
      </div>
      <a href="/api/auth/google/login" className="btn btn-quiet btn-lg btn-full">
        <GoogleMark />
        Continue with Google
      </a>
    </>
  );
};

export default GoogleSignInButton;
