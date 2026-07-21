/**
 * Activity page tests (redesign v3, C6): the sessions/disputes tabs, the
 * paginated-vs-legacy-array session history shapes, ErrorState-with-retry on
 * both tabs (never a fake empty list), the session-detail modal fetch (+ its
 * 404 toast path), and the "Report an issue" hand-off into DisputeModal
 * (which refetches the disputes tab on submit).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import Activity from './Activity';
import api from '../api/client';

vi.mock('../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

const toast = { ok: vi.fn(), error: vi.fn(), info: vi.fn(), warn: vi.fn() };
vi.mock('../components/ui', async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, useToast: () => toast };
});

let disputeModalProps = null;
vi.mock('../components/DisputeModal', () => ({
  default: (props) => {
    disputeModalProps = props;
    return props.open ? <div data-testid="dispute-modal">dispute for {props.sessionId}</div> : null;
  },
}));

vi.mock('../contexts/ConfigContext', () => ({
  useConfig: () => ({ coin_inr_rate: 1 }),
}));

const SESSIONS = [
  {
    id: 1,
    plug_id: 2,
    plug_name: 'Garage plug',
    started_at: '2026-07-10T10:00:00Z',
    energy_kwh: 1.234,
    coins_spent: 6.17,
    status: 'completed',
  },
  {
    id: 2,
    plug_id: 3,
    plug_name: 'Porch plug',
    started_at: '2026-07-09T10:00:00Z',
    energy_kwh: 0.5,
    coins_spent: 0,
    status: 'cancelled',
  },
];

const SESSION_DETAIL = {
  status: 'completed',
  session_id: 1,
  plug_id: 2,
  plug_name: 'Garage plug',
  energy_kwh: 1.234,
  peak_power_w: 3200,
  price_per_kwh: 5,
  coins_spent: 6.17,
  shortfall_coins: 0,
  balance_before: 100,
  balance_remaining: 93.83,
  duration_sec: 3725,
  started_at: '2026-07-10T10:00:00Z',
  ended_at: '2026-07-10T11:02:05Z',
  reason: null,
};

const DISPUTES = [
  {
    id: 9,
    session_id: 1,
    status: 'open',
    reason: '[Billing] Overcharged for the session.',
    resolution_note: null,
    refund_coins: null,
    created_at: '2026-07-11T08:00:00Z',
  },
  {
    id: 10,
    session_id: 4,
    status: 'approved',
    reason: '[Charger problem] Stopped early.',
    resolution_note: 'Confirmed a fault — refunded.',
    refund_coins: 3.5,
    created_at: '2026-07-05T08:00:00Z',
  },
];

const mockApiRoutes = ({ sessions = SESSIONS, disputes = DISPUTES } = {}) => {
  api.get.mockImplementation((url) => {
    if (url.startsWith('/api/sessions/history')) return Promise.resolve(sessions);
    if (url === '/api/sessions/disputes/my') return Promise.resolve(disputes);
    if (url === '/api/sessions/1') return Promise.resolve(SESSION_DETAIL);
    return Promise.reject(new Error(`unhandled url ${url}`));
  });
};

beforeEach(() => {
  vi.clearAllMocks();
  disputeModalProps = null;
  mockApiRoutes();
});

describe('Activity — sessions tab (default)', () => {
  it('fetches history with limit/offset and renders rows', async () => {
    render(<Activity />);
    expect(await screen.findByText('Garage plug')).toBeInTheDocument();
    expect(api.get).toHaveBeenCalledWith('/api/sessions/history?limit=20&offset=0');
    expect(screen.getByText('Porch plug')).toBeInTheDocument();
  });

  it('shows a retryable error instead of an empty state on failure', async () => {
    api.get.mockImplementation((url) => {
      if (url.startsWith('/api/sessions/history')) return Promise.reject(new Error('down'));
      return Promise.resolve(DISPUTES);
    });
    render(<Activity />);

    expect(await screen.findByText("Couldn't load this")).toBeInTheDocument();
    expect(screen.queryByText('No charging sessions yet')).not.toBeInTheDocument();

    mockApiRoutes();
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('Garage plug')).toBeInTheDocument();
  });

  it('shows the empty state only for a true zero-row result', async () => {
    mockApiRoutes({ sessions: [] });
    render(<Activity />);
    expect(await screen.findByText('No charging sessions yet')).toBeInTheDocument();
  });

  it('omits the pager for the legacy bare-array shape', async () => {
    render(<Activity />);
    await screen.findByText('Garage plug');
    expect(screen.queryByRole('button', { name: 'Next' })).not.toBeInTheDocument();
  });

  it('paginates when the backend returns the {total, items} shape', async () => {
    mockApiRoutes({ sessions: { total: 45, items: SESSIONS } });
    render(<Activity />);
    await screen.findByText('Garage plug');

    const next = screen.getByRole('button', { name: 'Next' });
    expect(next).toBeEnabled();
    await userEvent.click(next);
    expect(api.get).toHaveBeenLastCalledWith('/api/sessions/history?limit=20&offset=20');
  });

  it('renders a status badge via sessionStatusLabel', async () => {
    render(<Activity />);
    await screen.findByText('Garage plug');
    expect(screen.getByText('Completed')).toBeInTheDocument();
    expect(screen.getByText('Cancelled')).toBeInTheDocument();
  });
});

describe('Activity — session detail modal', () => {
  it('opens the detail modal on row click with energy/duration/cost', async () => {
    render(<Activity />);
    await userEvent.click(await screen.findByText('Garage plug'));

    expect(await screen.findByRole('heading', { name: 'Session detail' })).toBeInTheDocument();
    expect(api.get).toHaveBeenCalledWith('/api/sessions/1');
    const dialog = screen.getByRole('dialog');
    expect(within(dialog).getByText('1.23 kWh')).toBeInTheDocument();
    expect(within(dialog).getByText('1h 2m')).toBeInTheDocument();
  });

  it('shows a "Details unavailable" toast and does not open the modal on fetch failure', async () => {
    api.get.mockImplementation((url) => {
      if (url.startsWith('/api/sessions/history')) return Promise.resolve(SESSIONS);
      if (url === '/api/sessions/disputes/my') return Promise.resolve(DISPUTES);
      if (url === '/api/sessions/1') return Promise.reject(new Error('Session not found.'));
      return Promise.reject(new Error('unhandled'));
    });
    render(<Activity />);
    await userEvent.click(await screen.findByText('Garage plug'));

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Details unavailable.'));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('offers the invoice action only for a billed session with a cost, and hands off to DisputeModal', async () => {
    render(<Activity />);
    await userEvent.click(await screen.findByText('Garage plug'));
    await screen.findByRole('heading', { name: 'Session detail' });

    expect(screen.getByRole('button', { name: 'View GST invoice' })).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Report an issue' }));

    expect(screen.queryByRole('heading', { name: 'Session detail' })).not.toBeInTheDocument();
    expect(await screen.findByTestId('dispute-modal')).toHaveTextContent('dispute for 1');
  });

  it('opens the GST invoice via a raw token-bearing fetch and defers revoking the blob URL', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, blob: () => Promise.resolve(new Blob()) });
    vi.stubGlobal('fetch', fetchMock);
    const revokeSpy = vi.fn();
    vi.stubGlobal('URL', { ...URL, createObjectURL: vi.fn().mockReturnValue('blob:mock'), revokeObjectURL: revokeSpy });
    const openSpy = vi.spyOn(window, 'open').mockImplementation(() => {});
    const setTimeoutSpy = vi.spyOn(window, 'setTimeout');

    render(<Activity />);
    await userEvent.click(await screen.findByText('Garage plug'));
    await screen.findByRole('heading', { name: 'Session detail' });
    await userEvent.click(screen.getByRole('button', { name: 'View GST invoice' }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining('/api/sessions/1/invoice?format=html'),
        expect.any(Object)
      );
    });
    expect(openSpy).toHaveBeenCalledWith('blob:mock', '_blank');

    expect(revokeSpy).not.toHaveBeenCalled();
    const deferredRevoke = setTimeoutSpy.mock.calls.find(([, delay]) => delay === 60_000);
    expect(deferredRevoke).toBeTruthy();
    deferredRevoke[0]();
    expect(revokeSpy).toHaveBeenCalledWith('blob:mock');
  });

  it('refetches disputes when DisputeModal reports a submission', async () => {
    render(<Activity />);
    await userEvent.click(await screen.findByText('Garage plug'));
    await screen.findByRole('heading', { name: 'Session detail' });
    await userEvent.click(screen.getByRole('button', { name: 'Report an issue' }));
    await screen.findByTestId('dispute-modal');

    api.get.mockClear();
    disputeModalProps.onSubmitted();
    await waitFor(() => expect(api.get).toHaveBeenCalledWith('/api/sessions/disputes/my'));
  });
});

describe('Activity — issue reports tab', () => {
  const openDisputesTab = async () => {
    render(<Activity />);
    await screen.findByText('Garage plug');
    await userEvent.click(screen.getByRole('tab', { name: /Issue reports/ }));
  };

  it('lists disputes with status badges and resolution copy', async () => {
    await openDisputesTab();
    expect(await screen.findByText('Under review')).toBeInTheDocument();
    expect(screen.getByText('Approved')).toBeInTheDocument();
    expect(screen.getByText('Awaiting review')).toBeInTheDocument();
    expect(screen.getByText(/Confirmed a fault — refunded\./)).toBeInTheDocument();
    expect(screen.getByText(/refunded/)).toBeInTheDocument();
  });

  it('shows a retryable error instead of an empty state on failure', async () => {
    api.get.mockImplementation((url) => {
      if (url.startsWith('/api/sessions/history')) return Promise.resolve(SESSIONS);
      if (url === '/api/sessions/disputes/my') return Promise.reject(new Error('down'));
      return Promise.reject(new Error('unhandled'));
    });
    await openDisputesTab();
    expect(await screen.findByText("Couldn't load this")).toBeInTheDocument();
    expect(screen.queryByText('No issue reports')).not.toBeInTheDocument();
  });

  it('shows the empty state only for a true zero-row result', async () => {
    mockApiRoutes({ disputes: [] });
    await openDisputesTab();
    expect(await screen.findByText('No issue reports')).toBeInTheDocument();
  });
});
