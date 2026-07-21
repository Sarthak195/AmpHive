import { MapContainer, TileLayer, Marker, Popup } from 'react-leaflet';
import L from 'leaflet';
// Bundled from node_modules (was an unpkg.com <link> in index.html) so the
// CSP style-src needs no CDN origin. Safe to bundle: markers are divIcon
// (below), so Leaflet's default-icon PNG path detection is never exercised.
import 'leaflet/dist/leaflet.css';
import { AVAILABILITY_CSS_VAR, getPlugAvailability } from '../utils/plugAvailability';

// Colored dot markers (Available / In use / Offline) instead of Leaflet's
// default blue pin, so marker color encodes the same state as the Home
// legend + badges — see utils/plugAvailability.js for the shared mapping.
// `L.divIcon` renders arbitrary HTML, which avoids shipping separate PNG
// icon assets for each color.
const markerIcon = (state) => L.divIcon({
  className: 'plug-marker-icon',
  html: `<span class="plug-marker-dot" style="background: var(${AVAILABILITY_CSS_VAR[state]})"></span>`,
  iconSize: [18, 18],
  iconAnchor: [9, 9],
  popupAnchor: [0, -10],
});

export default function MapComponent({ plugs, onPlugSelect, selectLabel = 'Select' }) {
  // Center roughly on India, or a default location
  const center = [20.5937, 78.9629];
  const zoom = 4;

  // Only plot plugs with real coordinates (the backend fills these from
  // the plug's own lat/long, falling back to its gateway's location).
  // Previously this used Math.random() as a fallback, which scattered
  // markers AND moved them on every re-render. Plugs without a known
  // location are simply omitted from the map (they stay in the list).
  const located = plugs.filter((plug) => plug.latitude != null && plug.longitude != null);

  // Open on the markers, not the whole subcontinent: fit the initial view
  // to the plotted plugs (padded; maxZoom keeps a single plug from
  // rendering at rooftop level). Falls back to the India-wide default
  // when nothing has coordinates.
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
        {located
          .map((plug) => {
          const lat = plug.latitude;
          const lng = plug.longitude;
          const state = getPlugAvailability(plug);

          return (
            <Marker key={plug.id} position={[lat, lng]} icon={markerIcon(state)}>
              <Popup>
                <div style={{ textAlign: 'center' }}>
                  <strong>{plug.name}</strong><br />
                  <span style={{ fontSize: '0.85rem' }}>ID: {plug.id} | Status: {plug.status}</span><br />
                  {plug.status === 'available' && (
                    <button
                      className="btn btn-primary btn-sm"
                      style={{ marginTop: '0.5rem', padding: '0.2rem 0.5rem' }}
                      onClick={(e) => {
                        e.stopPropagation();
                        onPlugSelect(plug.id);
                      }}
                    >
                      {selectLabel}
                    </button>
                  )}
                </div>
              </Popup>
            </Marker>
          );
        })}
      </MapContainer>
    </div>
  );
}
