import React from 'react';
import { MapContainer, TileLayer, Marker, Popup } from 'react-leaflet';

export default function MapComponent({ plugs, onPlugSelect }) {
  // Center roughly on India, or a default location
  const center = [20.5937, 78.9629]; 
  const zoom = 4;

  return (
    <div style={{ height: '300px', width: '100%', marginBottom: '1.5rem', borderRadius: '12px', overflow: 'hidden' }}>
      <MapContainer center={center} zoom={zoom} style={{ height: '100%', width: '100%' }}>
        <TileLayer
          attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
          url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
        />
        {plugs.map((plug) => {
          // Fallback location for the mock/test
          const lat = plug.latitude || (28.6139 + Math.random() * 2 - 1);
          const lng = plug.longitude || (77.2090 + Math.random() * 2 - 1);
          
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
