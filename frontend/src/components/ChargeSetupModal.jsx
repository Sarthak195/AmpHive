/**
 * ChargeSetupModal — set optional limits, then start (or queue) a charge.
 * =======================================================================
 * Shared prop contract (redesign-v3):
 *   <ChargeSetupModal open onClose plug mode="start"|"queue" onConfirm />
 *
 * - mode="start" (default): "Set up your charge" / "Start charging".
 * - mode="queue": "Queue this charge" / "Join the queue" — the charge starts
 *   automatically when the circuit has room.
 *
 * Time limit is preset chips (No limit / 30m / 1h / 2h / 4h / 8h / Custom
 * hh:mm) — no fractional-hours input. Energy limit is a plain kWh field.
 * The coverage line uses the plug's OWN resolved price. `onConfirm(plugId,
 * limits)` may throw: the error surfaces inline via apiErrorCopy, and a
 * `circuit_full` rejection additionally offers "Ask for capacity"
 * (POST /api/plugs/{id}/request-capacity) instead of a dead end.
 */

import { useEffect, useState } from 'react';
import Modal from './ui/Modal';
import './ChargeSetupModal.css';
import Money from './ui/Money';
import api from '../api/client';
import { useConfig } from '../contexts/ConfigContext';
import { useWallet } from '../contexts/WalletContext';
import { apiErrorCopy } from '../utils/statusCopy';

const TIME_PRESETS = [
  { id: 'none', label: 'No limit', minutes: null },
  { id: '30m', label: '30m', minutes: 30 },
  { id: '1h', label: '1h', minutes: 60 },
  { id: '2h', label: '2h', minutes: 120 },
  { id: '4h', label: '4h', minutes: 240 },
  { id: '8h', label: '8h', minutes: 480 },
  { id: 'custom', label: 'Custom', minutes: null },
];

const CUSTOM_RE = /^(\d{1,2}):([0-5]\d)$/;

// "h:mm" → minutes, or null when it doesn't parse / is zero.
const parseCustomMinutes = (value) => {
  const m = CUSTOM_RE.exec(value.trim());
  if (!m) return null;
  const minutes = Number(m[1]) * 60 + Number(m[2]);
  return minutes > 0 ? minutes : null;
};

const COPY = {
  start: {
    title: 'Set up your charge',
    confirm: 'Start charging',
    busy: 'Starting…',
    intro: 'Set a stop limit if you want one — you can change it anytime while charging.',
  },
  queue: {
    title: 'Queue this charge',
    confirm: 'Join the queue',
    busy: 'Joining…',
    intro: 'Your charge starts automatically when the circuit has room.',
  },
};

