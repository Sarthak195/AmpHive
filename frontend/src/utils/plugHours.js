/**
 * [Plug hours] Driver-facing copy for a plug's opening hours, amenities and
 * max session length (backend services/plug_hours.py driver_fields).
 * The backend decides open/closed (open_now) and the next change
 * (hours_changes_at: the close when open, the next open when closed); this
 * only renders the time in the plug's tenant_timezone.
 */
import { fmtTimeInZone } from './reservationTime';

export const AMENITY_LABELS = {
  restroom: 'Restroom',
  cafe: 'Cafe',
  wifi: 'Wi-Fi',
  covered: 'Covered',
  lit: 'Lit at night',
  cctv: 'CCTV',
  accessible: 'Accessible',
};

export const AMENITY_TAGS = Object.keys(AMENITY_LABELS);

const DAY_MS = 24 * 3600 * 1000;

// "22:00", or "Mon 08:00" when the change is more than a day away.
function changeLabel(iso, tz) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  if (d.getTime() - Date.now() > DAY_MS) {
    return d.toLocaleString([], { weekday: 'short', hour: '2-digit', minute: '2-digit', timeZone: tz || undefined });
  }
  return fmtTimeInZone(iso, tz || undefined);
}

/**
 * "Open now, closes 22:00" / "Closed, opens 08:00", or '' for a plug with no
 * opening hours (open 24x7, the default: nothing to say).
 */
export function hoursLabel(plug) {
  if (plug?.open_now == null) return '';
  const at = plug.hours_changes_at ? changeLabel(plug.hours_changes_at, plug.tenant_timezone) : '';
  if (plug.open_now) return at ? `Open now, closes ${at}` : 'Open now';
  return at ? `Closed, opens ${at}` : 'Closed';
}

/** Dashboard "Hours" filter: '' any, 'open' open now (24x7 counts), '24x7'. */
export function matchesHours(plug, value) {
  if (value === 'open') return plug?.open_now !== false;
  if (value === '24x7') return plug?.opening_hours == null;
  return true;
}

/* --- CPO edit form (components/PlugHoursEditor) --- */

const pad = (n) => String(n).padStart(2, '0');
const toHHMM = (min) => `${pad(Math.floor((min % 1440) / 60))}:${pad(min % 60)}`;
const toMin = (hhmm) => {
  const [h, m] = String(hhmm || '').split(':').map(Number);
  return Number.isFinite(h) && Number.isFinite(m) ? h * 60 + m : null;
};

/** Stored plug fields -> editor state. */
export function hoursFormFromPlug(plug) {
  return {
    windows: (plug?.opening_hours || []).map((w) => ({
      days: w.days, start: toHHMM(w.start), end: toHHMM(w.end),
    })),
    max_session_minutes: plug?.max_session_minutes ?? '',
    amenities: plug?.amenities || [],
    access_note: plug?.access_note ?? '',
  };
}

/**
 * Editor state -> only the PUT body fields that changed (an empty object
 * when nothing did). Throws an Error with readable copy on an invalid row.
 */
export function hoursPatch(form, plug) {
  const body = {};
  const windows = form.windows.map((w) => {
    const start = toMin(w.start);
    const end = toMin(w.end);
    if (!w.days) throw new Error('Pick at least one day for each opening window.');
    if (start == null || end == null) throw new Error('Enter a start and end time for each window.');
    if (start === end) throw new Error('An opening window cannot start and end at the same time.');
    return { days: w.days, start, end };
  });
  const before = JSON.stringify(plug?.opening_hours || []);
  const after = JSON.stringify(windows.map((w) => ({ ...w, end: w.end === 0 ? 1440 : w.end })));
  if (before !== after) body.opening_hours = windows.length ? windows : null;
  if (String(form.max_session_minutes) !== String(plug?.max_session_minutes ?? '')) {
    body.max_session_minutes = form.max_session_minutes === '' ? null : Number(form.max_session_minutes);
  }
  if (JSON.stringify(form.amenities) !== JSON.stringify(plug?.amenities || [])) {
    body.amenities = form.amenities;
  }
  if (form.access_note !== (plug?.access_note ?? '')) body.access_note = form.access_note;
  return body;
}
