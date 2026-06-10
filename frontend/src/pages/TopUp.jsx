import React, { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useWallet } from '../contexts/WalletContext';
import { useAuth } from '../contexts/AuthContext';

const TopUp = () => {
  const { topUp, balance } = useWallet();
  const { user } = useAuth();
  const navigate = useNavigate();
  const [isProcessing, setIsProcessing] = useState(false);
  const [selectedAmount, setSelectedAmount] = useState(100);

  if (!user) {
    return (
      <div className="container" style={{ maxWidth: '600px', marginTop: '2rem', textAlign: 'center' }}>
        <h2>Sign in to top up</h2>
        <button className="btn btn-primary mt-4" onClick={() => navigate('/')}>Go Home</button>
      </div>
    );
  }

  const handlePayment = () => {
    setIsProcessing(true);
    // Simulate Stripe checkout delay
    setTimeout(() => {
      topUp(selectedAmount);
      setIsProcessing(false);
      navigate('/');
    }, 1500);
  };

  const options = [
    { coins: 50, price: '$5' },
    { coins: 100, price: '$10' },
    { coins: 250, price: '$22' },
    { coins: 500, price: '$40' }
  ];

  return (
    <div className="container flex flex-col gap-6" style={{ maxWidth: '600px', marginTop: '2rem' }}>
      <header>
        <h1 style={{ marginBottom: '0.5rem' }}>Top Up Wallet</h1>
        <p>Current Balance: <strong style={{ color: 'var(--color-accent)' }}>{balance} Coins</strong></p>
      </header>

      <div className="glass glass-panel flex flex-col gap-6">
        <h3>Select Amount</h3>
        
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '1rem' }}>
          {options.map(opt => (
            <div 
              key={opt.coins}
              onClick={() => setSelectedAmount(opt.coins)}
              style={{
                padding: '1.5rem',
                borderRadius: 'var(--radius-md)',
                border: `2px solid ${selectedAmount === opt.coins ? 'var(--color-primary)' : 'var(--color-surface-border)'}`,
                background: selectedAmount === opt.coins ? 'hsla(200, 80%, 55%, 0.1)' : 'transparent',
                cursor: 'pointer',
                textAlign: 'center',
                transition: 'all 0.2s'
              }}
            >
              <div style={{ fontSize: '1.5rem', fontWeight: 'bold', color: 'var(--color-accent)' }}>{opt.coins}</div>
              <div style={{ color: 'var(--color-text-secondary)', fontSize: '0.9rem' }}>Coins</div>
              <div style={{ marginTop: '0.5rem', fontWeight: 500 }}>{opt.price}</div>
            </div>
          ))}
        </div>

        <div style={{ marginTop: '1rem', borderTop: '1px solid var(--color-surface-border)', paddingTop: '1.5rem' }}>
          <button 
            className="btn btn-primary" 
            style={{ width: '100%', padding: '1rem', fontSize: '1.1rem' }}
            onClick={handlePayment}
            disabled={isProcessing}
          >
            {isProcessing ? 'Processing Payment...' : `Pay ${options.find(o => o.coins === selectedAmount)?.price}`}
          </button>
          <p style={{ textAlign: 'center', marginTop: '1rem', fontSize: '0.85rem', color: 'var(--color-text-secondary)' }}>
            This is a mock checkout. No real money will be charged.
          </p>
        </div>
      </div>
    </div>
  );
};

export default TopUp;
