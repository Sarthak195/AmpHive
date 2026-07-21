/**
 * MapPage tests: data source switches on auth (public vs available
 * endpoint), the fetch → skeleton → ErrorState-with-retry / EmptyState
 * pattern, the "not on the map yet" note for chargers missing coordinates,
 * the 5-state legend, manual refresh, geolocation, and that a plug
 * selection routes anonymous visitors to login (with ?next= preserved) and
 * signed-in drivers to the home deep link. Marker/popup gating itself is
 * covered by MapComponent.test.jsx — MapComponent is mocked here so these
 * tests stay about the page's data + routing logic, not Leaflet.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

import MapPage from './MapPage';
import api from '../api/client';
import { useAuth } from '../contexts/AuthContext';

vi.mock('../api/client', () => ({ default: { get: vi.fn() } }));
vi.mock('../contexts/AuthContext', () => ({ useAuth: vi.fn() }));

const navigateMock = vi.fn();
vi.mock('react-router-dom', async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, useNavigate: () => navigateMock };
});

vi.mock('../components/MapComponent', () => ({
  default: ({ plugs, authed, onSelectPlug, flyTo, userLocation }) => (
    <div
      data-testid="map-component"
      data-plug-count={plugs.length}
      data-authed={authed ? 'true' : 'false'}
      data-fly-to={flyTo ? `${flyTo.lat},${flyTo.lng}` : ''}
      data-user-location={userLocation ? `${userLocation.lat},${userLocation.lng}` : ''}
    >
      <button type="button" onClick={() => onSelectPlug(plugs[0]?.id)}>
        trigger-select
      </button>
    </div>
  ),
}));

const PLUGS_FIXTURE = [
  { id: 1, name: 'Alpha Charger', status: 'available', latitude: 1, longitude: 1, gateway_online: true, price_per_kwh: 6 },
  { id: 2, name: 'Beta Charger', status: 'occupied', latitude: null, longitude: null, gateway_online: true, price_per_kwh: 8 },
];

const renderPage = () =>
  render(
    <MemoryRouter>
      <MapPage />
    </MemoryRouter>
  );

beforeEach(() => {
  vi.clearAllMocks();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('MapPage — anonymous visitor', () => {
  beforeEach(() => useAuth.mockReturnValue({ user: null }));

  it('fetches the public discovery endpoint', async () => {
    api.get.mockResolvedValue(PLUGS_FIXTURE);
    renderPage();
    await waitFor(() => expect(api.get).toHaveBeenCalledWith('/api/plugs/public'));
  });

  it('shows a skeleton while loading, then the charger list', async () => {
    let resolveFetch;
    api.get.mockReturnValue(new Promise((resolve) => { resolveFetch = resolve; }));
    renderPage();
    expect(screen.getByText(/loading chargers/i)).toBeInTheDocument();
    resolveFetch(PLUGS_FIXTURE);
    expect(await screen.findByText('Alpha Charger')).toBeInTheDocument();
  });

  it('renders the 5-state legend', async () => {
    api.get.mockResolvedValue([]);
    renderPage();
    await screen.findByText(/no chargers to show/i);
    for (const label of ['Available', 'In use', 'No power', 'Offline', 'Under maintenance']) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
  });

  it('shows an ErrorState with retry on failure — never an empty state — and refetches on retry', async () => {
    api.get.mockRejectedValueOnce(new Error('boom'));
    renderPage();
    await screen.findByRole('alert');
    expect(screen.queryByText(/no chargers to show/i)).not.toBeInTheDocument();

    api.get.mockResolvedValueOnce(PLUGS_FIXTURE);
    await userEvent.click(screen.getByRole('button', { name: /retry/i }));
    expect(await screen.findByText('Alpha Charger')).toBeInTheDocument();
    expect(api.get).toHaveBeenCalledTimes(2);
  });

  it('shows an EmptyState for true zero-data', async () => {
    api.get.mockResolvedValue([]);
    renderPage();
    expect(await screen.findByText(/no chargers to show/i)).toBeInTheDocument();
  });

  it('notes chargers without map coordinates above the list', async () => {
    api.get.mockResolvedValue(PLUGS_FIXTURE);
    renderPage();
    expect(await screen.findByText(/1 charger not on the map yet/i)).toBeInTheDocument();
  });

  it('shows price per kWh for list rows and forwards the full plug list to the map', async () => {
    api.get.mockResolvedValue(PLUGS_FIXTURE);
    renderPage();
    await screen.findByText('Alpha Charger');
    expect(screen.getAllByText('/kWh').length).toBe(2);
    // MapComponent itself filters to located plugs — the page hands it everything.
    expect(screen.getByTestId('map-component')).toHaveAttribute('data-plug-count', '2');
  });

  it('re-fetches on manual refresh', async () => {
    api.get.mockResolvedValue(PLUGS_FIXTURE);
    renderPage();
    await screen.findByText('Alpha Charger');
    await userEvent.click(screen.getByRole('button', { name: /refresh chargers/i }));
    await waitFor(() => expect(api.get).toHaveBeenCalledTimes(2));
  });

  it('routes an anonymous selection to login with the plug preserved via ?next=', async () => {
    api.get.mockResolvedValue(PLUGS_FIXTURE);
    renderPage();
    await screen.findByText('Alpha Charger');
    await userEvent.click(screen.getByRole('button', { name: 'trigger-select' }));
    expect(navigateMock).toHaveBeenCalledWith(`/login?next=${encodeURIComponent('/?plug=1')}`);
  });

  it('tells the map it is unauthenticated', async () => {
    api.get.mockResolvedValue(PLUGS_FIXTURE);
    renderPage();
    await screen.findByText('Alpha Charger');
    expect(screen.getByTestId('map-component')).toHaveAttribute('data-authed', 'false');
  });
});

describe('MapPage — signed-in driver', () => {
  beforeEach(() => useAuth.mockReturnValue({ user: { id: 1, role: 'driver' } }));

  it('fetches the authenticated availability endpoint', async () => {
    api.get.mockResolvedValue(PLUGS_FIXTURE);
    renderPage();
    await waitFor(() => expect(api.get).toHaveBeenCalledWith('/api/plugs/available'));
  });

  it('routes a selection to the home deep link', async () => {
    api.get.mockResolvedValue(PLUGS_FIXTURE);
    renderPage();
    await screen.findByText('Alpha Charger');
    await userEvent.click(screen.getByRole('button', { name: 'trigger-select' }));
    expect(navigateMock).toHaveBeenCalledWith('/?plug=1');
  });
});

describe('MapPage — geolocation', () => {
  beforeEach(() => useAuth.mockReturnValue({ user: null }));

  it('passes the resolved location through to the map on success', async () => {
    api.get.mockResolvedValue(PLUGS_FIXTURE);
    const getCurrentPosition = vi.fn((success) => success({ coords: { latitude: 12.9, longitude: 77.6 } }));
    vi.stubGlobal('navigator', { ...navigator, geolocation: { getCurrentPosition } });
    renderPage();
    await screen.findByText('Alpha Charger');
    await userEvent.click(screen.getByRole('button', { name: /use my location/i }));
    expect(screen.getByTestId('map-component')).toHaveAttribute('data-user-location', '12.9,77.6');
    expect(screen.getByTestId('map-component')).toHaveAttribute('data-fly-to', '12.9,77.6');
  });

  it('degrades gracefully when location is denied — no crash, no location set', async () => {
    api.get.mockResolvedValue(PLUGS_FIXTURE);
    const getCurrentPosition = vi.fn((_success, error) => error(new Error('denied')));
    vi.stubGlobal('navigator', { ...navigator, geolocation: { getCurrentPosition } });
    renderPage();
    await screen.findByText('Alpha Charger');
    await userEvent.click(screen.getByRole('button', { name: /use my location/i }));
    expect(screen.getByTestId('map-component')).toHaveAttribute('data-user-location', '');
  });
});
