/**
 * ChargeRoutines: recurring weekly charges (feature C5), on the Account page.
 *
 *   GET    /api/me/routines        -> { routines: [...], max_routines }
 *   POST   /api/me/routines        {plug_id, weekdays, start_time, max_kwh?, max_duration_seconds?, auto_stop_when_full}
 *   PATCH  /api/me/routines/{id}   {enabled} (and any other field)
 *   DELETE /api/me/routines/{id}
 *
 * weekdays is a bitmask, Monday=1 .. Sunday=64. start_time is "HH:MM"
 * wall-clock in the charger's own timezone (tenant_timezone), sent and shown
 * as typed and never converted through the browser's zone. Each run is an
 * ordinary scheduled charge, so a time or energy limit is required, exactly
 * like scheduling a single charge. A read-only cpo_viewer sees nothing.
 */

import { useCallback, useEffect, useState } from 'react';
import { Repeat } from 'lucide-react';
import ConfirmDialog from './ui/ConfirmDialog';
import { useToast } from './ui';
import api from '../api/client';
import { useAuth } from '../contexts/AuthContext';
import { apiErrorCopy } from '../utils/statusCopy';
import './ChargeRoutines.css';

const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

const daysLabel = (mask) => {
  if (mask === 127) return 'Every day';
  if (mask === 31) return 'Weekdays';
  if (mask === 96) return 'Weekends';
  return DAYS.filter((_, i) => mask & (1 << i)).join(' ');
};

const limitLabel = (r) => {
  const parts = [];
  if (r.max_duration_seconds) {
    const mins = Math.round(r.max_duration_seconds / 60);
    parts.push(mins % 60 === 0 ? `${mins / 60} h` : `${mins} min`);
  }
  if (r.max_kwh) parts.push(`${r.max_kwh} kWh`);
  return parts.join(' or ');
};

const EMPTY = { plugId: '', mask: 31, time: '07:00', hours: '', kwh: '', autoStop: false };

