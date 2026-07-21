/**
 * CpoSessions page tests (redesign v3, D5): filters drive the analytics
 * query, server-side `totals` render in the KPI strip (with a client-side
 * fallback + visible caveat for a legacy bare-array response), pagination,
 * ErrorState-with-retry (never a fake empty list), the CSV export button's
 * busy/toast states, and the session-detail modal.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

import CpoSessions from './CpoSessions';
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

vi.mock('../../contexts/ConfigContext', () => ({
  useConfig: () => ({ coin_inr_rate: 1 }),
}));

const PLUGS = [{ id: 1, name: 'Garage plug' }, { id: 2, name: 'Porch plug' }];

const SESSIONS_PAGE = {
  total: 45,
  totals: { count: 45, energy_kwh: 12.5, revenue_coins: 62.5 },
  items: [
    {
      id: 1, plug_id: 1, plug_name: 'Garage plug', user_email: 'driver@amphive.test',
      started_at: '2026-07-10T10:00:00Z', ended_at: '2026-07-10T11:00:00Z',
      duration_minutes: 60, energy_kwh: 1.5, coins_spent: 7.5, status: 'completed',
    },
    {
      id: 2, plug_id: 2, plug_name: 'Porch plug', user_email: 'other@amphive.test',
      started_at: '2026-07-09T09:00:00Z', ended_at: null,
      duration_minutes: null, energy_kwh: 0, coins_spent: 0, status: 'active',
    },
  ],
};

const SESSION_DETAIL = {
  status: 'completed',
  session_id: 1,
  plug_id: 1,
  plug_name: 'Garage plug',
  energy_kwh: 1.5,
  peak_power_w: 3000,
  price_per_kwh: 5,
  coins_spent: 7.5,
  shortfall_coins: 0,
  duration_sec: 3600,
  started_at: '2026-07-10T10:00:00Z',
  ended_at: '2026-07-10T11:00:00Z',
  reason: null,
};

const mockRoutes = ({ sessions = SESSIONS_PAGE, plugs = PLUGS } = {}) => {
  api.get.mockImplementation((url) => {
    if (url.startsWith('/api/cpo/analytics/sessions')) return Promise.resolve(sessions);
    if (url === '/api/cpo/plugs') return Promise.resolve(plugs);
    if (url === '/api/sessions/1') return Promise.resolve(SESSION_DETAIL);
    return Promise.reject(new Error(`unhandled url ${url}`));
  });
};

const renderPage = (initialEntries = ['/cpo/sessions']) =>
  render(
    <MemoryRouter initialEntries={initialEntries}>
      <CpoSessions />
    </MemoryRouter>
  );

// The fetched plug name doubles as a <select> option label, so anchoring on
// row text needs the table cell specifically (not the filter dropdown).
const findRowCell = (name) => screen.findByRole('cell', { name });
const waitForLoaded = () => findRowCell('Garage plug');

beforeEach(() => {
  vi.clearAllMocks();
  mockRoutes();
  window.fetch = vi.fn();
});

describe('CpoSessions — list + filters', () => {
  it('fetches with days/limit/offset and renders rows', async () => {
    renderPage();
    await waitForLoaded();
    expect(api.get).toHaveBeenCalledWith('/api/cpo/analytics/sessions?days=30&limit=20&offset=0');
    expect(await findRowCell('Porch plug')).toBeInTheDocument();
  });

  it('renders server-side totals in the KPI strip without a caveat', async () => {
    renderPage();
    await waitForLoaded();
    expect(screen.getByText('45')).toBeInTheDocument();
    expect(screen.queryByText(/Totals computed from/)).not.toBeInTheDocument();
  });

  it('falls back to client-side totals with a visible caveat for a legacy bare array', async () => {
    mockRoutes({ sessions: SESSIONS_PAGE.items });
    renderPage();
    await waitForLoaded();
    expect(await screen.findByText(/Totals computed from the first 2 sessions shown/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Next' })).not.toBeInTheDocument();
  });

  it('re-queries when the plug filter changes, resetting to offset 0', async () => {
    renderPage();
    await waitForLoaded();
    api.get.mockClear();
    await userEvent.selectOptions(screen.getByLabelText('Charger'), '1');
    await waitFor(() =>
      expect(api.get).toHaveBeenCalledWith('/api/cpo/analytics/sessions?days=30&limit=20&offset=0&plug_id=1')
    );
  });

  it('paginates using the server total', async () => {
    renderPage();
    await waitForLoaded();
    await userEvent.click(screen.getByRole('button', { name: 'Next' }));
    expect(api.get).toHaveBeenLastCalledWith('/api/cpo/analytics/sessions?days=30&limit=20&offset=20');
  });

  it('shows a retryable error instead of an empty state on failure', async () => {
    api.get.mockImplementation((url) => {
      if (url.startsWith('/api/cpo/analytics/sessions')) return Promise.reject(new Error('down'));
      return Promise.resolve(PLUGS);
    });
    renderPage();
    expect(await screen.findByText("Couldn't load this")).toBeInTheDocument();
    expect(screen.queryByText('No sessions found')).not.toBeInTheDocument();

    mockRoutes();
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await waitForLoaded();
  });

  it('shows the empty state only for a true zero-row result', async () => {
    mockRoutes({ sessions: { total: 0, totals: { count: 0, energy_kwh: 0, revenue_coins: 0 }, items: [] } });
    renderPage();
    expect(await screen.findByText('No sessions found')).toBeInTheDocument();
  });

  it('seeds the status filter from a ?status= deep link (CpoDashboard "Active sessions")', async () => {
    renderPage(['/cpo/sessions?status=active']);
    await waitForLoaded();
    expect(api.get).toHaveBeenCalledWith(
      '/api/cpo/analytics/sessions?days=30&limit=20&offset=0&status_filter=active'
    );
    expect(screen.getByLabelText('Status')).toHaveValue('active');
  });
});

describe('CpoSessions — CSV export', () => {
  it('downloads the filtered export and shows a success toast', async () => {
    renderPage();
    await waitForLoaded();

    const blob = new Blob(['id,plug\n'], { type: 'text/csv' });
    window.fetch.mockResolvedValue({ ok: true, blob: () => Promise.resolve(blob) });
    window.URL.createObjectURL = vi.fn(() => 'blob:mock');
    window.URL.revokeObjectURL = vi.fn();

    await userEvent.click(screen.getByRole('button', { name: /Export CSV/ }));

    await waitFor(() => expect(toast.ok).toHaveBeenCalledWith('Sessions exported.'));
    expect(window.fetch).toHaveBeenCalledWith(
      expect.stringContaining('/api/cpo/analytics/sessions.csv?days=30'),
      expect.any(Object)
    );
  });

  it('shows an error toast when the export fails', async () => {
    renderPage();
    await waitForLoaded();
    window.fetch.mockResolvedValue({ ok: false, status: 500 });

    await userEvent.click(screen.getByRole('button', { name: /Export CSV/ }));
    await waitFor(() => expect(toast.error).toHaveBeenCalled());
  });

  it('disables export while there are no sessions', async () => {
    mockRoutes({ sessions: { total: 0, totals: { count: 0, energy_kwh: 0, revenue_coins: 0 }, items: [] } });
    renderPage();
    await screen.findByText('No sessions found');
    expect(screen.getByRole('button', { name: /Export CSV/ })).toBeDisabled();
  });
});

describe('CpoSessions — detail modal', () => {
  it('opens on row click, fetches the receipt shape, and shows the driver from the row', async () => {
    renderPage();
    await userEvent.click(await waitForLoaded());

    expect(await screen.findByRole('heading', { name: 'Session detail' })).toBeInTheDocument();
    expect(api.get).toHaveBeenCalledWith('/api/sessions/1');
    const dialog = screen.getByRole('dialog');
    expect(within(dialog).getByText('driver@amphive.test')).toBeInTheDocument();
    expect(within(dialog).getByText('1.50 kWh')).toBeInTheDocument();
    expect(within(dialog).getByText('1h 0m')).toBeInTheDocument();
  });

  it('shows a toast and does not open the modal on fetch failure', async () => {
    api.get.mockImplementation((url) => {
      if (url.startsWith('/api/cpo/analytics/sessions')) return Promise.resolve(SESSIONS_PAGE);
      if (url === '/api/cpo/plugs') return Promise.resolve(PLUGS);
      if (url === '/api/sessions/1') return Promise.reject(new Error('Session not found.'));
      return Promise.reject(new Error('unhandled'));
    });
    renderPage();
    await userEvent.click(await waitForLoaded());

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Details unavailable.'));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });
});
