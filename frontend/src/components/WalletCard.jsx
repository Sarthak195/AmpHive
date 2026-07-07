/**
 * AmpHive Wallet Card Component
 * ==============================
 * Displays the user's coin balance with a glassmorphic card design.
 * Includes top-up and history buttons.
 */

import { useNavigate } from 'react-router-dom';
import { useWallet } from '../contexts/WalletContext';
import { useAuth } from '../contexts/AuthContext';

const WalletCard = () => {
  const { balance } = useWallet();
  const { user } = useAuth();
  const navigate = useNavigate();

  if (!user) {
    return (
      <div className="glass glass-panel flex flex-col items-center justify-center gap-4" style={{ textAlign: 'center', minHeight: '180px' }}>
        <h2 style={{ marginBottom: 0 }}>Sign in to view your wallet</h2>
        <p>You need an account to top up and start charging.</p>
      </div>
    );
  }

  return (
    <div className="glass glass-panel flex flex-col gap-6" style={{ position: 'relative', overflow: 'hidden' }}>
      {/* Decorative glow blobs */}
      <div style={{
        position: 'absolute',
        top: '-40px',
        right: '-40px',
        width: '140px',
        height: '140px',
        background: 'var(--color-primary)',
        filter: 'blur(80px)',
        opacity: 0.2,
        borderRadius: '50%',
        zIndex: 0,
        pointerEvents: 'none',
      }} />
      <div style={{
        position: 'absolute',
        bottom: '-30px',
        left: '-30px',
        width: '100px',
        height: '100px',
        background: 'var(--color-accent)',
        filter: 'blur(60px)',
        opacity: 0.15,
        borderRadius: '50%',
        zIndex: 0,
        pointerEvents: 'none',
      }} />

      <div style={{ zIndex: 1, position: 'relative' }}>
        <h3 style={{
          color: 'var(--color-text-muted)',
          fontWeight: 500,
          fontSize: '0.85rem',
          marginBottom: '0.5rem',
          textTransform: 'uppercase',
          letterSpacing: '0.08em',
        }}>
          Current Balance
        </h3>
        <div className="flex items-baseline gap-3">
          <span style={{
            fontSize: '3rem',
            fontWeight: 700,
            color: 'var(--color-accent)',
            lineHeight: 1,
          }}>
            {balance.toFixed(2)}
          </span>
          <span style={{
            fontSize: '1.1rem',
            color: 'var(--color-text-secondary)',
            paddingBottom: '0.3rem',
          }}>
            Coins
          </span>
        </div>
      </div>

      <div className="flex gap-3" style={{ zIndex: 1, position: 'relative' }}>
        <button className="btn btn-primary" onClick={() => navigate('/topup')} style={{ flex: 1 }}>
          Top Up Balance
        </button>
        <button className="btn btn-ghost" onClick={() => navigate('/history')} style={{ flex: 1 }}>
          View History
        </button>
      </div>
    </div>
  );
};

export default WalletCard;
