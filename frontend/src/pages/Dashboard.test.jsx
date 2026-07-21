/**
 * Dashboard tests — the signed-in driver home:
 * plug grid with [Your groups | Public] segments and state/group filters
 * (skeleton → ErrorState-with-retry → EmptyState only on true zero-data),
 * the debounced charge-now lookup (?plug= deep-link autofill, unknown-id
 * copy, state-appropriate action), the queued-charge affordance gated on
 * queue_available, the up-next strip with ConfirmDialog'd cancels, the
 * notify-me watch toggle, the active-session banner, and the rail's month
 * stats that hide when /api/me/stats errors.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Routes, Route } from 'react-router-dom';

import Dashboard from './Dashboard';
import api from '../api/client';
import { useAuth } from '../contexts/AuthContext';
import { useSession } from '../contexts/SessionContext';

vi.mock('../api/client', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn(),
    queueCharge: vi.fn(),
    listQueuedCharges: vi.fn(),
    cancelQueuedCharge: vi.fn(),
  },
}));
vi.mock('../contexts/AuthContext', () => ({ useAuth: vi.fn() }));
vi.mock('../contexts/SessionContext', () => ({ useSession: vi.fn() }));
vi.mock('../contexts/ConfigContext', () => ({
  useConfig: () => ({ coins_per_kwh: 5, coin_inr_rate: 1, min_start_balance_coins: 50 }),
}));
vi.mock('../contexts/WalletContext', () => ({ useWallet: () => ({ balance: 500 }) }));
// The two modals have their own test files — stub them so these tests assert
// only that Dashboard opens them with the right plug + mode.
vi.mock('../components/ChargeSetupModal', () => ({
  default: ({ open, mode, plug }) =>
    open ? <div data-testid="setup-modal">{`${mode}:${plug.id}`}</div> : null,
}));
vi.mock('../components/ReserveModal', () => ({
  default: ({ open, plug }) =>
    open ? <div data-testid="reserve-modal">{String(plug.id)}</div> : null,
}));

const DRIVER = { email: 'driver@amphive.test', role: 'driver', full_name: 'Dana Driver' };

// One plug per interesting bucket: private available, private in-use,
// public available, public unpowered-and-queueable.
const PLUGS = [
  { id: 1, name: 'Lobby Plug', status: 'available', gateway_online: true, plug_powered: true, price_per_kwh: 8, group_name: 'Sunrise Apartments', is_private: true },
  { id: 2, name: 'Garage Plug', status: 'occupied', gateway_online: true, plug_powered: true, price_per_kwh: 8, group_name: 'Sunrise Apartments', is_private: true },
  { id: 3, name: 'Mall Plug', status: 'available', gateway_online: true, plug_powered: true, price_per_kwh: 6, group_name: 'Downtown Mall', is_private: false },
  { id: 4, name: 'Basement Plug', status: 'available', gateway_online: true, plug_powered: false, queue_available: true, price_per_kwh: 6, group_name: 'Downtown Mall', is_private: false },
];

let routes;
const setRoute = (url, value) => {
  routes[url] = value;
};

const renderDash = (entry = '/') =>
  render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/session" element={<div>session page</div>} />
      </Routes>
    </MemoryRouter>
  );

beforeEach(() => {
  vi.clearAllMocks();
  useAuth.mockReturnValue({ user: DRIVER });
  useSession.mockReturnValue({
    activeSessions: [],
    sessionData: null,
    sessionId: null,
    switchSession: vi.fn(),
    startSession: vi.fn(),
    socket: null,
  });
  routes = {
    '/api/plugs/available': () => Promise.resolve(PLUGS),
    '/api/reservations/my': () => Promise.resolve({ upcoming: [] }),
    '/api/me/stats': () => Promise.reject(Object.assign(new Error('nope'), { status: 404 })),
  };
  api.get.mockImplementation((url) => {
    const route = routes[url];
    if (route) return route();
    return Promise.reject(Object.assign(new Error(`Plug not found`), { status: 404 }));
  });
  api.listQueuedCharges.mockResolvedValue([]);
});

describe('Dashboard — charger grid', () => {
  it('defaults to "Your groups" when private chargers exist; Public shows the rest', async () => {
    renderDash();
    expect(await screen.findByText('Lobby Plug')).toBeInTheDocument();
    expect(screen.getByText('Garage Plug')).toBeInTheDocument();
    expect(screen.queryByText('Mall Plug')).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: /public/i }));
    expect(screen.getByText('Mall Plug')).toBeInTheDocument();
    expect(screen.getByText('Basement Plug')).toBeInTheDocument();
    expect(screen.queryByText('Lobby Plug')).not.toBeInTheDocument();
  });

  it('a failed plug fetch shows an ErrorState whose Retry reloads the grid', async () => {
    setRoute('/api/plugs/available', () => Promise.reject(new Error('down')));
    renderDash();

    expect(await screen.findByText("Couldn't load your chargers")).toBeInTheDocument();

    setRoute('/api/plugs/available', () => Promise.resolve(PLUGS));
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('Lobby Plug')).toBeInTheDocument();
  });

  it('true zero-data gets the EmptyState with Join a group + Browse the map', async () => {
    setRoute('/api/plugs/available', () => Promise.resolve([]));
    renderDash();

    expect(await screen.findByText('No chargers yet')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /join a group/i })).toHaveAttribute('href', '/groups');
    expect(screen.getByRole('link', { name: /browse the map/i })).toHaveAttribute('href', '/map');
  });

  it('the state filter narrows the grid to one availability bucket', async () => {
    renderDash();
    await screen.findByText('Lobby Plug');

    await userEvent.selectOptions(screen.getByLabelText('Filter by state'), 'in_use');
    expect(screen.queryByText('Lobby Plug')).not.toBeInTheDocument();
    expect(screen.getByText('Garage Plug')).toBeInTheDocument();
  });

  it('?group= preselects the group filter', async () => {
    renderDash('/?group=Downtown Mall');
    const select = await screen.findByLabelText('Filter by group');
    expect(select).toHaveValue('Downtown Mall');
    // The preselected group has no private plugs — Public shows its chargers.
    await userEvent.click(screen.getByRole('button', { name: /public/i }));
    expect(screen.getByText('Mall Plug')).toBeInTheDocument();
    expect(screen.queryByText('Lobby Plug')).not.toBeInTheDocument();
  });

  it('an unpowered queue_available card opens the setup modal in queue mode', async () => {
    renderDash();
    await screen.findByText('Lobby Plug');
    await userEvent.click(screen.getByRole('button', { name: /public/i }));

    await userEvent.click(screen.getByRole('button', { name: /queue charge/i }));
    expect(screen.getByTestId('setup-modal')).toHaveTextContent('queue:4');
  });

  it('the notify-me bell arms a watch optimistically via POST /watch', async () => {
    api.post.mockResolvedValue({});
    renderDash();
    await screen.findByText('Garage Plug');

    const bell = screen.getByRole('button', { name: /notify me when garage plug is free/i });
    await userEvent.click(bell);
    expect(api.post).toHaveBeenCalledWith('/api/plugs/2/watch');
    expect(screen.getByRole('button', { name: /stop watching/i })).toHaveAttribute('aria-pressed', 'true');
  });
});

describe('Dashboard — charge now lookup', () => {
  it('?plug= deep link autofills, auto-looks-up and offers Start charging', async () => {
    setRoute('/api/plugs/7', () =>
      Promise.resolve({ id: 7, name: 'Visitor Plug', status: 'available', gateway_online: true, plug_powered: true, price_per_kwh: 9 })
    );
    renderDash('/?plug=7');

    expect(screen.getByLabelText('Charger ID')).toHaveValue('7');
    expect(await screen.findByText('Visitor Plug')).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Start charging' }));
    expect(screen.getByTestId('setup-modal')).toHaveTextContent('start:7');
  });

  it('an unknown id shows the inline "check the label" copy', async () => {
    renderDash();
    await screen.findByText('Lobby Plug');

    await userEvent.type(screen.getByLabelText('Charger ID'), '999');
    expect(
      await screen.findByText('No charger with ID 999 — check the label.')
    ).toBeInTheDocument();
  });

  it('an in-use charger gets the reserve hint instead of a Start button', async () => {
    setRoute('/api/plugs/2', () =>
      Promise.resolve({ id: 2, name: 'Garage Plug', status: 'occupied', gateway_online: true, plug_powered: true, price_per_kwh: 8 })
    );
    renderDash();
    await screen.findByText('Lobby Plug');

    await userEvent.type(screen.getByLabelText('Charger ID'), '2');
    expect(await screen.findByText('In use — you can reserve it')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Start charging' })).not.toBeInTheDocument();

    await userEvent.click(within(screen.getByRole('status')).getByRole('button', { name: 'Reserve' }));
    expect(screen.getByTestId('reserve-modal')).toHaveTextContent('2');
  });
});

describe('Dashboard — up next strip', () => {
  const RESERVATION = {
    id: 11,
    plug_id: 1,
    plug_name: 'Lobby Plug',
    start_at: '2030-06-01T10:00:00Z',
    end_at: '2030-06-01T11:00:00Z',
  };

  it('cancelling a reservation goes through a ConfirmDialog then POSTs the cancel', async () => {
    setRoute('/api/reservations/my', () => Promise.resolve({ upcoming: [RESERVATION] }));
    api.post.mockResolvedValue({});
    renderDash();

    expect(await screen.findByText('Up next')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(screen.getByRole('dialog', { name: 'Cancel this reservation?' })).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Cancel reservation' }));
    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith('/api/reservations/11/cancel')
    );
  });

  it('cancelling a queued charge goes through a ConfirmDialog then DELETEs it', async () => {
    api.listQueuedCharges.mockResolvedValue([
      { id: 5, plug_id: 4, plug_name: 'Basement Plug', expires_at: new Date(Date.now() + 3600_000).toISOString() },
    ]);
    api.cancelQueuedCharge.mockResolvedValue({});
    renderDash();

    expect(await screen.findByText(/Waiting for power/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(screen.getByRole('dialog', { name: 'Cancel this queued charge?' })).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Cancel queued charge' }));
    await waitFor(() => expect(api.cancelQueuedCharge).toHaveBeenCalledWith(5));
  });
});

describe('Dashboard — sessions banner and rail', () => {
  it('shows a live banner per active session; Open session focuses it and navigates', async () => {
    const switchSession = vi.fn();
    useSession.mockReturnValue({
      activeSessions: [{ session_id: 's1', plug_id: 1, plug_name: 'Lobby Plug' }],
      sessionData: { cost_coins: 12, power_w: 2300 },
      sessionId: 's1',
      switchSession,
      startSession: vi.fn(),
      socket: null,
    });
    renderDash();

    expect(await screen.findByText('Charging at Lobby Plug')).toBeInTheDocument();
    expect(screen.getByText(/₹12\.00/)).toBeInTheDocument();
    expect(screen.getByText(/2\.3 kW/)).toBeInTheDocument();

    // Screen-reader-only aria-live cost announcement.
    const region = document.querySelector('[aria-live="polite"].sr-only');
    expect(region).toHaveTextContent('Current cost 12 rupees, 0.00 kilowatt hours');

    await userEvent.click(screen.getByRole('button', { name: 'Open session' }));
    expect(switchSession).toHaveBeenCalled();
    expect(screen.getByText('session page')).toBeInTheDocument();
  });

  it('renders month stats when /api/me/stats resolves', async () => {
    setRoute('/api/me/stats', () =>
      Promise.resolve({ month: { energy_kwh: 42.5, spend_coins: 210, sessions: 9 } })
    );
    renderDash();

    expect(await screen.findByText('Energy this month')).toBeInTheDocument();
    expect(screen.getByText('42.50 kWh')).toBeInTheDocument();
    expect(screen.getByText(/₹210\.00/)).toBeInTheDocument();
  });

  it('hides the stats block when /api/me/stats errors (endpoint may not exist yet)', async () => {
    renderDash();
    await screen.findByText('Lobby Plug');
    expect(screen.queryByText('Energy this month')).not.toBeInTheDocument();
    // The charging-credit card itself still renders.
    expect(screen.getByText('Charging credit')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Add credit' })).toHaveAttribute('href', '/credit');
  });
});
