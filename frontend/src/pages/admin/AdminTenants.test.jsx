/**
 * AdminTenants tests: renders the cross-tenant roster with its fleet/usage
 * aggregates, debounces the search box into a `q` query param (resetting to
 * page 1), pages via DataTable's pagination footer, navigates to the detail
 * route on row click, and shows a retryable error (never a clean-looking
 * empty state) when the list fails to load.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

import AdminTenants from './AdminTenants';
import api from '../../api/client';

vi.mock('../../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

vi.mock('../../contexts/ConfigContext', () => ({
  useConfig: () => ({ coin_inr_rate: 1 }),
}));

const navigateMock = vi.fn();
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom');
  return { ...actual, useNavigate: () => navigateMock };
});

const TENANT_A = {
  id: 1,
  name: 'Acme Charging',
  created_at: '2026-01-10T00:00:00Z',
  user_count: 12,
  gateway_count: 5,
  gateways_online: 4,
  plug_count: 9,
  sessions_30d: 210,
  revenue_30d_coins: 15400,
  pending_payouts: 2,
};

const TENANT_B = {
  id: 2,
  name: 'Volt Retail',
  created_at: '2026-02-01T00:00:00Z',
  user_count: 3,
  gateway_count: 1,
  gateways_online: 0,
  plug_count: 2,
  sessions_30d: 0,
  revenue_30d_coins: 0,
  pending_payouts: 0,
};

const page = (items, total) => ({ total, items });

const renderPage = () => render(<MemoryRouter><AdminTenants /></MemoryRouter>);

beforeEach(() => vi.clearAllMocks());
afterEach(() => vi.restoreAllMocks());

describe('AdminTenants', () => {
  it('lists tenants with their fleet/usage aggregates', async () => {
    api.get.mockResolvedValue(page([TENANT_A, TENANT_B], 2));
    renderPage();

    await screen.findByText('Acme Charging');
    expect(screen.getByText('Volt Retail')).toBeInTheDocument();
    expect(screen.getByText('4/5 online')).toBeInTheDocument();
    expect(screen.getByText('₹15,400.00')).toBeInTheDocument();
    expect(screen.getByText('2 pending')).toBeInTheDocument();
    // Zero pending payouts renders a dash, not a badge.
    expect(screen.getByText('2 organizations')).toBeInTheDocument();
  });

  it('shows a retryable error instead of an empty state on failure, then recovers', async () => {
    api.get.mockRejectedValueOnce(new Error('down'));
    renderPage();

    expect(await screen.findByText("Couldn't load this")).toBeInTheDocument();
    expect(screen.queryByText('No organizations yet')).not.toBeInTheDocument();

    api.get.mockResolvedValue(page([TENANT_A], 1));
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('Acme Charging')).toBeInTheDocument();
  });

  it('shows the empty state only for a true zero-row result', async () => {
    api.get.mockResolvedValue(page([], 0));
    renderPage();
    expect(await screen.findByText('No organizations yet')).toBeInTheDocument();
  });

  it('debounces the search box into a q param and resets to page 1', async () => {
    api.get.mockResolvedValue(page([TENANT_A, TENANT_B], 2));
    renderPage();
    await screen.findByText('Acme Charging');
    api.get.mockClear();

    api.get.mockResolvedValue(page([TENANT_A], 1));
    await userEvent.type(screen.getByLabelText('Search organizations'), 'Acme');

    await waitFor(() => {
      const lastUrl = api.get.mock.calls.at(-1)[0];
      expect(lastUrl).toContain('q=Acme');
      expect(lastUrl).toContain('offset=0');
    });
  });

  it('navigates to the tenant detail route on row click', async () => {
    api.get.mockResolvedValue(page([TENANT_A, TENANT_B], 2));
    renderPage();

    const row = await screen.findByText('Acme Charging');
    await userEvent.click(row.closest('tr'));
    expect(navigateMock).toHaveBeenCalledWith('/admin/tenants/1');
  });

  it('pages forward through DataTable pagination', async () => {
    api.get.mockResolvedValue(page([TENANT_A], 45));
    renderPage();
    await screen.findByText('Acme Charging');
    api.get.mockClear();

    api.get.mockResolvedValue(page([TENANT_B], 45));
    await userEvent.click(screen.getByRole('button', { name: 'Next' }));

    await waitFor(() => {
      const lastUrl = api.get.mock.calls.at(-1)[0];
      expect(lastUrl).toContain('offset=20');
    });
    expect(await screen.findByText('Volt Retail')).toBeInTheDocument();
  });
});
