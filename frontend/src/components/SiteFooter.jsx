/**
 * SiteFooter — the legal/nav footer shown on every surface.
 * ========================================================
 * Before this component the ONLY footer in the app was the marketing one
 * (pages/Marketing.jsx), which renders on the anonymous homepage alone. The
 * moment a user signed in, `HomeGate` swapped Marketing for Dashboard and
 * every legal link disappeared — so a signed-in driver, and every operator and
 * admin, had no route to the Privacy Policy or the Terms from anywhere in the
 * product. That is the state a privacy policy is specifically supposed not to
 * be in.
 *
 * Rendered by DriverShell, CpoLayout, AdminLayout and AuthShell, so the links
 * are reachable from any page including the sign-in screens.
 *
 * `compact` drops the site nav and keeps only the legal row — used on the auth
 * screens, where a full nav would compete with the one thing the page is for.
 *
 * Cross-origin note: the driver app and the host console are separate origins,
 * so the legal pages (which live on the driver origin) are plain <a href> with
 * an absolute origin when rendered inside the console — a <Link> there would
 * hit the console's router, which has no such route.
 */

import { Link } from 'react-router-dom';
import { driverOrigin, isCpoHost } from '../utils/appHost';
import { SITE_NAME, SUPPORT_EMAIL } from '../utils/legal';
import './SiteFooter.css';

const LEGAL_LINKS = [
  { to: '/terms', label: 'Terms' },
  { to: '/privacy', label: 'Privacy' },
  { to: '/refunds', label: 'Refunds' },
  { to: '/charging-credit-terms', label: 'Charging credit' },
  { to: '/contact', label: 'Contact' },
];

export default function SiteFooter({ compact = false, className = '' }) {
  // On the console origin the legal routes don't exist locally — link out.
  const external = isCpoHost();
  const origin = external ? driverOrigin() : '';

  const renderLink = ({ to, label }) =>
    external ? (
      <a key={to} href={`${origin}${to}`}>
        {label}
      </a>
    ) : (
      <Link key={to} to={to}>
        {label}
      </Link>
    );

  return (
    <footer
      className={`site-footer${compact ? ' site-footer-compact' : ''}${className ? ` ${className}` : ''}`}
    >
      <nav className="site-footer-links" aria-label="Legal and policies">
        {LEGAL_LINKS.map(renderLink)}
        <a href={`mailto:${SUPPORT_EMAIL}`}>{SUPPORT_EMAIL}</a>
      </nav>
      <p className="site-footer-note text-3">
        © {new Date().getFullYear()} {SITE_NAME}. Shared EV charging on the plug
        points India already has.
      </p>
    </footer>
  );
}
