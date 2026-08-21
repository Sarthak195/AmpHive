/**
 * MapPage — charger discovery map (`/map`, no account needed).
 * ===============================================================
 * Full-height Leaflet map (components/MapComponent) with a floating list
 * panel (desktop: left card; mobile: bottom sheet) over the top. Data comes
 * from `GET /api/plugs/available` when signed in, `GET /api/plugs/public`
 * for anonymous visitors — both endpoints already carry `latitude`/
 * `longitude`, `price_per_kwh` and the fields `getPlugAvailability` needs.
 * Polls every 60s (only while the tab is visible) plus a manual refresh.
 *
 * The popup/CTA gating and marker coloring both live in MapComponent, keyed
 * off the shared `getPlugAvailability` classification — never the raw
 * `status` field — so a reachable-but-unpowered or gateway-offline plug is
 * never offered as chargeable here.
 *
 * [Discovery] The list panel adds a client-side search box + power / price /
 * connector / favorites-only filters (utils/plugSearch — shared with the
 * Dashboard) over the fetched array; they narrow the LIST, the map keeps
 * plotting everything. Once "use my location" resolves, the list sorts by
 * haversine distance (fetch order otherwise), and each located row gets a
 * "Navigate" hand-off to the device's maps app (utils/navHandoff).
 */
import { useCallback, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { RefreshCw, LocateFixed, MapPin, Navigation, Star } from 'lucide-react';
import api from '../api/client';
import { useAuth } from '../contexts/AuthContext';
import { useConfig } from '../contexts/ConfigContext';
import usePoll from '../hooks/usePoll';
import { PageHeader, ErrorState, EmptyState, Skeleton, StatusDot, Money, useToast } from '../components/ui';
import MapComponent from '../components/MapComponent';
import ReportProblemModal from '../components/ReportProblemModal';
import PlugReviewsModal from '../components/PlugReviewsModal';
import { AVAILABILITY_STATES, AVAILABILITY_LABELS, getPlugAvailability } from '../utils/plugAvailability';
import useDocumentMeta from '../hooks/useDocumentMeta';
import {
  POWER_BUCKETS,
  PRICE_BUCKETS,
  distinctConnectors,
  matchesPowerBucket,
  matchesPriceBucket,
  matchesQuery,
} from '../utils/plugSearch';
import { googleMapsDirUrl } from '../utils/navHandoff';
import { apiErrorCopy } from '../utils/statusCopy';
import './MapPage.css';

const EARTH_RADIUS_KM = 6371;

function distanceKm(a, b) {
  const dLat = ((b.lat - a.lat) * Math.PI) / 180;
  const dLng = ((b.lng - a.lng) * Math.PI) / 180;
  const lat1 = (a.lat * Math.PI) / 180;
  const lat2 = (b.lat * Math.PI) / 180;
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLng / 2) ** 2;
  return 2 * EARTH_RADIUS_KM * Math.asin(Math.sqrt(h));
}

function formatDistance(km) {
  return km < 1 ? `${Math.round(km * 1000)} m away` : `${km.toFixed(1)} km away`;
}

