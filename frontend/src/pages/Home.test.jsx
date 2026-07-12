/**
 * Home page tests: QR/deep-link start (`?plug=` prefill, the auth gate that
 * preserves it through login, and the "unknown id" notice) plus the map/list
 * availability + group filters with their live legend counts.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Routes, Route } from 'react-router-dom';

import Home from './Home';
import api from '../api/client';
import { useAuth } from '../contexts/AuthContext';
import { useSession } from '../contexts/SessionContext';
import { useConfig } from '../contexts/ConfigContext';

vi.mock('../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));
vi.mock('../contexts/AuthContext', () => ({ useAuth: vi.fn() }));
vi.mock('../contexts/SessionContext', () => ({ useSession: vi.fn() }));
vi.mock('../contexts/ConfigContext', () => ({ useConfig: vi.fn() }));
vi.mock('../contexts/WalletContext', () => ({
  useWallet: () => ({ balance: 0, refreshBalance: vi.fn() }),
}));
// MapComponent renders real Leaflet — stub it so these tests exercise
// Home's own filter/legend logic and just verify what gets handed down.
vi.mock('../components/MapComponent', () => ({
  default: ({ plugs }) => (
    <div data-testid="map-mock">{plugs.map((p) => p.id).join(',')}</div>
  ),
}));

const DRIVER = { email: 'driver@amphive.test', role: 'driver', coin_balance: 500 };

// One plug per availability bucket, across two named groups plus one
// ungrouped: available=1 (id 1 — id 3 is status=available but its gateway
// is offline, so it lands in the offline bucket), in_use=1 (id 2),
// offline=2 (id 3 via gateway_online, id 4 via status).
const PLUGS = [
  { id: 1, name: 'Lobby Plug', status: 'available', current_power_w: 0, plug_model: 'tapo_p110', group_name: 'Sunrise Apartments', latitude: 12.9, longitude: 77.6, gateway_online: true },
  { id: 2, name: 'Garage Plug', status: 'occupied', current_power_w: 1400, plug_model: 'tapo_p110', group_name: 'Sunrise Apartments', latitude: 12.91, longitude: 77.61, gateway_online: true },
  { id: 3, name: 'Rooftop Plug', status: 'available', current_power_w: 0, plug_model: 'tapo_p110', group_name: null, latitude: 12.92, longitude: 77.62, gateway_online: false },
  { id: 4, name: 'Basement Plug', status: 'offline', current_power_w: 0, plug_model: 'tapo_p110', group_name: 'Downtown Mall', latitude: null, longitude: null, gateway_online: true },
];

const renderHome = (initialEntry = '/') =>
  render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <Routes>
        <Route path="/" element={<Home />} />
        <Route path="/login" element={<div>login page</div>} />
      </Routes>
    </MemoryRouter>
  );

beforeEach(() => {
  vi.clearAllMocks();
  useSession.mockReturnValue({
    startSession: vi.fn(),
    activeSessions: [],
    switchSession: vi.fn(),
    error: null,
    socket: null,
  });
  useConfig.mockReturnValue({ coins_per_kwh: 5, min_start_balance_coins: 50 });
  api.get.mockResolvedValue([]);
});

describe('Home — QR / deep-link start', () => {
  it('prefills the Plug ID input from ?plug= and focuses the Start Charging card', async () => {
    useAuth.mockReturnValue({ user: DRIVER });
    api.get.mockResolvedValue(PLUGS);
    renderHome('/?plug=42');

    const input = screen.getByPlaceholderText('Enter Plug ID (e.g. 1)');
    expect(input).toHaveValue('42');
    expect(input).toHaveFocus();

    // Let the async plugs fetch resolve inside this test's act() scope.
    await screen.findByText('Lobby Plug');
  });

  it('does not prefill the input when there is no ?plug= param', async () => {
    useAuth.mockReturnValue({ user: DRIVER });
    api.get.mockResolvedValue(PLUGS);
    renderHome('/');
    await screen.findByText('Lobby Plug');
    expect(screen.getByPlaceholderText('Enter Plug ID (e.g. 1)')).toHaveValue('');
  });

  it('sends an anonymous visitor straight to /login, preserving the ?plug= target', () => {
    useAuth.mockReturnValue({ user: null });
    renderHome('/?plug=42');
    expect(screen.getByText('login page')).toBeInTheDocument();
    expect(api.get).not.toHaveBeenCalled();
  });

  it('shows a small notice for an unknown/inaccessible plug id, without blocking the page', async () => {
    useAuth.mockReturnValue({ user: DRIVER });
    api.get.mockResolvedValue(PLUGS); // no id 999 in the fixture
    renderHome('/?plug=999');

    expect(
      await screen.findByText(/Plug 999 from your link wasn't found or isn't available/)
    ).toBeInTheDocument();
    // Home still renders normally alongside the notice.
    expect(screen.getByText('Start Charging')).toBeInTheDocument();
    expect(screen.getByPlaceholderText('Enter Plug ID (e.g. 1)')).toHaveValue('999');
  });

  it('clears the notice once the driver edits the prefilled value', async () => {
    useAuth.mockReturnValue({ user: DRIVER });
    api.get.mockResolvedValue(PLUGS);
    renderHome('/?plug=999');
    await screen.findByText(/wasn't found/);

    await userEvent.type(screen.getByPlaceholderText('Enter Plug ID (e.g. 1)'), '1');
    expect(screen.queryByText(/wasn't found/)).not.toBeInTheDocument();
  });
});

describe('Home — map legend + availability/group filters', () => {
  beforeEach(() => {
    useAuth.mockReturnValue({ user: DRIVER });
    api.get.mockResolvedValue(PLUGS);
  });

  it('legend counts match the fixture data', async () => {
    renderHome('/');
    await screen.findByText('Lobby Plug');

    expect(screen.getByText('Available (1)')).toBeInTheDocument();
    expect(screen.getByText('In use (1)')).toBeInTheDocument();
    expect(screen.getByText('Offline (2)')).toBeInTheDocument();
  });

  it('the availability filter reduces the rendered plug list and updates the legend', async () => {
    renderHome('/');
    await screen.findByText('Lobby Plug');
    expect(screen.getByText('Garage Plug')).toBeInTheDocument();
    expect(screen.getByText('Rooftop Plug')).toBeInTheDocument();
    expect(screen.getByText('Basement Plug')).toBeInTheDocument();

    await userEvent.selectOptions(screen.getByLabelText('Filter by availability'), 'available');

    expect(screen.getByText('Lobby Plug')).toBeInTheDocument();
    expect(screen.queryByText('Garage Plug')).not.toBeInTheDocument();
    // Rooftop Plug has status=available but its gateway is offline, so the
    // "available" filter correctly excludes it too.
    expect(screen.queryByText('Rooftop Plug')).not.toBeInTheDocument();
    expect(screen.queryByText('Basement Plug')).not.toBeInTheDocument();
    expect(screen.getByText('1 of 4 plugs')).toBeInTheDocument();

    // Legend now reflects the filtered set.
    expect(screen.getByText('Available (1)')).toBeInTheDocument();
    expect(screen.getByText('In use (0)')).toBeInTheDocument();
    expect(screen.getByText('Offline (0)')).toBeInTheDocument();
  });

  it('the group filter reduces the rendered plug list and the map markers', async () => {
    renderHome('/');
    await screen.findByText('Lobby Plug');

    await userEvent.selectOptions(screen.getByLabelText('Filter by group'), 'Downtown Mall');

    expect(screen.queryByText('Lobby Plug')).not.toBeInTheDocument();
    expect(screen.queryByText('Garage Plug')).not.toBeInTheDocument();
    expect(screen.getByText('Basement Plug')).toBeInTheDocument();
    expect(screen.getByTestId('map-mock')).toHaveTextContent('4');
  });

  it('offers an "Ungrouped" filter option for plugs with no group_name', async () => {
    renderHome('/');
    await screen.findByText('Lobby Plug');

    await userEvent.selectOptions(screen.getByLabelText('Filter by group'), 'Ungrouped');

    expect(screen.getByText('Rooftop Plug')).toBeInTheDocument();
    expect(screen.queryByText('Lobby Plug')).not.toBeInTheDocument();
  });

  it('shows a no-charger empty state when no plugs are accessible at all (filters not offered)', async () => {
    api.get.mockResolvedValue([]);
    renderHome('/');
    expect(await screen.findByText('No chargers available yet.')).toBeInTheDocument();
    expect(screen.queryByLabelText('Filter by availability')).not.toBeInTheDocument();
  });
});
