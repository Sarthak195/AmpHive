/**
 * AppBar — driver-app top chrome (replaces the old Navbar).
 *
 * Brand + primary nav on the left, wallet pill / notifications / user menu
 * on the right. Below 720px the nav links hide (the MobileTabBar covers
 * them) while brand, wallet pill and bell remain. On the CPO host only the
 * brand and auth affordances render — driver navigation belongs to the
 * driver origin.
 */

import { useEffect, useRef, useState } from 'react';
import { Link, NavLink, useLocation, useNavigate } from 'react-router-dom';
import { Zap, Wallet as WalletIcon, User, LogOut, PlugZap } from 'lucide-react';
import { useAuth } from '../contexts/AuthContext';
import { useWallet } from '../contexts/WalletContext';
import NotificationBell from './NotificationBell';
import Money from './ui/Money';
import { isCpoHost, cpoOrigin } from '../utils/appHost';

const navLinkClass = ({ isActive }) => `appbar-link${isActive ? ' active' : ''}`;

const AppBar = () => {
  const { user, logout } = useAuth();
  const { balance } = useWallet();
  const location = useLocation();
  const navigate = useNavigate();
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef(null);
  const cpoHost = isCpoHost();

  // Close the user menu on any route change so it never lingers open.
  useEffect(() => {
    setMenuOpen(false);
  }, [location.pathname]);

  // Close on outside click / Escape while open.
  useEffect(() => {
    if (!menuOpen) return undefined;
    const onPointerDown = (e) => {
      if (menuRef.current && !menuRef.current.contains(e.target)) setMenuOpen(false);
    };
    const onKeyDown = (e) => {
      if (e.key === 'Escape') setMenuOpen(false);
    };
    document.addEventListener('mousedown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('mousedown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [menuOpen]);

  const handleSignOut = async () => {
    setMenuOpen(false);
    await logout();
    navigate('/');
  };

  const initial = (user?.full_name || user?.email || '?').trim().charAt(0).toUpperCase();

  return (
    <header className="appbar">
      <div className="appbar-inner">
        <Link to="/" className="brand">
          <span className="brand-bolt">
            <Zap size={16} aria-hidden="true" />
          </span>
          AmpHive
        </Link>

        {!cpoHost && (
          <nav className="appbar-nav" aria-label="Primary">
            <NavLink to="/" end className={navLinkClass}>Home</NavLink>
            <NavLink to="/map" className={navLinkClass}>Map</NavLink>
            <NavLink to="/activity" className={navLinkClass}>Activity</NavLink>
            <NavLink to="/groups" className={navLinkClass}>Groups</NavLink>
          </nav>
        )}

        <div className="appbar-spacer" />

        <div className="appbar-side">
          {user && !cpoHost && (
            <Link to="/wallet" className="wallet-pill" aria-label="Wallet balance">
              <WalletIcon size={15} aria-hidden="true" />
              <Money coins={balance} />
            </Link>
          )}

          {user && <NotificationBell />}

          {user ? (
            <div className="user-menu" ref={menuRef}>
              <button
                type="button"
                className="btn btn-quiet btn-icon"
                aria-label="Account menu"
                aria-haspopup="menu"
                aria-expanded={menuOpen}
                onClick={() => setMenuOpen((open) => !open)}
              >
                {initial}
              </button>
              {menuOpen && (
                <div className="user-menu-panel" role="menu" aria-label="Account">
                  <Link to="/account" className="user-menu-item" role="menuitem">
                    <User size={16} aria-hidden="true" />
                    Account
                  </Link>
                  {!cpoHost && (
                    <a href={`${cpoOrigin()}/cpo`} className="user-menu-item" role="menuitem">
                      <PlugZap size={16} aria-hidden="true" />
                      Host your chargers
                    </a>
                  )}
                  <button type="button" className="user-menu-item" role="menuitem" onClick={handleSignOut}>
                    <LogOut size={16} aria-hidden="true" />
                    Sign out
                  </button>
                </div>
              )}
            </div>
          ) : (
            <Link to="/login" className="btn btn-primary btn-sm">Sign in</Link>
          )}
        </div>
      </div>
    </header>
  );
};

export default AppBar;
