/**
 * AmpHive Navbar
 * ==============
 * Top navigation bar with glassmorphic styling.
 * Shows nav links, coin balance, and auth state.
 * Responsive: below 768px the nav links collapse behind a hamburger toggle
 * that opens a dropdown panel (the same <Link> markup as desktop — no
 * duplicated nav items, just a CSS-driven layout swap).
 */

import { useState, useEffect } from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';
import { useWallet } from '../contexts/WalletContext';
import NotificationBell from './NotificationBell';

const Navbar = () => {
  const { user, logout } = useAuth();
  const { balance } = useWallet();
  const location = useLocation();
  const navigate = useNavigate();
  const [menuOpen, setMenuOpen] = useState(false);

  // Close the mobile dropdown whenever the route changes (link tap, back
  // button, programmatic navigation, etc.) so it never lingers open.
  useEffect(() => {
    setMenuOpen(false);
  }, [location.pathname]);

  const handleAuth = () => {
    setMenuOpen(false);
    if (user) {
      logout();
      navigate('/');
    } else {
      navigate('/login');
    }
  };

  return (
    <nav className="glass navbar flex justify-between items-center">
      {/* Logo */}
      <div className="flex items-center gap-4">
        <Link to="/" className="nav-link navbar-logo">
          ⚡ AmpHive
        </Link>
      </div>

      {/* Navigation Links + Auth */}
      <div className="flex items-center gap-6 navbar-actions">
        {/* Nav links: a horizontal row on desktop; on mobile this becomes a
            dropdown panel toggled by the hamburger button below. */}
        <div className={`flex items-center gap-4 nav-desktop ${menuOpen ? 'open' : ''}`}>
          <Link to="/" className={`nav-link ${location.pathname === '/' ? 'active' : ''}`}>
            Home
          </Link>
          {/* Public charger map — no account needed; also an entry point for
              anonymous visitors (the navbar renders on /login too). */}
          <Link to="/map" className={`nav-link ${location.pathname === '/map' ? 'active' : ''}`}>
            🗺️ Map
          </Link>
          <Link to="/topup" className={`nav-link ${location.pathname === '/topup' ? 'active' : ''}`}>
            Top Up
          </Link>
          <Link to="/groups" className={`nav-link ${location.pathname === '/groups' ? 'active' : ''}`}>
            Groups
          </Link>

          {/* CPO Portal link — only for users with the 'cpo' role */}
          {user && (user.role === 'cpo' || user.role === 'admin') && (
            <Link
              to="/cpo/dashboard"
              className={`nav-link ${location.pathname.startsWith('/cpo') ? 'active' : ''}`}
              style={{ color: location.pathname.startsWith('/cpo') ? 'var(--color-cpo-accent)' : undefined }}
            >
              ⚡ CPO Portal
            </Link>
          )}

          {/* Become a Host link — for authenticated drivers who aren't CPOs yet */}
          {user && user.role === 'driver' && (
            <Link
              to="/cpo"
              className="nav-link"
              style={{ color: 'var(--color-cpo-accent)' }}
            >
              🏢 Become a Host
            </Link>
          )}
        </div>

        {/* User info + balance (full name/email hides on mobile to save space —
            the compact balance badge remains visible). */}
        {user && (
          <div className="flex items-center gap-3 navbar-user">
            <NotificationBell />
            <span style={{ fontWeight: 600, color: 'var(--color-accent)', fontSize: '0.9rem' }}>
              🪙 {Number(balance).toFixed(2)}
            </span>
            <span className="navbar-user-name" style={{ color: 'var(--color-text-muted)', fontSize: '0.85rem' }}>
              {user.full_name || user.email}
            </span>
          </div>
        )}

        {/* Auth button */}
        <button
          onClick={handleAuth}
          className="btn btn-primary btn-sm"
        >
          {user ? 'Sign Out' : 'Sign In'}
        </button>

        {/* Mobile menu toggle — hidden on desktop via CSS */}
        <button
          type="button"
          className="navbar-toggle"
          onClick={() => setMenuOpen((open) => !open)}
          aria-label={menuOpen ? 'Close navigation menu' : 'Open navigation menu'}
          aria-expanded={menuOpen}
        >
          {menuOpen ? '✕' : '☰'}
        </button>
      </div>

      {/* Tap-outside-to-close overlay for the mobile dropdown */}
      {menuOpen && (
        <div
          className="navbar-mobile-overlay"
          onClick={() => setMenuOpen(false)}
          aria-hidden="true"
        />
      )}
    </nav>
  );
};

export default Navbar;
