/**
 * CpoEarnings tests: the earnings page renders the lifetime/unsettled
 * summary (with the ₹-equivalent labelling and the unsettled window),
 * drives request-payout through its confirm dialog (success refreshes,
 * a 409 shows the backend message inline), lists payout history rows with
 * status badges, cancels a REQUESTED payout, and gates "Mark paid" to the
 * admin role.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

import CpoEarnings from './CpoEarnings';
import api from '../../api/client';

vi.mock('../../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

// Mutable auth state so individual tests can flip the role to 'admin'.
const authState = vi.hoisted(() => ({ user: null }));
vi.mock('../../contexts/AuthContext', () => ({
  useAuth: () => authState,
}));

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
  {
    id: 1, tenant_id: 1,
    period_start: '2026-04-01T00:00:00+00:00', period_end: '2026-05-01T00:00:00+00:00',
    gross_coins: 100.0, platform_fee_coins: 10.0, net_coins: 90.0,
    status: 'cancelled', requested_by_user_id: 1,
    requested_at: '2026-05-01T09:00:00+00:00', paid_at: null, note: null,
  },
];

const PROFILE = { tenant: { name: 'Acme Charging' } };

const mockApiGet = (earnings = EARNINGS, payouts = PAYOUTS, profile = PROFILE) => {
  api.get.mockImplementation((url) => {
    if (url === '/api/cpo/earnings') return Promise.resolve(earnings);
    if (url === '/api/cpo/payouts') return Promise.resolve(payouts);
    if (url === '/api/cpo/profile') return Promise.resolve(profile);
    return Promise.resolve([]);
  });
};

const renderPage = () => render(
  <MemoryRouter>
    <CpoEarnings />
  </MemoryRouter>
);

/**
 * Open the request-payout confirm dialog and return its modal element.
 */
const openRequestDialog = async (user) => {
  await user.click(screen.getByRole('button', { name: 'Request payout' }));
  const intro = await screen.findByText(/This snapshots your unsettled earnings/);
  return intro.closest('.modal-content');
};

beforeEach(() => {
  vi.clearAllMocks();
  authState.user = { email: 'cpo@amphive.test', full_name: 'Ops', role: 'cpo' };
});

