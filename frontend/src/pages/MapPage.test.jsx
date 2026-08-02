/**
 * MapPage tests: data source switches on auth (public vs available
 * endpoint), the fetch → skeleton → ErrorState-with-retry / EmptyState
 * pattern, the "not on the map yet" note for chargers missing coordinates,
 * the 5-state legend, manual refresh, geolocation, and that a plug
 * selection routes anonymous visitors to login (with ?next= preserved) and
 * signed-in drivers to the home deep link. Marker/popup gating itself is
 * covered by MapComponent.test.jsx — MapComponent is mocked here so these
 * tests stay about the page's data + routing logic, not Leaflet.
 *
 * [Discovery] Also: the list search box (name/ID match, list-only — the map
 * keeps everything), the power/connector filters, nearest-first sorting once
 * geolocation resolves, and the per-row Navigate hand-off.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
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

describe('MapPage — discovery search + filters', () => {
  beforeEach(() => useAuth.mockReturnValue({ user: null }));

  it('the search box narrows the list (name match) but never the map', async () => {
    api.get.mockResolvedValue(PLUGS_FIXTURE);
    renderPage();
    await screen.findByText('Alpha Charger');

    await userEvent.type(screen.getByRole('searchbox', { name: /search chargers/i }), 'beta');
    expect(screen.queryByText('Alpha Charger')).not.toBeInTheDocument();
    expect(screen.getByText('Beta Charger')).toBeInTheDocument();
    // The map keeps plotting the full fetched array.
    expect(screen.getByTestId('map-component')).toHaveAttribute('data-plug-count', '2');
  });

  it('searching by numeric charger ID matches too', async () => {
    api.get.mockResolvedValue(PLUGS_FIXTURE);
    renderPage();
    await screen.findByText('Alpha Charger');

    await userEvent.type(screen.getByRole('searchbox', { name: /search chargers/i }), '2');
    expect(screen.getByText('Beta Charger')).toBeInTheDocument();
    expect(screen.queryByText('Alpha Charger')).not.toBeInTheDocument();
  });

  it('a no-match query shows the filtered-empty note, not the zero-data EmptyState', async () => {
    api.get.mockResolvedValue(PLUGS_FIXTURE);
    renderPage();
    await screen.findByText('Alpha Charger');

    await userEvent.type(screen.getByRole('searchbox', { name: /search chargers/i }), 'zzz');
    expect(screen.getByText(/no chargers match the current filters/i)).toBeInTheDocument();
    expect(screen.queryByText(/no chargers to show/i)).not.toBeInTheDocument();
  });

  it('the power filter buckets on rated_power_w; unset specs only match "Any"', async () => {
    api.get.mockResolvedValue([
      { id: 1, name: 'Slow Charger', status: 'available', latitude: 1, longitude: 1, gateway_online: true, price_per_kwh: 6, rated_power_w: 2300 },
      { id: 2, name: 'Fast Charger', status: 'available', latitude: 2, longitude: 2, gateway_online: true, price_per_kwh: 6, rated_power_w: 7400 },
      { id: 3, name: 'Unspecced Charger', status: 'available', latitude: 3, longitude: 3, gateway_online: true, price_per_kwh: 6 },
    ]);
    renderPage();
    await screen.findByText('Slow Charger');

    await userEvent.selectOptions(screen.getByLabelText('Filter by power'), 'lte3300');
    expect(screen.getByText('Slow Charger')).toBeInTheDocument();
    expect(screen.queryByText('Fast Charger')).not.toBeInTheDocument();
    expect(screen.queryByText('Unspecced Charger')).not.toBeInTheDocument();
  });

  it('the connector filter renders only when >1 distinct connector type exists', async () => {
    api.get.mockResolvedValue([
      { id: 1, name: 'A', status: 'available', latitude: 1, longitude: 1, gateway_online: true, connector_type: 'Type 2' },
      { id: 2, name: 'B', status: 'available', latitude: 2, longitude: 2, gateway_online: true, connector_type: 'Type 2' },
    ]);
    renderPage();
    await screen.findByText('A');
    expect(screen.queryByLabelText('Filter by connector')).not.toBeInTheDocument();

    api.get.mockResolvedValue([
      { id: 1, name: 'A', status: 'available', latitude: 1, longitude: 1, gateway_online: true, connector_type: 'Type 2' },
      { id: 2, name: 'B', status: 'available', latitude: 2, longitude: 2, gateway_online: true, connector_type: '3-pin 16A' },
    ]);
    await userEvent.click(screen.getByRole('button', { name: /refresh chargers/i }));
    expect(await screen.findByLabelText('Filter by connector')).toBeInTheDocument();

    await userEvent.selectOptions(screen.getByLabelText('Filter by connector'), '3-pin 16A');
    expect(screen.getByText('B')).toBeInTheDocument();
    expect(screen.queryByText('A')).not.toBeInTheDocument();
  });
});

describe('MapPage — distance sort + navigate', () => {
  beforeEach(() => useAuth.mockReturnValue({ user: null }));

  const LOCATED = [
    { id: 1, name: 'Far Charger', status: 'available', latitude: 20, longitude: 20, gateway_online: true, price_per_kwh: 6 },
    { id: 2, name: 'Near Charger', status: 'available', latitude: 10.001, longitude: 10.001, gateway_online: true, price_per_kwh: 6 },
  ];

  const rowNames = () =>
    screen.getAllByRole('listitem').map((li) => within(li).getByText(/Charger/).textContent);

  it('keeps fetch order without a location, sorts nearest-first once located', async () => {
    api.get.mockResolvedValue(LOCATED);
    const getCurrentPosition = vi.fn((success) => success({ coords: { latitude: 10, longitude: 10 } }));
    vi.stubGlobal('navigator', { ...navigator, geolocation: { getCurrentPosition } });
    renderPage();
    await screen.findByText('Far Charger');

    expect(rowNames()).toEqual(['Far Charger', 'Near Charger']); // fetch order

    await userEvent.click(screen.getByRole('button', { name: /use my location/i }));
    expect(rowNames()).toEqual(['Near Charger', 'Far Charger']); // nearest first
  });

  it('a located row gets a Navigate button that opens the Google Maps hand-off', async () => {
    api.get.mockResolvedValue(LOCATED);
    const open = vi.fn();
    vi.stubGlobal('open', open);
    renderPage();
    await screen.findByText('Far Charger');

    await userEvent.click(screen.getByRole('button', { name: 'Navigate to Near Charger' }));
    expect(open).toHaveBeenCalledWith(
      'https://www.google.com/maps/dir/?api=1&destination=10.001,10.001',
      '_blank',
      'noopener'
    );
  });

  it('rows without coordinates get no Navigate button', async () => {
    api.get.mockResolvedValue(PLUGS_FIXTURE); // Beta Charger has no coords
    renderPage();
    await screen.findByText('Beta Charger');
    expect(screen.getByRole('button', { name: 'Navigate to Alpha Charger' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Navigate to Beta Charger' })).not.toBeInTheDocument();
  });
});
