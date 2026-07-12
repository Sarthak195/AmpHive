import { MapContainer, TileLayer, Marker, Popup } from 'react-leaflet';

export default function MapComponent({ plugs, onPlugSelect }) {
  // Center roughly on India, or a default location
  const center = [20.5937, 78.9629]; 
  const zoom = 4;

  return (
    <div className="map-container">
      <MapContainer center={center} zoom={zoom} style={{ height: '100%', width: '100%' }}>
        <TileLayer
          attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
          url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
        />
        {plugs
          // Only plot plugs with real coordinates (the backend fills these from
          // the plug's own lat/long, falling back to its gateway's location).
          // Previously this used Math.random() as a fallback, which scattered
          // markers AND moved them on every re-render. Plugs without a known
          // location are simply omitted from the map (they stay in the list).
          .filter((plug) => plug.latitude != null && plug.longitude != null)
          .map((plug) => {
          const lat = plug.latitude;
          const lng = plug.longitude;

          return (
            <Marker key={plug.id} position={[lat, lng]}>
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
                      Select
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
