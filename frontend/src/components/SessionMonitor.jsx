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
import { useWallet } from '../contexts/WalletContext';

// Consider the live feed stale after this many ms without a telemetry frame.
// Matches the backend TELEMETRY_STALE_AFTER_SEC default (15 s).
const STALE_AFTER_MS = 15000;

const StatBox = ({ label, value, unit, colorVar = '--color-text-primary' }) => (
  <div
    className="glass flex flex-col justify-center"
    style={{
      padding: '1.25rem',
      background: 'hsla(74, 14%, 14%, 0.6)',
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
      <span style={{ fontSize: '1.75rem', fontWeight: 700, color: `var(${colorVar})`, fontFamily: 'var(--font-mono)', fontVariantNumeric: 'tabular-nums', letterSpacing: '-0.02em' }}>
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
    sessionData, sessionId, isActive, stopSession, updateLimits,
    lastFrameAt, focusedStartedAt, focusedLimits, alarms,
  } = useSession();
  const { coins_per_kwh } = useConfig();
  const { balance } = useWallet();

  // A 1 Hz clock so the elapsed timer and the staleness check update smoothly
  // between telemetry frames.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  // Inline editor for the focused session's stop conditions — "start now, set
  // the target later" (PATCH /api/sessions/{id}/limits via updateLimits).
  const [editingLimit, setEditingLimit] = useState(false);
  const [editKwh, setEditKwh] = useState('');
  const [editHours, setEditHours] = useState('');
  const [savingLimit, setSavingLimit] = useState(false);
  const [limitError, setLimitError] = useState('');

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

  // Low-balance warning: the wallet balance was captured at start (it's only
  // debited on stop), so remaining ≈ balance − accrued cost. Warn as it nears
  // zero; the backend auto-stops the session when the wallet is exhausted.
  const walletBalance = Number(balance) || 0;
  const accruedCost = Number(sessionData?.cost_coins) || 0;
  const remainingCoins = walletBalance - accruedCost;
  const rate = coins_per_kwh || 5;
  const lowBalance = isActive && accruedCost > 0 &&
    remainingCoins <= Math.max(10, walletBalance * 0.15);

  // The most recent unacknowledged alarm for this plug (last 2 minutes).
  const plugId = sessionData?.plug_id;
  const recentAlarm = (alarms || []).find(
    (a) => a.plug_id === plugId && (now - (a.received_at || 0)) < 120000
  );

  // Charging-limit progress: this session's stop conditions (max_kwh /
  // max_duration_seconds — user-chosen or the standard caps), which the
  // backend auto-stops at. Presented like the low-balance warning above,
  // but informational — it's an expected, driver-chosen outcome.
  const limitMaxKwh = focusedLimits?.max_kwh;
  const limitMaxDurationSec = focusedLimits?.max_duration_seconds;
  const energyNum = Number(sessionData?.energy_kwh) || 0;
  const hasLimit = isActive && (limitMaxKwh != null || limitMaxDurationSec != null);

  // Open the inline editor prefilled with the current limits (kWh + hours).
  const openLimitEditor = () => {
    setEditKwh(limitMaxKwh != null ? String(limitMaxKwh) : '');
    setEditHours(
      limitMaxDurationSec != null
        ? String(Number((limitMaxDurationSec / 3600).toFixed(2)))
        : ''
    );
    setLimitError('');
    setEditingLimit(true);
  };

  // Send only the fields the driver actually set (>0). At least one required.
  const saveLimitEdit = async () => {
    const limits = {};
    const kwh = parseFloat(editKwh);
    const hours = parseFloat(editHours);
    if (!Number.isNaN(kwh) && kwh > 0) limits.max_kwh = kwh;
    if (!Number.isNaN(hours) && hours > 0) limits.max_duration_seconds = Math.round(hours * 3600);
    if (limits.max_kwh == null && limits.max_duration_seconds == null) {
      setLimitError('Enter an energy (kWh) or time (hours) limit.');
      return;
    }
    setSavingLimit(true);
    setLimitError('');
    try {
      await updateLimits(sessionId, limits);
      setEditingLimit(false);
    } catch (err) {
      setLimitError(err?.message || 'Could not update the limit.');
    } finally {
      setSavingLimit(false);
    }
  };

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
            fontFamily: 'var(--font-mono)',
            fontVariantNumeric: 'tabular-nums',
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

      {/* Low-balance warning */}
      {lowBalance && (
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
          <span style={{ fontSize: '1.1rem' }}>🔋</span>
          <span>
            Low balance — about <strong>{Math.max(0, remainingCoins).toFixed(1)}</strong> coins
            (≈ {Math.max(0, remainingCoins / rate).toFixed(2)} kWh) left. Charging will stop
            automatically when your wallet is used up.
          </span>
        </div>
      )}

      {/* Charging-limit progress ("0.42 / 1.00 kWh · stops automatically"),
          with an inline Edit to change the target mid-session. */}
      {hasLimit && (
        <div
          className="flex items-center gap-2"
          style={{
            padding: '0.75rem 1rem',
            borderRadius: 'var(--radius-md)',
            background: 'hsla(73, 100%, 50%, 0.10)',
            border: '1px solid hsla(73, 100%, 50%, 0.35)',
            color: 'var(--color-text-secondary)',
            fontSize: '0.9rem',
          }}
        >
          <span style={{ fontSize: '1.1rem' }}>🎯</span>
          <span style={{ flex: 1 }}>
            Limit:{' '}
            {limitMaxKwh != null && (
              <strong style={{ color: 'var(--color-text-primary)' }}>
                {energyNum.toFixed(2)} / {Number(limitMaxKwh).toFixed(2)} kWh
              </strong>
            )}
            {limitMaxKwh != null && limitMaxDurationSec != null && ' · '}
            {limitMaxDurationSec != null && (
              <strong style={{ color: 'var(--color-text-primary)' }}>
                {formatTime(elapsedSec)} / {formatTime(limitMaxDurationSec)}
              </strong>
            )}
            {' · stops automatically'}
          </span>
          {isActive && updateLimits && !editingLimit && (
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={openLimitEditor}
              style={{ flexShrink: 0, padding: '0.3rem 0.75rem', fontSize: '0.78rem' }}
            >
              Edit
            </button>
          )}
        </div>
      )}

      {/* Inline limit editor — change the target after the session started. */}
      {editingLimit && (
        <div
          className="glass flex flex-col gap-3"
          style={{
            padding: '1rem',
            borderRadius: 'var(--radius-md)',
            background: 'hsla(73, 100%, 50%, 0.06)',
            border: '1px solid hsla(73, 100%, 50%, 0.3)',
          }}
        >
          <div style={{ fontWeight: 600, fontSize: '0.9rem', color: 'var(--color-text-primary)' }}>
            Change charging limit
          </div>
          <div className="flex gap-3 flex-wrap">
            <div className="input-group" style={{ flex: 1, minWidth: '130px' }}>
              <label htmlFor="edit-kwh">Energy limit (kWh)</label>
              <input
                id="edit-kwh"
                className="input"
                type="number"
                min="0"
                step="0.1"
                value={editKwh}
                onChange={(e) => setEditKwh(e.target.value)}
                placeholder="e.g. 5"
              />
            </div>
            <div className="input-group" style={{ flex: 1, minWidth: '130px' }}>
              <label htmlFor="edit-hours">Time limit (hours)</label>
              <input
                id="edit-hours"
                className="input"
                type="number"
                min="0"
                step="0.25"
                value={editHours}
                onChange={(e) => setEditHours(e.target.value)}
                placeholder="e.g. 2"
              />
            </div>
          </div>
          <p style={{ fontSize: '0.78rem', color: 'var(--color-text-muted)', margin: 0 }}>
            Takes effect within a few seconds. Raising a limit above what the charger
            was set at start may be capped by the charger until it updates.
          </p>
          {limitError && <div className="error-text">{limitError}</div>}
          <div className="flex gap-2" style={{ justifyContent: 'flex-end' }}>
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={() => setEditingLimit(false)}
              disabled={savingLimit}
            >
              Cancel
            </button>
            <button
              type="button"
              className="btn btn-primary btn-sm"
              onClick={saveLimitEdit}
              disabled={savingLimit}
            >
              {savingLimit ? 'Saving…' : 'Save limit'}
            </button>
          </div>
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
