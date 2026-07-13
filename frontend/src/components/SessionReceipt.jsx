/**
 * AmpHive Session Receipt
 * =======================
 * Post-session summary shown after a driver stops charging (or a session is
 * auto-finalized): energy delivered, duration, peak power, the coins billed,
 * and the wallet balance before → after. Reads the stop response from
 * SessionContext (`receipt`). "Charge Again" / "View History" dismiss it.
 */

import { useNavigate } from 'react-router-dom';
import { useSession } from '../contexts/SessionContext';

const fmtDuration = (sec) => {
  if (sec == null || sec < 0) return '—';
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = Math.floor(sec % 60);
  if (h > 0) return `${h}h ${m}m ${s}s`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
};

const Row = ({ label, children, strong }) => (
  <div className="flex justify-between items-center" style={{ padding: '0.5rem 0' }}>
    <span style={{ color: 'var(--color-text-muted)', fontSize: '0.9rem' }}>{label}</span>
    <span style={{
      color: strong ? 'var(--color-text-primary)' : 'var(--color-text-secondary)',
      fontWeight: strong ? 700 : 500,
    }}>
      {children}
    </span>
  </div>
);

const SessionReceipt = () => {
  const { receipt, dismissReceipt } = useSession();
  const navigate = useNavigate();

  if (!receipt) return null;

  const {
    plug_name, energy_kwh, peak_power_w, coins_spent, shortfall_coins,
    balance_before, balance_remaining, duration_sec, ended_at, reason,
  } = receipt;

  const goHome = () => { dismissReceipt(); navigate('/'); };
  const goHistory = () => { dismissReceipt(); navigate('/history'); };

  const autoStopped = reason && /auto-stopped|telemetry lost|exhaust/i.test(reason);

  return (
    <div className="glass glass-panel flex flex-col gap-4 animate-fade-in">
      {/* Header */}
      <div className="flex flex-col items-center gap-2" style={{ textAlign: 'center' }}>
        <div style={{
          width: '52px', height: '52px', borderRadius: '50%',
          background: 'hsla(73, 100%, 50%, 0.12)',
          border: '2px solid var(--color-success)',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          fontSize: '1.6rem',
        }}>
          ✓
        </div>
        <h2 style={{ margin: 0 }}>Session Complete</h2>
        <p style={{ margin: 0, color: 'var(--color-text-muted)', fontSize: '0.9rem' }}>
          {plug_name || 'Charger'}
          {ended_at && ` · ${new Date(ended_at).toLocaleString()}`}
        </p>
      </div>

      {autoStopped && (
        <div
          style={{
            padding: '0.6rem 0.85rem',
            borderRadius: 'var(--radius-md)',
            background: 'hsla(38, 90%, 50%, 0.12)',
            border: '1px solid hsla(38, 90%, 50%, 0.4)',
            color: 'var(--color-warning, #f0a020)',
            fontSize: '0.85rem',
            textAlign: 'center',
          }}
        >
          This session was stopped automatically ({reason.replace(/^auto-stopped:\s*/i, '')}).
        </div>
      )}

      {/* Delivered */}
      <div style={{ borderTop: '1px solid var(--color-surface-border, hsla(0,0%,100%,0.08))' }}>
        <Row label="Energy delivered" strong>{(energy_kwh ?? 0).toFixed(3)} kWh</Row>
        <Row label="Duration">{fmtDuration(duration_sec)}</Row>
        <Row label="Peak power">{(peak_power_w ?? 0).toFixed(0)} W</Row>
      </div>

      {/* Billing */}
      <div style={{
        borderTop: '1px solid var(--color-surface-border, hsla(0,0%,100%,0.08))',
        paddingTop: '0.25rem',
      }}>
        <Row label="Charged" strong>
          <span style={{ color: 'var(--color-danger)' }}>−{(coins_spent ?? 0).toFixed(2)} coins</span>
        </Row>
        <Row label="Balance">
          {(balance_before ?? 0).toFixed(2)} → <strong style={{ color: 'var(--color-text-primary)' }}>{(balance_remaining ?? 0).toFixed(2)}</strong> coins
        </Row>
        {shortfall_coins > 0 && (
          <Row label="Uncollected (low balance)">
            <span style={{ color: 'var(--color-warning, #f0a020)' }}>{shortfall_coins.toFixed(2)} coins</span>
          </Row>
        )}
      </div>

      {/* Actions */}
      <div className="flex gap-3" style={{ marginTop: '0.5rem' }}>
        <button className="btn btn-ghost btn-full" onClick={goHistory}>View History</button>
        <button className="btn btn-accent btn-full" onClick={goHome}>Charge Again</button>
      </div>
    </div>
  );
};

export default SessionReceipt;
