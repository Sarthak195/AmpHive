/**
 * AdminTenantDetail tests: fetches GET /api/admin/tenants/:id and renders the
 * identity/GST card, the KPI row, recent sessions and payout history tables;
 * falls back gracefully when GST/tariff fields are null or the lists are
 * empty; surfaces a retryable error (never a stale/empty-looking page) on
 * failure; and links back to the tenant list.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Routes, Route } from 'react-router-dom';

import AdminTenantDetail from './AdminTenantDetail';
import api from '../../api/client';

vi.mock('../../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

vi.mock('../../contexts/ConfigContext', () => ({
  useConfig: () => ({ coin_inr_rate: 1 }),
}));

const TENANT_DETAIL = {
  id: 7,
  name: 'Acme Charging',
  created_at: '2026-01-10T00:00:00Z',
  user_count: 12,
  gateway_count: 5,
  gateways_online: 4,
  plug_count: 9,
  sessions_30d: 210,
  revenue_30d_coins: 15400,
  pending_payouts: 1,
  gst_number: '27ABCDE1234F1Z5',
  legal_name: 'Acme Charging Pvt Ltd',
  default_tariff_id: 3,
  recent_sessions: [
    {
      id: 501,
      plug_id: 9,
      plug_name: 'Bay 2',
      user_email: 'driver@amphive.test',
      started_at: '2026-07-19T08:00:00Z',
      ended_at: '2026-07-19T09:00:00Z',
      energy_kwh: 4.2,
      coins_spent: 210,
      status: 'completed',
    },
  ],
  payouts: [
    {
      id: 30,
      tenant_id: 7,
      period_start: '2026-06-01T00:00:00Z',
      period_end: '2026-06-30T00:00:00Z',
      gross_coins: 5000,
      platform_fee_coins: 250,
      net_coins: 4750,
      status: 'requested',
      requested_at: '2026-07-01T00:00:00Z',
      paid_at: null,
    },
  ],
};

const renderPage = (id = '7') =>
  render(
    <MemoryRouter initialEntries={[`/admin/tenants/${id}`]}>
      <Routes>
        <Route path="/admin/tenants/:id" element={<AdminTenantDetail />} />
      </Routes>
    </MemoryRouter>
  );

beforeEach(() => vi.clearAllMocks());
afterEach(() => vi.restoreAllMocks());

describe('AdminTenantDetail', () => {
  it('fetches the tenant by id and renders identity, KPIs, sessions and payouts', async () => {
    api.get.mockResolvedValue(TENANT_DETAIL);
    renderPage();

    expect(await screen.findByRole('heading', { name: 'Acme Charging' })).toBeInTheDocument();
    expect(api.get).toHaveBeenCalledWith('/api/admin/tenants/7');

    expect(screen.getByText('Acme Charging Pvt Ltd')).toBeInTheDocument();
    expect(screen.getByText('27ABCDE1234F1Z5')).toBeInTheDocument();
    expect(screen.getByText('#3')).toBeInTheDocument();
    expect(screen.getByText('4/5')).toBeInTheDocument();
    expect(screen.getByText('₹15,400.00')).toBeInTheDocument();

    expect(screen.getByText('Bay 2')).toBeInTheDocument();
    expect(screen.getByText('driver@amphive.test')).toBeInTheDocument();
    expect(screen.getByText('Completed')).toBeInTheDocument();

    expect(screen.getByText('requested')).toBeInTheDocument();
    expect(screen.getByText('₹4,750.00')).toBeInTheDocument();

    expect(screen.getByRole('link', { name: /all tenants/i })).toHaveAttribute(
      'href',
      '/admin/tenants'
    );
  });

  it('falls back gracefully when GST/tariff fields are null', async () => {
    api.get.mockResolvedValue({
      ...TENANT_DETAIL,
      gst_number: null,
      default_tariff_id: null,
    });
    renderPage();

    await screen.findByRole('heading', { name: 'Acme Charging' });
    expect(screen.getByText('Not provided')).toBeInTheDocument();
    expect(screen.getByText('None')).toBeInTheDocument();
  });

  it('shows empty states (not errors) for zero sessions/payouts', async () => {
    api.get.mockResolvedValue({ ...TENANT_DETAIL, recent_sessions: [], payouts: [] });
    renderPage();

    await screen.findByRole('heading', { name: 'Acme Charging' });
    expect(screen.getByText('No sessions yet')).toBeInTheDocument();
    expect(screen.getByText('No payouts yet')).toBeInTheDocument();
  });

  it('shows a retryable error instead of a stale/empty page on failure', async () => {
    api.get.mockRejectedValueOnce(new Error('down'));
    renderPage();

    expect(await screen.findByText("Couldn't load this tenant")).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Acme Charging' })).not.toBeInTheDocument();

    api.get.mockResolvedValue(TENANT_DETAIL);
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByRole('heading', { name: 'Acme Charging' })).toBeInTheDocument();
  });
});
