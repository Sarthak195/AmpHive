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
 */
import { useCallback, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { RefreshCw, LocateFixed, MapPin } from 'lucide-react';
import api from '../api/client';
import { useAuth } from '../contexts/AuthContext';
import { useConfig } from '../contexts/ConfigContext';
import usePoll from '../hooks/usePoll';
import { PageHeader, ErrorState, EmptyState, Skeleton, StatusDot, Money, useToast } from '../components/ui';
import MapComponent from '../components/MapComponent';
import ReportProblemModal from '../components/ReportProblemModal';
import { AVAILABILITY_STATES, AVAILABILITY_LABELS, getPlugAvailability } from '../utils/plugAvailability';
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

  const unlocatedCount = useMemo(
    () => plugs.filter((p) => p.latitude == null || p.longitude == null).length,
    [plugs]
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
                <ul className="map-list">
                  {plugs.map((plug) => {
                    const state = getPlugAvailability(plug);
                    const hasLocation = plug.latitude != null && plug.longitude != null;
                    const dist = userLocation && hasLocation
                      ? distanceKm(userLocation, { lat: plug.latitude, lng: plug.longitude })
                      : null;
                    return (
                      <li key={plug.id} className="map-list-row">
                        <span className="map-list-row-main">
                          <StatusDot state={state} />
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
                      </li>
                    );
                  })}
                </ul>
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
    </main>
  );
}
