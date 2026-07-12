/**
 * AmpHive Home Page
 * =================
 * Dashboard showing wallet balance, plug ID entry, and available charger list.
 * Replaces the Phase 1 QR code flow with Plug ID entry + group-based access.
 *
 * QR / deep-link start (2026-07-12): visiting `/?plug=<id>` prefills the
 * Plug ID input below and scrolls/focuses it — still fully auth-gated (an
 * anonymous visitor is bounced to /login, which returns here afterward; see
 * ProtectedRoutes.jsx + Login.jsx for the shared "return to origin" state).
 *
 * Map legend + filters (2026-07-12): the driver plug list/map can be
 * filtered by availability (All / Available now / In use / Offline) and by
 * group name, with a small legend showing live counts per state — see
 * utils/plugAvailability.js for the shared 3-state classification also used
 * by MapComponent's marker colors.
 *
 * Sectioned charger list (2026-07-12): the flat list is now three collapsible
 * sections — "Your chargers" (private groups the driver joined: their
 * society/office plugs, the primary use case) first, then "Public chargers",
 * then the map at the bottom (public plugs only; collapsed by default — a
 * society resident already knows where their own charger is). Each section
 * header carries live per-status counts. The public list starts open only
 * for drivers with no private chargers.
 */

import { useState, useEffect, useMemo, useRef } from 'react';
import { useNavigate, useLocation, Navigate } from 'react-router-dom';
import WalletCard from '../components/WalletCard';
import { useAuth } from '../contexts/AuthContext';
import { useSession } from '../contexts/SessionContext';
import { useConfig } from '../contexts/ConfigContext';
import api from '../api/client';
import MapComponent from '../components/MapComponent';
import { AVAILABILITY_CSS_VAR, AVAILABILITY_LABELS, AVAILABILITY_STATES, getPlugAvailability } from '../utils/plugAvailability';

// Sentinel value for the group filter's "no group assigned" option — distinct
// from '' (which means "All groups") since group_name itself is never this.
const UNGROUPED = '__ungrouped__';

// Live per-status counts for a plug list — drives the section-header summary
// dots and the map legend.
const countByAvailability = (list) => {
  const counts = { available: 0, in_use: 0, offline: 0 };
  for (const p of list) counts[getPlugAvailability(p)] += 1;
  return counts;
};

// Collapsible charger section ("dropdown") — the header is a real <button>
// (keyboard/screen-reader operable, aria-expanded) showing the section name
// plus live per-status counts; the body holds the plug cards or the map.
const PlugSection = ({ title, icon, counts, open, onToggle, children }) => (
  <section className="plug-section">
    <button
      type="button"
      className="plug-section-header"
      onClick={onToggle}
      aria-expanded={open}
    >
      <span className="plug-section-title">
        <span aria-hidden="true">{icon}</span>
        <span>{title}</span>
      </span>
      <span className="flex items-center gap-3" style={{ flexShrink: 0 }}>
        {counts && (
          <span className="plug-section-counts">
            {AVAILABILITY_STATES.filter((s) => counts[s] > 0).map((s) => (
              <span key={s} className="map-legend-item">
                <span
                  className="map-legend-dot"
                  style={{ background: `var(${AVAILABILITY_CSS_VAR[s]})` }}
                />
                {counts[s]} {AVAILABILITY_LABELS[s].toLowerCase()}
              </span>
            ))}
          </span>
        )}
        <span className={`plug-section-chevron ${open ? 'open' : ''}`} aria-hidden="true">
          ▾
        </span>
      </span>
    </button>
    {open && <div className="plug-section-body">{children}</div>}
  </section>
);

