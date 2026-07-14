/**
 * Home page tests: QR/deep-link start (`?plug=` prefill, the auth gate that
 * preserves it through login, and the "unknown id" notice), the sectioned
 * charger list (private "Your chargers" first, "Public chargers" collapsed
 * by default when private ones exist, map at the bottom, collapsed), the
 * availability + group filters with their live legend counts, the
 * reservations surface (the "Reserved until" badge, the Reserve action
 * opening the modal, and the "Your reservations" strip with cancel), and the
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

describe('Home — charger tabs (Your chargers / Public / Map)', () => {
  beforeEach(() => {
    useAuth.mockReturnValue({ user: DRIVER });
    api.get.mockResolvedValue(PLUGS);
  });

  it('defaults to Your chargers; Public is one tab-click away (one list at a time)', async () => {
    renderHome('/');
    await screen.findByText('Lobby Plug');

    // Your chargers active by default → both Sunrise plugs visible…
    expect(screen.getByRole('button', { name: /Your chargers/ })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByText('Garage Plug')).toBeInTheDocument();
    // …and the public list is not rendered until its tab is chosen.
    expect(screen.queryByText('Rooftop Plug')).not.toBeInTheDocument();
    expect(screen.queryByText('Basement Plug')).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: /Public chargers/ }));

    // One list at a time: public plugs show, private ones hide.
    expect(screen.getByText('Rooftop Plug')).toBeInTheDocument();
    expect(screen.getByText('Basement Plug')).toBeInTheDocument();
    expect(screen.queryByText('Lobby Plug')).not.toBeInTheDocument();
  });

  it('each tab shows its plug count', async () => {
    renderHome('/');
    await screen.findByText('Lobby Plug');
    // Sunrise (private) = 2; public (Rooftop + Basement) = 2.
    expect(screen.getByRole('button', { name: /Your chargers/ })).toHaveTextContent('2');
    expect(screen.getByRole('button', { name: /Public chargers/ })).toHaveTextContent('2');
  });

  it('the Map tab mounts the map lazily and plots public plugs only', async () => {
    renderHome('/');
    await screen.findByText('Lobby Plug');

    // Not on the map tab → nothing mounted (no tiles fetched), no legend.
    expect(screen.queryByTestId('map-mock')).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: /Map/ }));

    expect(screen.getByTestId('map-mock')).toHaveTextContent('3,4'); // public ids only
    expect(screen.getByText('Available (0)')).toBeInTheDocument();
    expect(screen.getByText('In use (0)')).toBeInTheDocument();
    expect(screen.getByText('Offline (2)')).toBeInTheDocument();
  });

  it('defaults to Public and shows a join hint on Your chargers when the driver has none', async () => {
    api.get.mockResolvedValue(PLUGS.map((p) => ({ ...p, is_private: false })));
    renderHome('/');
    await screen.findByText('Lobby Plug'); // all public → shown on the default Public tab

    expect(screen.getByRole('button', { name: /Public chargers/ })).toHaveAttribute('aria-pressed', 'true');

    await userEvent.click(screen.getByRole('button', { name: /Your chargers/ }));
    expect(screen.getByText(/join their group with an access code/)).toBeInTheDocument();
  });

  it('the availability filter narrows the active tab + the "X of Y" count', async () => {
    renderHome('/');
    await screen.findByText('Lobby Plug');
    expect(screen.getByText('Garage Plug')).toBeInTheDocument();

    await userEvent.selectOptions(screen.getByLabelText('Filter by availability'), 'available');

    expect(screen.getByText('Lobby Plug')).toBeInTheDocument();
    expect(screen.queryByText('Garage Plug')).not.toBeInTheDocument();
    expect(screen.getByText('1 of 4 plugs')).toBeInTheDocument();

    // Public has no available plug (Rooftop's gateway is offline) → empty tab.
    await userEvent.click(screen.getByRole('button', { name: /Public chargers/ }));
    expect(screen.queryByText('Rooftop Plug')).not.toBeInTheDocument();
    expect(screen.getByText('None match the current filters.')).toBeInTheDocument();
  });

  it('the group filter narrows the list and the map markers', async () => {
    renderHome('/');
    await screen.findByText('Lobby Plug');

    await userEvent.selectOptions(screen.getByLabelText('Filter by group'), 'Downtown Mall');

    // Sunrise plugs filtered out of the (default) Your chargers tab.
    expect(screen.queryByText('Lobby Plug')).not.toBeInTheDocument();
    expect(screen.queryByText('Garage Plug')).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: /Public chargers/ }));
    expect(screen.getByText('Basement Plug')).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: /Map/ }));
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

  it('shows a no-charger empty state when no plugs are accessible at all (tabs/filters not offered)', async () => {
    api.get.mockResolvedValue([]);
    renderHome('/');
    expect(await screen.findByText('No chargers available yet.')).toBeInTheDocument();
    expect(screen.queryByLabelText('Filter by availability')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Your chargers/ })).not.toBeInTheDocument();
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

describe('Home — tap a charger opens the setup panel (no instant start)', () => {
  let startSession;

  const renderWithSessionRoute = () =>
    render(
      <MemoryRouter initialEntries={['/']}>
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/session" element={<div>session page</div>} />
        </Routes>
      </MemoryRouter>
    );

  beforeEach(() => {
    useAuth.mockReturnValue({ user: DRIVER });
    api.get.mockResolvedValue(PLUGS);
    startSession = vi.fn().mockResolvedValue({ session_id: 9 });
    useSession.mockReturnValue({
      startSession,
      activeSessions: [],
      switchSession: vi.fn(),
      error: null,
      socket: null,
    });
  });

  it('tapping a startable charger opens the setup modal instead of charging instantly', async () => {
    renderWithSessionRoute();
    await screen.findByText('Lobby Plug');

    await userEvent.click(screen.getByText('Lobby Plug')); // tap the card

    expect(await screen.findByRole('heading', { name: /Set up your charge/i })).toBeInTheDocument();
    expect(startSession).not.toHaveBeenCalled(); // NOT an instant start
  });

  it('starts with no limit from the setup modal when both fields are blank', async () => {
    renderWithSessionRoute();
    await screen.findByText('Lobby Plug');
    await userEvent.click(screen.getByText('Lobby Plug'));
    await screen.findByRole('heading', { name: /Set up your charge/i });

    await userEvent.click(screen.getByRole('button', { name: /Start charging/ }));
    expect(startSession).toHaveBeenCalledWith(1, null);
  });

  it('forwards a kWh limit set in the modal', async () => {
    renderWithSessionRoute();
    await screen.findByText('Lobby Plug');
    await userEvent.click(screen.getByText('Lobby Plug'));
    await screen.findByRole('heading', { name: /Set up your charge/i });

    await userEvent.type(screen.getByLabelText(/kWh to dispense/i), '5');
    await userEvent.click(screen.getByRole('button', { name: /Start charging/ }));
    expect(startSession).toHaveBeenCalledWith(1, { max_kwh: 5 });
  });

  it('typing a Plug ID and hitting "Set up" opens the modal (does not start)', async () => {
    renderWithSessionRoute();
    await screen.findByText('Lobby Plug');

    await userEvent.type(screen.getByPlaceholderText('Enter Plug ID (e.g. 1)'), '2');
    await userEvent.click(screen.getByRole('button', { name: 'Set up' }));

    expect(await screen.findByRole('heading', { name: /Set up your charge/i })).toBeInTheDocument();
    expect(startSession).not.toHaveBeenCalled();
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

    // Your chargers tab (default): startable Lobby has no bell; occupied Garage does.
    expect(
      screen.queryByRole('button', { name: /Notify me when Lobby Plug is free/ })
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /Notify me when Garage Plug is free/ })
    ).toBeInTheDocument();

    // Public tab: both public plugs are non-startable (gateway/status offline) → bells.
    await userEvent.click(screen.getByRole('button', { name: /Public chargers/ }));
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

describe('Home — live connectivity + poll backstop', () => {
  beforeEach(() => {
    useAuth.mockReturnValue({ user: DRIVER });
    api.get.mockResolvedValue(PLUGS);
  });

  it('flips a plug card offline live on a plug_connectivity push', async () => {
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
    renderHome('/');
    await screen.findByText('Lobby Plug');

    // Lobby (id 1) starts available + gateway online → startable, no free bell.
    expect(screen.getByText('Charge →')).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: /Notify me when Lobby Plug is free/ })
    ).not.toBeInTheDocument();

    // Its gateway drops → the card goes offline in place: no longer startable,
    // and the "notify me when free" bell appears — all without a refetch.
    act(() => handlers.plug_connectivity({ plug_id: 1, gateway_online: false }));

    expect(screen.queryByText('Charge →')).not.toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /Notify me when Lobby Plug is free/ })
    ).toBeInTheDocument();

    // Gateway recovers via the same push → back to a startable card.
    act(() => handlers.plug_connectivity({ plug_id: 1, gateway_online: true }));
    expect(screen.getByText('Charge →')).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: /Notify me when Lobby Plug is free/ })
    ).not.toBeInTheDocument();
  });

  it('polls the plug fetcher every 30s as a backstop', async () => {
    vi.useFakeTimers();
    try {
      renderHome('/');
      // Initial load fetch (available) + reservations.
      const initialCalls = api.get.mock.calls.filter(
        ([url]) => url === '/api/plugs/available'
      ).length;

      await act(async () => { vi.advanceTimersByTime(30000); });
      const afterOne = api.get.mock.calls.filter(
        ([url]) => url === '/api/plugs/available'
      ).length;
      expect(afterOne).toBe(initialCalls + 1);

      await act(async () => { vi.advanceTimersByTime(30000); });
      const afterTwo = api.get.mock.calls.filter(
        ([url]) => url === '/api/plugs/available'
      ).length;
      expect(afterTwo).toBe(initialCalls + 2);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('Home — time-of-day next-price ribbon', () => {
  beforeEach(() => {
    useAuth.mockReturnValue({ user: DRIVER });
  });

  it('previews the upcoming price when a TOD boundary changes it later today', async () => {
    api.get.mockResolvedValue([
      { ...PLUGS[0], price_per_kwh: 5, price_next_per_kwh: 6, price_changes_at: '2026-07-14T18:00:00+05:30' },
    ]);
    renderHome('/');
    await screen.findByText('Lobby Plug');

    // The "next price" hint renders (→ 6 @ <local time>); assert the price
    // part, not the exact clock, to stay timezone-independent in CI.
    expect(screen.getByText(/→\s*6\s*@/)).toBeInTheDocument();
  });

  it('omits the next-price line for a flat plug', async () => {
    api.get.mockResolvedValue([
      { ...PLUGS[0], price_per_kwh: 5, price_next_per_kwh: null, price_changes_at: null },
    ]);
    renderHome('/');
    await screen.findByText('Lobby Plug');

    // The "→ <price>" ribbon hint is absent (don't match the "Charge →" button).
    expect(screen.queryByText(/→\s*\d/)).not.toBeInTheDocument();
  });
});
