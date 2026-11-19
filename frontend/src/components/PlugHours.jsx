/**
 * [Plug hours] Opening-hours line, max session length, amenity chips and the
 * operator's access note for a plug (PlugCard, the Home "charge now"
 * lookup and the reviews modal). The note is operator free text: rendered as a React text node,
 * never as HTML. Renders nothing for a plug with none of these set.
 */
import { Clock } from 'lucide-react';
import { AMENITY_LABELS, hoursLabel } from '../utils/plugHours';
import './PlugHours.css';

export default function PlugHours({ plug }) {
  const status = hoursLabel(plug);
  const amenities = Array.isArray(plug?.amenities) ? plug.amenities : [];
  const maxMin = plug?.max_session_minutes;
  if (!status && !amenities.length && !plug?.access_note && !maxMin) return null;
  return (
    <div className="plug-hours text-sm">
      {(status || maxMin) && (
        <p className={`plug-hours-status ${plug.open_now === false ? 'plug-hours-closed' : 'text-2'}`}>
          <Clock size={14} aria-hidden="true" />
          {status}
          {status && maxMin ? ' · ' : ''}
          {maxMin ? `Max ${maxMin} min per session` : ''}
        </p>
      )}
      {amenities.length > 0 && (
        <ul className="plug-hours-amenities" aria-label="Amenities">
          {amenities.map((a) => (
            <li key={a} className="chip">{AMENITY_LABELS[a] || a}</li>
          ))}
        </ul>
      )}
      {plug.access_note && <p className="plug-hours-note text-3">{plug.access_note}</p>}
    </div>
  );
}
