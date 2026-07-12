/**
 * AmpHive Charge Limit Control
 * ============================
 * Optional, collapsed-by-default "Set a charging limit" control for the Home
 * start flow ("only charge 1 kWh"). The driver can cap the session by:
 *   - energy (kWh)             → sent as `max_kwh`
 *   - time (hours)             → sent as `max_duration_seconds`
 *   - money (₹ / coins)        → converted client-side to kWh at the plug's
 *                                 own `price_per_kwh` (falling back to the
 *                                 global `coins_per_kwh` config) and sent as
 *                                 `max_kwh` — the backend bills energy, so a
 *                                 coin cap IS an energy cap at that rate.
 *
 * The control emits a raw SPEC ({ mode, value } | null) via onChange rather
 * than computed limits: the coin→kWh conversion must use the rate of the
 * plug actually being started (a card click may target a different plug than
 * the typed id), so the parent converts at send time with
 * computeChargeLimits(spec, rateForThatPlug). No limit chosen → null → the
 * parent sends nothing and the backend defaults (30 kWh / 4 h safety caps)
 * apply, exactly as before this control existed.
 */

import { useEffect, useState } from 'react';
import { computeChargeLimits, formatDuration } from '../utils/chargeLimits';

const MODES = [
  { id: 'kwh', label: 'kWh' },
  { id: 'time', label: 'Time' },
  { id: 'coins', label: '₹ / coins' },
];

const PRESETS = {
  kwh: [
    { label: '1 kWh', value: '1' },
    { label: '2 kWh', value: '2' },
    { label: '5 kWh', value: '5' },
    { label: '10 kWh', value: '10' },
  ],
  time: [
    { label: '30 min', value: '0.5' },
    { label: '1 h', value: '1' },
    { label: '2 h', value: '2' },
    { label: '4 h', value: '4' },
  ],
  coins: [
    { label: '₹10', value: '10' },
    { label: '₹25', value: '25' },
    { label: '₹50', value: '50' },
    { label: '₹100', value: '100' },
  ],
};

const UNIT_LABEL = { kwh: 'kWh', time: 'hours', coins: '₹ (coins)' };

const ChargeLimitControl = ({ rate, onChange }) => {
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState('kwh');
  const [value, setValue] = useState('');

  // Push the current spec up whenever it changes; closing the control means
  // "no limit" (null), same as never opening it.
  useEffect(() => {
    onChange(open ? { mode, value } : null);
  }, [open, mode, value, onChange]);

  const limits = computeChargeLimits(open ? { mode, value } : null, rate);

  let preview = null;
  if (open && value.trim() !== '' && !limits) {
    preview = 'Enter a positive number, or leave blank for no limit.';
  } else if (limits?.max_kwh != null) {
    preview =
      mode === 'coins'
        ? `≈ ${limits.max_kwh.toFixed(2)} kWh at ${Number(rate) > 0 ? Number(rate) : 5} coins/kWh — stops automatically.`
        : `Stops automatically at ${limits.max_kwh.toFixed(2)} kWh.`;
  } else if (limits?.max_duration_seconds != null) {
    preview = `Stops automatically after ${formatDuration(limits.max_duration_seconds)}.`;
  }

  return (
    <div style={{ marginTop: '0.9rem' }}>
      <button
        type="button"
        className="btn btn-ghost btn-sm"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        style={{ paddingLeft: 0 }}
      >
        <span aria-hidden="true">🎯</span> Set a charging limit (optional)
        <span aria-hidden="true"> {open ? '▴' : '▾'}</span>
      </button>

      {open && (
        <div
          className="glass"
          style={{
            marginTop: '0.5rem',
            padding: '0.85rem',
            borderRadius: 'var(--radius-md)',
            display: 'flex',
            flexDirection: 'column',
            gap: '0.6rem',
          }}
        >
          {/* Mode switch: cap by energy, time, or money */}
          <div className="flex gap-2" role="group" aria-label="Limit type" style={{ flexWrap: 'wrap' }}>
            {MODES.map((m) => (
              <button
                key={m.id}
                type="button"
                className={`btn btn-sm ${mode === m.id ? 'btn-primary' : 'btn-ghost'}`}
                aria-pressed={mode === m.id}
                onClick={() => {
                  setMode(m.id);
                  setValue(''); // units changed — a stale number would mislead
                }}
              >
                {m.label}
              </button>
            ))}
          </div>

          {/* Quick presets */}
          <div className="flex gap-2" style={{ flexWrap: 'wrap' }}>
            {PRESETS[mode].map((p) => (
              <button
                key={p.label}
                type="button"
                className={`btn btn-sm ${value === p.value ? 'btn-accent' : 'btn-ghost'}`}
                aria-pressed={value === p.value}
                onClick={() => setValue(p.value)}
              >
                {p.label}
              </button>
            ))}
          </div>

          {/* Custom value */}
          <div className="flex items-center gap-2">
            <input
              type="number"
              className="input"
              inputMode="decimal"
              min="0"
              step="any"
              aria-label="Limit value"
              placeholder="Custom"
              value={value}
              onChange={(e) => setValue(e.target.value)}
              style={{ maxWidth: '10rem' }}
            />
            <span style={{ fontSize: '0.85rem', color: 'var(--color-text-secondary)' }}>
              {UNIT_LABEL[mode]}
            </span>
          </div>

          <div style={{ fontSize: '0.82rem', color: 'var(--color-text-muted)' }}>
            {preview || 'No limit — charging stops at the standard safety caps.'}
          </div>
        </div>
      )}
    </div>
  );
};

export default ChargeLimitControl;
