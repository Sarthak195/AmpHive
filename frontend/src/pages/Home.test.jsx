/**
 * Home page tests: QR/deep-link start (`?plug=` prefill, the auth gate that
 * preserves it through login, and the "unknown id" notice), the sectioned
 * charger list (private "Your chargers" first, "Public chargers" collapsed
 * by default when private ones exist, map at the bottom, collapsed), the
 * availability + group filters with their live legend counts, and the
 * reservations surface (the "Reserved until" badge, the Reserve action
 * opening the modal, and the "Your reservations" strip with cancel).
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
import { fmtTime, fmtWindow } from '../utils/reservationTime';

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

describe('Home — reservations', () => {
  const RESERVED_UNTIL = '2030-01-01T15:30:00+00:00';
  const MY_RESERVATION = {
    id: 11,
    plug_id: 1,
    plug_name: 'Lobby Plug',
    start_at: '2030-01-02T10:00:00+00:00',
    end_at: '2030-01-02T11:00:00+00:00',
    status: 'booked',
  };

  // Route-aware GET mock: Home fetches both the plug list and the
  // reservations strip; ReserveModal additionally fetches a plug schedule.
  const mockGets = ({ plugs = PLUGS, upcoming = [] } = {}) => {
    api.get.mockImplementation((url) => {
      if (url === '/api/plugs/available') return Promise.resolve(plugs);
      if (url === '/api/reservations/my') return Promise.resolve({ upcoming, history: [] });
      if (/^\/api\/plugs\/\d+\/reservations$/.test(url)) return Promise.resolve([]);
      return Promise.resolve([]);
    });
  };

  beforeEach(() => {
    useAuth.mockReturnValue({ user: DRIVER });
  });

  it('shows a "Reserved until HH:MM" badge (distinct from occupied) and blocks the start click', async () => {
    mockGets({
      plugs: PLUGS.map((p) =>
        p.id === 1
          ? { ...p, reserved_now: true, reserved_now_by_me: false, reserved_until: RESERVED_UNTIL }
          : p
      ),
    });
    renderHome('/');
    await screen.findByText('Lobby Plug');

    expect(screen.getByText(`Reserved until ${fmtTime(RESERVED_UNTIL)}`)).toBeInTheDocument();
    // Still shows its plain hardware status alongside — reserved ≠ occupied.
    expect(screen.getByText('available')).toBeInTheDocument();
    // The only otherwise-startable plug is inside someone else's window, so
    // no card invites a "Charge →" click.
    expect(screen.queryByText('Charge →')).not.toBeInTheDocument();
  });

  it('shows "Reserved for you" to the holder and keeps the card startable', async () => {
    mockGets({
      plugs: PLUGS.map((p) =>
        p.id === 1
          ? { ...p, reserved_now: true, reserved_now_by_me: true, reserved_until: RESERVED_UNTIL }
          : p
      ),
    });
    renderHome('/');
    await screen.findByText('Lobby Plug');

    expect(screen.getByText('Reserved for you')).toBeInTheDocument();
    expect(screen.getByText('Charge →')).toBeInTheDocument();
  });

  it('opens the reserve modal from a plug card "Reserve" action', async () => {
    mockGets();
    renderHome('/');
    await screen.findByText('Lobby Plug');

    // One Reserve button per visible card; the private section lists
    // Lobby (id 1) first.
    await userEvent.click(screen.getAllByRole('button', { name: 'Reserve' })[0]);

    expect(await screen.findByRole('heading', { name: /Reserve Lobby Plug/ })).toBeInTheDocument();
    expect(api.get).toHaveBeenCalledWith('/api/plugs/1/reservations');
  });

  it('renders the "Your reservations" strip and cancels via its button', async () => {
    mockGets({ upcoming: [MY_RESERVATION] });
    api.post.mockResolvedValue({ status: 'cancelled' });
    renderHome('/');

    expect(await screen.findByText('Your reservations')).toBeInTheDocument();
    expect(
      screen.getByText(fmtWindow(MY_RESERVATION.start_at, MY_RESERVATION.end_at))
    ).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(api.post).toHaveBeenCalledWith('/api/reservations/11/cancel');
  });

  it('hides the strip when there are no upcoming reservations', async () => {
    mockGets({ upcoming: [] });
    renderHome('/');
    await screen.findByText('Lobby Plug');
    expect(screen.queryByText('Your reservations')).not.toBeInTheDocument();
  });
});
