/**
 * CpoEarnings tests (redesign v3, D7): the lifetime/unsettled summary cards
 * (₹-first via <Money/>, unsettled window, platform fee %), request-payout
 * through its ConfirmDialog (disabled-with-reason when nothing's unsettled
 * or a request is already pending; a 400/409 shows the backend message
 * inline and keeps the dialog open), payout history with a status badge,
 * cancelling a REQUESTED payout, and — per the redesign brief — NO
 * embedded "Mark paid" action (the admin portal owns that).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import CpoEarnings from './CpoEarnings';
import api from '../../api/client';

vi.mock('../../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

vi.mock('../../components/CpoLayout', () => ({
  default: ({ children }) => <div data-testid="cpo-layout">{children}</div>,
}));

const toast = { ok: vi.fn(), error: vi.fn(), info: vi.fn(), warn: vi.fn() };
vi.mock('../../components/ui', async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, useToast: () => toast };
});

const EARNINGS = {
  watermark: '2026-06-01T00:00:00+00:00',
  as_of: '2026-07-12T10:00:00+00:00',
  platform_fee_pct: 10.0,
  lifetime: { gross_coins: 1234.5, platform_fee_coins: 123.45, net_coins: 1111.05 },
  unsettled: {
    period_start: '2026-06-01T00:00:00+00:00',
    period_end: '2026-07-12T10:00:00+00:00',
    gross_coins: 200.0,
    platform_fee_coins: 20.0,
    net_coins: 180.0,
  },
};

const PAYOUTS = [
  {
    id: 3, tenant_id: 1,
    period_start: '2026-06-01T00:00:00+00:00', period_end: '2026-07-01T00:00:00+00:00',
    gross_coins: 500.0, platform_fee_coins: 50.0, net_coins: 450.0,
    status: 'requested', requested_by_user_id: 1,
    requested_at: '2026-07-01T09:00:00+00:00', paid_at: null, note: null,
  },
  {
    id: 2, tenant_id: 1,
    period_start: '2026-05-01T00:00:00+00:00', period_end: '2026-06-01T00:00:00+00:00',
    gross_coins: 300.0, platform_fee_coins: 30.0, net_coins: 270.0,
    status: 'paid', requested_by_user_id: 1,
    requested_at: '2026-06-01T09:00:00+00:00', paid_at: '2026-06-02T12:00:00+00:00', note: null,
  },
];

// A payout history with nothing REQUESTED — used by the tests that need the
// "Request payout" button enabled (the default PAYOUTS fixture always has a
// pending #3, which is itself the subject of the disabled-reason test).
const PAYOUTS_NO_PENDING = [PAYOUTS[1]];

const mockApiGet = ({ earnings = EARNINGS, payouts = PAYOUTS } = {}) => {
  api.get.mockImplementation((url) => {
    if (url === '/api/cpo/earnings') return Promise.resolve(earnings);
    if (url === '/api/cpo/payouts') return Promise.resolve(payouts);
    return Promise.reject(new Error(`unhandled url ${url}`));
  });
};

const renderPage = () => render(<CpoEarnings />);

const openRequestDialog = async (user) => {
  await user.click(screen.getByRole('button', { name: 'Request payout' }));
  await screen.findByRole('heading', { name: 'Request payout' });
  return document.querySelector('.modal');
};

beforeEach(() => {
  vi.clearAllMocks();
  mockApiGet();
});

describe('CpoEarnings', () => {
  it('renders lifetime + unsettled summary cards with ₹ amounts and the unsettled window', async () => {
    renderPage();

    expect(await screen.findByText('Lifetime earnings')).toBeInTheDocument();
    expect(screen.getByText('Unsettled earnings')).toBeInTheDocument();
    expect(screen.getByText('₹1,234.50')).toBeInTheDocument();
    expect(screen.getByText('₹1,111.05')).toBeInTheDocument();
    expect(screen.getByText('₹180.00')).toBeInTheDocument();
    expect(screen.getByText(/Window:.*2026/)).toBeInTheDocument();
    expect(screen.getAllByText(/Platform fee \(10%\)/).length).toBe(2);
  });

  it('renders payout history with status badges and no Mark paid action anywhere', async () => {
    renderPage();

    await screen.findByText('₹450.00');
    const rows = screen.getAllByRole('row');
    const requestedRow = rows.find((r) => r.textContent.includes('#3'));
    expect(within(requestedRow).getByText('Requested')).toBeInTheDocument();
    expect(within(requestedRow).getByRole('button', { name: 'Cancel' })).toBeInTheDocument();

    const paidRow = rows.find((r) => r.textContent.includes('#2'));
    expect(within(paidRow).getByText('Paid')).toBeInTheDocument();
    expect(within(paidRow).queryByRole('button')).not.toBeInTheDocument();

    expect(screen.queryByRole('button', { name: 'Mark paid' })).not.toBeInTheDocument();
  });

  it('requests a payout through the confirm dialog and refreshes on success', async () => {
    mockApiGet({ payouts: PAYOUTS_NO_PENDING });
    api.post.mockResolvedValue({ id: 4, status: 'requested' });
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('Lifetime earnings');

    const modal = await openRequestDialog(user);
    expect(within(modal).getByText('₹180.00')).toBeInTheDocument();
    await user.click(within(modal).getByRole('button', { name: 'Request payout' }));

    expect(api.post).toHaveBeenCalledWith('/api/cpo/payouts', {});
    await waitFor(() => expect(screen.queryByRole('heading', { name: 'Request payout' })).not.toBeInTheDocument());
    expect(api.get.mock.calls.filter(([u]) => u === '/api/cpo/earnings').length).toBe(2);
    expect(toast.ok).toHaveBeenCalled();
  });

  it('surfaces a 409 (already pending) inline and keeps the dialog open', async () => {
    mockApiGet({ payouts: PAYOUTS_NO_PENDING });
    api.post.mockRejectedValue(new Error('A payout request is already pending for this tenant.'));
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('Lifetime earnings');

    const modal = await openRequestDialog(user);
    await user.click(within(modal).getByRole('button', { name: 'Request payout' }));

    expect(await screen.findByText('A payout request is already pending for this tenant.')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Request payout' })).toBeInTheDocument();
  });

  it('disables Request payout with a reason when a request is already pending', async () => {
    renderPage();
    await screen.findByText('Lifetime earnings');

    expect(screen.getByRole('button', { name: 'Request payout' })).toBeDisabled();
    expect(screen.getByText('A payout request is already pending.')).toBeInTheDocument();
  });

  it('disables Request payout with a reason when there is nothing unsettled', async () => {
    mockApiGet({
      earnings: { ...EARNINGS, unsettled: { ...EARNINGS.unsettled, net_coins: 0 } },
      payouts: [],
    });
    renderPage();
    await screen.findByText('Lifetime earnings');

    expect(screen.getByRole('button', { name: 'Request payout' })).toBeDisabled();
    expect(screen.getByText("There's nothing unsettled to pay out yet.")).toBeInTheDocument();
  });

  it('cancels a REQUESTED payout after confirmation', async () => {
    api.post.mockResolvedValue({ id: 3, status: 'cancelled' });
    const user = userEvent.setup();
    renderPage();

    await screen.findByText('₹450.00');
    await user.click(screen.getByRole('button', { name: 'Cancel' }));

    const confirmBtn = await screen.findByRole('button', { name: 'Cancel payout' });
    await user.click(confirmBtn);

    expect(api.post).toHaveBeenCalledWith('/api/cpo/payouts/3/cancel', {});
    await waitFor(() => expect(toast.ok).toHaveBeenCalled());
  });

  it('shows a retryable ErrorState on fetch failure', async () => {
    api.get.mockImplementation((url) => {
      if (url === '/api/cpo/earnings') return Promise.reject(new Error('Network down'));
      return Promise.resolve([]);
    });
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByText("Couldn't load this")).toBeInTheDocument();
    mockApiGet();
    await user.click(screen.getByRole('button', { name: 'Retry' }));
    await screen.findByText('Lifetime earnings');
  });
});
