import React from 'react';
import { useNavigate } from 'react-router-dom';
import { useWallet } from '../contexts/WalletContext';
import { useAuth } from '../contexts/AuthContext';

const WalletCard = () => {
  const { balance } = useWallet();
  const { user } = useAuth();
  const navigate = useNavigate();

  if (!user) {
    return (
      <div className="glass glass-panel flex flex-col items-center justify-center gap-4" style={{ textAlign: 'center', minHeight: '200px' }}>
        <h2 style={{ marginBottom: 0 }}>Sign in to view your wallet</h2>
        <p>You need an account to top up and start charging.</p>
      </div>
    );
  }

  return (
    <div className="glass glass-panel flex flex-col gap-6" style={{ position: 'relative', overflow: 'hidden' }}>
      {/* Decorative background blur */}
      <div style={{
        position: 'absolute',
        top: '-50px',
        right: '-50px',
        width: '150px',
        height: '150px',
        background: 'var(--color-primary)',
        filter: 'blur(80px)',
        opacity: 0.3,
        borderRadius: '50%',
        zIndex: 0
      }} />

      <div style={{ zIndex: 1, position: 'relative' }}>
        <h3 style={{ color: 'var(--color-text-secondary)', fontWeight: 500, fontSize: '1rem', marginBottom: '0.5rem' }}>Current Balance</h3>
        <div className="flex items-center gap-4">
          <span style={{ fontSize: '3.5rem', fontWeight: 700, color: 'var(--color-accent)', lineHeight: 1 }}>
            {balance}
          </span>
          <span style={{ fontSize: '1.2rem', color: 'var(--color-text-secondary)', alignSelf: 'flex-end', paddingBottom: '0.5rem' }}>Coins</span>
        </div>
      </div>

      <div className="flex gap-4" style={{ zIndex: 1, position: 'relative', marginTop: '1rem' }}>
        <button className="btn btn-primary" onClick={() => navigate('/topup')} style={{ flex: 1 }}>
          Top Up Balance
        </button>
        <button className="btn btn-danger" style={{ flex: 1 }}>
          View History
        </button>
      </div>
    </div>
  );
};

export default WalletCard;