describe('CpoEarnings', () => {
  it('renders lifetime + unsettled summary cards with the ₹-equivalent label and window', async () => {
    mockApiGet();
    renderPage();

    expect(await screen.findByText('Lifetime earnings')).toBeInTheDocument();
    expect(screen.getByText('Unsettled earnings')).toBeInTheDocument();

    // Lifetime gross / fee / net
    expect(screen.getByText('🪙 1234.50')).toBeInTheDocument();
    expect(screen.getByText('🪙 123.45')).toBeInTheDocument();
    expect(screen.getByText('🪙 1111.05')).toBeInTheDocument();

    // Unsettled gross / fee / net + its window dates
    expect(screen.getByText('🪙 200.00')).toBeInTheDocument();
    expect(screen.getByText('🪙 20.00')).toBeInTheDocument();
    expect(screen.getByText('🪙 180.00')).toBeInTheDocument();
    expect(screen.getByText(/Window:.*2026/)).toBeInTheDocument();

    // Fee percentage from the API, and the coin denomination note
    expect(screen.getAllByText(/Platform fee \(10%\)/).length).toBe(2);
    expect(screen.getByText(/1 coin = ₹1/)).toBeInTheDocument();
  });

  it('renders payout history rows with period, amounts, status badge, and timestamps', async () => {
    mockApiGet();
    renderPage();

    await screen.findByText('🪙 450.00'); // requested row's net
    const rows = screen.getAllByRole('row');

    const requestedRow = rows.find((r) => r.textContent.includes('#3'));
    expect(within(requestedRow).getByText('requested')).toBeInTheDocument();
    expect(within(requestedRow).getByText('🪙 500.00')).toBeInTheDocument(); // gross
    expect(within(requestedRow).getByText('🪙 50.00')).toBeInTheDocument(); // fee

    const paidRow = rows.find((r) => r.textContent.includes('#2'));
    expect(within(paidRow).getByText('paid')).toBeInTheDocument();
    expect(within(paidRow).getByText('🪙 270.00')).toBeInTheDocument();
    // Paid rows carry both timestamps and no actions
    expect(within(paidRow).queryByRole('button')).not.toBeInTheDocument();

    const cancelledRow = rows.find((r) => r.textContent.includes('#1'));
    expect(within(cancelledRow).getByText('cancelled')).toBeInTheDocument();
  });

  it('requests a payout through the confirm dialog and refreshes on success', async () => {
    mockApiGet();
    api.post.mockResolvedValue({ id: 4, status: 'requested' });
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('Lifetime earnings');

    const modal = await openRequestDialog(user);
    expect(within(modal).getByText('🪙 180.00')).toBeInTheDocument(); // unsettled net in the dialog
    await user.click(within(modal).getByRole('button', { name: 'Request payout' }));

    expect(api.post).toHaveBeenCalledWith('/api/cpo/payouts', {});
    await waitFor(() =>
      expect(screen.queryByText(/This snapshots your unsettled earnings/)).not.toBeInTheDocument()
    );
    // Initial load + refresh after the successful request
    const earningsCalls = api.get.mock.calls.filter(([url]) => url === '/api/cpo/earnings');
    expect(earningsCalls.length).toBe(2);
  });

  it('surfaces the backend 409 message inline and keeps the dialog open', async () => {
    mockApiGet();
    api.post.mockRejectedValue(new Error('A payout request is already pending for this tenant.'));
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('Lifetime earnings');

    const modal = await openRequestDialog(user);
    await user.click(within(modal).getByRole('button', { name: 'Request payout' }));

    expect(
      await screen.findByText('A payout request is already pending for this tenant.')
    ).toBeInTheDocument();
    // Dialog stays open so the message is visible in context
    expect(screen.getByText(/This snapshots your unsettled earnings/)).toBeInTheDocument();
  });

  it('surfaces the backend 400 (nothing unsettled) message inline', async () => {
    mockApiGet();
    api.post.mockRejectedValue(new Error('No unsettled earnings to pay out.'));
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('Lifetime earnings');

    const modal = await openRequestDialog(user);
    await user.click(within(modal).getByRole('button', { name: 'Request payout' }));

    expect(await screen.findByText('No unsettled earnings to pay out.')).toBeInTheDocument();
  });

  it('cancels a REQUESTED payout after confirmation', async () => {
    mockApiGet();
    api.post.mockResolvedValue({ id: 3, status: 'cancelled' });
    const user = userEvent.setup();
    renderPage();

    await screen.findByText('🪙 450.00');
    // Only the requested row (#3) offers Cancel
    await user.click(screen.getByRole('button', { name: 'Cancel' }));

    const confirmBtn = await screen.findByRole('button', { name: 'Cancel payout' });
    await user.click(confirmBtn);

    expect(api.post).toHaveBeenCalledWith('/api/cpo/payouts/3/cancel', {});
  });

  it('does not offer Mark paid to a cpo user', async () => {
    mockApiGet();
    renderPage();

    await screen.findByText('🪙 450.00');
    expect(screen.queryByRole('button', { name: 'Mark paid' })).not.toBeInTheDocument();
  });

  it('offers Mark paid to an admin and posts mark_paid after confirmation', async () => {
    authState.user = { email: 'admin@amphive.test', full_name: 'Root', role: 'admin' };
    mockApiGet();
    api.post.mockResolvedValue({ id: 3, status: 'paid' });
    const user = userEvent.setup();
    renderPage();

    await screen.findByText('🪙 450.00');
    await user.click(screen.getByRole('button', { name: 'Mark paid' }));

    // Confirm dialog explains this records an out-of-band transfer
    expect(await screen.findByText(/does not move money/)).toBeInTheDocument();
    const dialog = screen.getByText(/does not move money/).closest('.modal-content');
    await user.click(within(dialog).getByRole('button', { name: 'Mark paid' }));

    expect(api.post).toHaveBeenCalledWith('/api/cpo/payouts/3/mark_paid', {});
  });
});
