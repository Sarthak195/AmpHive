/**
 * MapComponent tests: marker color + popup state label come from the shared
 * availability classification (utils/plugAvailability.js — `--state-*`
 * tokens), never the raw `status` field. The popup's action button is gated
 * the same way: authed drivers only get an enabled "Charge" button when the
 * plug is actually available; anonymous visitors always get "Sign in to
 * charge" regardless of plug state (signing in never depends on it).
 *
 * react-leaflet is mocked out — these tests aren't about Leaflet's map
 * engine, just that MapComponent hands each Marker the right icon/position,
 * filters out plugs with no known location, gates the popup action
 * correctly, and forwards plug selection.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

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
  Popup: ({ children }) => <div data-testid="popup">{children}</div>,
  useMap: () => ({ setView: vi.fn() }),
}));

const PLUGS = [
  { id: 1, name: 'Available Plug', status: 'available', latitude: 1, longitude: 1, gateway_online: true, price_per_kwh: 6 },
  { id: 2, name: 'Occupied Plug', status: 'occupied', latitude: 2, longitude: 2, gateway_online: true },
  { id: 3, name: 'Offline-status Plug', status: 'offline', latitude: 3, longitude: 3, gateway_online: true },
  { id: 4, name: 'Unreachable-gateway Plug', status: 'available', latitude: 4, longitude: 4, gateway_online: false },
  { id: 5, name: 'No location Plug', status: 'available', latitude: null, longitude: null, gateway_online: true },
  { id: 6, name: 'Unpowered Plug', status: 'available', latitude: 6, longitude: 6, gateway_online: true, plug_powered: false },
  { id: 7, name: 'Maintenance Plug', status: 'maintenance', latitude: 7, longitude: 7, gateway_online: true },
];

describe('MapComponent marker colors', () => {
  it('colors an available plug with the success color', () => {
    render(<MapComponent plugs={[PLUGS[0]]} authed onSelectPlug={vi.fn()} />);
    expect(screen.getByTestId('marker').dataset.iconHtml).toContain('var(--state-available)');
  });

  it('colors an occupied ("in use") plug with the warning color', () => {
    render(<MapComponent plugs={[PLUGS[1]]} authed onSelectPlug={vi.fn()} />);
    expect(screen.getByTestId('marker').dataset.iconHtml).toContain('var(--state-in-use)');
  });

  it('colors an offline-status plug with the danger color', () => {
    render(<MapComponent plugs={[PLUGS[2]]} authed onSelectPlug={vi.fn()} />);
    expect(screen.getByTestId('marker').dataset.iconHtml).toContain('var(--state-offline)');
  });

  it('colors an "available" plug with an unreachable gateway as offline (danger), not available', () => {
    render(<MapComponent plugs={[PLUGS[3]]} authed onSelectPlug={vi.fn()} />);
    expect(screen.getByTestId('marker').dataset.iconHtml).toContain('var(--state-offline)');
  });

  it('omits plugs with no known location from the map', () => {
    render(<MapComponent plugs={[PLUGS[4]]} authed onSelectPlug={vi.fn()} />);
    expect(screen.queryByTestId('marker')).not.toBeInTheDocument();
  });

  it('colors a reachable-but-unpowered plug distinctly from both available and offline', () => {
    render(<MapComponent plugs={[PLUGS[5]]} authed onSelectPlug={vi.fn()} />);
    expect(screen.getByTestId('marker').dataset.iconHtml).toContain('var(--state-unpowered)');
  });

  it('colors a maintenance plug with the maintenance color', () => {
    render(<MapComponent plugs={[PLUGS[6]]} authed onSelectPlug={vi.fn()} />);
    expect(screen.getByTestId('marker').dataset.iconHtml).toContain('var(--state-maintenance)');
  });

  it('plots one marker per located plug', () => {
    render(<MapComponent plugs={PLUGS} authed onSelectPlug={vi.fn()} />);
    // 7 fixtures, 1 has no location → 6 markers.
    expect(screen.getAllByTestId('marker')).toHaveLength(6);
  });

  it('fits the initial view to the plotted plugs instead of the country-wide default', () => {
    render(<MapComponent plugs={PLUGS} authed onSelectPlug={vi.fn()} />);
    expect(screen.getByTestId('map-container').dataset.hasBounds).toBe('true');
  });

  it('falls back to the country-wide center/zoom when no plug has coordinates', () => {
    render(<MapComponent plugs={[PLUGS[4]]} authed onSelectPlug={vi.fn()} />);
    const container = screen.getByTestId('map-container');
    expect(container.dataset.hasBounds).toBe('false');
    expect(container.dataset.zoom).toBe('4');
  });
});

describe('MapComponent popup — signed-in driver', () => {
  it('shows an enabled Charge button for an available plug and forwards the plug id', async () => {
    const onSelectPlug = vi.fn();
    render(<MapComponent plugs={[PLUGS[0]]} authed onSelectPlug={onSelectPlug} />);
    const button = screen.getByRole('button', { name: 'Charge' });
    expect(button).toBeEnabled();
    await userEvent.click(button);
    expect(onSelectPlug).toHaveBeenCalledWith(1);
  });

  it('shows the price per kWh in the popup', () => {
    render(<MapComponent plugs={[PLUGS[0]]} authed onSelectPlug={vi.fn()} />);
    expect(screen.getByTestId('popup')).toHaveTextContent('/kWh');
  });

  it('does not offer a Charge button for an occupied plug — a note explains why instead', () => {
    render(<MapComponent plugs={[PLUGS[1]]} authed onSelectPlug={vi.fn()} />);
    expect(screen.queryByRole('button', { name: 'Charge' })).not.toBeInTheDocument();
    expect(screen.getByTestId('popup')).toHaveTextContent(/can.t start a charge/i);
  });

  it('gates on getPlugAvailability, not the raw status: an "available" plug behind an offline gateway gets no Charge button', () => {
    render(<MapComponent plugs={[PLUGS[3]]} authed onSelectPlug={vi.fn()} />);
    expect(screen.queryByRole('button', { name: 'Charge' })).not.toBeInTheDocument();
  });

  it('shows the plug state label next to the marker in the popup', () => {
    render(<MapComponent plugs={[PLUGS[1]]} authed onSelectPlug={vi.fn()} />);
    expect(screen.getByTestId('popup')).toHaveTextContent('In use');
  });
});

describe('MapComponent popup — anonymous visitor', () => {
  it('shows "Sign in to charge" for an available plug', async () => {
    const onSelectPlug = vi.fn();
    render(<MapComponent plugs={[PLUGS[0]]} authed={false} onSelectPlug={onSelectPlug} />);
    const button = screen.getByRole('button', { name: 'Sign in to charge' });
    await userEvent.click(button);
    expect(onSelectPlug).toHaveBeenCalledWith(1);
  });

  it('still shows "Sign in to charge" even when the plug is occupied — signing in never depends on plug state', () => {
    render(<MapComponent plugs={[PLUGS[1]]} authed={false} onSelectPlug={vi.fn()} />);
    expect(screen.getByRole('button', { name: 'Sign in to charge' })).toBeInTheDocument();
  });
});
