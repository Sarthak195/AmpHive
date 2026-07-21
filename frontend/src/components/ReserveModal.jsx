/**
 * ReserveModal — book a future time slot on a charger.
 * ====================================================
 * Shared prop contract (redesign-v3):
 *   <ReserveModal open onClose plug onReserved />
 *
 * Fetches the plug's upcoming BOOKED windows (GET /api/plugs/{id}/reservations)
 * so group members book around each other. A fetch failure shows an ErrorState
 * INSIDE the modal (never a clean-looking empty schedule). The chosen window
 * is checked client-side against those windows for overlap before submitting;
 * duration comes from preset chips (30m / 1h / 2h / 4h) or a custom end time.
 * POSTs {plug_id, start_at, end_at}; server rejections surface inline.
 * Reservations are free — no money is held.
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import Modal from './ui/Modal';
import './ReserveModal.css';
import ErrorState from './ui/ErrorState';
import Skeleton from './ui/Skeleton';
import { useToast } from './ui/Toast';
import api from '../api/client';
import { apiErrorCopy } from '../utils/statusCopy';
import { fmtWindow } from '../utils/reservationTime';

const DURATIONS = [
  { id: '30', label: '30m', minutes: 30 },
  { id: '60', label: '1h', minutes: 60 },
  { id: '120', label: '2h', minutes: 120 },
  { id: '240', label: '4h', minutes: 240 },
  { id: 'custom', label: 'Custom end', minutes: null },
];

const pad = (n) => String(n).padStart(2, '0');

// Local-time defaults: the next 30-minute boundary, with a small lead so
// "now" hasn't slipped into the past by submit time.
const defaultStart = () => {
  const d = new Date(Date.now() + 5 * 60 * 1000);
  d.setSeconds(0, 0);
  const rem = d.getMinutes() % 30;
  if (rem !== 0) d.setMinutes(d.getMinutes() + (30 - rem));
  return d;
};
const toDateValue = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const toTimeValue = (d) => `${pad(d.getHours())}:${pad(d.getMinutes())}`;

export default function ReserveModal({ open, onClose, plug, onReserved }) {
  const toast = useToast();

  const [date, setDate] = useState('');
  const [time, setTime] = useState('');
  const [duration, setDuration] = useState('60');
  const [customEnd, setCustomEnd] = useState('');
  const [windows, setWindows] = useState(null); // null = loading
  const [windowsError, setWindowsError] = useState(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const fetchWindows = useCallback(async () => {
    setWindowsError(null);
    setWindows(null);
    try {
      const data = await api.get(`/api/plugs/${plug.id}/reservations`);
      setWindows(Array.isArray(data) ? data : []);
    } catch (err) {
      setWindowsError(err);
      setWindows(null);
    }
  }, [plug]);

  // Fresh form + schedule every time the modal opens.
  useEffect(() => {
    if (!open || !plug) return;
    const start = defaultStart();
    setDate(toDateValue(start));
    setTime(toTimeValue(start));
    setDuration('60');
    setCustomEnd('');
    setError('');
    setBusy(false);
    fetchWindows();
  }, [open, plug, fetchWindows]);

  // The chosen [start, end) window, or null while the inputs don't parse.
  const chosen = useMemo(() => {
    if (!date || !time) return null;
    const start = new Date(`${date}T${time}`);
    if (Number.isNaN(start.getTime())) return null;
    if (duration === 'custom') {
      if (!customEnd) return null;
      const end = new Date(`${date}T${customEnd}`);
      if (Number.isNaN(end.getTime()) || end <= start) return null;
      return { start, end };
    }
    const minutes = DURATIONS.find((d) => d.id === duration)?.minutes;
    if (!minutes) return null;
    return { start, end: new Date(start.getTime() + minutes * 60 * 1000) };
  }, [date, time, duration, customEnd]);

  // Client-side overlap check against the fetched windows.
  const overlap = useMemo(() => {
    if (!chosen || !Array.isArray(windows)) return null;
    return (
      windows.find(
        (w) => chosen.start < new Date(w.end_at) && chosen.end > new Date(w.start_at)
      ) || null
    );
  }, [chosen, windows]);

  if (!open || !plug) return null;

  const customEndInvalid =
    duration === 'custom' && customEnd !== '' && chosen == null && date && time;

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError('');
    if (!chosen) {
      setError(
        duration === 'custom'
          ? 'Pick a valid date, start time and an end time after the start.'
          : 'Pick a valid date and start time.'
      );
      return;
    }
    if (overlap) {
      setError(
        `That time overlaps an existing reservation (${fmtWindow(overlap.start_at, overlap.end_at)}) — pick another slot.`
      );
      return;
    }
    setBusy(true);
    try {
      await api.post('/api/reservations', {
        plug_id: plug.id,
        start_at: chosen.start.toISOString(),
        end_at: chosen.end.toISOString(),
      });
      toast.ok(`Reserved — ${plug.name || `Charger ${plug.id}`}, ${fmtWindow(chosen.start, chosen.end)}`);
      onReserved?.();
    } catch (err) {
      // 409s (slot taken / cap reached) and 400s (window rules) land here.
      setError(apiErrorCopy(err));
      setBusy(false);
    }
  };

  return (
    <Modal open={open} onClose={busy ? undefined : onClose} title={`Reserve ${plug.name || `charger ${plug.id}`}`}>
      <div className="stack">
        <p className="text-2 text-sm">
          Book a time slot — during it, only you can start a charge on this charger. Reservations
          are free.
        </p>

        <div>
          <h3 className="reserve-schedule-title">Upcoming reservations</h3>
          {windowsError ? (
            <ErrorState
              error={windowsError}
              onRetry={fetchWindows}
              title="Couldn't load the schedule"
            />
          ) : windows === null ? (
            <Skeleton lines={2} />
          ) : windows.length === 0 ? (
            <p className="text-3 text-sm">No upcoming reservations — the schedule is clear.</p>
          ) : (
            <ul className="reserve-window-list text-sm text-2">
              {windows.map((w) => (
                <li key={w.id}>
                  {fmtWindow(w.start_at, w.end_at)}
                  {w.is_mine ? ' (yours)' : w.user_name ? ` — ${w.user_name}` : ''}
                </li>
              ))}
            </ul>
          )}
        </div>

        <form onSubmit={handleSubmit} className="stack">
          <div className="reserve-when">
            <div className="field">
              <label className="field-label" htmlFor="reserve-date">
                Date
              </label>
              <input
                id="reserve-date"
                type="date"
                className="input"
                value={date}
                onChange={(e) => setDate(e.target.value)}
              />
            </div>
            <div className="field">
              <label className="field-label" htmlFor="reserve-time">
                Start time
              </label>
              <input
                id="reserve-time"
                type="time"
                className="input"
                value={time}
                onChange={(e) => setTime(e.target.value)}
              />
            </div>
          </div>

          <div className="field">
            <span className="field-label" id="reserve-duration-label">
              Duration
            </span>
            <div className="reserve-chips" role="group" aria-labelledby="reserve-duration-label">
              {DURATIONS.map((d) => (
                <button
                  key={d.id}
                  type="button"
                  className={`btn btn-sm ${duration === d.id ? 'btn-primary' : 'btn-quiet'}`}
                  aria-pressed={duration === d.id}
                  onClick={() => setDuration(d.id)}
                >
                  {d.label}
                </button>
              ))}
            </div>
            {duration === 'custom' && (
              <>
                <label className="field-label" htmlFor="reserve-end">
                  End time
                </label>
                <input
                  id="reserve-end"
                  type="time"
                  className="input"
                  value={customEnd}
                  onChange={(e) => setCustomEnd(e.target.value)}
                  aria-invalid={Boolean(customEndInvalid)}
                />
                {customEndInvalid && (
                  <p className="field-error">End time must be after the start time.</p>
                )}
              </>
            )}
          </div>

          {overlap && (
            <p className="field-error" role="alert">
              That time overlaps an existing reservation (
              {fmtWindow(overlap.start_at, overlap.end_at)}) — pick another slot.
            </p>
          )}
          {error && (
            <p className="field-error" role="alert">
              {error}
            </p>
          )}

          <div className="modal-actions">
            <button type="button" className="btn btn-quiet" onClick={onClose} disabled={busy}>
              Cancel
            </button>
            <button
              type="submit"
              className="btn btn-primary"
              disabled={busy || Boolean(overlap)}
            >
              {busy ? 'Reserving…' : 'Reserve'}
            </button>
          </div>
        </form>
      </div>
    </Modal>
  );
}
