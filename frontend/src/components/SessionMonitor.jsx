/**
 * AmpHive Session Monitor Component
 * ===================================
 * Displays real-time charging telemetry in a grid layout.
 * Shows: power (kW), energy (kWh), current (A), cost (coins),
 * elapsed time, and session status with a stop button.
 */

import { useSession } from '../contexts/SessionContext';

const StatBox = ({ label, value, unit, colorVar = '--color-text-primary' }) => (
  <div
    className="glass flex flex-col justify-center"
    style={{
      padding: '1.25rem',
      background: 'hsla(220, 20%, 15%, 0.6)',
      borderRadius: 'var(--radius-md)',
    }}
  >
    <span style={{
      color: 'var(--color-text-muted)',
      fontSize: '0.75rem',
      textTransform: 'uppercase',
      letterSpacing: '0.08em',
      marginBottom: '0.4rem',
      fontWeight: 600,
    }}>
      {label}
    </span>
    <div className="flex items-baseline gap-1">
      <span style={{ fontSize: '1.75rem', fontWeight: 700, color: `var(${colorVar})` }}>
        {value}
      </span>
      <span style={{ color: 'var(--color-text-secondary)', fontSize: '0.85rem' }}>
        {unit}
      </span>
    </div>
  </div>
);

const SessionMonitor = () => {
  const { sessionData, isActive, stopSession } = useSession();

  if (!isActive && !sessionData) {
    return (
      <div
        className="glass glass-panel flex flex-col items-center justify-center gap-4"
        style={{ minHeight: '300px', textAlign: 'center' }}
      >
        <span style={{ fontSize: '3rem' }}>🔌</span>
        <h2 style={{ marginBottom: 0 }}>No Active Session</h2>
        <p>Enter a Plug ID on the Home page to start charging.</p>
      </div>
    );
  }

  // Formatting helpers
  const formatTime = (seconds) => {
    if (!seconds) return '00:00:00';
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    return [h, m, s].map(v => v.toString().padStart(2, '0')).join(':');
  };

  const powerW = sessionData?.power_w ? sessionData.power_w.toFixed(0) : '0';
  const energyKwh = sessionData?.energy_kwh ? sessionData.energy_kwh.toFixed(3) : '0.000';
  const cost = sessionData?.cost_coins ? sessionData.cost_coins.toFixed(2) : '0.00';
  const current = sessionData?.current_a ? parseFloat(sessionData.current_a).toFixed(1) : '0.0';

  return (
    <div className="glass glass-panel flex flex-col gap-6 animate-fade-in">
      {/* Header: Title + Status + Timer */}
      <div className="flex justify-between items-center">
        <div>
          <h2 style={{ marginBottom: '0.25rem' }}>Charging Session</h2>
          <div className="flex items-center gap-2">
            {isActive ? (
              <>
                <div
                  style={{
                    width: '10px',
                    height: '10px',
                    borderRadius: '50%',
                    background: 'var(--color-success)',
                    boxShadow: '0 0 8px var(--color-success-glow)',
                  }}
                  className="animate-pulse"
                />
                <span style={{ color: 'var(--color-success)', fontWeight: 600, fontSize: '0.9rem' }}>
                  Active
                </span>
              </>
            ) : (
              <>
                <div style={{ width: '10px', height: '10px', borderRadius: '50%', background: 'var(--color-text-muted)' }} />
                <span style={{ color: 'var(--color-text-muted)', fontWeight: 500, fontSize: '0.9rem' }}>
                  Completed
                </span>
              </>
            )}
          </div>
        </div>

        <div style={{ textAlign: 'right' }}>
          <span style={{ color: 'var(--color-text-muted)', fontSize: '0.75rem', display: 'block', marginBottom: '0.25rem', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
            Elapsed
          </span>
          <span style={{
            fontSize: '1.5rem',
            fontFamily: 'monospace',
            fontWeight: 700,
            color: 'var(--color-primary)',
            letterSpacing: '0.05em',
          }}>
            {formatTime(sessionData?.duration_sec)}
          </span>
        </div>
      </div>

      {/* Stats Grid */}
      <div className="stat-grid">
        <StatBox label="Charging Speed" value={powerW} unit="W" colorVar="--color-primary" />
        <StatBox label="Energy Added" value={energyKwh} unit="kWh" colorVar="--color-accent" />
        <StatBox label="Current Drawn" value={current} unit="A" colorVar="--color-text-primary" />
        <StatBox label="Session Cost" value={cost} unit="Coins" colorVar="--color-danger" />
      </div>

      {/* Stop button */}
      {isActive && (
        <button
          className="btn btn-danger btn-lg btn-full"
          onClick={stopSession}
          style={{ marginTop: '0.5rem' }}
        >
          Stop Charging
        </button>
      )}
    </div>
  );
};

export default SessionMonitor;
