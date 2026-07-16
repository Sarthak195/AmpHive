/**
 * AmpHive CPO Tariffs Page (Pricing v2 Phase 4)
 * =============================================
 * Operators manage pricing plans (tariffs) and their time-of-day (TOD) slots:
 * a base coins-per-kWh rate, plus optional windows that override it at certain
 * hours (e.g. peak 18:00–22:00). Slots are half-open [start, end) in the
 * tenant's local timezone (shown read-only); a window crossing midnight is
 * entered as two slots. Each slot can also be scoped to specific weekdays via a
 * days_mask (Mon=bit0 … Sun=bit6; default = every day).
 *
 * Data: GET/POST/PUT/DELETE /api/cpo/tariffs[/{id}], and
 * GET/POST/DELETE /api/cpo/tariffs/{id}/slots[/{slotId}].
 */

import { useState, useEffect, useCallback } from 'react';
import CpoLayout from '../../components/CpoLayout';
import api from '../../api/client';

// "HH:MM" -> minute-of-day. An empty/blank value yields null.
const toMinutes = (hhmm) => {
  if (!hhmm) return null;
  const [h, m] = hhmm.split(':').map(Number);
  if (Number.isNaN(h) || Number.isNaN(m)) return null;
  return h * 60 + m;
};

// minute-of-day -> "HH:MM" (1440 -> "24:00" for a window ending at midnight).
const fmtMinutes = (min) => `${String(Math.floor(min / 60)).padStart(2, '0')}:${String(min % 60).padStart(2, '0')}`;

// Weekday bit convention matches the backend: Mon=bit0 … Sun=bit6.
const WEEKDAYS = [['Mon', 0], ['Tue', 1], ['Wed', 2], ['Thu', 3], ['Fri', 4], ['Sat', 5], ['Sun', 6]];
const ALL_DAYS_MASK = 127;        // every day
const WEEKDAYS_MASK = 31;         // Mon–Fri (bits 0–4)
const WEEKENDS_MASK = 96;         // Sat+Sun (bits 5–6)

// Human label for a days_mask (e.g. 127 -> "Every day", 96 -> "Weekends",
// else the set day abbreviations).
const daysLabel = (mask) => {
  const m = Number(mask);
  if (m === ALL_DAYS_MASK) return 'Every day';
  if (m === WEEKDAYS_MASK) return 'Weekdays';
  if (m === WEEKENDS_MASK) return 'Weekends';
  const on = WEEKDAYS.filter(([, i]) => (m >> i) & 1).map(([label]) => label);
  return on.length ? on.join(', ') : '—';
};

