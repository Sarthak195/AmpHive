/**
 * ChargeSetupModal — set optional limits, then start (or queue) a charge.
 * =======================================================================
 * Shared prop contract (redesign-v3):
 *   <ChargeSetupModal open onClose plug mode="start"|"queue" onConfirm />
 *
 * - mode="start" (default): "Set up your charge" / "Start charging".
 * - mode="queue": "Queue this charge" / "Join the queue" — the charge starts
 *   automatically when the circuit has room.
 *
 * Time limit is preset chips (No limit / 30m / 1h / 2h / 4h / 8h / Custom
 * hh:mm), defaulting to "No limit" — no fractional-hours input. Energy limit
 * is a plain kWh field, blank by default. [Opt-in charging limits] Neither
 * field is sent to the backend unless the driver actually picks one — a
 * "no limit" charge persists no duration/energy cap at all and runs until
 * stopped (see backend/schemas.py SessionStartRequest); this modal never
 * fills in a hidden default. The coverage line uses the plug's OWN resolved
 * price. `onConfirm(plugId, limits)` may throw: the error surfaces inline
 * via apiErrorCopy, and a `circuit_full` rejection additionally offers
 * "Ask for capacity" (POST /api/plugs/{id}/request-capacity) instead of a
 * dead end.
 *
 * [Scheduled start] In start mode, when the plug reports schedule_available
 * and a tenant_timezone, a "Start: Now | At a time" choice appears. "At a time"
 * takes a wall-clock time in the TENANT's zone, requires a time or energy
 * limit (nobody is at the plug when it switches on; 8h is preselected), and
 * calls `onConfirm(plugId, limits, notBeforeIso)`. "Now" keeps the two-arg call.
 *
 * [Cheapest window] When schedulable, GET /api/plugs/{id}/tariff-preview's
 * `cheapest_window` (computed on the backend with the billing slot logic, null
 * on a flat tariff or when already cheapest) is offered as one tap that picks
 * "At a time" and fills the start time. Best-effort: a failed fetch hides it.
 *
 * [Idle fee C1] The same preview (fetched for every plug while the modal is
 * open) carries `idle_fee` ({grace_min, per_min, cap}, null when the plug's
 * tariff has none). When set, the terms are shown under the auto-stop toggle:
 * an opted-in auto-stop never pays it.
 *
 * [Charge complete] An opt-in "Stop automatically when the car is full" toggle
 * (default off). When on, `limits.auto_stop_when_full = true` is passed to
 * onConfirm; the backend then stops the session once the car's draw has stayed
 * near zero (backend/services/mqtt/charge_full.py). Off sends nothing.
 *
 * [Estimate] Under the limits, an "Estimate" line shows energy, duration, finish
 * time and cost from GET /api/plugs/{id}/estimate (debounced as the limits or
 * start time change; best-effort, hidden when it fails or no limit is set). It
 * is display only: nothing it returns is sent back when starting.
 *
 * `priceStale` marks the price as last saved (the plug row came from the
 * instant-open snapshot and the live list has not answered yet).
 */

import { useEffect, useState } from 'react';
import Modal from './ui/Modal';
import './ChargeSetupModal.css';
import Money from './ui/Money';
import api from '../api/client';
import { useConfig } from '../contexts/ConfigContext';
import { useWallet } from '../contexts/WalletContext';
import { apiErrorCopy } from '../utils/statusCopy';
import { fmtTime, fmtTimeInZone, nextOccurrenceInZone } from '../utils/reservationTime';

const TIME_PRESETS = [
  { id: 'none', label: 'No limit', minutes: null },
  { id: '30m', label: '30m', minutes: 30 },
  { id: '1h', label: '1h', minutes: 60 },
  { id: '2h', label: '2h', minutes: 120 },
  { id: '4h', label: '4h', minutes: 240 },
  { id: '8h', label: '8h', minutes: 480 },
  { id: 'custom', label: 'Custom', minutes: null },
];

const CUSTOM_RE = /^(\d{1,2}):([0-5]\d)$/;

