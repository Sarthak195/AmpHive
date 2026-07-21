/**
 * CpoLayout — the operator console shell.
 * =======================================
 * Volt-theme sidebar chrome for all 13 console pages: brand, org name
 * (TenantContext), grouped navigation with live count-pill badges
 * (Health = unacked events, Disputes = open disputes, Groups = pending
 * capacity requests), a footer with the signed-in email / driver-app link /
 * sign out, and — on mobile — a sticky topbar with an off-canvas drawer.
 *
 * It mounts <TenantProvider> and renders <CpoAlerts> above the page content
 * so every console page shows unacknowledged critical/warning events.
 * App.jsx mounts CPO routes as direct elements (pages wrap themselves in
 * this layout), so page content arrives via `children`; <Outlet/> is the
 * fallback should the route tree ever switch to a layout route.
 */

import { useEffect, useRef, useState } from 'react';
import { Link, Outlet, useLocation } from 'react-router-dom';
import {
  BatteryCharging,
  CalendarClock,
  ExternalLink,
  FileText,
  HeartPulse,
  LayoutDashboard,
  LogOut,
  Menu,
  PlugZap,
  Radio,
  Scale,
  Settings,
  Tags,
  Users,
  Wallet,
  Zap,
} from 'lucide-react';
import { useAuth } from '../contexts/AuthContext';
import { TenantProvider, useTenant } from '../contexts/TenantContext';
import useTheme from '../hooks/useTheme';
import { driverOrigin } from '../utils/appHost';
import NotificationBell from './NotificationBell';
import CpoAlerts from './CpoAlerts';
import './CpoLayout.css';

/** Grouped nav. `badge` names a key of TenantContext counts. */
const NAV_GROUPS = [
  {
    group: null,
    items: [{ to: '/cpo/dashboard', label: 'Dashboard', icon: LayoutDashboard }],
  },
  {
    group: 'Infrastructure',
    items: [
      { to: '/cpo/chargers', label: 'Chargers', icon: PlugZap },
      { to: '/cpo/gateways', label: 'Gateways', icon: Radio },
      { to: '/cpo/groups', label: 'Groups', icon: Users, badge: 'pendingCapacity' },
      { to: '/cpo/health', label: 'Health', icon: HeartPulse, badge: 'unackedEvents' },
    ],
  },
  {
    group: 'Operations',
    items: [
      { to: '/cpo/sessions', label: 'Sessions', icon: BatteryCharging },
      { to: '/cpo/reservations', label: 'Reservations', icon: CalendarClock },
    ],
  },
  {
    group: 'Revenue',
    items: [
      { to: '/cpo/pricing', label: 'Pricing', icon: Tags },
      { to: '/cpo/earnings', label: 'Earnings', icon: Wallet },
      { to: '/cpo/invoices', label: 'Invoices', icon: FileText },
      { to: '/cpo/disputes', label: 'Disputes', icon: Scale, badge: 'openDisputes' },
    ],
  },
];

const SETTINGS_ITEM = { to: '/cpo/settings', label: 'Settings', icon: Settings };

const ALL_ITEMS = [...NAV_GROUPS.flatMap((g) => g.items), SETTINGS_ITEM];

const NavItem = ({ item, counts, pathname }) => {
  const { to, label, icon: Icon, badge } = item;
  const active = pathname.startsWith(to);
  const count = badge ? counts[badge] : null;
  return (
    <Link
      to={to}
      className={`console-link${active ? ' active' : ''}`}
      aria-current={active ? 'page' : undefined}
    >
      <Icon aria-hidden="true" />
      {label}
      {count > 0 && <span className="count-pill">{count > 99 ? '99+' : count}</span>}
    </Link>
  );
};

const CpoLayoutInner = ({ children }) => {
  const [drawerOpen, setDrawerOpen] = useState(false);
  const { pathname } = useLocation();
  const { user, logout } = useAuth();
  const { profile, counts } = useTenant();
  const sidebarRef = useRef(null);
  const menuButtonRef = useRef(null);

  const orgName = profile?.tenant?.name;
  const pageTitle =
    ALL_ITEMS.find((item) => pathname.startsWith(item.to))?.label || 'Console';

  const closeDrawer = (restoreFocus = false) => {
    setDrawerOpen(false);
    if (restoreFocus) menuButtonRef.current?.focus();
  };

  // Route change (nav click) closes the drawer.
  useEffect(() => {
    setDrawerOpen(false);
  }, [pathname]);

  // Open drawer: focus moves in; Esc closes and returns focus to the toggle.
  useEffect(() => {
    if (!drawerOpen) return undefined;
    sidebarRef.current?.querySelector('a, button')?.focus();
    const onKey = (e) => {
      if (e.key === 'Escape') {
        setDrawerOpen(false);
        menuButtonRef.current?.focus();
      }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [drawerOpen]);

  return (
    <div className="console">
      <aside
        ref={sidebarRef}
        id="console-sidebar"
        className={`console-sidebar${drawerOpen ? ' open' : ''}`}
      >
        <div className="console-brand">
          <Zap size={20} aria-hidden="true" />
          <span>AmpHive Console</span>
        </div>
        {orgName && <div className="console-org">{orgName}</div>}

        <nav className="console-nav" aria-label="Console">
          {NAV_GROUPS.map(({ group, items }) => (
            <div key={group || 'main'} className="console-nav-section">
              {group && <div className="console-nav-group">{group}</div>}
              {items.map((item) => (
                <NavItem key={item.to} item={item} counts={counts} pathname={pathname} />
              ))}
            </div>
          ))}
          <div className="console-nav-section console-nav-end">
            <NavItem item={SETTINGS_ITEM} counts={counts} pathname={pathname} />
          </div>
        </nav>

        <div className="console-footer">
          {user?.email && <span className="console-footer-email">{user.email}</span>}
          <a className="console-footer-action" href={driverOrigin()}>
            Driver app
            <ExternalLink size={13} aria-hidden="true" />
          </a>
          <button type="button" className="console-footer-action" onClick={logout}>
            Sign out
            <LogOut size={13} aria-hidden="true" />
          </button>
        </div>
      </aside>

      {drawerOpen && (
        <button
          type="button"
          className="console-scrim"
          aria-label="Close menu"
          onClick={() => closeDrawer()}
        />
      )}

      <div className="console-column">
        <header className="console-topbar">
          <button
            ref={menuButtonRef}
            type="button"
            className="btn btn-quiet btn-icon"
            aria-label="Open menu"
            aria-expanded={drawerOpen}
            aria-controls="console-sidebar"
            onClick={() => setDrawerOpen(true)}
          >
            <Menu size={20} aria-hidden="true" />
          </button>
          <span className="console-topbar-title">{pageTitle}</span>
          <NotificationBell />
        </header>

        <main className="console-main">
          <CpoAlerts />
          {children ?? <Outlet />}
        </main>
      </div>
    </div>
  );
};

const CpoLayout = ({ children }) => {
  useTheme('volt');
  return (
    <TenantProvider>
      <CpoLayoutInner>{children}</CpoLayoutInner>
    </TenantProvider>
  );
};

export default CpoLayout;
