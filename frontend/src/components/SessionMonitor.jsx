import React from 'react';
import { useSession } from '../contexts/SessionContext';

const StatBox = ({ label, value, unit, colorClass = "color-text-primary" }) => (
  <div className="glass flex flex-col justify-center p-4" style={{ padding: '1.5rem', background: 'hsla(220, 20%, 20%, 0.6)' }}>
    <span style={{ color: 'var(--color-text-secondary)', fontSize: '0.85rem', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: '0.5rem' }}>{label}</span>
    <div className="flex items-baseline gap-1">
      <span style={{ fontSize: '2rem', fontWeight: 600, color: `var(--${colorClass})` }}>{value}</span>
      <span style={{ color: 'var(--color-text-secondary)', fontSize: '1rem' }}>{unit}</span>
    </div>
  </div>
);

const SessionMonitor = () => {
  const { sessionData, isActive, stopSession } = useSession();

  if (!isActive && !sessionData) {
    return (
      <div className="glass glass-panel flex flex-col items-center justify-center gap-4" style={{ minHeight: '300px', textAlign: 'center' }}>
        <h2 style={{ marginBottom: 0 }}>No Active Session</h2>
        <p>Scan a QR code to start charging.</p>
      </div>
    );
  }

  // Formatting helpers
  const formatTime = (seconds) => {
    if (!seconds) return "00:00:00";
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    return [h, m, s].map(v => v.toString().padStart(2, '0')).join(':');
  };

  const powerKw = sessionData?.power_w ? (sessionData.power_w / 1000).toFixed(2) : "0.00";
  const energyKwh = sessionData?.energy_kwh ? sessionData.energy_kwh.toFixed(3) : "0.000";
  const cost = sessionData?.cost_coins ? sessionData.cost_coins.toFixed(2) : "0.00";
  
  return (
    <div className="glass glass-panel flex flex-col gap-6">
      <div className="flex justify-between items-center">
        <div>
          <h2 style={{ marginBottom: '0.25rem' }}>Charging Session</h2>
          <div className="flex items-center gap-2">
            {isActive ? (
              <>
                <div style={{ width: '10px', height: '10px', borderRadius: '50%', background: 'var(--color-success)' }} className="animate-pulse" />
                <span style={{ color: 'var(--color-success)', fontWeight: 500, fontSize: '0.9rem' }}>Active</span>
              </>
            ) : (
              <>
                <div style={{ width: '10px', height: '10px', borderRadius: '50%', background: 'var(--color-text-secondary)' }} />
                <span style={{ color: 'var(--color-text-secondary)', fontWeight: 500, fontSize: '0.9rem' }}>Completed</span>
              </>
            )}
          </div>
        </div>
        
        <div style={{ textAlign: 'right' }}>
          <span style={{ color: 'var(--color-text-secondary)', fontSize: '0.85rem', display: 'block', marginBottom: '0.25rem' }}>Elapsed Time</span>
          <span style={{ fontSize: '1.5rem', fontFamily: 'monospace', color: 'var(--color-primary)' }}>{formatTime(sessionData?.duration_sec)}</span>
        </div>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: '1rem' }}>
        <StatBox label="Current Speed" value={powerKw} unit="kW" colorClass="color-primary" />
        <StatBox label="Energy Added" value={energyKwh} unit="kWh" colorClass="color-accent" />
        <StatBox label="Current Drawn" value={sessionData?.current_a || "0.0"} unit="A" />
        <StatBox label="Session Cost" value={cost} unit="Coins" colorClass="color-danger" />
      </div>

      {isActive && (
        <button 
          className="btn btn-danger" 
          onClick={stopSession}
          style={{ width: '100%', marginTop: '1rem', padding: '1rem', fontSize: '1.1rem' }}
        >
          Stop Charging
        </button>
      )}
    </div>
  );
};

export default SessionMonitor;
