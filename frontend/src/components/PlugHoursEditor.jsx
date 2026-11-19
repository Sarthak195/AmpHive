/**
 * [Plug hours] CPO edit-form section: weekly opening hours, max session
 * length, amenity tags and the driver access note (PUT /api/cpo/plugs/{id}).
 * Times are the tenant's local wall-clock. A window whose end is not after
 * its start runs past midnight (the backend splits it in two, so a saved
 * overnight window reopens here as two rows). No windows = open 24x7.
 */
import { Plus, Trash2 } from 'lucide-react';
import { AMENITY_LABELS, AMENITY_TAGS } from '../utils/plugHours';

const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const ACCESS_NOTE_MAX = 280;

export default function PlugHoursEditor({ value, onChange }) {
  const set = (patch) => onChange({ ...value, ...patch });
  const setWindow = (i, patch) =>
    set({ windows: value.windows.map((w, j) => (j === i ? { ...w, ...patch } : w)) });

  return (
    <>
      <div className="field">
        <span className="field-label">Opening hours</span>
        {value.windows.length === 0 && (
          <p className="field-help">Open 24x7. Add a window to limit the hours.</p>
        )}
        {value.windows.map((w, i) => (
          <div key={i} className="plug-hours-row" role="group" aria-label={`Opening window ${i + 1}`}>
            <div className="plug-hours-days">
              {DAYS.map((d, bit) => {
                const on = Boolean(w.days & (1 << bit));
                return (
                  <button
                    key={d}
                    type="button"
                    className={`btn btn-sm ${on ? 'btn-primary' : 'btn-ghost'}`}
                    aria-pressed={on}
                    onClick={() => setWindow(i, { days: w.days ^ (1 << bit) })}
                  >
                    {d}
                  </button>
                );
              })}
            </div>
            <input
              className="input"
              type="time"
              aria-label="Opens at"
              value={w.start}
              onChange={(e) => setWindow(i, { start: e.target.value })}
            />
            <input
              className="input"
              type="time"
              aria-label="Closes at"
              value={w.end}
              onChange={(e) => setWindow(i, { end: e.target.value })}
            />
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              aria-label={`Remove opening window ${i + 1}`}
              onClick={() => set({ windows: value.windows.filter((_, j) => j !== i) })}
            >
              <Trash2 size={14} aria-hidden="true" />
            </button>
          </div>
        ))}
        <div>
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={() => set({ windows: [...value.windows, { days: 127, start: '08:00', end: '22:00' }] })}
          >
            <Plus size={14} aria-hidden="true" />
            Add window
          </button>
        </div>
        <p className="field-help">
          Local time. A closing time earlier than the opening time runs past midnight. A closed
          charger refuses new starts and bookings; a charge already running is not stopped.
        </p>
      </div>
      <div className="field">
        <label className="field-label" htmlFor="cpo-chargers-edit-max-session">
          Max session length (minutes)
        </label>
        <input
          id="cpo-chargers-edit-max-session"
          className="input"
          type="number"
          min="1"
          max="1440"
          step="1"
          placeholder="No limit"
          value={value.max_session_minutes}
          onChange={(e) => set({ max_session_minutes: e.target.value })}
        />
      </div>
      <fieldset className="field plug-hours-amenity-set">
        <legend className="field-label">Amenities</legend>
        {AMENITY_TAGS.map((tag) => (
          <label key={tag} className="checkbox-row text-sm">
            <input
              type="checkbox"
              checked={value.amenities.includes(tag)}
              onChange={(e) =>
                set({
                  amenities: e.target.checked
                    ? AMENITY_TAGS.filter((t) => t === tag || value.amenities.includes(t))
                    : value.amenities.filter((t) => t !== tag),
                })
              }
            />
            {AMENITY_LABELS[tag]}
          </label>
        ))}
      </fieldset>
      <div className="field">
        <label className="field-label" htmlFor="cpo-chargers-edit-access-note">
          Access note for drivers
        </label>
        <textarea
          id="cpo-chargers-edit-access-note"
          className="textarea"
          maxLength={ACCESS_NOTE_MAX}
          placeholder="For example: basement level 2, ask security for the gate."
          value={value.access_note}
          onChange={(e) => set({ access_note: e.target.value })}
        />
        <p className="field-help">
          Plain text, shown on the charger card. {value.access_note.length}/{ACCESS_NOTE_MAX}
        </p>
      </div>
    </>
  );
}