export default function ChargeRoutines({ className = 'card', titleClassName = 'account-card-title' }) {
  const { user } = useAuth();
  const toast = useToast();
  const [routines, setRoutines] = useState([]);
  const [maxRoutines, setMaxRoutines] = useState(5);
  const [plugs, setPlugs] = useState([]);
  const [form, setForm] = useState(EMPTY);
  const [busy, setBusy] = useState(false);
  const [deleting, setDeleting] = useState(null);
  const [deleteBusy, setDeleteBusy] = useState(false);

  const isViewer = user?.role === 'cpo_viewer';

  const load = useCallback(async () => {
    try {
      const res = await api.get('/api/me/routines');
      setRoutines(Array.isArray(res?.routines) ? res.routines : []);
      if (Number.isInteger(res?.max_routines)) setMaxRoutines(res.max_routines);
    } catch {
      setRoutines([]);
    }
  }, []);

  useEffect(() => {
    if (isViewer) return;
    load();
    (async () => {
      try {
        const res = await api.get('/api/plugs/available');
        setPlugs(Array.isArray(res) ? res : []);
      } catch {
        setPlugs([]);
      }
    })();
  }, [isViewer, load]);

  if (isViewer) return null;

  const set = (patch) => setForm((f) => ({ ...f, ...patch }));
  const plug = plugs.find((p) => String(p.id) === String(form.plugId));
  const hours = form.hours === '' ? null : Number(form.hours);
  const kwh = form.kwh === '' ? null : Number(form.kwh);
  const hoursOk = hours === null || (hours > 0 && hours <= 24);
  const kwhOk = kwh === null || (kwh > 0 && kwh <= 100);
  const hasLimit = hours !== null || kwh !== null;
  const atCap = routines.length >= maxRoutines;
  const canSave =
    !busy && !atCap && form.plugId !== '' && form.mask > 0 && form.time && hasLimit && hoursOk && kwhOk;

  const handleCreate = async (e) => {
    e.preventDefault();
    if (!canSave) return;
    setBusy(true);
    try {
      await api.post('/api/me/routines', {
        plug_id: Number(form.plugId),
        weekdays: form.mask,
        start_time: form.time,
        max_duration_seconds: hours === null ? null : Math.round(hours * 3600),
        max_kwh: kwh,
        auto_stop_when_full: form.autoStop,
      });
      toast.ok('Routine saved.');
      setForm(EMPTY);
      await load();
    } catch (err) {
      toast.error(apiErrorCopy(err));
    } finally {
      setBusy(false);
    }
  };

  const toggle = async (r) => {
    try {
      await api.patch(`/api/me/routines/${r.id}`, { enabled: !r.enabled });
      await load();
    } catch (err) {
      toast.error(apiErrorCopy(err));
    }
  };

  const confirmDelete = async () => {
    if (!deleting) return;
    setDeleteBusy(true);
    try {
      await api.delete(`/api/me/routines/${deleting.id}`);
      toast.ok('Routine deleted.');
      setDeleting(null);
      await load();
    } catch (err) {
      toast.error(apiErrorCopy(err));
    } finally {
      setDeleteBusy(false);
    }
  };

  return (
    <section className={className} aria-labelledby="routines-title">
      <h2 className={titleClassName} id="routines-title">
        <Repeat size={18} aria-hidden="true" />
        Charge routines
      </h2>
      <div className="stack-sm">
        <p className="text-2 text-sm">
          Charge on the same days at the same time without tapping start. Each run
          is a scheduled charge: it needs a time or energy limit, uses your balance
          like any charge, and starts only if the charger is free. The charger
          switches on at that time whether or not a car is connected, so keep yours
          plugged in. Up to {maxRoutines}.
        </p>

        {routines.length > 0 && (
          <ul className="routine-list" aria-label="Your charge routines">
            {routines.map((r) => (
              <li key={r.id} className="row-between">
                <div>
                  <div>
                    {r.plug_name}: {daysLabel(r.weekdays)} at {r.start_time}{' '}
                    <span className="text-3 text-xs">({r.tenant_timezone})</span>
                  </div>
                  <div className="text-3 text-xs">
                    Stops after {limitLabel(r)}
                    {r.auto_stop_when_full ? ', or when the car is full' : ''}
                    {r.consecutive_failures > 0 &&
                      ` · didn't start ${r.consecutive_failures} time${r.consecutive_failures > 1 ? 's' : ''} in a row`}
                  </div>
                </div>
                <div className="routine-actions">
                  <label className="checkbox-row text-sm">
                    <input
                      type="checkbox"
                      checked={r.enabled}
                      onChange={() => toggle(r)}
                      aria-label={`Routine on ${r.plug_name} at ${r.start_time} is ${r.enabled ? 'on' : 'off'}`}
                    />
                    On
                  </label>
                  <button
                    type="button"
                    className="btn btn-quiet btn-sm"
                    onClick={() => setDeleting(r)}
                    aria-label={`Delete routine on ${r.plug_name} at ${r.start_time}`}
                  >
                    Delete
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}

        <form className="stack-sm" onSubmit={handleCreate} aria-label="New charge routine">
          <div className="field">
            <label className="field-label" htmlFor="routine-plug">
              Charger
            </label>
            <select
              id="routine-plug"
              className="select"
              value={form.plugId}
              onChange={(e) => set({ plugId: e.target.value })}
            >
              <option value="">Choose a charger</option>
              {plugs.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </div>

          <fieldset className="field routine-days">
            <legend className="field-label">Days</legend>
            {DAYS.map((d, i) => (
              <label key={d} className="checkbox-row text-sm">
                <input
                  type="checkbox"
                  checked={Boolean(form.mask & (1 << i))}
                  onChange={() => set({ mask: form.mask ^ (1 << i) })}
                />
                {d}
              </label>
            ))}
          </fieldset>

          <div className="field">
            <label className="field-label" htmlFor="routine-time">
              Start time{plug?.tenant_timezone ? ` (${plug.tenant_timezone})` : ''}
            </label>
            <input
              id="routine-time"
              type="time"
              className="input"
              value={form.time}
              onChange={(e) => set({ time: e.target.value })}
            />
          </div>

          <div className="routine-limits">
            <div className="field">
              <label className="field-label" htmlFor="routine-hours">
                Time limit (hours)
              </label>
              <input
                id="routine-hours"
                type="number"
                className="input"
                min="0.25"
                max="24"
                step="0.25"
                inputMode="decimal"
                value={form.hours}
                onChange={(e) => set({ hours: e.target.value })}
              />
            </div>
            <div className="field">
              <label className="field-label" htmlFor="routine-kwh">
                Energy limit (kWh)
              </label>
              <input
                id="routine-kwh"
                type="number"
                className="input"
                min="0.1"
                max="100"
                step="0.1"
                inputMode="decimal"
                value={form.kwh}
                onChange={(e) => set({ kwh: e.target.value })}
              />
            </div>
          </div>
          {!hasLimit && <p className="field-hint text-sm">A routine needs a time or energy limit.</p>}
          {(!hoursOk || !kwhOk) && (
            <p className="field-error text-sm" role="alert">
              Use up to 24 hours and up to 100 kWh, above zero.
            </p>
          )}

          <label className="checkbox-row text-sm">
            <input
              type="checkbox"
              checked={form.autoStop}
              onChange={(e) => set({ autoStop: e.target.checked })}
            />
            Stop automatically when the car is full
          </label>

          <div>
            <button type="submit" className="btn btn-quiet" disabled={!canSave}>
              {busy ? 'Saving…' : 'Save routine'}
            </button>
          </div>
          {atCap && <p className="text-3 text-sm">Delete a routine to add another.</p>}
        </form>
      </div>

      <ConfirmDialog
        open={Boolean(deleting)}
        onClose={() => setDeleting(null)}
        onConfirm={confirmDelete}
        title="Delete this routine?"
        body="It won't start any more charges. A charge it already started keeps running."
        confirmLabel="Delete routine"
        busy={deleteBusy}
        busyLabel="Deleting…"
      />
    </section>
  );
}
