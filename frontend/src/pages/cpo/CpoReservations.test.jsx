/**
 * CpoReservations tests: the reservations page renders tenant bookings
 * (plug, driver, window, status badge, linked session), offers Cancel only
 * on still-BOOKED rows and posts the operator-cancel through window.confirm,
 * surfaces a backend 409 inline, applies the status filter to the query, and
 * shows the empty state.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

import CpoReservations from './CpoReservations';
import api from '../../api/client';

vi.mock('../../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

// CpoLayout reads useAuth() for its sidebar footer — stub it.
vi.mock('../../contexts/AuthContext', () => ({
  useAuth: () => ({ user: { email: 'cpo@amphive.test', full_name: 'Ops', role: 'cpo' } }),
}));

const RESERVATIONS = [
  {
    id: 7, plug_id: 2, plug_name: 'Bay 2',
    user_id: 5, user_email: 'asha@example.com', user_name: 'Asha',
    start_at: '2026-07-13T09:00:00+00:00', end_at: '2026-07-13T10:00:00+00:00',
    status: 'booked', session_id: null, created_at: '2026-07-13T08:00:00+00:00',
  },
  {
    id: 6, plug_id: 1, plug_name: 'Bay 1',
    user_id: 4, user_email: 'ravi@example.com', user_name: 'Ravi',
    start_at: '2026-07-12T18:00:00+00:00', end_at: '2026-07-12T19:00:00+00:00',
    status: 'fulfilled', session_id: 88, created_at: '2026-07-12T17:00:00+00:00',
  },
  {
    id: 5, plug_id: 1, plug_name: 'Bay 1',
    user_id: 5, user_email: 'asha@example.com', user_name: 'Asha',
    start_at: '2026-07-11T08:00:00+00:00', end_at: '2026-07-11T09:00:00+00:00',
    status: 'cancelled', session_id: null, created_at: '2026-07-11T07:00:00+00:00',
  },
];

const PROFILE = { tenant: { name: 'Acme Charging' } };

const mockApiGet = (reservations = RESERVATIONS, profile = PROFILE) => {
  api.get.mockImplementation((url) => {
    if (url.startsWith('/api/cpo/reservations')) return Promise.resolve(reservations);
    if (url === '/api/cpo/profile') return Promise.resolve(profile);
    return Promise.resolve([]);
  });
};

const renderPage = () => render(
  <MemoryRouter>
    <CpoReservations />
  </MemoryRouter>
);

beforeEach(() => {
  vi.clearAllMocks();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('CpoReservations', () => {
  it('renders booking rows with plug, driver, status badge, and linked session', async () => {
    mockApiGet();
    renderPage();

    await screen.findByText('Bay 2');
    const rows = screen.getAllByRole('row');

    const bookedRow = rows.find((r) => r.textContent.includes('#7'));
    expect(within(bookedRow).getByText('Asha')).toBeInTheDocument();
    expect(within(bookedRow).getByText('asha@example.com')).toBeInTheDocument();
    expect(within(bookedRow).getByText('booked')).toBeInTheDocument();

    const fulfilledRow = rows.find((r) => r.textContent.includes('#6'));
    expect(within(fulfilledRow).getByText('fulfilled')).toBeInTheDocument();
    expect(within(fulfilledRow).getByText('#88')).toBeInTheDocument(); // linked session

    // Summary count reflects loaded rows + active (booked) tally.
    expect(screen.getByText(/3 shown · 1 active/)).toBeInTheDocument();
  });

  it('offers Cancel only on BOOKED rows', async () => {
    mockApiGet();
    renderPage();

    await screen.findByText('Bay 2');
    const rows = screen.getAllByRole('row');

    const bookedRow = rows.find((r) => r.textContent.includes('#7'));
    expect(within(bookedRow).getByRole('button', { name: 'Cancel' })).toBeInTheDocument();

    const fulfilledRow = rows.find((r) => r.textContent.includes('#6'));
    expect(within(fulfilledRow).queryByRole('button', { name: 'Cancel' })).not.toBeInTheDocument();

    const cancelledRow = rows.find((r) => r.textContent.includes('#5'));
    expect(within(cancelledRow).queryByRole('button', { name: 'Cancel' })).not.toBeInTheDocument();
  });

  it('cancels a BOOKED reservation through window.confirm and refetches', async () => {
    mockApiGet();
    api.post.mockResolvedValue({ id: 7, status: 'cancelled' });
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    const user = userEvent.setup();
    renderPage();

    await screen.findByText('Bay 2');
    await user.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(api.post).toHaveBeenCalledWith('/api/reservations/7/cancel', {});
    // Initial load + refetch after the successful cancel.
    await waitFor(() => {
      const listCalls = api.get.mock.calls.filter(([url]) => url.startsWith('/api/cpo/reservations'));
      expect(listCalls.length).toBe(2);
    });
  });

  it('does not cancel when the operator declines the confirm', async () => {
    mockApiGet();
    vi.spyOn(window, 'confirm').mockReturnValue(false);
    const user = userEvent.setup();
    renderPage();

    await screen.findByText('Bay 2');
    await user.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(api.post).not.toHaveBeenCalled();
  });

  it('surfaces a backend cancel error inline', async () => {
    mockApiGet();
    api.post.mockRejectedValue(new Error('This reservation is already fulfilled.'));
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    const user = userEvent.setup();
    renderPage();

    await screen.findByText('Bay 2');
    await user.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(await screen.findByText('This reservation is already fulfilled.')).toBeInTheDocument();
  });

  it('applies the status filter to the query', async () => {
    mockApiGet();
    const user = userEvent.setup();
    renderPage();

    await screen.findByText('Bay 2');
    await user.selectOptions(screen.getByRole('combobox'), 'fulfilled');

    await waitFor(() => {
      const listCalls = api.get.mock.calls.filter(([url]) => url.startsWith('/api/cpo/reservations'));
      expect(listCalls.some(([url]) => url.includes('status=fulfilled'))).toBe(true);
    });
  });

  it('shows the empty state when there are no reservations', async () => {
    mockApiGet([]);
    renderPage();

    expect(await screen.findByText('No reservations found')).toBeInTheDocument();
  });
});