const Home = () => {
  const { user } = useAuth();
  const { startSession, activeSessions, switchSession, error: sessionError, socket } = useSession();
  const { coins_per_kwh, min_start_balance_coins } = useConfig();
  const navigate = useNavigate();
  const location = useLocation();

  const [plugId, setPlugId] = useState('');
  const [plugs, setPlugs] = useState([]);
  const [loadingPlugs, setLoadingPlugs] = useState(false);
  const [startError, setStartError] = useState('');
  const [starting, setStarting] = useState(false);

  // Map/list filters (shared state — both the list and MapComponent's
  // markers read from the same filteredPlugs).
  const [availabilityFilter, setAvailabilityFilter] = useState('');
  const [groupFilter, setGroupFilter] = useState('');

  // Collapsible sections. Private starts open (it's the primary use case);
  // the map starts closed (deprioritized for society/office installs — and
  // Leaflet tiles aren't fetched at all while collapsed). `publicOpen` is
  // tri-state: null = auto (open only when the driver has no private
  // chargers), true/false once the driver toggles it.
  const [privateOpen, setPrivateOpen] = useState(true);
  const [publicOpen, setPublicOpen] = useState(null);
  const [mapOpen, setMapOpen] = useState(false);

  // QR / deep-link start
  const plugParam = new URLSearchParams(location.search).get('plug');
  const [deepLinkNotice, setDeepLinkNotice] = useState('');
  const startCardRef = useRef(null);
  const plugInputRef = useRef(null);

  const fetchPlugs = async () => {
    setLoadingPlugs(true);
    try {
      const data = await api.get('/api/plugs/available');
      setPlugs(data);
    } catch (err) {
      console.error('Failed to fetch plugs:', err);
    } finally {
      setLoadingPlugs(false);
    }
  };

  // Fetch available plugs when user is logged in
  useEffect(() => {
    if (!user) return;
    fetchPlugs();
  }, [user]);

  // Live plug-availability: when any plug flips OCCUPIED/AVAILABLE (someone
  // else started/stopped a session), update its badge in place so the list
  // stays current without a manual refresh.
  useEffect(() => {
    if (!socket) return;
    const handlePlugStatus = ({ plug_id, status }) => {
      setPlugs((prev) =>
        prev.map((p) => (p.id === plug_id ? { ...p, status } : p))
      );
    };
    socket.on('plug_status', handlePlugStatus);
    return () => socket.off('plug_status', handlePlugStatus);
  }, [socket]);

  // QR / deep-link start: prefill the Plug ID input from `?plug=<id>` and
  // bring the Start Charging card into view/focus so a driver arriving via
  // QR scan lands exactly where they need to be — they still have to tap
  // Start themselves, same as typing the ID by hand.
  useEffect(() => {
    if (!user || !plugParam) return;
    setPlugId(plugParam);
    startCardRef.current?.scrollIntoView?.({ behavior: 'smooth', block: 'start' });
    plugInputRef.current?.focus?.();
  }, [user, plugParam]);

  // Once the accessible-plugs list has loaded, flag a deep-linked id that
  // doesn't resolve to anything the driver can see (unknown id, typo, or a
  // private-group plug they haven't joined) — a small notice, not a hard
  // error; Home renders normally either way.
  useEffect(() => {
    if (!user || !plugParam || loadingPlugs || plugs.length === 0) return;
    const found = plugs.some((p) => String(p.id) === String(plugParam));
    setDeepLinkNotice(
      found ? '' : `Plug ${plugParam} from your link wasn't found or isn't available to your account — check the ID below.`
    );
  }, [user, plugParam, plugs, loadingPlugs]);

  // Group-name options for the filter select, derived from whatever plugs
  // are currently loaded. Plugs without a group (public/legacy) are grouped
  // under a distinct "Ungrouped" option so every plug stays reachable via
  // some filter combination.
  const groupOptions = useMemo(() => {
    const names = new Set();
    let hasUngrouped = false;
    for (const p of plugs) {
      if (p.group_name) names.add(p.group_name);
      else hasUngrouped = true;
    }
    const options = [
      { value: '', label: 'All groups' },
      ...Array.from(names).sort((a, b) => a.localeCompare(b)).map((name) => ({ value: name, label: name })),
    ];
    if (hasUngrouped) options.push({ value: UNGROUPED, label: 'Ungrouped' });
    return options;
  }, [plugs]);

  // Shared filter state drives both the list below and the map markers.
  const filteredPlugs = useMemo(() => plugs.filter((p) => {
    if (availabilityFilter && getPlugAvailability(p) !== availabilityFilter) return false;
    if (groupFilter === UNGROUPED) {
      if (p.group_name) return false;
    } else if (groupFilter && p.group_name !== groupFilter) {
      return false;
    }
    return true;
  }), [plugs, availabilityFilter, groupFilter]);

  // Private = plugs from closed groups this driver joined (their society/
  // office chargers); everything else — public groups and ungrouped/legacy
  // plugs — is "public". Older API data without is_private lands in public.
  const privatePlugs = useMemo(() => filteredPlugs.filter((p) => p.is_private), [filteredPlugs]);
  const publicPlugs = useMemo(() => filteredPlugs.filter((p) => !p.is_private), [filteredPlugs]);
  // Pre-filter check, so the "join a group" hint only shows when the driver
  // truly has no private chargers (not merely filtered them all out).
  const hasAnyPrivate = useMemo(() => plugs.some((p) => p.is_private), [plugs]);
  const publicIsOpen = publicOpen ?? !hasAnyPrivate;

  // Per-section header counts + the map legend (public plugs are what the
  // map plots) — all update live as the filters above narrow the set.
  const privateCounts = useMemo(() => countByAvailability(privatePlugs), [privatePlugs]);
  const publicCounts = useMemo(() => countByAvailability(publicPlugs), [publicPlugs]);

  const handleStartSession = async (e, targetPlugId = null) => {
    if (e) e.preventDefault();
    const pid = targetPlugId || plugId.trim();
    if (!pid) return;

    setStartError('');
    setStarting(true);

    try {
      await startSession(pid);
      navigate('/session');
    } catch (err) {
      setStartError(err.message);
    } finally {
      setStarting(false);
    }
  };

  // Status badge color mapping
  const statusColor = (status) => {
    switch (status) {
      case 'available': return 'badge-success';
      case 'occupied': return 'badge-warning';
      case 'offline': return 'badge-danger';
      default: return 'badge-primary';
    }
  };

  // One plug card — shared by the private and public sections. A plug is
  // startable only if it is available AND its gateway is reachable right
  // now. gateway_online defaults true for older API data; an unreachable
  // charger is shown but not clickable so the driver isn't sent into a 409
  // at start.
  const renderPlugCard = (plug, index) => {
    const unreachable = plug.gateway_online === false;
    const startable = plug.status === 'available' && !unreachable;
    return (
      <div
        key={plug.id}
        className="glass glass-card flex justify-between items-center animate-slide-up"
        style={{
          animationDelay: `${index * 0.06}s`,
          cursor: startable ? 'pointer' : 'default',
          opacity: unreachable ? 0.6 : 1,
          gap: '0.75rem',
        }}
        onClick={() => {
          if (startable) {
            handleStartSession(null, plug.id);
          }
        }}
      >
        <div style={{ minWidth: 0 }}>
          <div className="flex items-center gap-2" style={{ marginBottom: '0.25rem', flexWrap: 'wrap' }}>
            <span style={{ fontWeight: 600 }}>{plug.name}</span>
            <span className={`badge ${unreachable ? 'badge-danger' : statusColor(plug.status)}`}>
              {unreachable ? 'charger offline' : plug.status}
            </span>
          </div>
          <div className="flex items-center gap-3" style={{ fontSize: '0.85rem', color: 'var(--color-text-secondary)', flexWrap: 'wrap' }}>
            <span>ID: {plug.id}</span>
            <span>•</span>
            <span>{plug.plug_model}</span>
            {plug.group_name && (
              <>
                <span>•</span>
                <span>{plug.group_name}</span>
              </>
            )}
            {plug.price_per_kwh != null && (
              <>
                <span>•</span>
                <span>{plug.price_per_kwh} coins/kWh</span>
              </>
            )}
          </div>
        </div>
        {startable ? (
          <span style={{ color: 'var(--color-primary)', fontWeight: 600, fontSize: '0.9rem', flexShrink: 0 }}>
            Charge →
          </span>
        ) : unreachable ? (
          <span style={{ color: 'var(--color-text-muted)', fontSize: '0.8rem', flexShrink: 0 }}>
            Unreachable
          </span>
        ) : null}
      </div>
    );
  };

  // A QR/deep-link visit (`/?plug=<id>`) is still fully auth-gated: an
  // anonymous visitor is sent straight to /login (same as any ProtectedRoute)
  // rather than shown the generic "sign in" card, carrying this exact
  // location — including the query string — so Login returns them right
  // back here, prefilled, once they're signed in.
  if (!user && plugParam) {
    return <Navigate to="/login" replace state={{ from: location }} />;
  }

  return (
    <div className="page-container animate-fade-in">
      {/* Header */}
      <header className="text-center" style={{ marginBottom: '2rem' }}>
        <h1 style={{ color: 'var(--color-primary)', fontSize: '2.2rem', marginBottom: '0.25rem' }}>
          ⚡ AmpHive
        </h1>
        <p style={{ fontSize: '1.05rem' }}>Shared EV Charging Network</p>
      </header>


      {/* Active Session Banners — one per active session (max 2) */}
      {activeSessions.map((session) => (
        <div
          key={session.session_id}
          className="glass animate-slide-up"
          style={{
            padding: '1rem 1.25rem',
            background: 'rgba(235, 94, 40, 0.15)',
            border: '1px solid var(--color-primary)',
            borderRadius: 'var(--radius-md)',
            marginBottom: '1rem',
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
            flexWrap: 'wrap',
            gap: '0.75rem',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem', minWidth: 0 }}>
            <span style={{ fontSize: '1.25rem' }}>⚡</span>
            <div style={{ minWidth: 0 }}>
              <div style={{ fontWeight: 600 }}>Active charging session in progress</div>
              <div style={{ fontSize: '0.85rem', color: 'var(--color-text-secondary)' }}>
                Plug: {session.plug_name}
              </div>
            </div>
          </div>
          <button
            className="btn btn-accent btn-sm"
            style={{ whiteSpace: 'nowrap', flexShrink: 0 }}
            onClick={() => {
              switchSession(session);
              navigate('/session');
            }}
          >
            Resume Session
          </button>
        </div>
      ))}

      {/* Wallet Card */}
      <div style={{ marginBottom: '1.5rem' }}>
        <WalletCard />
      </div>

      {/* Start Charging Section */}
      {user && (
        <div ref={startCardRef} className="glass glass-panel animate-slide-up" style={{ marginBottom: '1.5rem' }}>
          <h3 style={{ marginBottom: '0.25rem' }}>Start Charging</h3>
          <p style={{ marginBottom: '1.25rem', fontSize: '0.9rem' }}>
            Enter a Plug ID to begin charging your vehicle.
          </p>

          {/* QR / deep-link notice: the ?plug= id from a scanned QR or
              shared link didn't resolve to a plug this account can see. */}
          {deepLinkNotice && (
            <div
              className="mt-2"
              style={{
                marginBottom: '1rem',
                padding: '0.65rem 0.85rem',
                borderRadius: 'var(--radius-md)',
                background: 'hsla(30, 90%, 55%, 0.12)',
                border: '1px solid hsla(30, 90%, 55%, 0.35)',
                color: 'var(--color-warning, #f0a020)',
                fontSize: '0.85rem',
              }}
            >
              {deepLinkNotice}
            </div>
          )}

          <form onSubmit={handleStartSession} className="flex gap-3">
            <input
              ref={plugInputRef}
              type="text"
              className="input"
              placeholder="Enter Plug ID (e.g. 1)"
              value={plugId}
              onChange={(e) => {
                setPlugId(e.target.value);
                if (deepLinkNotice) setDeepLinkNotice('');
              }}
              style={{ flex: 1 }}
            />
            <button
              type="submit"
              className="btn btn-accent"
              disabled={!plugId.trim() || starting}
            >
              {starting ? '...' : 'Start'}
            </button>
          </form>

          {(startError || sessionError) && (
            <div className="error-text mt-2">{startError || sessionError}</div>
          )}

          {/* Pricing hint: tariff + what the current balance covers, so the
              driver knows the cost before starting. */}
          {(() => {
            const balance = Number(user.coin_balance) || 0;
            const rate = coins_per_kwh || 5;
            const belowMin = balance < (min_start_balance_coins || 0);
            const estKwh = rate > 0 ? balance / rate : 0;
            return (
              <div style={{ marginTop: '0.75rem', fontSize: '0.82rem', color: 'var(--color-text-muted)' }}>
                Rate <strong style={{ color: 'var(--color-text-secondary)' }}>{rate} coins/kWh</strong>
                {' · '}your balance (<strong style={{ color: 'var(--color-text-secondary)' }}>{balance.toFixed(2)}</strong> coins)
                covers ≈ <strong style={{ color: 'var(--color-text-secondary)' }}>{estKwh.toFixed(1)} kWh</strong>.
                {belowMin && (
                  <span style={{ color: 'var(--color-warning, #f0a020)' }}>
                    {' '}Minimum {min_start_balance_coins} coins to start —{' '}
                    <span
                      style={{ color: 'var(--color-primary)', cursor: 'pointer', textDecoration: 'underline' }}
                      onClick={() => navigate('/topup')}
                    >
                      top up
                    </span>.
                  </span>
                )}
              </div>
            );
          })()}
        </div>
      )}

      {/* Chargers — sectioned list: the driver's private (society/office)
          chargers first, then public ones, with the map at the bottom. */}
      {user && (
        <div className="animate-slide-up" style={{ animationDelay: '0.15s' }}>
          <div className="flex justify-between items-center" style={{ marginBottom: '1rem' }}>
            <h3 style={{ margin: 0 }}>Chargers</h3>
            <button className="btn btn-ghost btn-sm" onClick={fetchPlugs}>
              Refresh
            </button>
          </div>

          {/* Filters — shared state feeds every section below (both lists
              and the map markers); there is no power-rating field anywhere
              in the plug data model, so (per spec) that filter is not
              offered here. */}
          {plugs.length > 0 && (
            <div className="filter-bar">
              <select
                value={availabilityFilter}
                onChange={(e) => setAvailabilityFilter(e.target.value)}
                aria-label="Filter by availability"
              >
                <option value="">All statuses</option>
                <option value="available">Available now</option>
                <option value="in_use">In use</option>
                <option value="offline">Offline</option>
              </select>
              <select
                value={groupFilter}
                onChange={(e) => setGroupFilter(e.target.value)}
                aria-label="Filter by group"
              >
                {groupOptions.map((g) => (
                  <option key={g.value} value={g.value}>{g.label}</option>
                ))}
              </select>
              <span style={{ color: 'var(--color-text-muted)', fontSize: '0.85rem' }}>
                {filteredPlugs.length} of {plugs.length} plug{plugs.length !== 1 ? 's' : ''}
              </span>
            </div>
          )}

          {loadingPlugs ? (
            <div className="flex flex-col gap-3">
              {[1, 2, 3].map(i => (
                <div key={i} className="skeleton" style={{ height: '72px', width: '100%' }} />
              ))}
            </div>
          ) : plugs.length === 0 ? (
            <div className="glass glass-panel text-center" style={{ padding: '2.5rem 2rem' }}>
              <p style={{ fontSize: '1.5rem', marginBottom: '0.5rem' }}>🔌</p>
              <p>No chargers available yet.</p>
              <p style={{ fontSize: '0.85rem', marginTop: '0.5rem' }}>
                Join a charger group to see available plugs, or enter a Plug ID directly above.
              </p>
              <button
                className="btn btn-primary btn-sm mt-4"
                onClick={() => navigate('/groups')}
              >
                Browse Groups
              </button>
            </div>
          ) : filteredPlugs.length === 0 ? (
            <div className="glass glass-panel text-center" style={{ padding: '2rem' }}>
              <p style={{ fontSize: '1.3rem', marginBottom: '0.5rem' }}>🔍</p>
              <p>No chargers match your filters.</p>
              <button
                className="btn btn-ghost btn-sm mt-4"
                onClick={() => { setAvailabilityFilter(''); setGroupFilter(''); }}
              >
                Clear filters
              </button>
            </div>
          ) : (
            <>
              {/* Private chargers — the driver's own society/office groups. */}
              <PlugSection
                title="Your chargers"
                icon="🏠"
                counts={privateCounts}
                open={privateOpen}
                onToggle={() => setPrivateOpen((o) => !o)}
              >
                {privatePlugs.length > 0 ? (
                  <div className="flex flex-col gap-3">
                    {privatePlugs.map(renderPlugCard)}
                  </div>
                ) : !hasAnyPrivate ? (
                  <div className="glass glass-panel text-center" style={{ padding: '1.5rem' }}>
                    <p style={{ fontSize: '0.9rem' }}>
                      No private chargers yet. If your society or office runs
                      AmpHive chargers, join their group with an access code.
                    </p>
                    <button
                      className="btn btn-primary btn-sm mt-4"
                      onClick={() => navigate('/groups')}
                    >
                      Join a group
                    </button>
                  </div>
                ) : (
                  <p style={{ fontSize: '0.85rem', color: 'var(--color-text-muted)' }}>
                    None match the current filters.
                  </p>
                )}
              </PlugSection>

              {/* Public chargers — public groups + ungrouped/legacy plugs. */}
              <PlugSection
                title="Public chargers"
                icon="🌐"
                counts={publicCounts}
                open={publicIsOpen}
                onToggle={() => setPublicOpen(!publicIsOpen)}
              >
                {publicPlugs.length > 0 ? (
                  <div className="flex flex-col gap-3">
                    {publicPlugs.map(renderPlugCard)}
                  </div>
                ) : (
                  <p style={{ fontSize: '0.85rem', color: 'var(--color-text-muted)' }}>
                    None match the current filters.
                  </p>
                )}
              </PlugSection>

              {/* Map — bottom of the page by design: society/office drivers
                  already know where their charger is. Public plugs only;
                  collapsed by default (tiles aren't fetched until opened). */}
              <PlugSection
                title="Map — public chargers"
                icon="🗺️"
                open={mapOpen}
                onToggle={() => setMapOpen((o) => !o)}
              >
                <div className="map-legend" aria-label="Plug availability legend">
                  {AVAILABILITY_STATES.map((state) => (
                    <span key={state} className="map-legend-item">
                      <span
                        className="map-legend-dot"
                        style={{ background: `var(${AVAILABILITY_CSS_VAR[state]})` }}
                      />
                      {AVAILABILITY_LABELS[state]} ({publicCounts[state]})
                    </span>
                  ))}
                </div>
                <MapComponent plugs={publicPlugs} onPlugSelect={(id) => handleStartSession(null, id)} />
              </PlugSection>
            </>
          )}
        </div>
      )}

      {/* Not logged in */}
      {!user && (
        <div className="glass glass-panel text-center animate-slide-up" style={{ padding: '3rem 2rem' }}>
          <p style={{ fontSize: '1.5rem', marginBottom: '0.75rem' }}>🔐</p>
          <h2 style={{ marginBottom: '0.5rem' }}>Sign in to get started</h2>
          <p style={{ marginBottom: '1.5rem' }}>
            Create an account to top up your wallet and start charging.
          </p>
          <button className="btn btn-primary btn-lg" onClick={() => navigate('/login', { state: { from: location } })}>
            Sign In
          </button>
        </div>
      )}
    </div>
  );
};

export default Home;
