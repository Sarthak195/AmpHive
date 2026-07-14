/**
 * AmpHive CPO Layout Component
 * =============================
 * Sidebar + content wrapper for all CPO admin dashboard pages.
 * Features a collapsible sidebar with navigation links, tenant info,
 * and a floating toggle button for mobile.
 *
 * The CPO portal is set apart from the driver app by its sidebar layout and
 * density — the accent is the shared amp-lime (the old purple is retired).
 */

import { useState } from 'react';
import { Link, useLocation } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';

/**
 * Navigation items for the CPO sidebar.
 * Each item has a path, label, and emoji icon.
 */
const NAV_ITEMS = [
  { path: '/cpo/dashboard', label: 'Dashboard', icon: '📊' },
  { path: '/cpo/gateways', label: 'Gateways', icon: '📡' },
  { path: '/cpo/plugs', label: 'Plugs', icon: '🔌' },
  { path: '/cpo/groups', label: 'Groups', icon: '👥' },
  { path: '/cpo/sessions', label: 'Sessions', icon: '⚡' },
  { path: '/cpo/tariffs', label: 'Tariffs', icon: '💲' },
  { path: '/cpo/reservations', label: 'Reservations', icon: '📅' },
  { path: '/cpo/earnings', label: 'Earnings', icon: '🪙' },
  { path: '/cpo/faults', label: 'Faults', icon: '🛠️' },
];

const CpoLayout = ({ children, tenantName }) => {
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const location = useLocation();
  const { user } = useAuth();

  return (
    <div className="cpo-layout">
      {/* Sidebar Navigation */}
      <aside className={`cpo-sidebar ${sidebarOpen ? 'open' : ''}`}>
        {/* Sidebar Header: Tenant branding */}
        <div className="cpo-sidebar-header">
          <h3>⚡ CPO Portal</h3>
          <p>{tenantName || 'Your Organization'}</p>
        </div>

        {/* Navigation Links */}
        <nav className="cpo-sidebar-nav">
          {NAV_ITEMS.map((item) => (
            <Link
              key={item.path}
              to={item.path}
              className={`cpo-nav-item ${location.pathname === item.path ? 'active' : ''}`}
              onClick={() => setSidebarOpen(false)} // Close sidebar on mobile nav
            >
              <span className="cpo-nav-icon">{item.icon}</span>
              {item.label}
            </Link>
          ))}
        </nav>

        {/* Sidebar Footer: User info */}
        {user && (
          <div style={{
            padding: '1rem 1.25rem',
            borderTop: '1px solid var(--color-surface-border)',
            marginTop: 'auto',
          }}>
            <p style={{
              fontSize: '0.8rem',
              color: 'var(--color-text-muted)',
              margin: 0,
            }}>
              Signed in as
            </p>
            <p style={{
              fontSize: '0.85rem',
              color: 'var(--color-text-secondary)',
              margin: '0.15rem 0 0',
              fontWeight: 500,
            }}>
              {user.full_name || user.email}
            </p>
          </div>
        )}
      </aside>

      {/* Mobile sidebar toggle button */}
      <button
        className="cpo-sidebar-toggle"
        onClick={() => setSidebarOpen(!sidebarOpen)}
        aria-label="Toggle sidebar"
      >
        {sidebarOpen ? '✕' : '☰'}
      </button>

      {/* Mobile overlay to close sidebar when clicking outside */}
      {sidebarOpen && (
        <div
          style={{
            position: 'fixed',
            inset: 0,
            background: 'rgba(0,0,0,0.4)',
            zIndex: 35,
          }}
          onClick={() => setSidebarOpen(false)}
        />
      )}

      {/* Main Content Area */}
      <main className="cpo-content">
        {children}
      </main>
    </div>
  );
};

export default CpoLayout;
