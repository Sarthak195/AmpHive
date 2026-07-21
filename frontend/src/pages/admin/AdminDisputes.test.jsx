/**
 * AdminDisputes tests: renders a read-only cross-tenant disputes table,
 * shows a banner explaining CPO resolution, pages via DataTable pagination,
 * and shows a retryable error (never a clean-looking empty state) when the
 * list fails to load.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

import AdminDisputes from './AdminDisputes';
import api from '../../api/client';

vi.mock('../../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

vi.mock('../../contexts/ConfigContext', () => ({
  useConfig: () => ({ coin_inr_rate: 1 }),
}));

const DISPUTE_A = {
  id: 1,
  tenant_name: 'Acme Charging',
  user_email: 'driver@example.com',
  session_cost_coins: 500,
  status: 'open',
  created_at: '2026-07-10T10:00:00Z',
};

const DISPUTE_B = {
  id: 2,
  tenant_name: 'Volt Retail',
  user_email: 'another@example.com',
  session_cost_coins: 750,
  status: 'approved',
  created_at: '2026-07-15T15:30:00Z',
};

const page = (items, total) => ({ total, items });

const renderPage = () => render(<MemoryRouter><AdminDisputes /></MemoryRouter>);

beforeEach(() => vi.clearAllMocks());
afterEach(() => vi.restoreAllMocks());

describe('AdminDisputes', () => {
  it('lists disputes with organization, driver, cost, and status', async () => {
    api.get.mockResolvedValue(page([DISPUTE_A, DISPUTE_B], 2));
    renderPage();

    await screen.findByText('Acme Charging');
    expect(screen.getByText('Volt Retail')).toBeInTheDocument();
    expect(screen.getByText('driver@example.com')).toBeInTheDocument();
    expect(screen.getByText('another@example.com')).toBeInTheDocument();
    expect(screen.getByText('₹500.00')).toBeInTheDocument();
    expect(screen.getByText('₹750.00')).toBeInTheDocument();
    expect(screen.getByText('Under review')).toBeInTheDocument();
    expect(screen.getByText('Approved')).toBeInTheDocument();
  });

  it('shows a retryable error instead of an empty state on failure', async () => {
    api.get.mockRejectedValueOnce(new Error('down'));
    renderPage();

    expect(await screen.findByText("Couldn't load this")).toBeInTheDocument();
    expect(screen.queryByText('No disputes yet')).not.toBeInTheDocument();

    api.get.mockResolvedValue(page([DISPUTE_A], 1));
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('Acme Charging')).toBeInTheDocument();
  });

  it('shows the empty state only for a true zero-row result', async () => {
    api.get.mockResolvedValue(page([], 0));
    renderPage();
    expect(await screen.findByText('No disputes yet')).toBeInTheDocument();
  });

  it('renders the CPO resolution banner', async () => {
    api.get.mockResolvedValue(page([DISPUTE_A], 1));
    renderPage();

    await screen.findByText('Acme Charging');
    expect(screen.getByText('Disputes are resolved by their CPO.')).toBeInTheDocument();
    expect(screen.getByText(/operator approves refunds or rejects/i)).toBeInTheDocument();
  });

  it('pages forward through DataTable pagination', async () => {
    api.get.mockResolvedValue(page([DISPUTE_A], 45));
    renderPage();
    await screen.findByText('Acme Charging');
    api.get.mockClear();

    api.get.mockResolvedValue(page([DISPUTE_B], 45));
    await userEvent.click(screen.getByRole('button', { name: 'Next' }));

    await waitFor(() => {
      const lastUrl = api.get.mock.calls.at(-1)[0];
      expect(lastUrl).toContain('offset=20');
    });
    expect(await screen.findByText('Volt Retail')).toBeInTheDocument();
  });
});
