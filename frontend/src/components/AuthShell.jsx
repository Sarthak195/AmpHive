/**
 * AuthShell — shared centered-card frame for the four auth pages (Login,
 * Signup, ForgotPassword, ResetPassword). Day theme, brand-bolt + wordmark
 * header linking home, one <h1> per page (the page title), optional
 * supporting copy, and a footer slot for the "New here? / Remembered it?"
 * cross-links. Identity comes entirely from tokens/primitives — this file
 * only owns centering/spacing layout.
 *
 * It also carries the compact site footer: sign-in and sign-up are the two
 * screens where somebody most wants to read the Terms or the Privacy Policy
 * BEFORE they commit, and they render outside every other shell in the app,
 * so without this they had no link to either.
 */

import { Link } from 'react-router-dom';
import { Zap } from 'lucide-react';
import SiteFooter from './SiteFooter';
import './AuthShell.css';

export default function AuthShell({ title, sub, children, footer }) {
  return (
    <main className="auth-shell">
      <div className="auth-shell-inner">
        <Link to="/" className="brand auth-brand">
          <span className="brand-bolt">
            <Zap size={16} aria-hidden="true" />
          </span>
          AmpHive
        </Link>

        <div className="card auth-card">
          <h1 className="auth-title">{title}</h1>
          {sub && <p className="auth-sub">{sub}</p>}
          {children}
        </div>

        {footer && <div className="auth-footer">{footer}</div>}

        {/* `compact` drops the site nav: on a screen whose whole job is one
            form, a full nav would compete with it. Inside .auth-shell-inner
            (a 420px grid column) rather than after <main>, so it stays inside
            the centred column instead of pushing the card off-centre. */}
        <SiteFooter compact />
      </div>
    </main>
  );
}
