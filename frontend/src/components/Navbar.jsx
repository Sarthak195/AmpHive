/**
 * AmpHive Navbar
 * ==============
 * Top navigation bar with glassmorphic styling.
 * Shows nav links, coin balance, and auth state.
 * Responsive: hides desktop nav items on mobile.
 */

import { Link, useLocation, useNavigate } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';
import { useWallet } from '../contexts/WalletContext';

const Navbar = () => {
  const { user, logout } = useAuth();
  const { balance } = useWallet();
  const location = useLocation();
  const navigate = useNavigate();

  const handleAuth = () => {
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
        <Link
          to="/"
          className="nav-link"
          style={{ fontSize: '1.2rem', fontWeight: 700, color: 'var(--color-primary)' }}
        >
          ⚡ AmpHive
        </Link>
      </div>

      {/* Navigation Links + Auth */}
      <div className="flex items-center gap-6">
        {/* Desktop nav links */}
        <div className="flex items-center gap-4 nav-desktop">
          <Link to="/" className={`nav-link ${location.pathname === '/' ? 'active' : ''}`}>
            Home
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

        {/* User info + balance */}
        {user && (
          <div
            className="flex items-center gap-3"
            style={{
              borderLeft: '1px solid var(--color-surface-border)',
              paddingLeft: '1rem',
            }}
          >
            <span style={{ fontWeight: 600, color: 'var(--color-accent)', fontSize: '0.9rem' }}>
              🪙 {Number(balance).toFixed(2)}
            </span>
            <span style={{ color: 'var(--color-text-muted)', fontSize: '0.85rem' }}>
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
      </div>
    </nav>
  );
};

export default Navbar;
