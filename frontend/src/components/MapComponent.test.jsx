/**
 * MapComponent tests: marker color encodes the shared availability
 * classification (see utils/plugAvailability.js — `--state-*` tokens)
 * instead of Leaflet's default blue pin for every plug regardless of
 * status.
 *
 * react-leaflet is mocked out — these tests aren't about Leaflet's map
 * engine, just that MapComponent hands each Marker the right icon/position,
 * filters out plugs with no known location, and forwards plug selection.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

import MapComponent from './MapComponent';

vi.mock('react-leaflet', () => ({
  MapContainer: ({ children, bounds, center, zoom }) => (
    <div
      data-testid="map-container"
      data-has-bounds={bounds ? 'true' : 'false'}
      data-center={center ? center.join(',') : ''}
      data-zoom={zoom ?? ''}
    >
      {children}
    </div>
  ),
  TileLayer: () => null,
  Marker: ({ position, icon, children }) => (
    <div data-testid="marker" data-position={position.join(',')} data-icon-html={icon?.options?.html}>
      {children}
    </div>
  ),
  Popup: ({ children }) => <div>{children}</div>,
}));

const PLUGS = [
  { id: 1, name: 'Available Plug', status: 'available', latitude: 1, longitude: 1, gateway_online: true },
  { id: 2, name: 'Occupied Plug', status: 'occupied', latitude: 2, longitude: 2, gateway_online: true },
  { id: 3, name: 'Offline-status Plug', status: 'offline', latitude: 3, longitude: 3, gateway_online: true },
  { id: 4, name: 'Unreachable-gateway Plug', status: 'available', latitude: 4, longitude: 4, gateway_online: false },
  { id: 5, name: 'No location Plug', status: 'available', latitude: null, longitude: null, gateway_online: true },
];

describe('MapComponent marker colors', () => {
  it('colors an available plug with the success color', () => {
    render(<MapComponent plugs={[PLUGS[0]]} onPlugSelect={vi.fn()} />);
    expect(screen.getByTestId('marker').dataset.iconHtml).toContain('var(--state-available)');
  });

  it('colors an occupied ("in use") plug with the warning color', () => {
    render(<MapComponent plugs={[PLUGS[1]]} onPlugSelect={vi.fn()} />);
    expect(screen.getByTestId('marker').dataset.iconHtml).toContain('var(--state-in-use)');
  });

  it('colors an offline-status plug with the danger color', () => {
    render(<MapComponent plugs={[PLUGS[2]]} onPlugSelect={vi.fn()} />);
    expect(screen.getByTestId('marker').dataset.iconHtml).toContain('var(--state-offline)');
  });

  it('colors an "available" plug with an unreachable gateway as offline (danger), not available', () => {
    render(<MapComponent plugs={[PLUGS[3]]} onPlugSelect={vi.fn()} />);
    expect(screen.getByTestId('marker').dataset.iconHtml).toContain('var(--state-offline)');
  });

  it('omits plugs with no known location from the map', () => {
    render(<MapComponent plugs={[PLUGS[4]]} onPlugSelect={vi.fn()} />);
    expect(screen.queryByTestId('marker')).not.toBeInTheDocument();
  });

  it('plots one marker per located plug', () => {
    render(<MapComponent plugs={PLUGS} onPlugSelect={vi.fn()} />);
    // 5 fixtures, 1 has no location → 4 markers.
    expect(screen.getAllByTestId('marker')).toHaveLength(4);
  });

  it('fits the initial view to the plotted plugs instead of the country-wide default', () => {
    render(<MapComponent plugs={PLUGS} onPlugSelect={vi.fn()} />);
    expect(screen.getByTestId('map-container').dataset.hasBounds).toBe('true');
  });

  it('falls back to the country-wide center/zoom when no plug has coordinates', () => {
    render(<MapComponent plugs={[PLUGS[4]]} onPlugSelect={vi.fn()} />);
    const container = screen.getByTestId('map-container');
    expect(container.dataset.hasBounds).toBe('false');
    expect(container.dataset.zoom).toBe('4');
  });
});
