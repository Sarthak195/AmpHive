/**
 * VehicleSettings: the driver's own vehicle figures for the Insights estimates
 * and the opt-in monthly insights email (F8V).
 *
 *   GET /api/me/vehicle -> { km_per_kwh, petrol_price_per_l, petrol_km_per_l,
 *                            monthly_insights_email, defaults: {...} }
 *   PUT /api/me/vehicle    same three figures (empty = null = the default) plus
 *                          monthly_insights_email
 *
 * Only the driver's own dashboard, receipt and email use these figures; they
 * are estimates, never measurements. Leaving a field empty keeps the default,
 * shown as the placeholder. A read-only cpo_viewer cannot save, so the panel is
 * not shown to one.
 */

import { useEffect, useState } from 'react';
import { Gauge } from 'lucide-react';
import { useToast } from './ui';
import api from '../api/client';
import { useAuth } from '../contexts/AuthContext';
import { apiErrorCopy } from '../utils/statusCopy';

const FIELDS = [
  ['km_per_kwh', 'Your vehicle, km per kWh', '0.1', '0.5', '60'],
  ['petrol_km_per_l', 'Petrol vehicle replaced, km per litre', '0.1', '1', '100'],
  ['petrol_price_per_l', 'Petrol price, ₹ per litre', '0.1', '1', '500'],
];

const asText = (v) => (v == null ? '' : String(v));
const asNumber = (t) => (t.trim() === '' ? null : Number(t));
const figures = (res) => ({
  km_per_kwh: asText(res.km_per_kwh),
  petrol_km_per_l: asText(res.petrol_km_per_l),
  petrol_price_per_l: asText(res.petrol_price_per_l),
});

export default function VehicleSettings() {
  const { user } = useAuth();
  const toast = useToast();
  const [form, setForm] = useState(null);
  const [defaults, setDefaults] = useState({});
  const [busy, setBusy] = useState(false);

  const isViewer = user?.role === 'cpo_viewer';

  useEffect(() => {
    if (isViewer) return undefined;
    let cancelled = false;
    (async () => {
      try {
        const res = await api.get('/api/me/vehicle');
        if (cancelled || !res || typeof res.monthly_insights_email !== 'boolean') return;
        setForm({ ...figures(res), monthly_insights_email: res.monthly_insights_email });
        setDefaults(res.defaults || {});
      } catch {
        // Leave the panel hidden; the rest of the page still works.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [isViewer]);

  if (isViewer || !form) return null;

  const set = (patch) => setForm((f) => ({ ...f, ...patch }));

  const handleSave = async (e) => {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    try {
      const res = await api.put('/api/me/vehicle', {
        km_per_kwh: asNumber(form.km_per_kwh),
        petrol_km_per_l: asNumber(form.petrol_km_per_l),
        petrol_price_per_l: asNumber(form.petrol_price_per_l),
        monthly_insights_email: form.monthly_insights_email,
      });
      if (res) set(figures(res));
      toast.ok('Vehicle settings saved.');
    } catch (err) {
      toast.error(apiErrorCopy(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="card">
      <h2 className="account-card-title">
        <Gauge size={18} aria-hidden="true" />
        Vehicle and insights
      </h2>
      <form className="stack-sm" onSubmit={handleSave}>
        <p className="text-2 text-sm">
          Your own figures make the CO₂ and petrol estimates on your dashboard, receipts and
          monthly email closer to your vehicle. Leave a field empty to use the standard value.
        </p>
        {FIELDS.map(([key, label, step, min, max]) => (
          <label key={key} className="field">
            <span className="field-label">{label}</span>
            <input
              className="input"
              type="number"
              inputMode="decimal"
              step={step}
              min={min}
              max={max}
              placeholder={defaults[key] != null ? `Standard: ${defaults[key]}` : ''}
              value={form[key]}
              onChange={(e) => set({ [key]: e.target.value })}
            />
          </label>
        ))}
        <label className="checkbox-row text-sm">
          <input
            type="checkbox"
            checked={form.monthly_insights_email}
            onChange={(e) => set({ monthly_insights_email: e.target.checked })}
          />
          Email me a monthly charging summary
        </label>
        <p className="field-help">
          Sent once a month, on the 1st after 9:00 (India time), only if you charged. Off by
          default.
        </p>
        <div>
          <button type="submit" className="btn btn-quiet" disabled={busy}>
            {busy ? 'Saving…' : 'Save vehicle settings'}
          </button>
        </div>
      </form>
    </section>
  );
}
