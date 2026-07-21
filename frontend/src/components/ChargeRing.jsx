/**
 * ChargeRing — the live-charge signature: a ~260px SVG ring around the
 * session's headline numbers.
 *
 * - With a kWh/time limit (`progress` 0–1) → a determinate arc filling toward
 *   the auto-stop target, exposed as role="progressbar".
 * - Without a limit (`progress` null) → a slow rotating brand-gradient arc
 *   (indeterminate). Reduced-motion users get a static arc (CSS media query).
 *
 * The center is caller-supplied (₹ cost + kWh); the ring itself is decorative
 * for screen readers except for the determinate progress semantics.
 */

import { useId } from 'react';
import './ChargeRing.css';

const SIZE = 260;
const STROKE = 14;
const R = (SIZE - STROKE) / 2;
const CIRC = 2 * Math.PI * R;

export default function ChargeRing({ progress = null, children }) {
  // useId emits colons, which break url(#…) references — sanitize.
  const gradientId = `cr-grad-${useId().replace(/[^a-zA-Z0-9-]/g, '')}`;

  const determinate = progress != null && Number.isFinite(Number(progress));
  const frac = determinate ? Math.min(1, Math.max(0, Number(progress))) : 0.28;

  const progressProps = determinate
    ? {
        role: 'progressbar',
        'aria-valuemin': 0,
        'aria-valuemax': 100,
        'aria-valuenow': Math.round(frac * 100),
        'aria-label': 'Progress toward your charging limit',
      }
    : {};

  return (
    <div className="charge-ring" {...progressProps}>
      <svg viewBox={`0 0 ${SIZE} ${SIZE}`} aria-hidden="true" focusable="false">
        <defs>
          <linearGradient id={gradientId} x1="0%" y1="0%" x2="100%" y2="100%">
            <stop className="charge-ring-stop-a" offset="0%" />
            <stop className="charge-ring-stop-b" offset="100%" />
          </linearGradient>
        </defs>
        <circle
          className="charge-ring-track"
          cx={SIZE / 2}
          cy={SIZE / 2}
          r={R}
          strokeWidth={STROKE}
        />
        <g className={determinate ? undefined : 'charge-ring-spin'}>
          <circle
            className="charge-ring-arc"
            cx={SIZE / 2}
            cy={SIZE / 2}
            r={R}
            strokeWidth={STROKE}
            stroke={`url(#${gradientId})`}
            strokeDasharray={`${frac * CIRC} ${CIRC}`}
            transform={`rotate(-90 ${SIZE / 2} ${SIZE / 2})`}
          />
        </g>
      </svg>
      <div className="charge-ring-center">{children}</div>
    </div>
  );
}
