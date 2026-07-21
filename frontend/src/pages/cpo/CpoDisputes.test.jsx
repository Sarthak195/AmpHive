/**
 * CpoDisputes tests (redesign v3, D7): the Open-by-default status tabs,
 * best-effort session enrichment (driver email + session cost, degrading to
 * raw ids when the lookup misses), the resolve ConfirmDialog — approve with
 * a refund input defaulting to and capped at the session's cost, reject
 * requiring a note — the read-only "View session" detail modal, and the
 * usual skeleton/ErrorState/EmptyState via DataTable.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import CpoDisputes from './CpoDisputes';
import api from '../../api/client';

vi.mock('../../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

vi.mock('../../components/CpoLayout', () => ({
  default: ({ children }) => <div data-testid="cpo-layout">{children}</div>,
}));

vi.mock('../../contexts/ConfigContext', () => ({
  useConfig: () => ({ coin_inr_rate: 1 }),
}));

const toast = { ok: vi.fn(), error: vi.fn(), info: vi.fn(), warn: vi.fn() };
vi.mock('../../components/ui', async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, useToast: () => toast };
});

const OPEN_DISPUTE = {
  id: 1, session_id: 101, tenant_id: 1, driver_user_id: 42,
  reason: 'Charger stopped early', status: 'open',
  resolution_note: null, refund_coins: null,
  created_at: '2026-07-10T10:00:00Z', resolved_at: null, resolved_by_user_id: null,
};

const APPROVED_DISPUTE = {
  id: 2, session_id: 102, tenant_id: 1, driver_user_id: 43,
  reason: 'Double charged', status: 'approved',
  resolution_note: 'Verified overbill', refund_coins: 12,
  created_at: '2026-07-09T08:30:00Z', resolved_at: '2026-07-09T09:00:00Z', resolved_by_user_id: 5,
};

const SESSIONS_ENRICHMENT = {
  total: 1,
  items: [
    {
      id: 101, plug_id: 5, plug_name: 'Garage plug', user_id: 42,
      user_email: 'driver@amphive.test', started_at: '2026-07-10T09:00:00Z',
      ended_at: '2026-07-10T09:30:00Z', duration_minutes: 30,
      energy_kwh: 5, coins_spent: 40, status: 'completed',
    },
  ],
};

const SESSION_DETAIL = {
  status: 'completed', session_id: 101, plug_id: 5, plug_name: 'Garage plug',
  energy_kwh: 5, peak_power_w: 1200, price_per_kwh: 8, settled_cost_coins: 40,
  coins_spent: 40, shortfall_coins: 0, balance_before: 100, balance_remaining: 60,
  duration_sec: 1800, started_at: '2026-07-10T09:00:00Z', ended_at: '2026-07-10T09:30:00Z',
  max_kwh: null, max_duration_seconds: null, reason: null,
};

const mockApi = ({ disputes = [OPEN_DISPUTE], enrichment = SESSIONS_ENRICHMENT, sessionDetail = SESSION_DETAIL } = {}) => {
  api.get.mockImplementation((url) => {
    if (url.startsWith('/api/cpo/disputes')) return Promise.resolve(disputes);
    if (url.startsWith('/api/cpo/analytics/sessions')) return Promise.resolve(enrichment);
    if (url === '/api/sessions/101') return Promise.resolve(sessionDetail);
    return Promise.reject(new Error(`unhandled url ${url}`));
  });
};

const renderPage = () => render(<CpoDisputes />);

beforeEach(() => {
  vi.clearAllMocks();
  mockApi();
});

describe('CpoDisputes', () => {
  it('defaults to the Open tab and fetches with status_filter=open', async () => {
    renderPage();
    await screen.findByText('Charger stopped early');

    expect(api.get).toHaveBeenCalledWith(expect.stringContaining('status_filter=open'));
    expect(screen.getByRole('tab', { name: 'Open' })).toHaveAttribute('aria-selected', 'true');
  });

  it('enriches the row with driver email and session cost from the best-effort lookup', async () => {
    renderPage();

    expect(await screen.findByText('driver@amphive.test')).toBeInTheDocument();
    expect(screen.getByText('₹40.00')).toBeInTheDocument();
    expect(screen.getByText(/Garage plug/)).toBeInTheDocument();
  });

  it('falls back to a raw driver id and "—" cost when enrichment has no match', async () => {
    mockApi({ enrichment: { total: 0, items: [] } });
    renderPage();

    await screen.findByText('Charger stopped early');
    expect(screen.getByText('Driver #42')).toBeInTheDocument();
  });

  it('switches tabs and refetches with the matching status_filter (or none for All)', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('Charger stopped early');

    mockApi({ disputes: [APPROVED_DISPUTE] });
    await user.click(screen.getByRole('tab', { name: 'Approved' }));
    expect(await screen.findByText('Double charged')).toBeInTheDocument();
    expect(api.get).toHaveBeenCalledWith(expect.stringContaining('status_filter=approved'));

    mockApi({ disputes: [OPEN_DISPUTE, APPROVED_DISPUTE] });
    await user.click(screen.getByRole('tab', { name: 'All' }));
    await screen.findByText('Charger stopped early');
    const lastCall = api.get.mock.calls.filter(([u]) => u.startsWith('/api/cpo/disputes')).pop();
    expect(lastCall[0]).not.toContain('status_filter');
  });

  it('shows the resolved status, refund and note on a non-open row with no actions', async () => {
    mockApi({ disputes: [APPROVED_DISPUTE] });
    renderPage();

    await screen.findByText('Double charged');
    const row = screen.getAllByRole('row').find((r) => r.textContent.includes('Double charged'));
    expect(within(row).getByText('Approved')).toBeInTheDocument();
    expect(within(row).getByText(/Verified overbill/)).toBeInTheDocument();
    expect(within(row).queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument();
  });

  it('opens the read-only session detail modal on "View session"', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('Charger stopped early');

    await user.click(screen.getByRole('button', { name: /#101/ }));

    expect(await screen.findByText('Session detail')).toBeInTheDocument();
    expect(api.get).toHaveBeenCalledWith('/api/sessions/101');
    await waitFor(() => expect(screen.getByText('5.00 kWh')).toBeInTheDocument());
  });

  it('approves with a refund defaulting to the session cost', async () => {
    api.post.mockResolvedValue({ id: 1, status: 'approved', refund_coins: 40 });
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('Charger stopped early');

    await user.click(screen.getByRole('button', { name: 'Approve' }));
    expect(await screen.findByText('Approve dispute')).toBeInTheDocument();

    const refundInput = await screen.findByLabelText('Refund amount');
    await waitFor(() => expect(refundInput).toHaveValue(40));

    await user.click(screen.getByRole('button', { name: 'Approve & refund' }));

    expect(api.post).toHaveBeenCalledWith('/api/cpo/disputes/1/resolve', { action: 'approve', refund_coins: 40 });
    await waitFor(() => expect(toast.ok).toHaveBeenCalled());
  });

  it('rejects a refund amount above the session cost', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('Charger stopped early');

    await user.click(screen.getByRole('button', { name: 'Approve' }));
    const refundInput = await screen.findByLabelText('Refund amount');
    await waitFor(() => expect(refundInput).toHaveValue(40));

    await user.clear(refundInput);
    await user.type(refundInput, '9999');
    await user.click(screen.getByRole('button', { name: 'Approve & refund' }));

    expect(await screen.findByText(/can't exceed this session's cost/)).toBeInTheDocument();
    expect(api.post).not.toHaveBeenCalled();
  });

  it('requires a note to reject a dispute', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('Charger stopped early');

    await user.click(screen.getByRole('button', { name: 'Reject' }));
    expect(await screen.findByRole('heading', { name: 'Reject dispute' })).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Reject dispute' }));
    expect(await screen.findByText('A note is required to reject a dispute.')).toBeInTheDocument();
    expect(api.post).not.toHaveBeenCalled();

    await user.type(screen.getByLabelText('Note'), 'Not a valid claim');
    await user.click(screen.getByRole('button', { name: 'Reject dispute' }));

    expect(api.post).toHaveBeenCalledWith('/api/cpo/disputes/1/resolve', {
      action: 'reject',
      note: 'Not a valid claim',
    });
  });

  it('surfaces a resolve 409 inline and keeps the dialog open', async () => {
    api.post.mockRejectedValue(new Error('This dispute was already resolved (approved).'));
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('Charger stopped early');

    await user.click(screen.getByRole('button', { name: 'Approve' }));
    await waitFor(() => expect(screen.getByLabelText('Refund amount')).toHaveValue(40));
    await user.click(screen.getByRole('button', { name: 'Approve & refund' }));

    expect(await screen.findByText('This dispute was already resolved (approved).')).toBeInTheDocument();
    expect(screen.getByText('Approve dispute')).toBeInTheDocument();
  });

  it('shows a retryable ErrorState on fetch failure', async () => {
    api.get.mockImplementation((url) => {
      if (url.startsWith('/api/cpo/disputes')) return Promise.reject(new Error('Network down'));
      return Promise.resolve({ total: 0, items: [] });
    });
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByText("Couldn't load this")).toBeInTheDocument();
    mockApi();
    await user.click(screen.getByRole('button', { name: 'Retry' }));
    await screen.findByText('Charger stopped early');
  });

  it('shows an empty state (not an error) when there are no open disputes', async () => {
    mockApi({ disputes: [] });
    renderPage();
    expect(await screen.findByText('No open disputes')).toBeInTheDocument();
  });
});
