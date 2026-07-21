/**
 * Dashboard — the signed-in driver home ("/").
 * ============================================
 * Single column on mobile; main column + right rail on desktop:
 *
 * 1. Active-session banner per active session (live ₹ + kW for the focused
 *    one from SessionContext) with "Open session".
 * 2. "Charge now" card — big mono charger-ID input with a debounced (400ms)
 *    lookup via GET /api/plugs/{id} → inline preview (name, StatusDot, price)
 *    and a state-appropriate action. Handles the printed-QR `?plug=` deep
 *    link (autofill + auto-lookup, then clears the param); `?group=`
 *    preselects the group filter.
 * 3. "Up next" strip — the driver's upcoming reservations and WAITING queued
 *    charges, each with a live countdown and a ConfirmDialog'd cancel.
 * 4. "Your chargers" — [Your groups | Public] segments, state + group
 *    filters, "See map" link, and the PlugCard grid (5-state machine).
 * 5. Desktop rail — charging credit (₹-first) + "Add credit", and month stats
 *    from GET /api/me/stats (the stats block hides if that endpoint errors).
 *
 * Live updates: socket plug_status / plug_connectivity patches cards in
 * place; a 30s usePoll refetch is the catch-all backstop. Queued charging is
 * feature-flagged server-side — the queue affordance renders only when a
 * plug's payload advertises queue_available (exactly as before).
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { CalendarClock, Hourglass, MapPin, PlugZap, Users } from 'lucide-react';

import api from '../api/client';
import { useAuth } from '../contexts/AuthContext';
import { useSession } from '../contexts/SessionContext';
import { useConfig } from '../contexts/ConfigContext';
import { useWallet } from '../contexts/WalletContext';
import usePoll from '../hooks/usePoll';
import {
  ConfirmDialog,
  EmptyState,
  ErrorState,
  Money,
  PageHeader,
  Skeleton,
  StatusDot,
  useToast,
} from '../components/ui';
import PlugCard from '../components/PlugCard';
import ChargeSetupModal from '../components/ChargeSetupModal';
import ReserveModal from '../components/ReserveModal';
import { AVAILABILITY_STATES, getPlugAvailability } from '../utils/plugAvailability';
import { apiErrorCopy, plugStateHint, plugStateLabel } from '../utils/statusCopy';
import { coinsToINR, formatKw, formatKwh } from '../utils/money';
import { fmtTime, fmtWindow } from '../utils/reservationTime';
import './Dashboard.css';

// Sentinel for the group filter's "no group assigned" option — distinct from
// '' ("All groups"); group_name itself is never this.
const UNGROUPED = '__ungrouped__';

const LOOKUP_DEBOUNCE_MS = 400;

// "Expires in 2h 5m" / "Expires in 4m 12s" for a queued charge, relative to
// nowMs so the strip counts down live. Blank if unparseable.
const fmtCountdown = (iso, nowMs) => {
  const ms = new Date(iso).getTime() - nowMs;
  if (Number.isNaN(ms)) return '';
  if (ms <= 0) return 'Expiring…';
  const totalMin = Math.floor(ms / 60000);
  const h = Math.floor(totalMin / 60);
  const m = totalMin % 60;
  if (h > 0) return `Expires in ${h}h ${m}m`;
  const s = Math.floor((ms % 60000) / 1000);
  return `Expires in ${m}m ${s}s`;
};

const Dashboard = () => {
  const { user } = useAuth();
  const { activeSessions, sessionData, sessionId, switchSession, startSession, socket } =
    useSession();
  const { coin_inr_rate } = useConfig();
  const { balance } = useWallet();
  const toast = useToast();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();

  /* ---- plugs ------------------------------------------------------------ */
  const [plugs, setPlugs] = useState(null); // null = not loaded yet
  const [plugsError, setPlugsError] = useState(null);

  const fetchPlugs = useCallback(async () => {
    try {
      const data = await api.get('/api/plugs/available');
      setPlugs(Array.isArray(data) ? data : []);
      setPlugsError(null);
    } catch (err) {
      // Keep already-loaded cards on a background refresh failure; only the
      // initial load (or a Retry) surfaces the ErrorState.
      setPlugsError(err);
    }
  }, []);

  usePoll(fetchPlugs, 30_000);

  /* ---- up next: reservations + queued charges --------------------------- */
  const [reservations, setReservations] = useState([]);
  const [queued, setQueued] = useState([]);
  const [upNextError, setUpNextError] = useState(null);
  const [nowMs, setNowMs] = useState(() => Date.now());

  const fetchUpNext = useCallback(async () => {
    try {
      const [res, q] = await Promise.all([
        api.get('/api/reservations/my'),
        api.listQueuedCharges(),
      ]);
      setReservations(Array.isArray(res?.upcoming) ? res.upcoming : []);
      setQueued(Array.isArray(q) ? q : []);
      setUpNextError(null);
    } catch (err) {
      setUpNextError(err);
    }
  }, []);

  useEffect(() => {
    fetchUpNext();
  }, [fetchUpNext]);

  // Tick the queued-charge countdowns once a second — only while showing.
  useEffect(() => {
    if (queued.length === 0) return undefined;
    const id = setInterval(() => setNowMs(Date.now()), 1000);
    return () => clearInterval(id);
  }, [queued.length]);

  /* ---- month stats (rail) ------------------------------------------------ */
  // GET /api/me/stats may not exist yet (backend builds in parallel) — the
  // stats block simply hides on any error.
  const [stats, setStats] = useState(null);
  useEffect(() => {
    let cancelled = false;
    api
      .get('/api/me/stats')
      .then((data) => {
        if (!cancelled && data?.month) setStats(data);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  /* ---- live socket updates ---------------------------------------------- */
  useEffect(() => {
    if (!socket) return undefined;
    // Someone started/stopped a session: patch that card in place. A flip to
    // 'available' also fired-and-cleared any armed watch server-side.
    const handlePlugStatus = ({ plug_id, status }) => {
      setPlugs((prev) =>
        prev
          ? prev.map((p) =>
              p.id === plug_id
                ? { ...p, status, ...(status === 'available' ? { watching: false } : {}) }
                : p
            )
          : prev
      );
    };
    // Gateway connectivity push — faster than the next poll.
    const handlePlugConnectivity = ({ plug_id, gateway_online }) => {
      setPlugs((prev) =>
        prev ? prev.map((p) => (p.id === plug_id ? { ...p, gateway_online } : p)) : prev
      );
    };
    socket.on('plug_status', handlePlugStatus);
    socket.on('plug_connectivity', handlePlugConnectivity);
    return () => {
      socket.off('plug_status', handlePlugStatus);
      socket.off('plug_connectivity', handlePlugConnectivity);
    };
  }, [socket]);

  /* ---- segments + filters ------------------------------------------------ */
  const [seg, setSeg] = useState(null); // null = auto
  const [stateFilter, setStateFilter] = useState('');
  const [groupFilter, setGroupFilter] = useState('');

  /* ---- charge-now lookup ------------------------------------------------- */
  const [query, setQuery] = useState('');
  const [lookup, setLookup] = useState({ status: 'idle' });
  const plugInputRef = useRef(null);

  useEffect(() => {
    const q = query.trim();
    if (!q) {
      setLookup({ status: 'idle' });
      return undefined;
    }
    if (!/^\d+$/.test(q)) {
      setLookup({ status: 'notfound', query: q });
      return undefined;
    }
    let cancelled = false;
    setLookup({ status: 'loading' });
    const t = setTimeout(async () => {
      try {
        const plug = await api.get(`/api/plugs/${q}`);
        if (!cancelled) setLookup({ status: 'found', plug });
      } catch (err) {
        if (cancelled) return;
        if (err?.status === 404) setLookup({ status: 'notfound', query: q });
        else setLookup({ status: 'error', error: err });
      }
    }, LOOKUP_DEBOUNCE_MS);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [query]);

  // Printed-QR deep link `/?plug=<id>`: autofill + auto-lookup (the debounced
  // effect above does the fetch), focus the input, then clear the param.
  // `?group=` preselects the group filter (map "Charge" hand-off).
  const paramsHandled = useRef(false);
  useEffect(() => {
    if (paramsHandled.current) return;
    paramsHandled.current = true;
    const plugParam = searchParams.get('plug');
    const groupParam = searchParams.get('group');
    if (groupParam) setGroupFilter(groupParam);
    if (plugParam) {
      setQuery(plugParam);
      plugInputRef.current?.focus?.();
      const next = new URLSearchParams(searchParams);
      next.delete('plug');
      setSearchParams(next, { replace: true });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const loadedPlugs = useMemo(() => plugs ?? [], [plugs]);
  const hasAnyPrivate = useMemo(() => loadedPlugs.some((p) => p.is_private), [loadedPlugs]);
  const effectiveSeg = seg ?? (hasAnyPrivate ? 'yours' : 'public');

  const groupOptions = useMemo(() => {
    const names = new Set();
    let hasUngrouped = false;
    for (const p of loadedPlugs) {
      if (p.group_name) names.add(p.group_name);
      else hasUngrouped = true;
    }
    const options = [
      { value: '', label: 'All groups' },
      ...Array.from(names)
        .sort((a, b) => a.localeCompare(b))
        .map((name) => ({ value: name, label: name })),
    ];
    if (hasUngrouped) options.push({ value: UNGROUPED, label: 'Ungrouped' });
    return options;
  }, [loadedPlugs]);

  const filteredPlugs = useMemo(
    () =>
      loadedPlugs.filter((p) => {
        if (stateFilter && getPlugAvailability(p) !== stateFilter) return false;
        if (groupFilter === UNGROUPED) {
          if (p.group_name) return false;
        } else if (groupFilter && p.group_name !== groupFilter) {
          return false;
        }
        return true;
      }),
    [loadedPlugs, stateFilter, groupFilter]
  );

  const privatePlugs = useMemo(() => filteredPlugs.filter((p) => p.is_private), [filteredPlugs]);
  const publicPlugs = useMemo(() => filteredPlugs.filter((p) => !p.is_private), [filteredPlugs]);
  const segPlugs = effectiveSeg === 'yours' ? privatePlugs : publicPlugs;
  const filtersActive = Boolean(stateFilter || groupFilter);

  /* ---- charge / queue / reserve actions ---------------------------------- */
  const [setupPlug, setSetupPlug] = useState(null);
  const [setupMode, setSetupMode] = useState('start');
  const [reservePlug, setReservePlug] = useState(null);

  const openCharge = (plug) => {
    setSetupMode('start');
    setSetupPlug(plug);
  };
  const openQueue = (plug) => {
    setSetupMode('queue');
    setSetupPlug(plug);
  };

  // ChargeSetupModal's onConfirm — throws so the modal shows the error inline.
  const handleConfirmSetup = async (plugId, limits) => {
    if (setupMode === 'queue') {
      const body = { plug_id: Number(plugId) };
      if (limits?.max_kwh) body.max_kwh = limits.max_kwh;
      if (limits?.max_duration_seconds) body.max_duration_seconds = limits.max_duration_seconds;
      await api.queueCharge(body);
      toast.ok('Charge queued — it starts automatically when power returns.');
      fetchUpNext();
      return;
    }
    await startSession(plugId, limits);
    navigate('/session');
  };

  /* ---- notify-me watch toggle (optimistic) -------------------------------- */
  const toggleWatch = async (plug) => {
    const next = !plug.watching;
    const patch = (watching) => (prev) =>
      prev ? prev.map((p) => (p.id === plug.id ? { ...p, watching } : p)) : prev;
    setPlugs(patch(next));
    try {
      if (next) await api.post(`/api/plugs/${plug.id}/watch`);
      else await api.delete(`/api/plugs/${plug.id}/watch`);
    } catch (err) {
      setPlugs(patch(!next));
      toast.error(apiErrorCopy(err));
    }
  };

  /* ---- cancellations (ConfirmDialog) -------------------------------------- */
  const [cancelReservationTarget, setCancelReservationTarget] = useState(null);
  const [cancelQueuedTarget, setCancelQueuedTarget] = useState(null);
  const [cancelBusy, setCancelBusy] = useState(false);

  const confirmCancelReservation = async () => {
    setCancelBusy(true);
    try {
      await api.post(`/api/reservations/${cancelReservationTarget.id}/cancel`);
      toast.ok('Reservation cancelled.');
      setCancelReservationTarget(null);
      fetchUpNext();
      fetchPlugs(); // reserved_now badges may have changed
    } catch (err) {
      toast.error(apiErrorCopy(err));
    } finally {
      setCancelBusy(false);
    }
  };

  const confirmCancelQueued = async () => {
    setCancelBusy(true);
    try {
      await api.cancelQueuedCharge(cancelQueuedTarget.id);
      toast.ok('Queued charge cancelled.');
      setCancelQueuedTarget(null);
      fetchUpNext();
    } catch (err) {
      toast.error(apiErrorCopy(err));
    } finally {
      setCancelBusy(false);
    }
  };

  /* ---- charge-now preview action ------------------------------------------ */
  const renderLookup = () => {
    if (lookup.status === 'idle') return null;
    if (lookup.status === 'loading') return <Skeleton lines={2} />;
    if (lookup.status === 'notfound') {
      return (
        <p className="field-error" role="status">
          No charger with ID {lookup.query} — check the label.
        </p>
      );
    }
    if (lookup.status === 'error') {
      return (
        <p className="field-error" role="alert">
          {apiErrorCopy(lookup.error)}
        </p>
      );
    }

    const plug = lookup.plug;
    const state = getPlugAvailability(plug);
    const reservedByOther = plug.reserved_now === true && plug.reserved_now_by_me !== true;
    const startable = state === 'available' && !reservedByOther;
    const queueable = state === 'unpowered' && plug.queue_available === true;

    return (
      <div className="dash-lookup-preview" role="status">
        <div className="dash-lookup-info">
          <span className="dash-lookup-name">{plug.name || `Charger ${plug.id}`}</span>
          <StatusDot state={state} label />
          {plug.price_per_kwh != null && (
            <span className="text-2 text-sm num">
              <Money coins={plug.price_per_kwh} rate={coin_inr_rate} />
              /kWh
            </span>
          )}
        </div>
        <div className="dash-lookup-action">
          {startable && (
            <button type="button" className="btn btn-primary btn-sm" onClick={() => openCharge(plug)}>
              Start charging
            </button>
          )}
          {queueable && (
            <button type="button" className="btn btn-primary btn-sm" onClick={() => openQueue(plug)}>
              Queue charge
            </button>
          )}
          {!startable && !queueable && (
            <span className="text-3 text-sm">
              {reservedByOther && plug.reserved_until
                ? `Reserved until ${fmtTime(plug.reserved_until)}`
                : plugStateHint(state) || plugStateLabel(state)}
            </span>
          )}
          {(state === 'available' || state === 'in_use') && (
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => setReservePlug(plug)}>
              Reserve
            </button>
          )}
        </div>
      </div>
    );
  };

  const firstName = user?.full_name?.split(' ')[0];

  return (
    <main className="page dash">
      <PageHeader
        title={firstName ? `Welcome back, ${firstName}` : 'Welcome back'}
        sub="Charge, reserve, or check on your chargers."
      />

      <div className="dash-layout">
        <div className="dash-main">
          {/* 1 — active session banners */}
          {activeSessions.map((s) => {
            const live = s.session_id === sessionId ? sessionData : null;
            return (
              <section key={s.session_id} className="card dash-banner" aria-label="Active charging session">
                <div className="dash-banner-info">
                  <StatusDot tone="ok" live />
                  <div>
                    <p className="dash-banner-title">Charging at {s.plug_name || `charger ${s.plug_id}`}</p>
                    {live && (
                      <p className="text-2 text-sm num">
                        <Money coins={live.cost_coins} rate={coin_inr_rate} /> ·{' '}
                        {formatKw(live.power_w)}
                      </p>
                    )}
                    {/* Screen-reader-only live cost announcement — only mutates
                        (and so only announces) when the whole-rupee value
                        changes, since telemetry ticks far more often than that. */}
                    {live && (
                      <div className="sr-only" aria-live="polite">
                        {`Current cost ${Math.floor(coinsToINR(live.cost_coins, coin_inr_rate))} rupees, ${(Number(live.energy_kwh) || 0).toFixed(2)} kilowatt hours`}
                      </div>
                    )}
                  </div>
                </div>
                <button
                  type="button"
                  className="btn btn-primary btn-sm"
                  onClick={() => {
                    switchSession(s);
                    navigate('/session');
                  }}
                >
                  Open session
                </button>
              </section>
            );
          })}

          {/* 2 — charge now */}
          <section className="card dash-charge-now" aria-labelledby="dash-charge-now-h">
            <h2 id="dash-charge-now-h">Charge now</h2>
            <p className="text-2 text-sm">Enter the charger ID printed on the label.</p>
            <div className="field">
              <label className="field-label" htmlFor="dash-plug-id">
                Charger ID
              </label>
              <input
                id="dash-plug-id"
                ref={plugInputRef}
                className="input dash-plug-input"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="e.g. 12"
                inputMode="numeric"
                autoComplete="off"
              />
            </div>
            {renderLookup()}
          </section>

          {/* 3 — up next */}
          {upNextError ? (
            <div className="banner banner-warn">
              <span>Couldn't load your reservations and queued charges.</span>
              <button type="button" className="btn btn-quiet btn-sm" onClick={fetchUpNext}>
                Retry
              </button>
            </div>
          ) : (
            (reservations.length > 0 || queued.length > 0) && (
              <section aria-labelledby="dash-upnext-h" className="dash-upnext">
                <h2 id="dash-upnext-h">Up next</h2>
                <ul className="dash-upnext-list">
                  {reservations.map((r) => (
                    <li key={`r-${r.id}`} className="card card-tight dash-upnext-item">
                      <CalendarClock size={18} aria-hidden="true" className="dash-upnext-icon" />
                      <div className="dash-upnext-body">
                        <p className="dash-upnext-name">{r.plug_name || `Charger ${r.plug_id}`}</p>
                        <p className="text-3 text-sm">{fmtWindow(r.start_at, r.end_at)}</p>
                      </div>
                      <button
                        type="button"
                        className="btn btn-ghost btn-sm"
                        onClick={() => setCancelReservationTarget(r)}
                      >
                        Cancel
                      </button>
                    </li>
                  ))}
                  {queued.map((q) => (
                    <li key={`q-${q.id}`} className="card card-tight dash-upnext-item">
                      <Hourglass size={18} aria-hidden="true" className="dash-upnext-icon" />
                      <div className="dash-upnext-body">
                        <p className="dash-upnext-name">{q.plug_name || `Charger ${q.plug_id}`}</p>
                        <p className="text-3 text-sm">
                          Waiting for power · {fmtCountdown(q.expires_at, nowMs)}
                        </p>
                      </div>
                      <button
                        type="button"
                        className="btn btn-ghost btn-sm"
                        onClick={() => setCancelQueuedTarget(q)}
                      >
                        Cancel
                      </button>
                    </li>
                  ))}
                </ul>
              </section>
            )
          )}

          {/* 4 — your chargers */}
          <section aria-labelledby="dash-chargers-h" className="dash-chargers">
            <div className="dash-chargers-head">
              <h2 id="dash-chargers-h">Your chargers</h2>
              <Link to="/map" className="dash-map-link">
                <MapPin size={16} aria-hidden="true" />
                See map
              </Link>
            </div>

            {plugs === null && plugsError ? (
              <ErrorState error={plugsError} onRetry={fetchPlugs} title="Couldn't load your chargers" />
            ) : plugs === null ? (
              <div className="dash-plug-grid">
                {[1, 2, 3].map((i) => (
                  <div key={i} className="card card-tight">
                    <Skeleton lines={3} />
                  </div>
                ))}
              </div>
            ) : loadedPlugs.length === 0 ? (
              <EmptyState
                icon={PlugZap}
                title="No chargers yet"
                body="Join your society or office group to see its chargers, or find public ones on the map."
                action={
                  <div className="dash-empty-actions">
                    <Link to="/groups" className="btn btn-primary">
                      <Users size={16} aria-hidden="true" />
                      Join a group
                    </Link>
                    <Link to="/map" className="btn btn-quiet">
                      <MapPin size={16} aria-hidden="true" />
                      Browse the map
                    </Link>
                  </div>
                }
              />
            ) : (
              <>
                <div className="filter-bar">
                  <div className="seg" role="group" aria-label="Charger segments">
                    <button
                      type="button"
                      className={`seg-item${effectiveSeg === 'yours' ? ' active' : ''}`}
                      aria-pressed={effectiveSeg === 'yours'}
                      onClick={() => setSeg('yours')}
                    >
                      Your groups <span className="count-pill">{privatePlugs.length}</span>
                    </button>
                    <button
                      type="button"
                      className={`seg-item${effectiveSeg === 'public' ? ' active' : ''}`}
                      aria-pressed={effectiveSeg === 'public'}
                      onClick={() => setSeg('public')}
                    >
                      Public <span className="count-pill">{publicPlugs.length}</span>
                    </button>
                  </div>
                  <select
                    className="select"
                    value={stateFilter}
                    onChange={(e) => setStateFilter(e.target.value)}
                    aria-label="Filter by state"
                  >
                    <option value="">All states</option>
                    {AVAILABILITY_STATES.map((s) => (
                      <option key={s} value={s}>
                        {plugStateLabel(s)}
                      </option>
                    ))}
                  </select>
                  <select
                    className="select"
                    value={groupFilter}
                    onChange={(e) => setGroupFilter(e.target.value)}
                    aria-label="Filter by group"
                  >
                    {groupOptions.map((g) => (
                      <option key={g.value} value={g.value}>
                        {g.label}
                      </option>
                    ))}
                  </select>
                  <span className="filter-count">
                    {segPlugs.length} charger{segPlugs.length !== 1 ? 's' : ''}
                  </span>
                </div>

                {segPlugs.length === 0 ? (
                  filtersActive ? (
                    <p className="text-3 text-sm">None match the current filters.</p>
                  ) : effectiveSeg === 'yours' ? (
                    <EmptyState
                      icon={Users}
                      title="No group chargers yet"
                      body="If your society or office runs AmpHive chargers, join their group with an access code."
                      action={
                        <Link to="/groups" className="btn btn-primary">
                          Join a group
                        </Link>
                      }
                    />
                  ) : (
                    <p className="text-3 text-sm">No public chargers right now.</p>
                  )
                ) : (
                  <div className="dash-plug-grid">
                    {segPlugs.map((plug) => (
                      <PlugCard
                        key={plug.id}
                        plug={plug}
                        onCharge={openCharge}
                        onQueue={openQueue}
                        onReserve={setReservePlug}
                        onToggleWatch={toggleWatch}
                      />
                    ))}
                  </div>
                )}
              </>
            )}
          </section>
        </div>

        {/* 5 — desktop rail */}
        <aside className="dash-rail" aria-label="Charging credit and monthly stats">
          <section className="card dash-wallet">
            <p className="dash-wallet-label text-3 text-sm">Charging credit</p>
            <p className="dash-wallet-amount num">
              <Money coins={balance} rate={coin_inr_rate} />
            </p>
            <Link to="/credit" className="btn btn-primary btn-full">
              Add credit
            </Link>
            {stats?.month && (
              <dl className="dash-stats">
                <div className="dash-stat">
                  <dt className="text-3 text-xs">Energy this month</dt>
                  <dd className="num">{formatKwh(stats.month.energy_kwh)}</dd>
                </div>
                <div className="dash-stat">
                  <dt className="text-3 text-xs">Spent this month</dt>
                  <dd className="num">
                    <Money coins={stats.month.spend_coins} rate={coin_inr_rate} />
                  </dd>
                </div>
                <div className="dash-stat">
                  <dt className="text-3 text-xs">Sessions this month</dt>
                  <dd className="num">{stats.month.sessions}</dd>
                </div>
              </dl>
            )}
          </section>
        </aside>
      </div>

      {/* Charge / queue setup */}
      <ChargeSetupModal
        open={Boolean(setupPlug)}
        onClose={() => setSetupPlug(null)}
        plug={setupPlug}
        mode={setupMode}
        onConfirm={handleConfirmSetup}
      />

      {/* Reserve */}
      <ReserveModal
        open={Boolean(reservePlug)}
        onClose={() => setReservePlug(null)}
        plug={reservePlug}
        onReserved={() => {
          setReservePlug(null);
          fetchUpNext();
          fetchPlugs(); // reserved_now / next_reservation may have changed
        }}
      />

      {/* Cancel confirmations */}
      <ConfirmDialog
        open={Boolean(cancelReservationTarget)}
        onClose={() => setCancelReservationTarget(null)}
        onConfirm={confirmCancelReservation}
        title="Cancel this reservation?"
        body={
          cancelReservationTarget
            ? `Your slot on ${cancelReservationTarget.plug_name || `charger ${cancelReservationTarget.plug_id}`} (${fmtWindow(cancelReservationTarget.start_at, cancelReservationTarget.end_at)}) will be released.`
            : ''
        }
        confirmLabel="Cancel reservation"
        busy={cancelBusy}
      />
      <ConfirmDialog
        open={Boolean(cancelQueuedTarget)}
        onClose={() => setCancelQueuedTarget(null)}
        onConfirm={confirmCancelQueued}
        title="Cancel this queued charge?"
        body={
          cancelQueuedTarget
            ? `Your queued charge on ${cancelQueuedTarget.plug_name || `charger ${cancelQueuedTarget.plug_id}`} won't start when power returns.`
            : ''
        }
        confirmLabel="Cancel queued charge"
        busy={cancelBusy}
      />
    </main>
  );
};

export default Dashboard;
