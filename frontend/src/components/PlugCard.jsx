/**
 * PlugCard — one charger in the Dashboard grid.
 * =============================================
 * An explicit, boring five-state machine over utils/plugAvailability:
 *
 * - available   → Charge (primary) + Reserve; "Reserved for you" badge when
 *                 the current window is the viewer's (Charge stays enabled),
 *                 "Reserved until HH:MM" badge (and no Charge) when it is
 *                 someone else's.
 * - in_use      → Notify-me bell toggle (one-shot watch, aria-pressed) +
 *                 Reserve; reservation badges as above.
 * - unpowered   → "Queue charge" ONLY when the payload advertises
 *                 queue_available (CPO feature flag), else the bell;
 *                 sublabel "No mains power right now".
 * - offline     → no actions; sublabel "Can't be reached right now".
 * - maintenance → no actions; "Under maintenance" badge.
 *
 * No whole-card click — every action is a real button. Copy comes from
 * utils/statusCopy; colors from the --state-* tokens via StatusDot.
 */

import { Bell, CalendarClock, Hourglass, Zap } from 'lucide-react';
import './PlugCard.css';
import StatusDot from './ui/StatusDot';
import Money from './ui/Money';
import { useConfig } from '../contexts/ConfigContext';
import { getPlugAvailability } from '../utils/plugAvailability';
import { plugStateHint } from '../utils/statusCopy';
import { fmtTime } from '../utils/reservationTime';

// A rate-change ISO instant → the viewer's local HH:MM (blank if unparseable).
const changeTime = (iso) => {
  if (!iso) return '';
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? ''
    : d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
};

export default function PlugCard({ plug, onCharge, onReserve, onQueue, onToggleWatch }) {
  const { coin_inr_rate } = useConfig();
  const state = getPlugAvailability(plug);

  const reservedForYou = plug.reserved_now === true && plug.reserved_now_by_me === true;
  const reservedByOther = plug.reserved_now === true && plug.reserved_now_by_me !== true;
  const queueable = state === 'unpowered' && plug.queue_available === true;
  const canCharge = state === 'available' && !reservedByOther;
  const canReserve = state === 'available' || state === 'in_use';
  const showBell =
    state === 'in_use' || (state === 'unpowered' && !queueable);
  const hint = state === 'unpowered' || state === 'offline' ? plugStateHint(state) : '';

  const priceChanges =
    plug.price_next_per_kwh != null &&
    plug.price_changes_at &&
    plug.price_next_per_kwh !== plug.price_per_kwh;

  return (
    <article className="card card-tight plug-card">
      <div className="plug-card-head">
        <h3 className="plug-card-name">{plug.name || `Charger ${plug.id}`}</h3>
        <StatusDot state={state} live={state === 'in_use'} label />
      </div>

      <div className="plug-card-meta">
        {plug.price_per_kwh != null && (
          <span className="plug-card-price num">
            <Money coins={plug.price_per_kwh} rate={coin_inr_rate} />
            /kWh
            {priceChanges && (
              <span className="text-3 text-xs plug-card-price-next">
                {' '}
                <Money coins={plug.price_next_per_kwh} rate={coin_inr_rate} /> after{' '}
                {changeTime(plug.price_changes_at)}
              </span>
            )}
          </span>
        )}
        {plug.group_name && <span className="chip">{plug.group_name}</span>}
      </div>

      {(reservedForYou || reservedByOther || state === 'maintenance') && (
        <div className="plug-card-badges">
          {reservedForYou && <span className="badge badge-brand">Reserved for you</span>}
          {reservedByOther && (
            <span className="badge badge-info">
              {plug.reserved_until ? `Reserved until ${fmtTime(plug.reserved_until)}` : 'Reserved'}
            </span>
          )}
          {state === 'maintenance' && <span className="badge badge-warn">Under maintenance</span>}
        </div>
      )}

      {hint && <p className="plug-card-hint text-3 text-sm">{hint}</p>}

      {(canCharge || canReserve || queueable || showBell) && (
        <div className="plug-card-actions">
          {canCharge && (
            <button
              type="button"
              className="btn btn-primary btn-sm"
              onClick={() => onCharge?.(plug)}
            >
              <Zap size={16} aria-hidden="true" />
              Charge
            </button>
          )}
          {queueable && (
            <button
              type="button"
              className="btn btn-primary btn-sm"
              onClick={() => onQueue?.(plug)}
            >
              <Hourglass size={16} aria-hidden="true" />
              Queue charge
            </button>
          )}
          {showBell && (
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              aria-pressed={plug.watching === true}
              aria-label={
                plug.watching
                  ? `Stop watching ${plug.name}`
                  : `Notify me when ${plug.name} is free`
              }
              onClick={() => onToggleWatch?.(plug)}
            >
              <Bell size={16} aria-hidden="true" />
              {plug.watching ? 'Watching' : 'Notify me'}
            </button>
          )}
          {canReserve && (
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={() => onReserve?.(plug)}
            >
              <CalendarClock size={16} aria-hidden="true" />
              Reserve
            </button>
          )}
        </div>
      )}
    </article>
  );
}
