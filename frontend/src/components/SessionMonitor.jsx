/**
 * SessionMonitor — the live charging screen (redesign v3, C4).
 *
 * Anatomy:
 *   - status row     — Charging / Reconnecting… / Completed pill.
 *   - ChargeRing     — ₹ cost + kWh in the center; determinate toward a
 *                      kWh/time limit, indeterminate brand arc otherwise.
 *   - meter row      — kW now · elapsed · plug name, plus a "₹x/kWh now"
 *                      rate line from /api/plugs/{id}/tariff-preview (hidden
 *                      on fetch failure — never the global config rate).
 *   - notices        — .banner rows: stale telemetry, gateway alarms
 *                      (eventTypeCopy), low balance (with a Top up link).
 *   - limit target   — progress toward the auto-stop target + inline editor
 *                      (PATCH limits via SessionContext.updateLimits).
 *   - stop           — ConfirmDialog stating the kWh/₹ consequence.
 *
 * Robustness: the elapsed timer ticks client-side from the session start so it
 * never freezes between telemetry frames; staleness = server is_stale flag OR
 * no frame for STALE_AFTER_MS.
 */

import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { Target } from 'lucide-react';

import { useSession } from '../contexts/SessionContext';
import { useConfig } from '../contexts/ConfigContext';
import { useWallet } from '../contexts/WalletContext';
import api from '../api/client';
import { ConfirmDialog, Money, useToast } from './ui';
import ChargeRing from './ChargeRing';
import { eventTypeCopy, apiErrorCopy } from '../utils/statusCopy';
import { formatINR, coinsToINR, formatKw, formatKwh, formatDuration } from '../utils/money';

// Consider the live feed stale after this many ms without a telemetry frame.
// Matches the backend TELEMETRY_STALE_AFTER_SEC default (15 s).
const STALE_AFTER_MS = 15000;

// Live status pill — Charging / Reconnecting… / Completed.
const StatusPill = ({ isActive, isStale }) => {
  if (!isActive) {
    return (
      <span className="session-status text-3">
        <span className="dot" aria-hidden="true" /> Completed
      </span>
    );
  }
  if (isStale) {
    return (
      <span className="session-status text-2">
        <span className="dot dot-warn" aria-hidden="true" /> Reconnecting…
      </span>
    );
  }
  return (
    <span className="session-status text-2">
      <span className="dot dot-ok dot-live" aria-hidden="true" /> Charging
    </span>
  );
};

