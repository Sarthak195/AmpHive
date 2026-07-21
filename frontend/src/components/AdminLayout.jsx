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

import { useEffect, useState } from 'react';
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom';
import {
  LayoutDashboard,
  Building2,
  Users,
  Banknote,
  Radio,
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

const NAV_ITEMS = [
  { path: '/admin', label: 'Overview', icon: LayoutDashboard, end: true },
  { path: '/admin/tenants', label: 'Tenants', icon: Building2 },
  { path: '/admin/users', label: 'Users', icon: Users },
  { path: '/admin/payouts', label: 'Payouts', icon: Banknote },
  { path: '/admin/gateways', label: 'Gateways', icon: Radio },
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

const AdminLayout = () => {
  useTheme('volt', 'admin');
  const { user, logout } = useAuth();
  const location = useLocation();
  const navigate = useNavigate();
  const [sidebarOpen, setSidebarOpen] = useState(false);

  // Close the drawer on every route change so it never lingers open.
  useEffect(() => {
    setSidebarOpen(false);
  }, [location.pathname]);

  const handleSignOut = async () => {
    await logout();
    navigate('/login');
  };

  return (
    <div className="console">
      <div className="console-topbar">
        <button
          type="button"
          className="btn btn-quiet btn-icon"
          aria-label={sidebarOpen ? 'Close menu' : 'Open menu'}
          aria-expanded={sidebarOpen}
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

      <aside className={`console-sidebar${sidebarOpen ? ' open' : ''}`}>
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
    </div>
  );
};

export default AdminLayout;
