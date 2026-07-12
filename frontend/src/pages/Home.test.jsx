/**
 * Home page tests: QR/deep-link start (`?plug=` prefill, the auth gate that
 * preserves it through login, and the "unknown id" notice), the sectioned
 * charger list (private "Your chargers" first, "Public chargers" collapsed
 * by default when private ones exist, map at the bottom, collapsed), the
 * availability + group filters with their live legend counts, and the
 * "notify me when free" bell toggle on non-startable plug cards (optimistic
 * POST/DELETE against /api/plugs/{id}/watch, revert on failure, cleared by
 * a live plug_status→available flip).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, act } from '@testing-library/react';
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
// offline=2 (id 3 via gateway_online, id 4 via status). Sunrise Apartments
// is a private (joined) group → ids 1-2 land in "Your chargers"; Downtown
// Mall is a public group and id 3 is ungrouped → ids 3-4 are public.
const PLUGS = [
  { id: 1, name: 'Lobby Plug', status: 'available', current_power_w: 0, plug_model: 'tapo_p110', group_name: 'Sunrise Apartments', latitude: 12.9, longitude: 77.6, gateway_online: true, is_private: true },
  { id: 2, name: 'Garage Plug', status: 'occupied', current_power_w: 1400, plug_model: 'tapo_p110', group_name: 'Sunrise Apartments', latitude: 12.91, longitude: 77.61, gateway_online: true, is_private: true },
  { id: 3, name: 'Rooftop Plug', status: 'available', current_power_w: 0, plug_model: 'tapo_p110', group_name: null, latitude: 12.92, longitude: 77.62, gateway_online: false, is_private: false },
  { id: 4, name: 'Basement Plug', status: 'offline', current_power_w: 0, plug_model: 'tapo_p110', group_name: 'Downtown Mall', latitude: null, longitude: null, gateway_online: true, is_private: false },
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

describe('Home — private/public sections + map at the bottom', () => {
  beforeEach(() => {
    useAuth.mockReturnValue({ user: DRIVER });
    api.get.mockResolvedValue(PLUGS);
  });

  it('splits plugs into "Your chargers" (open) and "Public chargers" (collapsed when private ones exist)', async () => {
    renderHome('/');
    await screen.findByText('Lobby Plug');

    // Private section open: both Sunrise Apartments plugs visible.
    expect(screen.getByText('Garage Plug')).toBeInTheDocument();
    // Public section collapsed by default (this driver has private plugs).
    expect(screen.queryByText('Rooftop Plug')).not.toBeInTheDocument();
    expect(screen.queryByText('Basement Plug')).not.toBeInTheDocument();

    const publicHeader = screen.getByRole('button', { name: /Public chargers/ });
    expect(publicHeader).toHaveAttribute('aria-expanded', 'false');
    await userEvent.click(publicHeader);

    expect(screen.getByText('Rooftop Plug')).toBeInTheDocument();
    expect(screen.getByText('Basement Plug')).toBeInTheDocument();
  });

  it('section headers carry live per-status counts', async () => {
    renderHome('/');
    await screen.findByText('Lobby Plug');

    const privateHeader = screen.getByRole('button', { name: /Your chargers/ });
    // Private: Lobby available + Garage in use (zero-count states hidden).
    expect(privateHeader).toHaveTextContent('1 available');
    expect(privateHeader).toHaveTextContent('1 in use');
    // Public: Rooftop (gateway offline) + Basement (status offline).
    expect(screen.getByRole('button', { name: /Public chargers/ })).toHaveTextContent('2 offline');
  });

  it('map section sits collapsed at the bottom and plots public plugs when opened', async () => {
    renderHome('/');
    await screen.findByText('Lobby Plug');

    // Collapsed: no map rendered (tiles not fetched), no legend.
    expect(screen.queryByTestId('map-mock')).not.toBeInTheDocument();

    const mapHeader = screen.getByRole('button', { name: /Map — public chargers/ });
    await userEvent.click(mapHeader);

    // Public plugs only (ids 3, 4) — private society plugs stay off the map.
    expect(screen.getByTestId('map-mock')).toHaveTextContent('3,4');
    // Legend counts cover what the map shows: both public plugs are offline.
    expect(screen.getByText('Available (0)')).toBeInTheDocument();
    expect(screen.getByText('In use (0)')).toBeInTheDocument();
    expect(screen.getByText('Offline (2)')).toBeInTheDocument();
  });

  it('shows a join-a-group hint (and opens the public list) when the driver has no private chargers', async () => {
    api.get.mockResolvedValue(PLUGS.map((p) => ({ ...p, is_private: false })));
    renderHome('/');
    await screen.findByText('Lobby Plug'); // public section auto-opens

    expect(screen.getByText(/join their group with an access code/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Public chargers/ })).toHaveAttribute('aria-expanded', 'true');
  });

  it('the availability filter reduces the rendered plug list and updates the section counts', async () => {
    renderHome('/');
    await screen.findByText('Lobby Plug');
    expect(screen.getByText('Garage Plug')).toBeInTheDocument();

    await userEvent.selectOptions(screen.getByLabelText('Filter by availability'), 'available');

    expect(screen.getByText('Lobby Plug')).toBeInTheDocument();
    expect(screen.queryByText('Garage Plug')).not.toBeInTheDocument();
    expect(screen.getByText('1 of 4 plugs')).toBeInTheDocument();

    // Rooftop Plug has status=available but its gateway is offline, so the
    // "available" filter correctly excludes it from the public section too.
    await userEvent.click(screen.getByRole('button', { name: /Public chargers/ }));
    expect(screen.queryByText('Rooftop Plug')).not.toBeInTheDocument();
    expect(screen.getByText('None match the current filters.')).toBeInTheDocument();
  });

  it('the group filter reduces the rendered plug list and the map markers', async () => {
    renderHome('/');
    await screen.findByText('Lobby Plug');

    await userEvent.selectOptions(screen.getByLabelText('Filter by group'), 'Downtown Mall');

    expect(screen.queryByText('Lobby Plug')).not.toBeInTheDocument();
    expect(screen.queryByText('Garage Plug')).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: /Public chargers/ }));
    expect(screen.getByText('Basement Plug')).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: /Map — public chargers/ }));
    expect(screen.getByTestId('map-mock')).toHaveTextContent('4');
  });

  it('offers an "Ungrouped" filter option for plugs with no group_name', async () => {
    renderHome('/');
    await screen.findByText('Lobby Plug');

    await userEvent.selectOptions(screen.getByLabelText('Filter by group'), 'Ungrouped');
    await userEvent.click(screen.getByRole('button', { name: /Public chargers/ }));

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

describe('Home — "notify me when free" bell', () => {
  beforeEach(() => {
    useAuth.mockReturnValue({ user: DRIVER });
    api.get.mockResolvedValue(PLUGS);
  });

  it('shows the bell only on non-startable plugs', async () => {
    renderHome('/');
    await screen.findByText('Lobby Plug');
    // Open the public section so all four cards are rendered.
    await userEvent.click(screen.getByRole('button', { name: /Public chargers/ }));

    // Startable (id 1, available + gateway online): no bell, "Charge →".
    expect(
      screen.queryByRole('button', { name: /Notify me when Lobby Plug is free/ })
    ).not.toBeInTheDocument();
    // Occupied (id 2), gateway-offline (id 3), and status-offline (id 4): bells.
    expect(
      screen.getByRole('button', { name: /Notify me when Garage Plug is free/ })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /Notify me when Rooftop Plug is free/ })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /Notify me when Basement Plug is free/ })
    ).toBeInTheDocument();
  });

  it('arms a watch optimistically: POSTs and flips the bell to Watching', async () => {
    api.post.mockResolvedValue({ status: 'watching', plug_id: 2, watching: true });
    renderHome('/');
    await screen.findByText('Garage Plug');

    await userEvent.click(
      screen.getByRole('button', { name: /Notify me when Garage Plug is free/ })
    );

    expect(api.post).toHaveBeenCalledWith('/api/plugs/2/watch');
    const watching = screen.getByRole('button', { name: /Stop watching Garage Plug/ });
    expect(watching).toHaveAttribute('aria-pressed', 'true');
    expect(watching).toHaveTextContent('Watching');
  });

  it('disarms a watching plug: DELETEs and flips the bell back', async () => {
    api.get.mockResolvedValue(
      PLUGS.map((p) => (p.id === 2 ? { ...p, watching: true } : p))
    );
    api.delete.mockResolvedValue({ status: 'not_watching', plug_id: 2, watching: false });
    renderHome('/');
    await screen.findByText('Garage Plug');

    await userEvent.click(
      screen.getByRole('button', { name: /Stop watching Garage Plug/ })
    );

    expect(api.delete).toHaveBeenCalledWith('/api/plugs/2/watch');
    expect(
      screen.getByRole('button', { name: /Notify me when Garage Plug is free/ })
    ).toHaveAttribute('aria-pressed', 'false');
  });

  it('reverts the optimistic flip when the API call fails', async () => {
    api.post.mockRejectedValue(new Error('This plug is available right now'));
    renderHome('/');
    await screen.findByText('Garage Plug');

    await userEvent.click(
      screen.getByRole('button', { name: /Notify me when Garage Plug is free/ })
    );

    // Reverted: back to the unarmed bell, no Watching state left behind.
    expect(
      await screen.findByRole('button', { name: /Notify me when Garage Plug is free/ })
    ).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: /Stop watching Garage Plug/ })
    ).not.toBeInTheDocument();
  });

  it('clears the bell when the plug flips available live (one-shot fired server-side)', async () => {
    const handlers = {};
    const socket = {
      on: (event, fn) => { handlers[event] = fn; },
      off: vi.fn(),
    };
    useSession.mockReturnValue({
      startSession: vi.fn(),
      activeSessions: [],
      switchSession: vi.fn(),
      error: null,
      socket,
    });
    api.get.mockResolvedValue(
      PLUGS.map((p) => (p.id === 2 ? { ...p, watching: true } : p))
    );
    renderHome('/');
    await screen.findByText('Garage Plug');
    expect(
      screen.getByRole('button', { name: /Stop watching Garage Plug/ })
    ).toBeInTheDocument();

    // The server fired + deleted the watch when the plug freed; the same
    // plug_status broadcast clears the local bell (and makes it startable).
    act(() => handlers.plug_status({ plug_id: 2, status: 'available' }));

    expect(
      screen.queryByRole('button', { name: /Stop watching Garage Plug/ })
    ).not.toBeInTheDocument();
    expect(screen.getAllByText('Charge →').length).toBeGreaterThan(1);
  });
});