const SessionMonitor = () => {
  const {
    sessionData, sessionId, isActive, stopSession, updateLimits,
    lastFrameAt, focusedStartedAt, focusedLimits, alarms,
  } = useSession();
  const { coin_inr_rate } = useConfig();
  const { availableBalance } = useWallet();
  const toast = useToast();

  // 1 Hz clock so the elapsed timer and staleness check tick smoothly between
  // telemetry frames.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  // Per-plug tariff preview — the ONLY thing allowed to feed the rate line.
  // Hidden entirely when the fetch fails (404 = endpoint not there yet).
  const plugId = sessionData?.plug_id;
  const [tariff, setTariff] = useState(null);
  useEffect(() => {
    let cancelled = false;
    setTariff(null);
    if (plugId == null) return undefined;
    api
      .get(`/api/plugs/${plugId}/tariff-preview`)
      .then((res) => { if (!cancelled) setTariff(res); })
      .catch(() => { if (!cancelled) setTariff(null); });
    return () => { cancelled = true; };
  }, [plugId]);

  // Stop-charging confirmation.
  const [confirmStop, setConfirmStop] = useState(false);
  const [stopping, setStopping] = useState(false);

  // Inline editor for the focused session's stop conditions.
  const [editingLimit, setEditingLimit] = useState(false);
  const [editKwh, setEditKwh] = useState('');
  const [editHours, setEditHours] = useState('');
  const [savingLimit, setSavingLimit] = useState(false);
  const [limitError, setLimitError] = useState('');

  if (!isActive && !sessionData) return null;

  const energyNum = Number(sessionData?.energy_kwh) || 0;
  const costCoins = Number(sessionData?.cost_coins) || 0;
  const costInr = coinsToINR(costCoins, coin_inr_rate);
  const powerW = Number(sessionData?.power_w) || 0;

  // Elapsed: client-side from the session start; fall back to the server's
  // duration_sec if we don't yet have a start time.
  const elapsedSec = focusedStartedAt
    ? Math.max(0, Math.floor((now - new Date(focusedStartedAt).getTime()) / 1000))
    : (sessionData?.duration_sec || 0);

  // Stale when the server flags it OR no frame has arrived recently.
  const noFrameFor = lastFrameAt ? now - lastFrameAt : null;
  const isStale = isActive && (
    sessionData?.is_stale === true ||
    (noFrameFor !== null && noFrameFor > STALE_AFTER_MS)
  );

  // Low balance: the wallet is only debited on stop, so remaining ≈ balance −
  // accrued cost. Uses availableBalance so a hold from a second concurrent
  // session is respected. The kWh-left estimate uses the plug's price_now
  // when known.
  const walletBalance = Number(availableBalance) || 0;
  const remainingCoins = walletBalance - costCoins;
  const lowBalance = isActive && costCoins > 0 &&
    remainingCoins <= Math.max(10, walletBalance * 0.15);
  const priceNow = tariff?.price_now != null ? Number(tariff.price_now) : null;
  const kwhLeft = priceNow > 0 ? Math.max(0, remainingCoins) / priceNow : null;

  // The most recent alarm for this plug (last 2 minutes).
  const recentAlarm = (alarms || []).find(
    (a) => a.plug_id === plugId && now - (a.received_at || 0) < 120000
  );

  // Ring progress toward the auto-stop target — the backend stops at whichever
  // limit trips first, so show the nearer of the two. No limit → indeterminate.
  const limitMaxKwh = focusedLimits?.max_kwh;
  const limitMaxDurationSec = focusedLimits?.max_duration_seconds;
  const hasLimit = limitMaxKwh != null || limitMaxDurationSec != null;
  const kwhFrac = limitMaxKwh > 0 ? energyNum / Number(limitMaxKwh) : null;
  const timeFrac = limitMaxDurationSec > 0 ? elapsedSec / Number(limitMaxDurationSec) : null;
  const ringProgress = isActive && hasLimit
    ? Math.max(kwhFrac ?? 0, timeFrac ?? 0)
    : isActive ? null : 1;

  const openLimitEditor = () => {
    setEditKwh(limitMaxKwh != null ? String(limitMaxKwh) : '');
    setEditHours(
      limitMaxDurationSec != null
        ? String(Number((limitMaxDurationSec / 3600).toFixed(2)))
        : ''
    );
    setLimitError('');
    setEditingLimit(true);
  };

  // Send only the fields the driver actually set (>0). At least one required.
  const saveLimitEdit = async () => {
    const limits = {};
    const kwh = parseFloat(editKwh);
    const hours = parseFloat(editHours);
    if (!Number.isNaN(kwh) && kwh > 0) limits.max_kwh = kwh;
    if (!Number.isNaN(hours) && hours > 0) limits.max_duration_seconds = Math.round(hours * 3600);
    if (limits.max_kwh == null && limits.max_duration_seconds == null) {
      setLimitError('Enter an energy (kWh) or time (hours) limit.');
      return;
    }
    setSavingLimit(true);
    setLimitError('');
    try {
      await updateLimits(sessionId, limits);
      setEditingLimit(false);
      toast.ok('Charging limit updated.');
    } catch (err) {
      setLimitError(apiErrorCopy(err));
    } finally {
      setSavingLimit(false);
    }
  };

  const handleStop = async () => {
    setStopping(true);
    try {
      await stopSession();
      setConfirmStop(false);
      toast.ok('Charging stopped.');
    } catch (err) {
      setConfirmStop(false);
      toast.error(apiErrorCopy(err));
    } finally {
      setStopping(false);
    }
  };

  return (
    <section className="session-monitor card anim-fade" aria-label="Live charging session">
      <div className="session-monitor-top">
        <StatusPill isActive={isActive} isStale={isStale} />
      </div>

      {/* Hero: the ring with ₹ + kWh in the center. */}
      <ChargeRing progress={ringProgress}>
        <span className="session-cost">
          <Money coins={costCoins} rate={coin_inr_rate} />
        </span>
        <span className="session-cost-energy num text-2">{formatKwh(energyNum)}</span>
      </ChargeRing>

      {/* Screen-reader-only live cost announcement — only mutates (and so
          only announces) when the whole-rupee value changes, since telemetry
          ticks far more often than that. */}
      {isActive && (
        <div className="sr-only" aria-live="polite">
          {`Current cost ${Math.floor(costInr)} rupees, ${energyNum.toFixed(2)} kilowatt hours`}
        </div>
      )}

      {/* Meter row: kW now · elapsed · plug name (+ the tariff rate line). */}
      <dl className="session-meters">
        <div className="session-meter">
          <dt className="text-3 text-xs">Power now</dt>
          <dd className="num">{formatKw(powerW)}</dd>
        </div>
        <div className="session-meter">
          <dt className="text-3 text-xs">Elapsed</dt>
          <dd className="num">{formatDuration(elapsedSec)}</dd>
        </div>
        <div className="session-meter">
          <dt className="text-3 text-xs">Charger</dt>
          <dd>{sessionData?.plug_name || '—'}</dd>
        </div>
      </dl>
      {priceNow != null && (
        <p className="session-rate text-3 text-sm">
          <span className="num">{formatINR(coinsToINR(priceNow, coin_inr_rate))}</span>/kWh now
        </p>
      )}

      {/* Notices — only what's currently true. */}
      {(isStale || recentAlarm || lowBalance) && (
        <div className="session-notices" aria-live="polite">
          {isStale && (
            <div className="banner banner-warn">
              <p>
                <strong>Live readings paused</strong> — reconnecting to the charger. Your
                session keeps running and billing uses metered energy.
              </p>
            </div>
          )}
          {recentAlarm && (
            <div className="banner banner-danger">
              <p>
                <strong>{eventTypeCopy(recentAlarm.event_type)}</strong>
                {recentAlarm.detail ? ` — ${recentAlarm.detail}` : ''}
              </p>
            </div>
          )}
          {lowBalance && (
            <div className="banner banner-warn">
              <p>
                <strong>Low balance</strong> — about{' '}
                <span className="num">{formatINR(coinsToINR(Math.max(0, remainingCoins), coin_inr_rate))}</span>
                {kwhLeft != null && <> (≈ {formatKwh(kwhLeft)})</>} left before charging
                stops automatically. <Link to="/wallet?next=/session">Top up</Link> —
                charging continues while you top up.
              </p>
            </div>
          )}
        </div>
      )}

      {/* Auto-stop target + inline editor. */}
      {hasLimit && !editingLimit && (
        <div className="banner banner-info session-limit">
          <Target size={16} aria-hidden="true" />
          <p>
            Limit:{' '}
            {limitMaxKwh != null && (
              <strong className="num">
                {energyNum.toFixed(2)} / {Number(limitMaxKwh).toFixed(2)} kWh
              </strong>
            )}
            {limitMaxKwh != null && limitMaxDurationSec != null && ' · '}
            {limitMaxDurationSec != null && (
              <strong className="num">
                {formatDuration(elapsedSec)} / {formatDuration(limitMaxDurationSec)}
              </strong>
            )}
            {' · stops automatically'}
          </p>
          {isActive && updateLimits && (
            <button type="button" className="btn btn-quiet btn-sm" onClick={openLimitEditor}>
              Edit
            </button>
          )}
        </div>
      )}
      {isActive && !hasLimit && !editingLimit && updateLimits && (
        <div className="session-limit-add">
          <button type="button" className="btn btn-quiet btn-sm" onClick={openLimitEditor}>
            <Target size={16} aria-hidden="true" /> Set a charging limit
          </button>
        </div>
      )}

      {editingLimit && (
        <div className="well session-limit-editor">
          <h3 className="text-sm">Change charging limit</h3>
          <div className="session-limit-fields">
            <div className="field">
              <label className="field-label" htmlFor="edit-kwh">Energy limit (kWh)</label>
              <input
                id="edit-kwh"
                className="input"
                type="number"
                min="0"
                step="0.1"
                value={editKwh}
                onChange={(e) => setEditKwh(e.target.value)}
                placeholder="e.g. 5"
              />
            </div>
            <div className="field">
              <label className="field-label" htmlFor="edit-hours">Time limit (hours)</label>
              <input
                id="edit-hours"
                className="input"
                type="number"
                min="0"
                step="0.25"
                value={editHours}
                onChange={(e) => setEditHours(e.target.value)}
                placeholder="e.g. 2"
              />
            </div>
          </div>
          <p className="field-help">
            Takes effect within a few seconds. Raising a limit above what the charger was
            set at start may be capped by the charger until it updates.
          </p>
          {limitError && <p className="field-error" role="alert">{limitError}</p>}
          <div className="session-limit-actions">
            <button
              type="button"
              className="btn btn-quiet btn-sm"
              onClick={() => setEditingLimit(false)}
              disabled={savingLimit}
            >
              Cancel
            </button>
            <button
              type="button"
              className="btn btn-primary btn-sm"
              onClick={saveLimitEdit}
              disabled={savingLimit}
            >
              {savingLimit ? 'Saving…' : 'Save limit'}
            </button>
          </div>
        </div>
      )}

      {/* Stop — destructive/billing action, so it confirms the consequence. */}
      {isActive && (
        <button
          type="button"
          className="btn btn-danger btn-lg btn-full"
          onClick={() => setConfirmStop(true)}
        >
          Stop charging
        </button>
      )}

      <ConfirmDialog
        open={confirmStop}
        onClose={() => setConfirmStop(false)}
        onConfirm={handleStop}
        title="Stop charging?"
        body={`You'll be billed for ${formatKwh(energyNum)} — about ${formatINR(costInr)}.`}
        confirmLabel="Stop charging"
        tone="danger"
        busy={stopping}
        busyLabel="Stopping…"
      />
    </section>
  );
};

export default SessionMonitor;
