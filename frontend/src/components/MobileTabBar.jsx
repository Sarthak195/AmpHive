/**
 * MobileTabBar — bottom tab navigation for signed-in drivers (<=720px; the
 * CSS hides it on desktop). Rendered by the driver shell only when authed;
 * the shell adds .has-tabbar padding so content never hides behind it.
 */

import { NavLink } from 'react-router-dom';
import { Home, Map, Wallet, Activity } from 'lucide-react';

const TABS = [
  { to: '/', label: 'Home', icon: Home, end: true },
  { to: '/map', label: 'Map', icon: Map },
  { to: '/wallet', label: 'Wallet', icon: Wallet },
  { to: '/activity', label: 'Activity', icon: Activity },
];

const tabClass = ({ isActive }) => `tabbar-item${isActive ? ' active' : ''}`;

const MobileTabBar = () => (
  <nav className="tabbar" aria-label="Primary">
    {TABS.map(({ to, label, icon: Icon, end }) => (
      <NavLink key={to} to={to} end={end} className={tabClass}>
        <Icon aria-hidden="true" />
        {label}
      </NavLink>
    ))}
  </nav>
);

export default MobileTabBar;
