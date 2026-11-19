/**
 * NotificationPrefs: per-type push toggles and quiet hours (C7).
 *
 *   GET /api/me/notification-prefs -> { push: {type: bool}, quiet_hours: {enabled, start, end}, timezone }
 *   PUT /api/me/notification-prefs   same shape without timezone
 *
 * Only push is affected: every notice still lands in the bell feed. Safety,
 * fault and billing notices, unattended (scheduled, queued or routine) starts,
 * and anything marked warning or critical (an operator cancelling a booking,
 * a reservation that will stop a session) always push, even in quiet hours,
 * so they have no toggle here. A toggle missing from a saved row reads as on. Times are in the
 * zone the server reports (the account's tenant zone; a driver has no
 * tenant, so it is the platform default, Asia/Kolkata). A read-only
 * cpo_viewer cannot save, so the panel is not shown to one.
 */

import { useEffect, useState } from 'react';
import { MoonStar } from 'lucide-react';
import { useToast } from './ui';
import api from '../api/client';
import { useAuth } from '../contexts/AuthContext';
import { apiErrorCopy } from '../utils/statusCopy';

const TOGGLES = [
  ['session_started', 'Charging started'],
  ['nearly_done', 'Almost done (90% of your energy limit)'],
  ['power_dropped', 'Charging slowed down'],
  ['rate_changed', 'My charging rate changes mid-session'],
  ['plug_available', 'A charger I am watching is free'],
  ['queued', 'A charge I queue or schedule is confirmed'],
  ['reservation_booked', 'A reservation is confirmed'],
  ['reservation_started', 'My reservation starts'],
];

export default function NotificationPrefs() {
  const { user } = useAuth();
  const toast = useToast();
  const [prefs, setPrefs] = useState(null);
  const [timezone, setTimezone] = useState('');
  const [busy, setBusy] = useState(false);

  const isViewer = user?.role === 'cpo_viewer';

  useEffect(() => {
    if (isViewer) return undefined;
    let cancelled = false;
    (async () => {
      try {
        const res = await api.get('/api/me/notification-prefs');
        if (cancelled || !res?.push || !res?.quiet_hours) return;
        setPrefs({ push: res.push, quiet_hours: res.quiet_hours });
        setTimezone(res.timezone || '');
      } catch {
        // Leave the panel hidden; the rest of the page still works.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [isViewer]);

  if (isViewer || !prefs) return null;

  const setPush = (type, on) => setPrefs((p) => ({ ...p, push: { ...p.push, [type]: on } }));
  const setQuiet = (patch) => setPrefs((p) => ({ ...p, quiet_hours: { ...p.quiet_hours, ...patch } }));
  const q = prefs.quiet_hours;
  const sameTimes = q.enabled && q.start === q.end;

  const handleSave = async (e) => {
    e.preventDefault();
    if (busy || sameTimes) return;
    setBusy(true);
    try {
      const res = await api.put('/api/me/notification-prefs', prefs);
      if (res?.push && res?.quiet_hours) setPrefs({ push: res.push, quiet_hours: res.quiet_hours });
      toast.ok('Notification settings saved.');
    } catch (err) {
      toast.error(apiErrorCopy(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="card">
      <h2 className="account-card-title">
        <MoonStar size={18} aria-hidden="true" />
        Push preferences
      </h2>
      <form className="stack-sm" onSubmit={handleSave}>
        <p className="text-2 text-sm">
          These only change push alerts. Every notice still appears in your bell, and safety,
          fault and billing alerts, operator changes to your bookings, and scheduled or routine
          starts always push.
        </p>
        <fieldset className="stack-sm notif-prefs-group">
          <legend className="field-label">Push me when</legend>
          {TOGGLES.map(([type, label]) => (
            <label key={type} className="checkbox-row text-sm">
              <input
                type="checkbox"
                checked={prefs.push[type] !== false}
                onChange={(e) => setPush(type, e.target.checked)}
              />
              {label}
            </label>
          ))}
        </fieldset>
        <label className="checkbox-row text-sm">
          <input
            type="checkbox"
            checked={q.enabled}
            onChange={(e) => setQuiet({ enabled: e.target.checked })}
          />
          Quiet hours (no routine pushes)
        </label>
        {q.enabled && (
          <div className="row-between">
            <label className="field">
              <span className="field-label">From</span>
              <input
                className="input"
                type="time"
                step="60"
                value={q.start}
                onChange={(e) => setQuiet({ start: e.target.value })}
              />
            </label>
            <label className="field">
              <span className="field-label">Until</span>
              <input
                className="input"
                type="time"
                step="60"
                value={q.end}
                onChange={(e) => setQuiet({ end: e.target.value })}
              />
            </label>
          </div>
        )}
        {q.enabled && timezone && <p className="field-help">Times are in {timezone}.</p>}
        {sameTimes && (
          <p className="field-error" role="alert">
            Start and end must be different times.
          </p>
        )}
        <div>
          <button type="submit" className="btn btn-quiet" disabled={busy || sameTimes}>
            {busy ? 'Saving…' : 'Save preferences'}
          </button>
        </div>
      </form>
    </section>
  );
}