// "h:mm" → minutes, or null when it doesn't parse / is zero.
const parseCustomMinutes = (value) => {
  const m = CUSTOM_RE.exec(value.trim());
  if (!m) return null;
  const minutes = Number(m[1]) * 60 + Number(m[2]);
  return minutes > 0 ? minutes : null;
};

const ESTIMATE_DEBOUNCE_MS = 400;

const fmtDuration = (min) => {
  const h = Math.floor(min / 60);
  const m = min % 60;
  return h ? `${h}h${m ? ` ${m}m` : ''}` : `${m}m`;
};

const COPY = {
  start: {
    title: 'Set up your charge',
    confirm: 'Start charging',
    busy: 'Starting…',
    intro: 'Set a stop limit if you want one — you can change it anytime while charging.',
  },
  schedule: {
    title: 'Set up your charge',
    confirm: 'Schedule charge',
    busy: 'Scheduling…',
    intro: 'Leave your car plugged in. Charging starts at the time you pick.',
  },
  queue: {
    title: 'Queue this charge',
    confirm: 'Join the queue',
    busy: 'Joining…',
    intro: 'Your charge starts automatically when the circuit has room.',
  },
};

export default function ChargeSetupModal({ open, onClose, plug, mode = 'start', onConfirm, priceStale = false }) {
  const { coins_per_kwh, coin_inr_rate } = useConfig();
  const { availableBalance, balanceFresh } = useWallet();

  const [timePreset, setTimePreset] = useState('none');
  const [customTime, setCustomTime] = useState('');
  const [kwh, setKwh] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [circuitFull, setCircuitFull] = useState(false);
  const [capacityAsked, setCapacityAsked] = useState(false);
  const [asking, setAsking] = useState(false);
  const [startAt, setStartAt] = useState('now');
  const [atTime, setAtTime] = useState('');
  const [autoLimit, setAutoLimit] = useState(false);
  const [cheapest, setCheapest] = useState(null);
  const [autoStopFull, setAutoStopFull] = useState(false);
  const [idleFee, setIdleFee] = useState(null);

  const schedulable = Boolean(open && mode === 'start' && plug?.schedule_available && plug?.tenant_timezone);
  // [Plug hours] A closed plug can only be scheduled: open on "At a time"
  // (with the 8h preselect chooseStart applies) and offer no "Now".
  const closedStart = schedulable && plug?.open_now === false;
  const plugId = plug?.id;
  useEffect(() => {
    setCheapest(null);
    setIdleFee(null);
    if (!open || plugId == null) return undefined;
    let cancelled = false;
    (async () => {
      try {
        const res = await api.get(`/api/plugs/${plugId}/tariff-preview`);
        if (cancelled) return;
        const w = res?.cheapest_window;
        if (schedulable && w && Number(w.price_per_kwh) < Number(res.price_now)) {
          setCheapest({ hhmm: w.start_hhmm, price: Number(w.price_per_kwh), now: Number(res.price_now) });
        }
        if (res?.idle_fee) setIdleFee(res.idle_fee);
      } catch {
        /* best-effort hint: hide on failure */
      }
    })();
    return () => { cancelled = true; };
  }, [open, schedulable, plugId]);

  // [Estimate] Limits as the endpoint takes them. An out-of-range value is
  // dropped so the fetch is only made for a request the backend accepts.
  const estKwhNum = kwh !== '' ? parseFloat(kwh) : NaN;
  const estKwh = estKwhNum > 0 && estKwhNum <= 100 ? estKwhNum : null;
  const estMinutesRaw =
    timePreset === 'custom'
      ? parseCustomMinutes(customTime)
      : TIME_PRESETS.find((p) => p.id === timePreset)?.minutes;
  const estMinutes = estMinutesRaw && estMinutesRaw <= 1440 ? estMinutesRaw : null;
  const estTz = plug?.tenant_timezone;
  const estStartIso =
    open && mode === 'start' && startAt === 'later' && atTime && estTz
      ? nextOccurrenceInZone(atTime, estTz).iso
      : null;
  const [estimate, setEstimate] = useState(null);
  useEffect(() => {
    setEstimate(null);
    if (!open || plugId == null || (estKwh == null && estMinutes == null)) return undefined;
    let cancelled = false;
    const timer = setTimeout(async () => {
      const qs = new URLSearchParams();
      if (estKwh != null) qs.set('max_kwh', String(estKwh));
      if (estMinutes != null) qs.set('max_duration_min', String(estMinutes));
      if (estStartIso) qs.set('start_at', estStartIso);
      try {
        const res = await api.get(`/api/plugs/${plugId}/estimate?${qs.toString()}`);
        if (!cancelled && res && res.is_estimate) setEstimate(res);
      } catch {
        /* best-effort line: hide on failure */
      }
    }, ESTIMATE_DEBOUNCE_MS);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [open, plugId, estKwh, estMinutes, estStartIso]);

  // Fresh form every time the modal opens (or for a different plug).
  useEffect(() => {
    if (!open) return;
    setTimePreset('none');
    setCustomTime('');
    setKwh('');
    setBusy(false);
    setError('');
    setCircuitFull(false);
    setCapacityAsked(false);
    setAsking(false);
    setStartAt(closedStart ? 'later' : 'now');
    setAtTime('');
    setAutoLimit(closedStart);
    if (closedStart) setTimePreset('8h');
    setAutoStopFull(false);
  }, [open, plug?.id, closedStart]);

  if (!open || !plug) return null;

  const tz = plug.tenant_timezone;
  const canSchedule = mode === 'start' && Boolean(plug.schedule_available && tz);
  const scheduling = canSchedule && startAt === 'later';
  const copy = scheduling ? COPY.schedule : COPY[mode] || COPY.start;
  const nextStart = scheduling && atTime ? nextOccurrenceInZone(atTime, tz) : null;

  // Coverage line: the plug's own price (falling back to the global rate).
  // A real 0 (free charging) is a valid rate — only null/undefined/non-numeric
  // fall back to the global rate. Uses availableBalance (not the raw balance)
  // so a hold from a second concurrent session is respected in the estimate.
  const plugRate = Number(plug.price_per_kwh);
  const rate =
    plug.price_per_kwh != null && Number.isFinite(plugRate) ? plugRate : (coins_per_kwh ?? 5);
  const covers = rate > 0 ? (Number(availableBalance) || 0) / rate : 0;

  // Validate custom time: must be valid format and within [0, 1440] minutes (24h max).
  const customMinutes = timePreset === 'custom' ? parseCustomMinutes(customTime) : null;
  const customOutOfRange = timePreset === 'custom' && customTime !== '' && customMinutes !== null && customMinutes > 1440;
  const customInvalid = timePreset === 'custom' && (parseCustomMinutes(customTime) == null || customOutOfRange);

  // Validate kWh: must be within (0, 100].
  const kwhNum = kwh !== '' ? parseFloat(kwh) : null;
  const kwhOutOfRange = kwhNum !== null && (kwhNum <= 0 || kwhNum > 100);
  const kwhInvalid = kwhNum !== null && kwhNum <= 0;
  const presetMinutes = TIME_PRESETS.find((p) => p.id === timePreset)?.minutes;
  const hasLimit = Boolean(presetMinutes || customMinutes || (kwhNum !== null && kwhNum > 0));
  const scheduleBlocked = scheduling && (!atTime || !hasLimit);

  const chooseStart = (value) => {
    setStartAt(value);
    // A scheduled charge must stop on its own: move off "No limit", and undo
    // that preselect if the driver goes back to "Now" without touching it.
    if (value === 'later' && timePreset === 'none' && kwh === '') {
      setTimePreset('8h');
      setAutoLimit(true);
    } else if (value === 'now' && autoLimit && timePreset === '8h') {
      setTimePreset('none');
      setAutoLimit(false);
    }
  };

  const pickCheapest = () => {
    chooseStart('later');
    setAtTime(cheapest.hhmm);
  };
  // Saving for the energy limit when one is set, else the per-kWh difference.
  const saveKwh = kwhNum !== null && kwhNum > 0 && !kwhOutOfRange ? kwhNum : null;

  const handleConfirm = async () => {
    const limits = {};
    const k = parseFloat(kwh);
    if (!Number.isNaN(k) && k > 0) limits.max_kwh = k;
    const minutes =
      timePreset === 'custom'
        ? parseCustomMinutes(customTime)
        : TIME_PRESETS.find((p) => p.id === timePreset)?.minutes;
    if (minutes) limits.max_duration_seconds = minutes * 60;
    if (autoStopFull) limits.auto_stop_when_full = true;

    setBusy(true);
    setError('');
    setCircuitFull(false);
    try {
      const chosen = Object.keys(limits).length ? limits : null;
      if (scheduling) {
        // Re-resolve at confirm time so a modal left open stays correct.
        await onConfirm(plug.id, chosen, nextOccurrenceInZone(atTime, tz).iso);
      } else {
        await onConfirm(plug.id, chosen);
      }
      onClose?.();
    } catch (err) {
      setError(apiErrorCopy(err));
      setCircuitFull(err?.code === 'circuit_full');
      setBusy(false);
    }
  };

  const handleAskCapacity = async () => {
    setAsking(true);
    setError('');
    try {
      await api.post(`/api/plugs/${plug.id}/request-capacity`);
      setCapacityAsked(true);
      setCircuitFull(false);
    } catch (err) {
      setError(apiErrorCopy(err));
    } finally {
      setAsking(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={busy ? undefined : onClose}
      title={copy.title}
      footer={
        <>
          <button type="button" className="btn btn-quiet" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button
            type="button"
            className="btn btn-primary"
            onClick={handleConfirm}
            disabled={busy || customInvalid || kwhOutOfRange || scheduleBlocked}
          >
            {busy ? copy.busy : copy.confirm}
          </button>
        </>
      }
    >
      <div className="stack">
        <div>
          <p className="charge-setup-plug">{plug.name || `Charger ${plug.id}`}</p>
          <p className="text-3 text-sm num">
            <Money coins={rate} rate={coin_inr_rate} />
            /kWh{priceStale && <span className="stale-tag">(last saved price)</span>} · your balance covers ≈ {covers.toFixed(1)} kWh
            {balanceFresh === false && <span className="stale-tag">(last saved balance)</span>}
          </p>
        </div>

        <p className="text-2 text-sm">{copy.intro}</p>

        {canSchedule && cheapest && (
          <button type="button" className="btn btn-quiet btn-sm" onClick={pickCheapest}>
            Cheapest: {cheapest.hhmm} at <Money coins={cheapest.price} rate={coin_inr_rate} />
            /kWh (save ~<Money coins={(cheapest.now - cheapest.price) * (saveKwh ?? 1)} rate={coin_inr_rate} />
            {saveKwh === null ? '/kWh' : ''})
          </button>
        )}

        {canSchedule && (
          <div className="field">
            <span className="field-label" id="charge-start-label">
              Start
            </span>
            <div className="charge-setup-chips" role="group" aria-labelledby="charge-start-label">
              {[
                ['now', 'Now'],
                ['later', 'At a time'],
              ].filter(([value]) => !(closedStart && value === 'now')).map(([value, label]) => (
                <button
                  key={value}
                  type="button"
                  className={`btn btn-sm ${startAt === value ? 'btn-primary' : 'btn-quiet'}`}
                  aria-pressed={startAt === value}
                  onClick={() => chooseStart(value)}
                >
                  {label}
                </button>
              ))}
            </div>
            {scheduling && (
              <>
                <input
                  className="input"
                  type="time"
                  step="60"
                  value={atTime}
                  onChange={(e) => setAtTime(e.target.value)}
                  aria-label="Start time"
                />
                <p className="field-hint text-sm">
                  {nextStart
                    ? `Times are in ${tz}. Starts ${nextStart.tomorrow ? 'tomorrow' : 'today'} at ${fmtTimeInZone(nextStart.iso, tz)}.`
                    : `Times are in ${tz}.`}
                </p>
              </>
            )}
          </div>
        )}

        <div className="field">
          <span className="field-label" id="charge-time-label">
            Time limit
          </span>
          <div className="charge-setup-chips" role="group" aria-labelledby="charge-time-label">
            {TIME_PRESETS.map((p) => (
              <button
                key={p.id}
                type="button"
                className={`btn btn-sm ${timePreset === p.id ? 'btn-primary' : 'btn-quiet'}`}
                aria-pressed={timePreset === p.id}
                onClick={() => setTimePreset(p.id)}
              >
                {p.label}
              </button>
            ))}
          </div>
          {timePreset === 'none' && !scheduling && (
            <p className="field-hint text-sm">
              Charges until you stop it — no time limit
            </p>
          )}
          {timePreset === 'custom' && (
            <>
              <input
                className="input"
                value={customTime}
                onChange={(e) => setCustomTime(e.target.value)}
                placeholder="h:mm — e.g. 1:30"
                aria-label="Custom time limit (hours:minutes)"
                aria-invalid={customTime !== '' && customInvalid}
                inputMode="numeric"
              />
              {customTime !== '' && customInvalid && (
                <p className="field-error">
                  {customOutOfRange ? 'Maximum 24 hours (24:00).' : 'Enter a time as hours:minutes, like 1:30.'}
                </p>
              )}
            </>
          )}
        </div>

        <div className="field">
          <label className="field-label" htmlFor="charge-setup-kwh">
            Energy limit (kWh)
          </label>
          <input
            id="charge-setup-kwh"
            className="input"
            type="number"
            min="0"
            max="100"
            step="0.1"
            value={kwh}
            onChange={(e) => setKwh(e.target.value)}
            placeholder="No limit"
            aria-invalid={kwh !== '' && kwhOutOfRange}
          />
          {kwh !== '' && kwhOutOfRange && (
            <p className="field-error">
              {kwhInvalid ? 'Energy limit must be greater than 0.' : 'Maximum 100 kWh.'}
            </p>
          )}
          {kwh === '' && !scheduling && (
            <p className="field-hint text-sm">
              Charges until you stop it — no energy limit
            </p>
          )}
        </div>

        {estimate && (
          <p className="charge-setup-estimate text-sm" data-testid="charge-estimate">
            <strong>Estimate:</strong> ≈ {Number(estimate.energy_kwh).toFixed(1)} kWh in{' '}
            {fmtDuration(estimate.duration_min)}, finishing about{' '}
            {estTz ? fmtTimeInZone(estimate.finish_at, estTz) : fmtTime(estimate.finish_at)}, costing about{' '}
            <Money coins={Number(estimate.cost_coins)} rate={coin_inr_rate} />.
            {estimate.rate_changes?.length > 0 && (
              <>
                {' '}The price changes during this charge (at{' '}
                {estimate.rate_changes
                  .map((c) => (estTz ? fmtTimeInZone(c.at, estTz) : fmtTime(c.at)))
                  .join(', ')}
                ).
              </>
            )}{' '}
            <span className="text-3">
              Based on {estimate.basis === 'recent_session' ? 'your last session here' : "this charger's rated power"};
              your car may draw less.
            </span>
          </p>
        )}

        <label className="checkbox-row text-sm">
          <input
            type="checkbox"
            checked={autoStopFull}
            onChange={(e) => setAutoStopFull(e.target.checked)}
          />
          Stop automatically when the car is full
        </label>
        {idleFee && (
          <p className="field-hint text-sm" data-testid="idle-fee-terms">
            Idle fee: <Money coins={idleFee.per_min} rate={coin_inr_rate} /> per minute if the car stays
            plugged in more than {idleFee.grace_min} min after it looks full, at most{' '}
            <Money coins={idleFee.cap} rate={coin_inr_rate} />.{' '}
            {autoStopFull ? 'Not charged: this session stops itself.' : 'Stop and unplug in time, or tick the box above.'}
          </p>
        )}

        {scheduling && !hasLimit && (
          <p className="field-hint text-sm">A scheduled charge needs a time or energy limit.</p>
        )}

        {error && (
          <p className="field-error" role="alert">
            {error}
          </p>
        )}

        {circuitFull && !capacityAsked && (
          <button
            type="button"
            className="btn btn-quiet"
            onClick={handleAskCapacity}
            disabled={asking}
          >
            {asking ? 'Asking…' : 'Ask for capacity'}
          </button>
        )}
        {capacityAsked && (
          <p className="text-2 text-sm" role="status">
            Asked — you'll get a notification when this circuit has room. You can close this and
            come back.
          </p>
        )}
      </div>
    </Modal>
  );
}
