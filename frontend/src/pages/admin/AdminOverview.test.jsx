/**
 * AdminOverview tests: the KPI grid renders platform stats from
 * GET /api/admin/stats/overview with each tile linking to its admin page,
 * shows a skeleton while loading, and surfaces a retryable ErrorState on
 * failure (never a blank/empty grid).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

import AdminOverview from './AdminOverview';
import api from '../../api/client';

vi.mock('../../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

const STATS = {
  tenants: 12,
  users: { total: 340, drivers: 300, cpos: 35, admins: 5 },
  gateways: { total: 40, online: 37 },
  plugs: { total: 88, public: 60, private: 28 },
  sessions: { active: 6, today: 54, total: 9021 },
  energy_kwh: { today: 210.5, total: 88213.4, last_30d: 6421.9 },
  revenue_coins: { today: 1052, total: 441200, last_30d: 31500 },
  payouts: { requested_count: 3, requested_net_coins: 4200 },
  disputes: { open: 2 },
};

const renderPage = () => render(<MemoryRouter><AdminOverview /></MemoryRouter>);

beforeEach(() => vi.clearAllMocks());
afterEach(() => vi.restoreAllMocks());

describe('AdminOverview', () => {
  it('renders KPI tiles from the overview stats, each linking to its admin page', async () => {
    api.get.mockResolvedValue(STATS);
    renderPage();

    await screen.findByText('Tenants');
    expect(screen.getByText('12')).toBeInTheDocument();
    expect(screen.getByText('340')).toBeInTheDocument();
    expect(screen.getByText('37 / 40')).toBeInTheDocument();
    expect(screen.getByText('2')).toBeInTheDocument(); // open disputes

    const links = screen.getAllByRole('link');
    const hrefFor = (label) =>
      links.find((l) => l.textContent.includes(label))?.getAttribute('href');

    expect(hrefFor('Tenants')).toBe('/admin/tenants');
    expect(hrefFor('Users')).toBe('/admin/users');
    expect(hrefFor('Gateways online')).toBe('/admin/gateways');
    expect(hrefFor('Requested payouts')).toBe('/admin/payouts');
    expect(hrefFor('Open disputes')).toBe('/admin/disputes');
  });

  it('renders the 30-day energy/revenue tiles and the public/private chargers split', async () => {
    api.get.mockResolvedValue(STATS);
    renderPage();

    await screen.findByText('Chargers');
    expect(screen.getByText('Energy (30d)')).toBeInTheDocument();
    expect(screen.getByText('Revenue (30d)')).toBeInTheDocument();
    expect(screen.getByText('88')).toBeInTheDocument(); // chargers total
    expect(screen.getByText('60 public · 28 private')).toBeInTheDocument();

    const chargersLink = screen
      .getAllByRole('link')
      .find((l) => l.textContent.includes('Chargers'));
    expect(chargersLink).toHaveAttribute('href', '/admin/chargers');
  });

  it('falls back to zeros when the new stat fields are missing', async () => {
    api.get.mockResolvedValue({
      tenants: 1,
      users: {},
      gateways: {},
      plugs: {},
      sessions: {},
      energy_kwh: {},
      revenue_coins: {},
      payouts: {},
      disputes: {},
    });
    renderPage();

    await screen.findByText('Chargers');
    expect(screen.getByText('0 public · 0 private')).toBeInTheDocument();
  });

  it('shows a skeleton grid while loading', () => {
    api.get.mockReturnValue(new Promise(() => {})); // never resolves
    renderPage();
    expect(screen.getByRole('status')).toHaveTextContent(/loading/i);
  });

  it('surfaces a retryable ErrorState on failure and recovers on retry', async () => {
    api.get.mockRejectedValueOnce(new Error('boom'));
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByText("Couldn't load platform stats")).toBeInTheDocument();

    api.get.mockResolvedValueOnce(STATS);
    await user.click(screen.getByRole('button', { name: 'Retry' }));

    await screen.findByText('Tenants');
    expect(screen.queryByText("Couldn't load platform stats")).not.toBeInTheDocument();
  });
});
