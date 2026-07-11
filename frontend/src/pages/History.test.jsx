/**
 * History tests: the tabbed view (Charging Sessions / Wallet Ledger) and the
 * unified-ledger rendering (credit vs debit, running balance) added 2026-07-10.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import History from './History';
import api from '../api/client';

vi.mock('../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

const SESSIONS = [
  { id: 1, plug_id: 2, energy_kwh: 1.234, coins_spent: 6.17, status: 'completed', started_at: '2026-07-10T10:00:00Z' },
];

const LEDGER = [
  { id: 10, amount: 100.0, transaction_type: 'topup', direction: 'credit', description: 'Wallet top-up', balance_after: 100.0, session_id: null, created_at: '2026-07-10T09:00:00Z' },
  { id: 11, amount: -6.17, transaction_type: 'session_debit', direction: 'debit', description: 'Charging session #1', balance_after: 93.83, session_id: 1, created_at: '2026-07-10T10:05:00Z' },
];

beforeEach(() => {
  vi.clearAllMocks();
  api.get.mockImplementation((url) => {
    if (url.includes('/sessions/history')) return Promise.resolve(SESSIONS);
    if (url.includes('/wallet/ledger')) return Promise.resolve(LEDGER);
    return Promise.resolve([]);
  });
});

describe('History', () => {
  it('shows the charging-sessions tab by default', async () => {
    render(<History />);
    // The session row's energy value (3 dp) renders once loaded.
    expect(await screen.findByText('1.234')).toBeInTheDocument();
    expect(api.get).toHaveBeenCalledWith('/api/sessions/history');
    expect(api.get).toHaveBeenCalledWith('/api/wallet/ledger');
  });

  it('renders the unified ledger with signed credit/debit amounts', async () => {
    render(<History />);
    await screen.findByText('1.234'); // wait for load

    await userEvent.click(screen.getByRole('button', { name: 'Wallet Ledger' }));

    // Credit shows a leading '+'; debit shows the negative amount as-is.
    expect(await screen.findByText('+100.00')).toBeInTheDocument();
    expect(screen.getByText('-6.17')).toBeInTheDocument();
    // Running balances from balance_after.
    expect(screen.getByText('93.83')).toBeInTheDocument();
    // Type labels are humanized.
    expect(screen.getByText('Top-up')).toBeInTheDocument();
    expect(screen.getByText('Charging')).toBeInTheDocument();
  });

  it('shows an empty-state when the ledger is empty', async () => {
    api.get.mockImplementation((url) => {
      if (url.includes('/wallet/ledger')) return Promise.resolve([]);
      return Promise.resolve(SESSIONS);
    });
    render(<History />);
    await screen.findByText('1.234');
    await userEvent.click(screen.getByRole('button', { name: 'Wallet Ledger' }));
    expect(await screen.findByText(/No wallet activity yet/)).toBeInTheDocument();
  });
});
