/**
 * CpoReservations page tests (redesign v3, D5): the List view (filters,
 * pagination, ErrorState-with-retry, operator cancel via ConfirmDialog with
 * the "driver will be notified" copy), the List↔Day `.seg` toggle, and the
 * Day view's client-side per-plug/per-day grouping of a single fetched page.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import CpoReservations from './CpoReservations';
import api from '../../api/client';

vi.mock('../../components/CpoLayout', () => ({
  default: ({ children }) => <div>{children}</div>,
}));

vi.mock('../../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

const toast = { ok: vi.fn(), error: vi.fn(), info: vi.fn(), warn: vi.fn() };
vi.mock('../../components/ui', async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, useToast: () => toast };
});

const RESERVATIONS_PAGE = {
  total: 2,
  items: [
    {
      id: 1, plug_id: 1, plug_name: 'Garage plug', user_name: 'Asha', user_email: 'asha@amphive.test',
      start_at: '2026-07-21T09:00:00Z', end_at: '2026-07-21T10:00:00Z', status: 'booked', session_id: null,
    },
    {
      id: 2, plug_id: 2, plug_name: 'Porch plug', user_name: 'Rae', user_email: 'rae@amphive.test',
      start_at: '2026-07-20T09:00:00Z', end_at: '2026-07-20T10:00:00Z', status: 'fulfilled', session_id: 9,
    },
  ],
};

const PLUGS = [{ id: 1, name: 'Garage plug' }, { id: 2, name: 'Porch plug' }];

const mockRoutes = ({ reservations = RESERVATIONS_PAGE, plugs = PLUGS } = {}) => {
  api.get.mockImplementation((url) => {
    if (url.startsWith('/api/cpo/reservations')) return Promise.resolve(reservations);
    if (url === '/api/cpo/plugs') return Promise.resolve(plugs);
    return Promise.reject(new Error(`unhandled url ${url}`));
  });
};

beforeEach(() => {
  vi.clearAllMocks();
  mockRoutes();
});

describe('CpoReservations — list view', () => {
  it('fetches with limit/offset and renders rows', async () => {
    render(<CpoReservations />);
    expect(await screen.findByText('Garage plug')).toBeInTheDocument();
    expect(api.get).toHaveBeenCalledWith('/api/cpo/reservations?limit=20&offset=0');
    expect(screen.getByText('Porch plug')).toBeInTheDocument();
    // "Booked"/"Fulfilled" also label <option>s in the status filter, so
    // scope these to the table's status badges.
    const table = screen.getByRole('table');
    expect(within(table).getByText('Booked')).toBeInTheDocument();
    expect(within(table).getByText('Fulfilled')).toBeInTheDocument();
  });

  it('applies the status filter and the upcoming-only checkbox', async () => {
    render(<CpoReservations />);
    await screen.findByText('Garage plug');
    api.get.mockClear();

    await userEvent.selectOptions(screen.getByLabelText('Status'), 'booked');
    await waitFor(() =>
      expect(api.get).toHaveBeenCalledWith('/api/cpo/reservations?limit=20&offset=0&status=booked')
    );

    api.get.mockClear();
    await userEvent.click(screen.getByLabelText('Upcoming only'));
    await waitFor(() =>
      expect(api.get).toHaveBeenCalledWith(
        '/api/cpo/reservations?limit=20&offset=0&status=booked&upcoming_only=true'
      )
    );
  });

  it('shows a retryable error instead of an empty state on failure', async () => {
    api.get.mockImplementation((url) => {
      if (url.startsWith('/api/cpo/reservations')) return Promise.reject(new Error('down'));
      return Promise.resolve(PLUGS);
    });
    render(<CpoReservations />);
    expect(await screen.findByText("Couldn't load this")).toBeInTheDocument();
    expect(screen.queryByText('No reservations found')).not.toBeInTheDocument();

    mockRoutes();
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('Garage plug')).toBeInTheDocument();
  });

  it('shows the empty state only for a true zero-row result', async () => {
    mockRoutes({ reservations: { total: 0, items: [] } });
    render(<CpoReservations />);
    expect(await screen.findByText('No reservations found')).toBeInTheDocument();
  });

  it('only offers Cancel on a booked reservation', async () => {
    render(<CpoReservations />);
    await screen.findByText('Garage plug');
    expect(screen.getAllByRole('button', { name: 'Cancel' })).toHaveLength(1);
  });

  it('cancels via ConfirmDialog with "the driver will be notified" and refetches', async () => {
    render(<CpoReservations />);
    await screen.findByText('Garage plug');

    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(await screen.findByText(/the driver will be notified/)).toBeInTheDocument();

    api.post.mockResolvedValue({});
    api.get.mockClear();
    await userEvent.click(screen.getByRole('button', { name: 'Cancel reservation' }));

    await waitFor(() => expect(api.post).toHaveBeenCalledWith('/api/reservations/1/cancel', {}));
    await waitFor(() => expect(toast.ok).toHaveBeenCalledWith('Reservation cancelled.'));
    await waitFor(() => expect(api.get).toHaveBeenCalledWith('/api/cpo/reservations?limit=20&offset=0'));
  });

  it('surfaces a cancel failure as an error toast without closing silently', async () => {
    render(<CpoReservations />);
    await screen.findByText('Garage plug');
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));

    api.post.mockRejectedValue(new Error('already fulfilled'));
    await userEvent.click(await screen.findByRole('button', { name: 'Cancel reservation' }));

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('already fulfilled'));
  });
});

describe('CpoReservations — Day view', () => {
  it('switches views via the seg toggle and fetches a page + the plug roster once', async () => {
    render(<CpoReservations />);
    await screen.findByText('Garage plug');
    api.get.mockClear();

    await userEvent.click(screen.getByRole('button', { name: 'Day' }));

    await waitFor(() => expect(api.get).toHaveBeenCalledWith('/api/cpo/plugs'));
    expect(api.get).toHaveBeenCalledWith(expect.stringContaining('/api/cpo/reservations?limit='));
    expect(await screen.findByText('Booked')).toBeInTheDocument(); // legend badge
  });

  it('renders one row per charger, with a booking placed only on its own day', async () => {
    render(<CpoReservations />);
    await screen.findByText('Garage plug');
    await userEvent.click(screen.getByRole('button', { name: 'Day' }));
    await screen.findByText('Booked');

    const dayInput = screen.getByLabelText('Day');
    fireEvent.change(dayInput, { target: { value: '2026-07-21' } });
    expect(await screen.findByTitle(/Asha.*Booked/s)).toBeInTheDocument();
    expect(screen.queryByTitle(/Rae.*Fulfilled/s)).not.toBeInTheDocument();
  });

  it('shows a retryable error instead of a fake empty timeline on failure', async () => {
    api.get.mockImplementation((url) => {
      if (url.startsWith('/api/cpo/reservations')) return Promise.reject(new Error('down'));
      return Promise.resolve(PLUGS);
    });
    render(<CpoReservations />);
    await screen.findByText("Couldn't load this"); // list view's own error, from the initial fetch
    await userEvent.click(screen.getByRole('button', { name: 'Day' }));
    expect(await screen.findAllByText("Couldn't load this")).not.toHaveLength(0);
  });
});
