/**
 * MapComponent — the Leaflet map surface for pages/MapPage.jsx.
 * ===============================================================
 * Renders OSM tiles (bundled leaflet.css, no CDN — CSP img-src allows OSM
 * tile images) with one colored dot marker per plug that has coordinates.
 * Marker color + popup state label come from the shared availability
 * classification (utils/plugAvailability.js) — never the raw `status` field,
 * so a reachable-but-unpowered or gateway-offline plug never reads as
 * "available" here.
 *
 * The popup action is gated the same way: authed drivers only get an enabled
 * "Charge" button when the plug is actually available; everyone else (any
 * other state) sees the state instead of a dead-end button. Anonymous
 * visitors always get "Sign in to charge" — signing in doesn't depend on the
 * plug's state, only starting a session does.
 *
 * `flyTo` lets the page pan the (already-mounted) map imperatively — used
 * for the geolocate button — via a tiny useMap child, since MapContainer's
 * own `center`/`bounds` props only apply once, at creation.
 *
 * The popup is also where ReliabilityBadge (components/ui) gets mounted:
 * Leaflet only renders a Popup's children once it's actually opened, so
 * this is the natural lazy-fetch spot — one reliability request per plug a
 * visitor actually clicks into, never one per marker on the map. Auth-gated
 * (like Charge) since GET /api/plugs/{id}/reliability requires a signed-in
 * caller — an anonymous visitor would just draw a silent 401.
 *
 * [Discovery] The popup also carries a favorite star (authed users, when the
 * page passes `onToggleFavorite`) and a "Navigate" hand-off that opens the
 * device's maps app with directions (utils/navHandoff) — navigation needs no
 * account, so that one shows for everyone.
 */
import { useEffect } from 'react';
import { MapContainer, TileLayer, Marker, Popup, useMap } from 'react-leaflet';
import L from 'leaflet';
import { Flag, Navigation, Star } from 'lucide-react';
import 'leaflet/dist/leaflet.css';
import { StatusDot, Money, ReliabilityBadge } from './ui';
import { useConfig } from '../contexts/ConfigContext';
import { AVAILABILITY_CSS_VAR, getPlugAvailability } from '../utils/plugAvailability';
import { googleMapsDirUrl } from '../utils/navHandoff';
import '../pages/MapPage.css';

// Colored dot markers (Available / In use / No power / Offline / Maintenance)
// instead of Leaflet's default blue pin, so marker color encodes the same
// state as the legend + list + badges. `L.divIcon` renders arbitrary HTML,
// which avoids shipping separate PNG icon assets for each color.
const markerIcon = (state) => L.divIcon({
  className: 'plug-marker-icon',
  html: `<span class="plug-marker-dot" style="background: var(${AVAILABILITY_CSS_VAR[state]})"></span>`,
  iconSize: [18, 18],
  iconAnchor: [9, 9],
  popupAnchor: [0, -10],
});

const userLocationIcon = L.divIcon({
  className: 'user-location-icon',
  html: '<span class="user-location-dot"></span>',
  iconSize: [16, 16],
  iconAnchor: [8, 8],
});

/** Imperatively pans an already-mounted map — MapContainer's center/bounds
 *  props only take effect at creation, so a later "use my location" click
 *  needs useMap + setView instead. */
function ViewController({ flyTo }) {
  const map = useMap();
  useEffect(() => {
    if (flyTo) map.setView([flyTo.lat, flyTo.lng], flyTo.zoom ?? 15);
  }, [flyTo, map]);
  return null;
}

export default function MapComponent({ plugs, authed, onSelectPlug, onReportPlug, onToggleFavorite, flyTo, userLocation }) {
  const { coin_inr_rate: rate } = useConfig();

  // Center roughly on India, or a default location, when nothing has coordinates.
  const center = [20.5937, 78.9629];
  const zoom = 4;

  // Only plot plugs with real coordinates (the backend fills these from the
  // plug's own lat/long, falling back to its gateway's location). Plugs
  // without a known location are simply omitted from the map (the page's
  // list still shows them, with a "not on the map yet" note).
  const located = plugs.filter((plug) => plug.latitude != null && plug.longitude != null);

  // Open on the markers, not the whole subcontinent: fit the initial view to
  // the plotted plugs (padded; maxZoom keeps a single plug from rendering at
  // rooftop level). Falls back to the India-wide default when nothing has
  // coordinates.
  const bounds = located.length > 0
    ? L.latLngBounds(located.map((p) => [p.latitude, p.longitude]))
    : null;
  const viewProps = bounds
    ? { bounds, boundsOptions: { padding: [40, 40], maxZoom: 15 } }
    : { center, zoom };

  return (
    <div className="map-container">
      <MapContainer {...viewProps} style={{ height: '100%', width: '100%' }}>
        <TileLayer
          attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
          url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
        />
        <ViewController flyTo={flyTo} />
        {userLocation && (
          <Marker
            position={[userLocation.lat, userLocation.lng]}
            icon={userLocationIcon}
            interactive={false}
          />
        )}
        {located.map((plug) => {
          const state = getPlugAvailability(plug);
          const canCharge = authed && state === 'available';

          return (
            <Marker key={plug.id} position={[plug.latitude, plug.longitude]} icon={markerIcon(state)}>
              <Popup>
                <div className="map-popup">
                  <strong className="map-popup-name">{plug.name}</strong>
                  <StatusDot state={state} label />
                  {plug.price_per_kwh != null && (
                    <div className="map-popup-price">
                      <Money coins={plug.price_per_kwh} rate={rate} />
                      <span>/kWh</span>
                    </div>
                  )}
                  {authed && <ReliabilityBadge plugId={plug.id} />}
                  {authed && onToggleFavorite && (
                    <button
                      type="button"
                      className={`map-popup-fav${plug.is_favorite ? ' active' : ''}`}
                      aria-pressed={plug.is_favorite === true}
                      aria-label={
                        plug.is_favorite
                          ? `Remove ${plug.name} from favorites`
                          : `Add ${plug.name} to favorites`
                      }
                      onClick={() => onToggleFavorite(plug)}
                    >
                      <Star
                        size={14}
                        aria-hidden="true"
                        fill={plug.is_favorite ? 'currentColor' : 'none'}
                      />
                      {plug.is_favorite ? 'Favorited' : 'Favorite'}
                    </button>
                  )}
                  {authed ? (
                    canCharge ? (
                      <button
                        type="button"
                        className="btn btn-primary btn-sm btn-full"
                        onClick={() => onSelectPlug(plug.id)}
                      >
                        Charge
                      </button>
                    ) : (
                      <p className="map-popup-note">Can&rsquo;t start a charge right now</p>
                    )
                  ) : (
                    <button
                      type="button"
                      className="btn btn-primary btn-sm btn-full"
                      onClick={() => onSelectPlug(plug.id)}
                    >
                      Sign in to charge
                    </button>
                  )}
                  {authed && onReportPlug && (
                    <button
                      type="button"
                      className="btn btn-ghost btn-sm btn-full map-popup-report"
                      onClick={() => onReportPlug(plug)}
                    >
                      <Flag size={14} aria-hidden="true" />
                      Report a problem
                    </button>
                  )}
                  <button
                    type="button"
                    className="btn btn-quiet btn-sm btn-full"
                    onClick={() =>
                      window.open(
                        googleMapsDirUrl(plug.latitude, plug.longitude),
                        '_blank',
                        'noopener'
                      )
                    }
                  >
                    <Navigation size={14} aria-hidden="true" />
                    Navigate
                  </button>
                </div>
              </Popup>
            </Marker>
          );
        })}
      </MapContainer>
    </div>
  );
}
