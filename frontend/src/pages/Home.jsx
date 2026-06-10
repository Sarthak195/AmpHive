import React, { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import WalletCard from '../components/WalletCard';
import { useAuth } from '../contexts/AuthContext';
import { useSession } from '../contexts/SessionContext';

const Home = () => {
  const { user } = useAuth();
  const { startSession } = useSession();
  const navigate = useNavigate();
  const [plugId, setPlugId] = useState('');

  const handleStartSession = (e) => {
    e.preventDefault();
    if (!plugId.trim()) return;
    
    // Simulate scanning a QR code and redirecting to the session page
    startSession(plugId);
    navigate('/session');
  };

  return (
    <div className="container flex flex-col gap-6" style={{ maxWidth: '600px', marginTop: '2rem' }}>
      <header style={{ textAlign: 'center', marginBottom: '2rem' }}>
        <h1 style={{ color: 'var(--color-primary)', fontSize: '2.5rem', marginBottom: '0.5rem' }}>AmpHive</h1>
        <p style={{ fontSize: '1.2rem', color: 'var(--color-text-secondary)' }}>Shared EV Charging Network</p>
      </header>

      <WalletCard />

      {user && (
        <div className="glass glass-panel flex flex-col gap-4">
          <h3>Start Charging</h3>
          <p>Scan a QR code or enter a Plug ID to begin.</p>
          
          <form onSubmit={handleStartSession} className="flex gap-4">
            <input 
              type="text" 
              placeholder="Enter Plug ID (e.g. plug_1)" 
              value={plugId}
              onChange={(e) => setPlugId(e.target.value)}
              style={{
                flex: 1,
                padding: '0.75rem 1rem',
                borderRadius: 'var(--radius-md)',
                border: '1px solid var(--color-surface-border)',
                background: 'hsla(220, 20%, 10%, 0.5)',
                color: 'white',
                fontSize: '1rem',
                outline: 'none'
              }}
            />
            <button type="submit" className="btn btn-accent" disabled={!plugId.trim()}>
              Start
            </button>
          </form>
        </div>
      )}
    </div>
  );
};

export default Home;