export default function ChargeSetupModal({ open, onClose, plug, mode = 'start', onConfirm }) {
  const { coins_per_kwh, coin_inr_rate } = useConfig();
  const { balance } = useWallet();

  const [timePreset, setTimePreset] = useState('none');
  const [customTime, setCustomTime] = useState('');
  const [kwh, setKwh] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [circuitFull, setCircuitFull] = useState(false);
  const [capacityAsked, setCapacityAsked] = useState(false);
  const [asking, setAsking] = useState(false);

  // Fresh form every time the modal opens (or for a different plug).
  useEffect(() => {
    if (!open) return;
    setTimePreset('none');
    setCustomTime('');
    setKwh('');
    setBusy(false);
    setError('');
    setCircuitFull(false);
    setCapacityAsked(false);
    setAsking(false);
  }, [open, plug?.id]);

  if (!open || !plug) return null;

  const copy = COPY[mode] || COPY.start;

  // Coverage line: the plug's own price (falling back to the global rate).
  const rate = Number(plug.price_per_kwh) > 0 ? Number(plug.price_per_kwh) : coins_per_kwh || 5;
  const covers = rate > 0 ? (Number(balance) || 0) / rate : 0;

  const customInvalid = timePreset === 'custom' && parseCustomMinutes(customTime) == null;

  const handleConfirm = async () => {
    const limits = {};
    const k = parseFloat(kwh);
    if (!Number.isNaN(k) && k > 0) limits.max_kwh = k;
    const minutes =
      timePreset === 'custom'
        ? parseCustomMinutes(customTime)
        : TIME_PRESETS.find((p) => p.id === timePreset)?.minutes;
    if (minutes) limits.max_duration_seconds = minutes * 60;

    setBusy(true);
    setError('');
    setCircuitFull(false);
    try {
      await onConfirm(plug.id, Object.keys(limits).length ? limits : null);
      onClose?.();
    } catch (err) {
      setError(apiErrorCopy(err));
      setCircuitFull(err?.code === 'circuit_full');
      setBusy(false);
    }
  };

  const handleAskCapacity = async () => {
    setAsking(true);
    setError('');
    try {
      await api.post(`/api/plugs/${plug.id}/request-capacity`);
      setCapacityAsked(true);
      setCircuitFull(false);
    } catch (err) {
      setError(apiErrorCopy(err));
    } finally {
      setAsking(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={busy ? undefined : onClose}
      title={copy.title}
      footer={
        <>
          <button type="button" className="btn btn-quiet" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button
            type="button"
            className="btn btn-primary"
            onClick={handleConfirm}
            disabled={busy || customInvalid}
          >
            {busy ? copy.busy : copy.confirm}
          </button>
        </>
      }
    >
      <div className="stack">
        <div>
          <p className="charge-setup-plug">{plug.name || `Charger ${plug.id}`}</p>
          <p className="text-3 text-sm num">
            <Money coins={rate} rate={coin_inr_rate} />
            /kWh · your balance covers ≈ {covers.toFixed(1)} kWh
          </p>
        </div>

        <p className="text-2 text-sm">{copy.intro}</p>

        <div className="field">
          <span className="field-label" id="charge-time-label">
            Time limit
          </span>
          <div className="charge-setup-chips" role="group" aria-labelledby="charge-time-label">
            {TIME_PRESETS.map((p) => (
              <button
                key={p.id}
                type="button"
                className={`btn btn-sm ${timePreset === p.id ? 'btn-primary' : 'btn-quiet'}`}
                aria-pressed={timePreset === p.id}
                onClick={() => setTimePreset(p.id)}
              >
                {p.label}
              </button>
            ))}
          </div>
          {timePreset === 'custom' && (
            <>
              <input
                className="input"
                value={customTime}
                onChange={(e) => setCustomTime(e.target.value)}
                placeholder="h:mm — e.g. 1:30"
                aria-label="Custom time limit (hours:minutes)"
                aria-invalid={customTime !== '' && customInvalid}
                inputMode="numeric"
              />
              {customTime !== '' && customInvalid && (
                <p className="field-error">Enter a time as hours:minutes, like 1:30.</p>
              )}
            </>
          )}
        </div>

        <div className="field">
          <label className="field-label" htmlFor="charge-setup-kwh">
            Energy limit (kWh)
          </label>
          <input
            id="charge-setup-kwh"
            className="input"
            type="number"
            min="0"
            step="0.1"
            value={kwh}
            onChange={(e) => setKwh(e.target.value)}
            placeholder="No limit"
          />
        </div>

        {error && (
          <p className="field-error" role="alert">
            {error}
          </p>
        )}

        {circuitFull && !capacityAsked && (
          <button
            type="button"
            className="btn btn-quiet"
            onClick={handleAskCapacity}
            disabled={asking}
          >
            {asking ? 'Asking…' : 'Ask for capacity'}
          </button>
        )}
        {capacityAsked && (
          <p className="text-2 text-sm" role="status">
            Asked — you'll get a notification when this circuit has room. You can close this and
            come back.
          </p>
        )}
      </div>
    </Modal>
  );
}
