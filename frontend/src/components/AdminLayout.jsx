/**
 * AdminLayout — platform-admin console shell (replaces the Phase A stub).
 *
 * Same `.console-*` shell pattern as the operator console (sidebar that
 * collapses to an off-canvas drawer under 900px): brand, flat nav
 * (Overview/Tenants/Users/Payouts/Gateways/Disputes/Audit — no group
 * headers, unlike the CPO console), footer with an optional link back to
 * the operator console for dual-role accounts, a link to the driver app,
 * the signed-in email, and sign out. `useTheme('volt', 'admin')` stamps
 * the shared charcoal-lime console theme with the violet admin accent.
 */

import { useEffect, useRef, useState } from 'react';
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom';
import {
  LayoutDashboard,
  Building2,
  Users,
  Banknote,
  Radio,
  PlugZap,
  UploadCloud,
  Scale,
  ScrollText,
  Zap,
  LogOut,
  Menu,
  X,
} from 'lucide-react';
import { useAuth } from '../contexts/AuthContext';
import useTheme from '../hooks/useTheme';
import { cpoOrigin, driverOrigin } from '../utils/appHost';
import SiteFooter from './SiteFooter';

const NAV_ITEMS = [
  { path: '/admin', label: 'Overview', icon: LayoutDashboard, end: true },
  { path: '/admin/tenants', label: 'Tenants', icon: Building2 },
  { path: '/admin/users', label: 'Users', icon: Users },
  { path: '/admin/payouts', label: 'Payouts', icon: Banknote },
  { path: '/admin/gateways', label: 'Gateways', icon: Radio },
  { path: '/admin/chargers', label: 'Chargers', icon: PlugZap },
  { path: '/admin/firmware-releases', label: 'Firmware', icon: UploadCloud },
  { path: '/admin/disputes', label: 'Disputes', icon: Scale },
  { path: '/admin/audit', label: 'Audit', icon: ScrollText },
];

const navLinkClass = ({ isActive }) => `console-link${isActive ? ' active' : ''}`;

/** Best-effort mobile topbar title: longest-matching nav item for the path. */
const currentTitle = (pathname) => {
  const match = NAV_ITEMS.find((item) =>
    item.end ? pathname === item.path : pathname.startsWith(item.path)
  );
  return match?.label || 'Admin';
};

/** Below this width the sidebar becomes an off-canvas drawer (see console.css). */
const MOBILE_QUERY = '(max-width: 900px)';

const AdminLayout = () => {
  useTheme('volt', 'admin');
  const { user, logout } = useAuth();
  const location = useLocation();
  const navigate = useNavigate();
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [isMobile, setIsMobile] = useState(() => window.matchMedia(MOBILE_QUERY).matches);
  const sidebarRef = useRef(null);
  const menuButtonRef = useRef(null);

  // Close the drawer on every route change so it never lingers open.
  useEffect(() => {
    setSidebarOpen(false);
  }, [location.pathname]);

  // Open drawer: focus moves in; Esc closes and returns focus to the toggle.
  useEffect(() => {
    if (!sidebarOpen) return undefined;
    sidebarRef.current?.querySelector('a, button')?.focus();
    const onKey = (e) => {
      if (e.key === 'Escape') {
        setSidebarOpen(false);
        menuButtonRef.current?.focus();
      }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [sidebarOpen]);

  // Track the mobile breakpoint so the closed off-canvas sidebar can be
  // marked inert (removed from tab order) on mobile only — on desktop the
  // sidebar is always visible and must stay interactive.
  useEffect(() => {
    const mql = window.matchMedia(MOBILE_QUERY);
    const onChange = (e) => setIsMobile(e.matches);
    mql.addEventListener('change', onChange);
    return () => mql.removeEventListener('change', onChange);
  }, []);

  const handleSignOut = async () => {
    await logout();
    navigate('/login');
  };

  return (
    <div className="console">
      <div className="console-topbar">
        <button
          ref={menuButtonRef}
          type="button"
          className="btn btn-quiet btn-icon"
          aria-label={sidebarOpen ? 'Close menu' : 'Open menu'}
          aria-expanded={sidebarOpen}
          aria-controls="admin-console-sidebar"
          onClick={() => setSidebarOpen((open) => !open)}
        >
          {sidebarOpen ? <X aria-hidden="true" /> : <Menu aria-hidden="true" />}
        </button>
        <strong>{currentTitle(location.pathname)}</strong>
      </div>

      {sidebarOpen && (
        <button
          type="button"
          className="console-scrim"
          aria-label="Close menu"
          onClick={() => setSidebarOpen(false)}
        />
      )}

      <aside
        ref={sidebarRef}
        id="admin-console-sidebar"
        className={`console-sidebar${sidebarOpen ? ' open' : ''}`}
        inert={isMobile && !sidebarOpen}
      >
        <div className="console-brand">
          <span className="brand-bolt">
            <Zap size={16} aria-hidden="true" />
          </span>
          AmpHive Admin
        </div>

        <nav className="console-nav" aria-label="Primary">
          {NAV_ITEMS.map(({ path, label, icon: Icon, end }) => (
            <NavLink key={path} to={path} end={end} className={navLinkClass}>
              <Icon aria-hidden="true" />
              {label}
            </NavLink>
          ))}
        </nav>

        <div className="console-footer">
          {user?.tenant_id && <a href={`${cpoOrigin()}/cpo`}>Operator console</a>}
          <a href={driverOrigin()}>Driver app ↗</a>
          <span>{user?.email}</span>
          <button type="button" className="btn btn-quiet btn-sm" onClick={handleSignOut}>
            <LogOut size={14} aria-hidden="true" />
            Sign out
          </button>
        </div>
      </aside>

      <main className="console-main">
        <Outlet />
      </main>

      {/* Same reasoning as CpoLayout: admins had no route to the legal pages
          either. A sibling of <main> so it is a real `contentinfo` landmark;
          .console is a 2-column grid here with no column wrapper, so
          .console-legal-footer pins it to column 2 rather than letting it
          land beneath the sidebar. */}
      <SiteFooter className="console-legal-footer" />
    </div>
  );
};

export default AdminLayout;
