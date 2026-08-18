/**
 * StatusDot — the LED. Give it a plug availability `state`
 * (available|in_use|unpowered|offline|maintenance → .dot-state-*) or a
 * semantic `tone` (ok|warn|danger|info|brand → .dot-*). `live` adds the
 * pulsing .dot-live ring. `label` renders text next to the dot — pass a
 * string, or `true` to use the statusCopy label for `state`.
 *
 * `srLabel` is the escape hatch for the handful of call sites that show the
 * dot with NO status text beside it (dense table cells). The dot itself is
 * aria-hidden because it is pure colour, so those cells convey online/offline
 * to sighted users only — WCAG 1.4.1 "Use of Color". srLabel puts the same
 * information in the screen-reader layer without changing the visual density.
 * Do not pass it when a visible label already says the same thing: that would
 * make assistive tech announce the status twice.
 */

import { plugStateLabel } from '../../utils/statusCopy';
import './ui.css';

export default function StatusDot({ state, tone, live = false, label, srLabel }) {
  const toneClass = state ? ` dot-state-${state}` : tone ? ` dot-${tone}` : '';
  const dot = (
    <span className={`dot${toneClass}${live ? ' dot-live' : ''}`} aria-hidden="true" />
  );

  const text = label === true ? plugStateLabel(state) : label;
  if (!text) {
    // Fragment rather than a wrapper element on purpose: these call sites sit
    // inside flex cells whose gap would otherwise pick up an extra child, and
    // .sr-only is position:absolute so it contributes nothing to layout.
    if (!srLabel) return dot;
    return (
      <>
        {dot}
        <span className="sr-only">{srLabel}</span>
      </>
    );
  }

  return (
    <span className="status-dot">
      {dot}
      <span className="status-dot-label">{text}</span>
    </span>
  );
}
