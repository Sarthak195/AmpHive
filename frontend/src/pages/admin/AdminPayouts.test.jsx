/**
 * AdminPayouts tests: cross-tenant payout list defaults to Requested, the
 * status seg switches the query param and resets to page 1, DataTable's
 * pagination footer pages through, "Mark paid" runs through ConfirmDialog
 * and calls the CPO mark_paid route, and a load failure shows a retryable
 * error (never a clean-looking empty state).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import AdminPayouts from './AdminPayouts';
import api from '../../api/client';

vi.mock('../../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

vi.mock('../../contexts/ConfigContext', () => ({
  useConfig: () => ({ coin_inr_rate: 1 }),
}));

const toast = { ok: vi.fn(), error: vi.fn(), info: vi.fn(), warn: vi.fn() };
vi.mock('../../components/ui', async () => {
  const actual = await vi.importActual('../../components/ui');
  return { ...actual, useToast: () => toast };
});

const PAYOUT_REQUESTED = {
  id: 7,
  tenant_id: 2,
  tenant_name: 'Acme Charging',
  period_start: '2026-07-01T00:00:00Z',
  period_end: '2026-07-14T00:00:00Z',
  gross_coins: 10000,
  platform_fee_coins: 1000,
  net_coins: 9000,
  status: 'requested',
  requested_by_user_id: 5,
  requested_at: '2026-07-14T10:00:00Z',
  paid_at: null,
  note: null,
};

const PAYOUT_PAID = {
  ...PAYOUT_REQUESTED,
  id: 8,
  tenant_name: 'Volt Retail',
  status: 'paid',
  paid_at: '2026-07-15T10:00:00Z',
};

const page = (items, total) => ({ total, items });

const renderPage = () => render(<AdminPayouts />);

beforeEach(() => {
  vi.clearAllMocks();
});

describe('AdminPayouts list', () => {
  it('defaults to the Requested tab and lists gross/fee/net/status', async () => {
    api.get.mockResolvedValue(page([PAYOUT_REQUESTED], 1));
    renderPage();

    await screen.findByText('Acme Charging');
    expect(screen.getByText('₹10,000.00')).toBeInTheDocument(); // gross
    expect(screen.getByText('₹1,000.00')).toBeInTheDocument(); // fee
    expect(screen.getByText('₹9,000.00')).toBeInTheDocument(); // net
    expect(screen.getByText('requested')).toBeInTheDocument();

    const lastUrl = api.get.mock.calls.at(-1)[0];
    expect(lastUrl).toContain('status=requested');
    expect(screen.getByRole('button', { name: 'Requested' })).toHaveAttribute(
      'aria-selected',
      'true'
    );
  });

  it('shows a retryable error instead of an empty state on failure, then recovers', async () => {
    api.get.mockRejectedValueOnce(new Error('down'));
    renderPage();

    expect(await screen.findByText("Couldn't load this")).toBeInTheDocument();
    expect(screen.queryByText('No payouts here')).not.toBeInTheDocument();

    api.get.mockResolvedValue(page([PAYOUT_REQUESTED], 1));
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('Acme Charging')).toBeInTheDocument();
  });

  it('shows the empty state only for a true zero-row result', async () => {
    api.get.mockResolvedValue(page([], 0));
    renderPage();
    expect(await screen.findByText('No payouts here')).toBeInTheDocument();
  });

  it('switches tabs into the status param and resets to page 1', async () => {
    api.get.mockResolvedValue(page([PAYOUT_REQUESTED], 1));
    renderPage();
    await screen.findByText('Acme Charging');
    api.get.mockClear();

    api.get.mockResolvedValue(page([PAYOUT_PAID], 1));
    await userEvent.click(screen.getByRole('button', { name: 'Paid' }));

    await waitFor(() => {
      const lastUrl = api.get.mock.calls.at(-1)[0];
      expect(lastUrl).toContain('status=paid');
      expect(lastUrl).toContain('offset=0');
    });
    expect(await screen.findByText('Volt Retail')).toBeInTheDocument();
  });

  it('omits the status param entirely on the All tab', async () => {
    api.get.mockResolvedValue(page([PAYOUT_REQUESTED], 1));
    renderPage();
    await screen.findByText('Acme Charging');
    api.get.mockClear();

    api.get.mockResolvedValue(page([PAYOUT_REQUESTED, PAYOUT_PAID], 2));
    await userEvent.click(screen.getByRole('button', { name: 'All' }));

    await waitFor(() => {
      const lastUrl = api.get.mock.calls.at(-1)[0];
      expect(lastUrl).not.toContain('status=');
    });
  });

  it('pages forward through DataTable pagination', async () => {
    api.get.mockResolvedValue(page([PAYOUT_REQUESTED], 45));
    renderPage();
    await screen.findByText('Acme Charging');
    api.get.mockClear();

    api.get.mockResolvedValue(page([PAYOUT_PAID], 45));
    await userEvent.click(screen.getByRole('button', { name: 'Next' }));

    await waitFor(() => {
      const lastUrl = api.get.mock.calls.at(-1)[0];
      expect(lastUrl).toContain('offset=20');
    });
  });
});

describe('mark paid', () => {
  it('only shows Mark paid for requested rows', async () => {
    api.get.mockResolvedValue(page([PAYOUT_REQUESTED, PAYOUT_PAID], 2));
    renderPage();
    await screen.findByText('Acme Charging');

    expect(screen.getAllByRole('button', { name: 'Mark paid' })).toHaveLength(1);
  });

  it('confirms, calls the CPO mark_paid route, toasts and refetches', async () => {
    api.get.mockResolvedValue(page([PAYOUT_REQUESTED], 1));
    api.post.mockResolvedValue({ ...PAYOUT_REQUESTED, status: 'paid' });
    renderPage();
    await screen.findByText('Acme Charging');

    await userEvent.click(screen.getByRole('button', { name: 'Mark paid' }));
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveTextContent('Mark payout #7 paid?');
    expect(dialog).toHaveTextContent('Confirm the transfer happened outside AmpHive first');

    await userEvent.click(within(dialog).getByRole('button', { name: 'Mark paid' }));

    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith('/api/cpo/payouts/7/mark_paid', {})
    );
    expect(toast.ok).toHaveBeenCalledWith('Payout #7 marked paid');
    expect(api.get).toHaveBeenCalledTimes(2);
  });

  it('toasts the error and keeps the row requested when mark_paid fails', async () => {
    api.get.mockResolvedValue(page([PAYOUT_REQUESTED], 1));
    api.post.mockRejectedValue(new Error("Payout is 'paid', not 'requested'."));
    renderPage();
    await screen.findByText('Acme Charging');

    await userEvent.click(screen.getByRole('button', { name: 'Mark paid' }));
    const dialog = screen.getByRole('dialog');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Mark paid' }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("Payout is 'paid', not 'requested'.")
    );
    expect(screen.getByText('requested')).toBeInTheDocument();
  });

  it('closing the dialog does not call mark_paid', async () => {
    api.get.mockResolvedValue(page([PAYOUT_REQUESTED], 1));
    renderPage();
    await screen.findByText('Acme Charging');

    await userEvent.click(screen.getByRole('button', { name: 'Mark paid' }));
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(api.post).not.toHaveBeenCalled();
  });
});
