/**
 * CpoPromosCard: lists codes with redemption counts and status, issues a code
 * and shows the one-time plain code, disables a code, and validates the form.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import CpoPromosCard from './CpoPromosCard';
import api from '../api/client';

vi.mock('../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

const toast = { ok: vi.fn(), error: vi.fn(), info: vi.fn(), warn: vi.fn() };
vi.mock('./ui', async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, useToast: () => toast };
});

const LIST = {
  total: 2,
  items: [
    {
      id: 7, tenant_id: 1, code_hint: 'QR7Z', amount_coins: 50, expires_at: null,
      max_redemptions: 10, per_user_limit: 1, redemption_count: 3, note: 'launch flyer',
      created_at: '2026-11-17T10:00:00Z', disabled_at: null, status: 'active',
    },
    {
      id: 6, tenant_id: 1, code_hint: 'AB23', amount_coins: 20, expires_at: '2026-11-01T00:00:00Z',
      max_redemptions: 1, per_user_limit: 1, redemption_count: 1, note: null,
      created_at: '2026-10-01T10:00:00Z', disabled_at: '2026-10-02T10:00:00Z', status: 'disabled',
    },
  ],
};

beforeEach(() => {
  vi.clearAllMocks();
  api.get.mockResolvedValue(LIST);
});

describe('CpoPromosCard', () => {
  it('lists codes by hint with redemption counts and status', async () => {
    render(<CpoPromosCard />);
    expect(await screen.findByText('…QR7Z')).toBeInTheDocument();
    expect(api.get).toHaveBeenCalledWith('/api/cpo/promos?limit=10&offset=0');
    expect(screen.getByText('3 / 10')).toBeInTheDocument();
    expect(screen.getByText('Active')).toBeInTheDocument();
    expect(screen.getByText('Disabled')).toBeInTheDocument();
    // Only the still-redeemable code offers Disable.
    expect(screen.getByRole('button', { name: 'Disable code ending QR7Z' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Disable code ending AB23' })).not.toBeInTheDocument();
  });

  it('issues a code and shows the plain code once', async () => {
    const user = userEvent.setup();
    api.post.mockResolvedValue({ ...LIST.items[0], id: 8, code_hint: 'WXYZ', code: 'ABCD-EFGH-JKMN-WXYZ' });
    render(<CpoPromosCard />);
    await screen.findByText('…QR7Z');

    await user.click(screen.getByRole('button', { name: /New code/ }));
    const dialog = await screen.findByRole('dialog');
    await user.type(within(dialog).getByLabelText('Amount per redemption (coins)'), '25');
    await user.clear(within(dialog).getByLabelText('Total redemptions'));
    await user.type(within(dialog).getByLabelText('Total redemptions'), '5');
    await user.clear(within(dialog).getByLabelText('Expires after (days)'));
    await user.click(within(dialog).getByRole('button', { name: 'Create code' }));

    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith('/api/cpo/promos', {
        amount_coins: 25,
        max_redemptions: 5,
        per_user_limit: 1,
        expires_in_days: null,
        note: undefined,
      })
    );
    expect(await screen.findByText('ABCD-EFGH-JKMN-WXYZ')).toBeInTheDocument();
    expect(screen.getByText(/can't be shown again/)).toBeInTheDocument();
  });

  it('rejects a non-positive amount without posting', async () => {
    const user = userEvent.setup();
    render(<CpoPromosCard />);
    await screen.findByText('…QR7Z');

    await user.click(screen.getByRole('button', { name: /New code/ }));
    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: 'Create code' }));

    expect(within(dialog).getByRole('alert')).toHaveTextContent('Enter an amount greater than 0.');
    expect(api.post).not.toHaveBeenCalled();
  });

  it('disables a code and refreshes the list', async () => {
    const user = userEvent.setup();
    api.post.mockResolvedValue({ ...LIST.items[0], status: 'disabled' });
    render(<CpoPromosCard />);
    await screen.findByText('…QR7Z');

    await user.click(screen.getByRole('button', { name: 'Disable code ending QR7Z' }));

    await waitFor(() => expect(api.post).toHaveBeenCalledWith('/api/cpo/promos/7/disable', {}));
    expect(toast.ok).toHaveBeenCalledWith('Code disabled.');
    expect(api.get).toHaveBeenCalledTimes(2);
  });
});
