/**
 * ChargeSetupModal
 * ================
 * Opens when a driver taps a charger (or hits "Set up" on a typed Plug ID) —
 * charging no longer begins instantly. The driver OPTIONALLY sets a timer
 * and/or a kWh target (leaving both blank = just start, no limit), then
 * confirms. The two fields map to the backend's `max_duration_seconds` /
 * `max_kwh`; both stay editable mid-charge via the session monitor
 * (PATCH /api/sessions/{id}/limits).
 *
 * Layout-only reuse: this is built entirely from the existing modal / input /
 * button classes in styles/global.css — no new colours.
 */
import { useState } from 'react';
import api from '../api/client';

const ChargeSetupModal = ({ plug, rate, balance, onStart, onClose }) => {
  const [hours, setHours] = useState('');
  const [kwh, setKwh] = useState('');
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState('');
  // [Caps] Set when the start was refused because the circuit is full — offers
  // the driver a "Request capacity" action instead of a dead end.
  const [circuitFull, setCircuitFull] = useState(false);
  const [capacityRequested, setCapacityRequested] = useState(false);

  if (!plug) return null;

  const r = Number(rate) || 5;
  const bal = Number(balance) || 0;
  const covers = r > 0 ? bal / r : 0;

  const handleStart = async () => {
    // Only send the fields the driver actually set (> 0). Neither → no limit.
    const limits = {};
    const h = parseFloat(hours);
    const k = parseFloat(kwh);
    if (!Number.isNaN(k) && k > 0) limits.max_kwh = k;
    if (!Number.isNaN(h) && h > 0) limits.max_duration_seconds = Math.round(h * 3600);

    setStarting(true);
    setError('');
    setCircuitFull(false);
    try {
      await onStart(plug.id, Object.keys(limits).length ? limits : null);
      // On success the caller navigates away (this modal unmounts).
    } catch (err) {
      setError(err?.message || 'Could not start charging.');
      // [Caps] The backend tags a full-circuit block with code "circuit_full"
      // (services/caps.py) — surface the "Request capacity" action for it.
      setCircuitFull(err?.code === 'circuit_full');
      setStarting(false);
    }
  };

  const handleRequestCapacity = async () => {
    setError('');
    try {
      await api.post(`/api/plugs/${plug.id}/request-capacity`);
      setCapacityRequested(true);
      setCircuitFull(false);
    } catch (err) {
      setError(err?.message || 'Could not send the request.');
    }
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-content" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h2>Set up your charge</h2>
          <button className="modal-close" onClick={onClose} aria-label="Close">✕</button>
        </div>

        <div style={{ marginBottom: '1rem' }}>
          <div style={{ fontWeight: 600 }}>{plug.name || `Plug ${plug.id}`}</div>
          <div className="mono" style={{ fontSize: '0.82rem', color: 'var(--color-text-muted)', marginTop: '0.2rem' }}>
            {plug.price_per_kwh != null ? plug.price_per_kwh : r} coins/kWh · balance covers ≈ {covers.toFixed(1)} kWh
          </div>
        </div>

        <p style={{ fontSize: '0.85rem', color: 'var(--color-text-secondary)', marginBottom: '1rem' }}>
          Set a stop condition, or leave both blank to just start — you can change either one anytime while charging.
        </p>

        <div className="flex gap-3 flex-wrap" style={{ marginBottom: '0.25rem' }}>
          <div className="input-group" style={{ flex: 1, minWidth: '130px' }}>
            <label htmlFor="setup-timer">Timer — hours (optional)</label>
            <input
              id="setup-timer" className="input" type="number" min="0" step="0.25"
              value={hours} onChange={(e) => setHours(e.target.value)} placeholder="e.g. 2"
            />
          </div>
          <div className="input-group" style={{ flex: 1, minWidth: '130px' }}>
            <label htmlFor="setup-kwh">kWh to dispense (optional)</label>
            <input
              id="setup-kwh" className="input" type="number" min="0" step="0.1"
              value={kwh} onChange={(e) => setKwh(e.target.value)} placeholder="e.g. 5"
            />
          </div>
        </div>

        {error && <div className="error-text mt-2">{error}</div>}

        {/* [Caps] Full-circuit block → let the driver queue a request rather
            than hit a dead end. On success they get a notification when the
            circuit frees (a session ends or the operator raises the cap). */}
        {circuitFull && !capacityRequested && (
          <button className="btn btn-ghost mt-2" onClick={handleRequestCapacity}>
            Request capacity
          </button>
        )}
        {capacityRequested && (
          <div className="mt-2" style={{ fontSize: '0.85rem', color: 'var(--color-text-secondary)' }}>
            Requested — you'll get a notification when this circuit has room. You can close this and come back.
          </div>
        )}

        <div className="modal-actions">
          <button className="btn btn-ghost" onClick={onClose} disabled={starting}>Cancel</button>
          <button className="btn btn-primary" onClick={handleStart} disabled={starting}>
            {starting ? 'Starting…' : 'Start charging'}
          </button>
        </div>
      </div>
    </div>
  );
};

export default ChargeSetupModal;
