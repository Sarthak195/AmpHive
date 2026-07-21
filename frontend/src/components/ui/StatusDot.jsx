/**
 * StatusDot — the LED. Give it a plug availability `state`
 * (available|in_use|unpowered|offline|maintenance → .dot-state-*) or a
 * semantic `tone` (ok|warn|danger|info|brand → .dot-*). `live` adds the
 * pulsing .dot-live ring. `label` renders text next to the dot — pass a
 * string, or `true` to use the statusCopy label for `state`.
 */

import { plugStateLabel } from '../../utils/statusCopy';
import './ui.css';

export default function StatusDot({ state, tone, live = false, label }) {
  const toneClass = state ? ` dot-state-${state}` : tone ? ` dot-${tone}` : '';
  const dot = (
    <span className={`dot${toneClass}${live ? ' dot-live' : ''}`} aria-hidden="true" />
  );

  const text = label === true ? plugStateLabel(state) : label;
  if (!text) return dot;

  return (
    <span className="status-dot">
      {dot}
      <span className="status-dot-label">{text}</span>
    </span>
  );
}
