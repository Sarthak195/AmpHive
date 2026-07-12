/**
 * ReserveModal — book a future time slot on a plug (feat/reservations).
 *
 * Shows the plug's upcoming BOOKED windows (GET /api/plugs/{id}/reservations)
 * so group members book around each other — the private society/office use
 * case — then POSTs {plug_id, start_at, end_at} as ISO strings built from a
 * local date + start time + a duration preset (30 min / 1 h / 2 h / 4 h).
 * Server-side policy rejections (409 slot taken / reservation cap, 400
 * window rules) surface inline via the api client's error message.
 * Reservations are free — no coins are held.
 */
import { useState, useEffect } from 'react';
import api from '../api/client';
import { fmtWindow } from '../utils/reservationTime';

const DURATIONS = [
  { label: '30 min', minutes: 30 },
  { label: '1 h', minutes: 60 },
  { label: '2 h', minutes: 120 },
  { label: '4 h', minutes: 240 },
];

const pad = (n) => String(n).padStart(2, '0');

// Local-time defaults for the inputs: today + the next 30-minute boundary
// (with a small lead so "now" hasn't slipped into the past by submit time).
const defaultStart = () => {
  const d = new Date(Date.now() + 5 * 60 * 1000);
  d.setSeconds(0, 0);
  const rem = d.getMinutes() % 30;
  if (rem !== 0) d.setMinutes(d.getMinutes() + (30 - rem));
  return d;
};
const toDateValue = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const toTimeValue = (d) => `${pad(d.getHours())}:${pad(d.getMinutes())}`;

const ReserveModal = ({ plug, onClose, onBooked }) => {
  const start = defaultStart();
  const [date, setDate] = useState(toDateValue(start));
  const [time, setTime] = useState(toTimeValue(start));
  const [duration, setDuration] = useState(60);
  const [windows, setWindows] = useState([]);
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);

  // The plug's upcoming schedule, so the driver books around it.
  useEffect(() => {
    let cancelled = false;
    api
      .get(`/api/plugs/${plug.id}/reservations`)
      .then((data) => {
        if (!cancelled) setWindows(Array.isArray(data) ? data : []);
      })
      .catch((err) => console.error('Failed to fetch plug schedule:', err));
    return () => {
      cancelled = true;
    };
  }, [plug.id]);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError('');

    const startAt = new Date(`${date}T${time}`);
    if (Number.isNaN(startAt.getTime())) {
      setError('Pick a valid date and start time.');
      return;
    }
    const endAt = new Date(startAt.getTime() + duration * 60 * 1000);

    setSubmitting(true);
    try {
      await api.post('/api/reservations', {
        plug_id: plug.id,
        start_at: startAt.toISOString(),
        end_at: endAt.toISOString(),
      });
      onBooked?.();
    } catch (err) {
      // 409s (slot taken / cap reached) and 400s (window rules) land here
      // with the backend's specific message.
      setError(err.message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-content" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h2>Reserve {plug.name}</h2>
          <button className="modal-close" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </div>

        <p style={{ fontSize: '0.85rem', color: 'var(--color-text-secondary)', marginBottom: '1rem' }}>
          Book a time slot — during it, only you can start a session on this
          plug. Reservations are free.
        </p>

        {/* Upcoming windows on this plug, so people book around them. */}
        <div style={{ marginBottom: '1.25rem' }}>
          <h4 style={{ fontSize: '0.9rem', marginBottom: '0.5rem' }}>Upcoming reservations</h4>
          {windows.length === 0 ? (
            <p style={{ fontSize: '0.85rem', color: 'var(--color-text-muted)' }}>
              No upcoming reservations — the schedule is clear.
            </p>
          ) : (
            <ul style={{ margin: 0, paddingLeft: '1.1rem', fontSize: '0.85rem', color: 'var(--color-text-secondary)' }}>
              {windows.map((w) => (
                <li key={w.id} style={{ marginBottom: '0.25rem' }}>
                  {fmtWindow(w.start_at, w.end_at)}
                  {w.is_mine ? ' (yours)' : w.user_name ? ` — ${w.user_name}` : ''}
                </li>
              ))}
            </ul>
          )}
        </div>

        <form onSubmit={handleSubmit}>
          <div className="flex gap-3" style={{ flexWrap: 'wrap', marginBottom: '1rem' }}>
            <label style={{ flex: 1, minWidth: '140px', fontSize: '0.85rem' }}>
              Date
              <input
                type="date"
                className="input"
                aria-label="Reservation date"
                value={date}
                onChange={(e) => setDate(e.target.value)}
                style={{ width: '100%', marginTop: '0.25rem' }}
              />
            </label>
            <label style={{ flex: 1, minWidth: '120px', fontSize: '0.85rem' }}>
              Start time
              <input
                type="time"
                className="input"
                aria-label="Reservation start time"
                value={time}
                onChange={(e) => setTime(e.target.value)}
                style={{ width: '100%', marginTop: '0.25rem' }}
              />
            </label>
          </div>

          <div style={{ marginBottom: '1rem' }}>
            <div style={{ fontSize: '0.85rem', marginBottom: '0.35rem' }}>Duration</div>
            <div className="flex gap-2" role="group" aria-label="Duration">
              {DURATIONS.map((d) => (
                <button
                  key={d.minutes}
                  type="button"
                  className={`btn btn-sm ${duration === d.minutes ? 'btn-accent' : 'btn-ghost'}`}
                  aria-pressed={duration === d.minutes}
                  onClick={() => setDuration(d.minutes)}
                >
                  {d.label}
                </button>
              ))}
            </div>
          </div>

          {error && <div className="error-text mt-2" style={{ marginBottom: '0.75rem' }}>{error}</div>}

          <div className="modal-actions">
            <button type="button" className="btn btn-ghost" onClick={onClose}>
              Cancel
            </button>
            <button type="submit" className="btn btn-accent" disabled={submitting}>
              {submitting ? '...' : 'Reserve'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
};

export default ReserveModal;