const CpoTariffs = () => {
  const [tariffs, setTariffs] = useState([]);
  const [tenantName, setTenantName] = useState('');
  const [tenantTz, setTenantTz] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  // Create-tariff form
  const [newName, setNewName] = useState('');
  const [newPrice, setNewPrice] = useState('');
  const [creating, setCreating] = useState(false);

  // Slot management for the expanded tariff
  const [selectedId, setSelectedId] = useState(null);
  const [slots, setSlots] = useState([]);
  const [slotsLoading, setSlotsLoading] = useState(false);
  const [slotStart, setSlotStart] = useState('18:00');
  const [slotEnd, setSlotEnd] = useState('22:00');
  const [slotPrice, setSlotPrice] = useState('');
  const [slotDays, setSlotDays] = useState(ALL_DAYS_MASK); // which weekdays the slot applies
  const [addingSlot, setAddingSlot] = useState(false);

  const fetchTariffs = useCallback(async () => {
    setLoading(true);
    try {
      const [tariffRes, profileRes] = await Promise.all([
        api.get('/api/cpo/tariffs'),
        api.get('/api/cpo/profile'),
      ]);
      setTariffs(tariffRes);
      setTenantName(profileRes.tenant?.name || '');
      setTenantTz(profileRes.tenant?.timezone || '');
    } catch (err) {
      console.error('Failed to load tariffs:', err);
      setError(err.message || 'Failed to load tariffs.');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { fetchTariffs(); }, [fetchTariffs]);

  const loadSlots = useCallback(async (tariffId) => {
    setSlotsLoading(true);
    try {
      setSlots(await api.get(`/api/cpo/tariffs/${tariffId}/slots`));
    } catch (err) {
      setError(err.message || 'Failed to load slots.');
    } finally {
      setSlotsLoading(false);
    }
  }, []);

  const selectTariff = (tariffId) => {
    const next = selectedId === tariffId ? null : tariffId;
    setSelectedId(next);
    setError('');
    if (next != null) loadSlots(next);
  };

  const createTariff = async (e) => {
    e.preventDefault();
    setError('');
    setCreating(true);
    try {
      await api.post('/api/cpo/tariffs', {
        name: newName.trim(),
        price_per_kwh: Number(newPrice),
      });
      setNewName('');
      setNewPrice('');
      await fetchTariffs();
    } catch (err) {
      setError(err.message || 'Failed to create the tariff.');
    } finally {
      setCreating(false);
    }
  };

  const deleteTariff = async (t) => {
    if (!window.confirm(`Delete tariff "${t.name}"? Any plug or group using it falls back to the next pricing rule.`)) return;
    setError('');
    try {
      await api.delete(`/api/cpo/tariffs/${t.id}`);
      if (selectedId === t.id) setSelectedId(null);
      await fetchTariffs();
    } catch (err) {
      setError(err.message || 'Failed to delete the tariff.');
    }
  };

  const addSlot = async (e) => {
    e.preventDefault();
    setError('');
    const start = toMinutes(slotStart);
    let end = toMinutes(slotEnd);
    if (end === 0) end = 1440; // a window ending at midnight
    if (start == null || end == null) { setError('Enter a start and end time.'); return; }
    if (!slotDays) { setError('Select at least one day.'); return; } // backend requires days_mask >= 1
    setAddingSlot(true);
    try {
      await api.post(`/api/cpo/tariffs/${selectedId}/slots`, {
        start_min: start,
        end_min: end,
        price_per_kwh: Number(slotPrice),
        days_mask: slotDays,
      });
      setSlotPrice('');
      await loadSlots(selectedId);
    } catch (err) {
      setError(err.message || 'Failed to add the slot.');
    } finally {
      setAddingSlot(false);
    }
  };

  const deleteSlot = async (slotId) => {
    setError('');
    try {
      await api.delete(`/api/cpo/tariffs/${selectedId}/slots/${slotId}`);
      await loadSlots(selectedId);
    } catch (err) {
      setError(err.message || 'Failed to remove the slot.');
    }
  };

  return (
    <CpoLayout tenantName={tenantName}>
      <div className="cpo-page-header">
        <h1>Tariffs</h1>
        <p>
          Pricing plans and time-of-day rates
          {tenantTz && <> — slot times are in <strong>{tenantTz}</strong></>}
        </p>
      </div>

      {/* New tariff */}
      <form className="filter-bar" onSubmit={createTariff}>
        <input
          type="text"
          placeholder="Tariff name (e.g. Society Default)"
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
          required
        />
        <input
          type="number"
          step="0.01"
          min="0.01"
          placeholder="Base coins/kWh"
          value={newPrice}
          onChange={(e) => setNewPrice(e.target.value)}
          required
          style={{ maxWidth: '10rem' }}
        />
        <button className="btn btn-primary btn-sm" type="submit" disabled={creating}>
          {creating ? 'Adding…' : 'Add tariff'}
        </button>
      </form>

      {error && (
        <div className="glass" style={{
          padding: '0.75rem 1rem', marginBottom: '1rem',
          borderRadius: 'var(--radius-md)',
          border: '1px solid var(--color-danger)',
          color: 'var(--color-danger)', fontSize: '0.85rem',
        }}>
          {error}
        </div>
      )}

      {loading ? (
        <div className="data-table-container">
          {[1, 2, 3].map((i) => (
            <div key={i} style={{ padding: '1rem', borderBottom: '1px solid hsla(0,0%,100%,0.04)' }}>
              <div className="skeleton" style={{ width: '100%', height: '18px' }} />
            </div>
          ))}
        </div>
      ) : tariffs.length > 0 ? (
        <div className="data-table-container animate-fade-in">
          <table className="data-table">
            <thead>
              <tr>
                <th>Tariff</th>
                <th>Base rate</th>
                <th>TOD slots</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {tariffs.map((t) => (
                <tr key={t.id}>
                  <td style={{ color: 'var(--color-text-primary)', fontWeight: 500 }}>{t.name}</td>
                  <td className="num">{t.price_per_kwh}/kWh</td>
                  <td>
                    <button className="btn btn-ghost btn-sm" onClick={() => selectTariff(t.id)}>
                      {selectedId === t.id ? 'Hide slots' : 'Manage slots'}
                    </button>
                  </td>
                  <td>
                    <button className="btn btn-ghost btn-sm" onClick={() => deleteTariff(t)}>Delete</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="empty-state glass">
          <div className="empty-state-icon">💲</div>
          <h3>No tariffs yet</h3>
          <p>Create a pricing plan above, then assign it to a plug or group from the Plugs page.</p>
        </div>
      )}

      {/* Slot editor for the expanded tariff */}
      {selectedId != null && (
        <div className="data-table-container animate-fade-in" style={{ marginTop: '1rem' }}>
          <div style={{ padding: '0.85rem 1rem', borderBottom: '1px solid hsla(0,0%,100%,0.06)' }}>
            <strong>Time-of-day slots</strong>{' '}
            <span style={{ color: 'var(--color-text-muted)', fontSize: '0.82rem' }}>
              — a window overrides the base rate at those hours. Outside every slot, the base rate applies.
            </span>
          </div>

          <form onSubmit={addSlot} style={{ display: 'flex', gap: '0.5rem', alignItems: 'center', flexWrap: 'wrap', padding: '0.75rem 1rem' }}>
            <label style={{ fontSize: '0.82rem', color: 'var(--color-text-secondary)' }}>
              From <input type="time" value={slotStart} onChange={(e) => setSlotStart(e.target.value)} required />
            </label>
            <label style={{ fontSize: '0.82rem', color: 'var(--color-text-secondary)' }}>
              To <input type="time" value={slotEnd} onChange={(e) => setSlotEnd(e.target.value)} required />
            </label>
            <input
              type="number" step="0.01" min="0.01"
              placeholder="coins/kWh"
              value={slotPrice}
              onChange={(e) => setSlotPrice(e.target.value)}
              required
              style={{ maxWidth: '9rem' }}
            />
            <div role="group" aria-label="Days of week" style={{ display: 'flex', gap: '0.3rem', flexWrap: 'wrap' }}>
              {WEEKDAYS.map(([label, i]) => {
                const on = (slotDays >> i) & 1;
                return (
                  <button
                    key={label}
                    type="button"
                    className="chip"
                    aria-pressed={on ? 'true' : 'false'}
                    onClick={() => setSlotDays((d) => d ^ (1 << i))}
                    style={{
                      cursor: 'pointer',
                      border: '1px solid var(--color-primary)',
                      background: on ? 'var(--color-primary)' : 'transparent',
                      color: on ? '#000' : 'var(--color-text-secondary)',
                    }}
                  >
                    {label}
                  </button>
                );
              })}
            </div>
            <button className="btn btn-primary btn-sm" type="submit" disabled={addingSlot}>
              {addingSlot ? 'Adding…' : 'Add slot'}
            </button>
          </form>

          {slotsLoading ? (
            <div style={{ padding: '1rem' }}><div className="skeleton" style={{ width: '100%', height: '16px' }} /></div>
          ) : slots.length > 0 ? (
            <table className="data-table">
              <thead>
                <tr><th>Window</th><th>Days</th><th>Rate</th><th></th></tr>
              </thead>
              <tbody>
                {slots.map((s) => (
                  <tr key={s.id}>
                    <td className="num">{fmtMinutes(s.start_min)} – {fmtMinutes(s.end_min)}</td>
                    <td>{daysLabel(s.days_mask ?? ALL_DAYS_MASK)}</td>
                    <td className="num">{s.price_per_kwh}/kWh</td>
                    <td>
                      <button className="btn btn-ghost btn-sm" onClick={() => deleteSlot(s.id)}>Remove</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <p style={{ padding: '0.75rem 1rem', color: 'var(--color-text-muted)', fontSize: '0.85rem' }}>
              No slots — this tariff charges its base rate around the clock. Add a window above to vary the price by time of day.
            </p>
          )}
        </div>
      )}
    </CpoLayout>
  );
};

export default CpoTariffs;
