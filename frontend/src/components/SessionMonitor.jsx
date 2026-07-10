/**
 * AmpHive Session Monitor Component
 * ===================================
 * Displays real-time charging telemetry in a grid layout.
 * Shows: power (W), energy (kWh), current (A), cost (coins), voltage + relay
 * state, a live elapsed timer, and session status with a stop button.
 *
 * Robustness: the elapsed timer ticks client-side from the session start time
 * (so it never freezes between telemetry frames), and a "connection lost"
 * banner appears when telemetry goes stale — either the server flags the
 * snapshot `is_stale`, or no frame has arrived for a while (dropped gateway /
 * socket). Gateway alarms affecting this plug (e.g. someone physically
 * switching the plug on/off) surface as an inline warning.
 */

import { useEffect, useState } from 'react';
import { useSession } from '../contexts/SessionContext';
import { useConfig } from '../contexts/ConfigContext';

// Consider the live feed stale after this many ms without a telemetry frame.
// Matches the backend TELEMETRY_STALE_AFTER_SEC default (15 s).
const STALE_AFTER_MS = 15000;

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
  const {
    sessionData, isActive, stopSession,
    lastFrameAt, focusedStartedAt, alarms,
  } = useSession();
  const { coins_per_kwh } = useConfig();

  // A 1 Hz clock so the elapsed timer and the staleness check update smoothly
  // between telemetry frames.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

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

  const formatTime = (seconds) => {
    if (!seconds || seconds < 0) return '00:00:00';
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = Math.floor(seconds % 60);
    return [h, m, s].map(v => v.toString().padStart(2, '0')).join(':');
  };

  const powerW = sessionData?.power_w ? sessionData.power_w.toFixed(0) : '0';
  const energyKwh = sessionData?.energy_kwh ? sessionData.energy_kwh.toFixed(3) : '0.000';
  const cost = sessionData?.cost_coins ? sessionData.cost_coins.toFixed(2) : '0.00';
  const current = sessionData?.current_a ? parseFloat(sessionData.current_a).toFixed(1) : '0.0';
  const voltage = sessionData?.voltage_v ? parseFloat(sessionData.voltage_v).toFixed(0) : null;
  const relayOn = sessionData?.relay_on;

  // Elapsed: client-side from the session start (smooth, never frozen); fall
  // back to the server's duration_sec if we don't yet have a start time.
  const elapsedSec = focusedStartedAt
    ? Math.floor((now - new Date(focusedStartedAt).getTime()) / 1000)
    : (sessionData?.duration_sec || 0);

  // Stale when the server flags it OR no frame has arrived recently. Only
  // meaningful while the session is active (a completed session is done, not
  // stale). We also require at least one frame to have been expected.
  const noFrameFor = lastFrameAt ? (now - lastFrameAt) : null;
  const isStale = isActive && (
    sessionData?.is_stale === true ||
    (noFrameFor !== null && noFrameFor > STALE_AFTER_MS)
  );

  // The most recent unacknowledged alarm for this plug (last 2 minutes).
  const plugId = sessionData?.plug_id;
  const recentAlarm = (alarms || []).find(
    (a) => a.plug_id === plugId && (now - (a.received_at || 0)) < 120000
  );

  return (
    <div className="glass glass-panel flex flex-col gap-6 animate-fade-in">
      {/* Header: Title + Status + Timer */}
      <div className="flex justify-between items-center">
        <div>
          <h2 style={{ marginBottom: '0.25rem' }}>Charging Session</h2>
          <div className="flex items-center gap-2">
            {!isActive ? (
              <>
                <div style={{ width: '10px', height: '10px', borderRadius: '50%', background: 'var(--color-text-muted)' }} />
                <span style={{ color: 'var(--color-text-muted)', fontWeight: 500, fontSize: '0.9rem' }}>
                  Completed
                </span>
              </>
            ) : isStale ? (
              <>
                <div style={{ width: '10px', height: '10px', borderRadius: '50%', background: 'var(--color-warning, #f0a020)' }} />
                <span style={{ color: 'var(--color-warning, #f0a020)', fontWeight: 600, fontSize: '0.9rem' }}>
                  Reconnecting…
                </span>
              </>
            ) : (
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
            {formatTime(elapsedSec)}
          </span>
        </div>
      </div>

      {/* Connection-lost banner */}
      {isStale && (
        <div
          className="flex items-center gap-2"
          style={{
            padding: '0.75rem 1rem',
            borderRadius: 'var(--radius-md)',
            background: 'hsla(38, 90%, 50%, 0.12)',
            border: '1px solid hsla(38, 90%, 50%, 0.4)',
            color: 'var(--color-warning, #f0a020)',
            fontSize: '0.9rem',
          }}
        >
          <span style={{ fontSize: '1.1rem' }}>⚠️</span>
          <span>
            Live readings paused — the charger stopped reporting. Values below
            are the last known. Your session is still running and safety limits
            are enforced on the charger; billing uses metered energy.
          </span>
        </div>
      )}

      {/* Gateway alarm banner (e.g. plug switched on/off out-of-band, cutoffs) */}
      {recentAlarm && (
        <div
          className="flex items-center gap-2"
          style={{
            padding: '0.75rem 1rem',
            borderRadius: 'var(--radius-md)',
            background: 'hsla(0, 80%, 50%, 0.12)',
            border: '1px solid hsla(0, 80%, 50%, 0.4)',
            color: 'var(--color-danger)',
            fontSize: '0.9rem',
          }}
        >
          <span style={{ fontSize: '1.1rem' }}>🚨</span>
          <span>{recentAlarm.detail || `Charger alarm: ${recentAlarm.event_type}`}</span>
        </div>
      )}

      {/* Stats Grid */}
      <div className="stat-grid">
        <StatBox label="Charging Speed" value={powerW} unit="W" colorVar="--color-primary" />
        <StatBox label="Energy Added" value={energyKwh} unit="kWh" colorVar="--color-accent" />
        <StatBox label="Current Drawn" value={current} unit="A" colorVar="--color-text-primary" />
        <StatBox label="Session Cost" value={cost} unit="Coins" colorVar="--color-danger" />
      </div>

      {/* Secondary line: voltage + actual relay state */}
      <div
        className="flex items-center gap-4"
        style={{ color: 'var(--color-text-muted)', fontSize: '0.85rem', flexWrap: 'wrap' }}
      >
        {voltage && <span>Voltage: <strong style={{ color: 'var(--color-text-secondary)' }}>{voltage} V</strong></span>}
        {relayOn !== undefined && (
          <span>
            Relay:{' '}
            <strong style={{ color: relayOn ? 'var(--color-success)' : 'var(--color-text-muted)' }}>
              {relayOn ? 'ON' : 'OFF'}
            </strong>
          </span>
        )}
        <span style={{ color: 'var(--color-text-muted)' }}>Rate: {coins_per_kwh} coins / kWh</span>
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