export default function MapPage() {
  // Public and indexable — the discovery surface a search for "EV charger
  // near me" should be able to land on. Everything else in the app defaults
  // to noindex (see hooks/useDocumentMeta.js).
  useDocumentMeta({
    title: 'Find a charger near you',
    description:
      'Browse AmpHive charging points on the map: where they are, what they cost per kWh, and which are free right now.',
    path: '/map',
    index: true,
  });

  const { user } = useAuth();
  const config = useConfig();
  const navigate = useNavigate();
  const toast = useToast();

  const [plugs, setPlugs] = useState([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState(null);
  const [userLocation, setUserLocation] = useState(null);
  const [flyTarget, setFlyTarget] = useState(null);
  const [geoBusy, setGeoBusy] = useState(false);
  const [reportPlug, setReportPlug] = useState(null);
  const [reviewsPlug, setReviewsPlug] = useState(null);

  // [Discovery] Client-side list search + filters (shared utils/plugSearch).
  const [search, setSearch] = useState('');
  const [powerFilter, setPowerFilter] = useState('');
  const [priceFilter, setPriceFilter] = useState('');
  const [connectorFilter, setConnectorFilter] = useState('');
  const [favoritesOnly, setFavoritesOnly] = useState(false);

  const fetchPlugs = useCallback(async () => {
    try {
      setError(null);
      const data = await api.get(user ? '/api/plugs/available' : '/api/plugs/public');
      setPlugs(data);
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [user]);

  usePoll(fetchPlugs, 60000, [Boolean(user)]);

  const handleRefresh = () => {
    setRefreshing(true);
    fetchPlugs();
  };

  const handleSelectPlug = useCallback(
    (plugId) => {
      if (user) {
        navigate(`/?plug=${plugId}`);
      } else {
        navigate(`/login?next=${encodeURIComponent(`/?plug=${plugId}`)}`);
      }
    },
    [user, navigate]
  );

  const handleGeolocate = () => {
    if (!('geolocation' in navigator)) {
      toast.info("Your browser doesn't support location — you can still browse the map.");
      return;
    }
    setGeoBusy(true);
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        const loc = { lat: pos.coords.latitude, lng: pos.coords.longitude };
        setUserLocation(loc);
        setFlyTarget({ ...loc, zoom: 14 });
        setGeoBusy(false);
      },
      () => {
        setGeoBusy(false);
        toast.info("Couldn't get your location — you can still browse the map.");
      },
      { enableHighAccuracy: true, timeout: 8000 }
    );
  };

  // [Favorites] Star toggle in the map popup (authed only) — the same
  // optimistic patch-then-rollback pattern as the Dashboard's toggleWatch.
  const toggleFavorite = useCallback(
    async (plug) => {
      const next = !plug.is_favorite;
      const patch = (is_favorite) => (prev) =>
        prev.map((p) => (p.id === plug.id ? { ...p, is_favorite } : p));
      setPlugs(patch(next));
      try {
        if (next) await api.post(`/api/plugs/${plug.id}/favorite`);
        else await api.delete(`/api/plugs/${plug.id}/favorite`);
      } catch (err) {
        setPlugs(patch(!next));
        toast.error(apiErrorCopy(err));
      }
    },
    [toast]
  );

  // [Reviews] After a write in the modal, patch this plug's aggregate in place
  // so the popup/list reflect the new avg + count without a full refetch.
  const patchReviewAgg = useCallback((plugId, agg) => {
    setPlugs((prev) =>
      prev.map((p) => (p.id === plugId ? { ...p, ...agg } : p))
    );
  }, []);

  const unlocatedCount = useMemo(
    () => plugs.filter((p) => p.latitude == null || p.longitude == null).length,
    [plugs]
  );

  const connectorOptions = useMemo(() => distinctConnectors(plugs), [plugs]);

  // [Discovery] The LIST view: search + filters narrow it, then (once the
  // driver has shared their location) it sorts nearest-first — plugs without
  // coordinates sink to the end. Without a location it keeps fetch order.
  // The map itself keeps plotting the full fetched array.
  const listPlugs = useMemo(() => {
    const filtered = plugs.filter((p) => {
      if (!matchesQuery(p, search)) return false;
      if (!matchesPowerBucket(p, powerFilter)) return false;
      if (!matchesPriceBucket(p, priceFilter)) return false;
      if (connectorFilter && p.connector_type !== connectorFilter) return false;
      if (favoritesOnly && p.is_favorite !== true) return false;
      return true;
    });
    if (!userLocation) return filtered;
    return filtered
      .map((p) => ({
        plug: p,
        dist:
          p.latitude != null && p.longitude != null
            ? distanceKm(userLocation, { lat: p.latitude, lng: p.longitude })
            : Infinity,
      }))
      .sort((a, b) => a.dist - b.dist)
      .map((x) => x.plug);
  }, [plugs, search, powerFilter, priceFilter, connectorFilter, favoritesOnly, userLocation]);

  const listFiltersActive = Boolean(
    search.trim() || powerFilter || priceFilter || connectorFilter || favoritesOnly
  );

  return (
    <main className="page map-page-root">
      <PageHeader
        eyebrow="Discover"
        title="Charger map"
        sub={
          user
            ? 'Find a nearby charger and start in a tap.'
            : 'Browse chargers near you — sign in to start charging.'
        }
      />

      <div className="map-shell">
        <div className="map-canvas">
          <MapComponent
            plugs={plugs}
            authed={Boolean(user)}
            onSelectPlug={handleSelectPlug}
            onReportPlug={setReportPlug}
            onToggleFavorite={user ? toggleFavorite : undefined}
            onOpenReviews={setReviewsPlug}
            flyTo={flyTarget}
            userLocation={userLocation}
          />
        </div>

        <button
          type="button"
          className="btn btn-quiet btn-icon map-geolocate-btn"
          onClick={handleGeolocate}
          disabled={geoBusy}
          aria-label="Use my location"
        >
          <LocateFixed size={18} aria-hidden="true" />
        </button>

        <div className="map-list-panel">
          <div className="map-list-header">
            <h2 className="map-list-title">Chargers</h2>
            <button
              type="button"
              className="btn btn-ghost btn-icon"
              onClick={handleRefresh}
              disabled={refreshing}
              aria-label="Refresh chargers"
            >
              <RefreshCw size={16} aria-hidden="true" />
            </button>
          </div>

          <div className="map-list-filters">
            <input
              className="input"
              type="search"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search by name, ID, or group"
              aria-label="Search chargers"
            />
            <div className="map-list-filter-row">
              <select
                className="select"
                value={powerFilter}
                onChange={(e) => setPowerFilter(e.target.value)}
                aria-label="Filter by power"
              >
                {POWER_BUCKETS.map((b) => (
                  <option key={b.value} value={b.value}>
                    {b.label}
                  </option>
                ))}
              </select>
              <select
                className="select"
                value={priceFilter}
                onChange={(e) => setPriceFilter(e.target.value)}
                aria-label="Filter by price"
              >
                {PRICE_BUCKETS.map((b) => (
                  <option key={b.value} value={b.value}>
                    {b.label}
                  </option>
                ))}
              </select>
              {connectorOptions.length > 1 && (
                <select
                  className="select"
                  value={connectorFilter}
                  onChange={(e) => setConnectorFilter(e.target.value)}
                  aria-label="Filter by connector"
                >
                  <option value="">Any connector</option>
                  {connectorOptions.map((c) => (
                    <option key={c} value={c}>
                      {c}
                    </option>
                  ))}
                </select>
              )}
              {user && (
                <button
                  type="button"
                  className={`btn btn-sm ${favoritesOnly ? 'btn-primary' : 'btn-ghost'}`}
                  aria-pressed={favoritesOnly}
                  onClick={() => setFavoritesOnly((v) => !v)}
                >
                  <Star size={14} aria-hidden="true" />
                  Favorites
                </button>
              )}
            </div>
          </div>

          <div className="map-legend">
            {AVAILABILITY_STATES.map((state) => (
              <span key={state} className="map-legend-item">
                <StatusDot state={state} />
                {AVAILABILITY_LABELS[state]}
              </span>
            ))}
          </div>

          <div className="map-list-body" aria-busy={loading} aria-live="polite">
            {loading ? (
              <div className="map-list-skeleton">
                <span className="sr-only">Loading chargers…</span>
                <Skeleton lines={2} />
                <Skeleton lines={2} />
                <Skeleton lines={2} />
              </div>
            ) : error ? (
              <ErrorState error={error} onRetry={fetchPlugs} title="Couldn't load chargers" />
            ) : plugs.length === 0 ? (
              <EmptyState
                icon={MapPin}
                title="No chargers to show"
                body="Check back soon — new chargers come online often."
              />
            ) : (
              <>
                {unlocatedCount > 0 && (
                  <p className="map-unlocated-note">
                    {unlocatedCount} charger{unlocatedCount === 1 ? '' : 's'} not on the map yet
                  </p>
                )}
                {listPlugs.length === 0 && listFiltersActive ? (
                  <p className="text-3 text-sm">No chargers match the current filters.</p>
                ) : (
                <ul className="map-list">
                  {listPlugs.map((plug) => {
                    const state = getPlugAvailability(plug);
                    const hasLocation = plug.latitude != null && plug.longitude != null;
                    const dist = userLocation && hasLocation
                      ? distanceKm(userLocation, { lat: plug.latitude, lng: plug.longitude })
                      : null;
                    return (
                      <li key={plug.id} className="map-list-row">
                        <span className="map-list-row-main">
                          {/* The dot is aria-hidden, and this row's only other
                              content is the charger name — so without srLabel a
                              screen-reader user gets the name and NO
                              availability at all. The legend above (:306) can
                              show the label visibly because it is a legend;
                              here it has to be sr-only to keep the row compact. */}
                          <StatusDot state={state} srLabel={AVAILABILITY_LABELS[state]} />
                          <span className="map-list-row-name">{plug.name}</span>
                        </span>
                        <span className="map-list-row-meta">
                          {plug.price_per_kwh != null && (
                            <span className="map-list-row-price">
                              <Money coins={plug.price_per_kwh} rate={config.coin_inr_rate} />
                              <span>/kWh</span>
                            </span>
                          )}
                          {dist != null && (
                            <span className="map-list-row-distance">{formatDistance(dist)}</span>
                          )}
                        </span>
                        {hasLocation && (
                          <button
                            type="button"
                            className="btn btn-quiet btn-icon map-list-row-nav"
                            aria-label={`Navigate to ${plug.name}`}
                            onClick={() =>
                              window.open(
                                googleMapsDirUrl(plug.latitude, plug.longitude),
                                '_blank',
                                'noopener'
                              )
                            }
                          >
                            <Navigation size={14} aria-hidden="true" />
                          </button>
                        )}
                      </li>
                    );
                  })}
                </ul>
                )}
              </>
            )}
          </div>
        </div>
      </div>

      <ReportProblemModal
        open={Boolean(reportPlug)}
        onClose={() => setReportPlug(null)}
        plugId={reportPlug?.id}
        plugName={reportPlug?.name}
      />

      <PlugReviewsModal
        open={Boolean(reviewsPlug)}
        onClose={() => setReviewsPlug(null)}
        plug={reviewsPlug}
        onSaved={(agg) => reviewsPlug && patchReviewAgg(reviewsPlug.id, agg)}
      />
    </main>
  );
}
