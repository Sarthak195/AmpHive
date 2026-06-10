import React from 'react';
import { Link, useLocation } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';
import { useWallet } from '../contexts/WalletContext';

const Navbar = () => {
  const { user, login, logout } = useAuth();
  const { balance } = useWallet();
  const location = useLocation();

  const handleAuth = () => {
    if (user) {
      logout();
    } else {
      login({ name: 'EV Driver', id: 'usr_123' });
    }
  };

  return (
    <nav className="glass navbar flex justify-between items-center">
      <div className="flex items-center gap-4">
        <Link to="/" className="nav-link" style={{ fontSize: '1.25rem', fontWeight: '700', color: 'var(--color-primary)' }}>
          ⚡ AmpHive
        </Link>
      </div>

      <div className="flex items-center gap-6">
        <Link to="/" className={`nav-link ${location.pathname === '/' ? 'active' : ''}`}>Home</Link>
        <Link to="/topup" className={`nav-link ${location.pathname === '/topup' ? 'active' : ''}`}>Top Up</Link>
        
        {user && (
          <div className="flex items-center gap-4" style={{ borderLeft: '1px solid var(--color-surface-border)', paddingLeft: '1.5rem' }}>
            <span style={{ fontWeight: '600', color: 'var(--color-accent)' }}>
              🪙 {balance} Coins
            </span>
            <span style={{ color: 'var(--color-text-secondary)' }}>
              {user.name}
            </span>
          </div>
        )}
        
        <button onClick={handleAuth} className="btn btn-primary" style={{ padding: '0.5rem 1rem', fontSize: '0.9rem' }}>
          {user ? 'Sign Out' : 'Sign In'}
        </button>
      </div>
    </nav>
  );
};

export default Navbar;
