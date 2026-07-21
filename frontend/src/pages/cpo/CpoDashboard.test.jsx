/**
 * CpoDashboard tests: five independently-loaded sections (KPI overview,
 * revenue chart, energy chart, load chart, recent sessions) each render
 * their own skeleton -> ErrorState-with-retry -> content, so a failure in
 * one card never blanks the others; KPI tiles link to the right pages and
 * tint the gateways tile when some are offline.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

import CpoDashboard from './CpoDashboard';
import api from '../../api/client';

vi.mock('../../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

vi.mock('../../components/CpoLayout', () => ({
  default: ({ children }) => <div data-testid="cpo-layout">{children}</div>,
}));

vi.mock('../../contexts/ConfigContext', () => ({
  useConfig: () => ({ coin_inr_rate: 1 }),
}));

const OVERVIEW = {
  plugs: { total: 12 },
  gateways: { online: 3, total: 4 },
  active_sessions: 2,
  today: { sessions: 9, energy_kwh: 21.5, revenue_coins: 340 },
  all_time: { sessions: 500, energy_kwh: 8210, revenue_coins: 41000 },
};

const REVENUE = [
  { date: '2026-07-19', revenue_coins: 120, session_count: 3 },
  { date: '2026-07-20', revenue_coins: 220, session_count: 5 },
];

const ENERGY = [
  { date: '2026-07-19', energy_kwh: 10.2, session_count: 3 },
  { date: '2026-07-20', energy_kwh: 11.3, session_count: 5 },
];

const LOAD = [
  { timestamp: '2026-07-20T10:00:00Z', avg_power_w: 1500, max_power_w: 2200, avg_current_a: 6.8, max_current_a: 9.9, sample_count: 12 },
];

const SESSIONS = [
  {
    id: 1, plug_id: 5, plug_name: 'Bay A1', user_id: 9, user_email: 'driver@amphive.test',
    started_at: '2026-07-20T09:00:00Z', ended_at: '2026-07-20T10:00:00Z',
    duration_minutes: 60, energy_kwh: 4.2, coins_spent: 42, status: 'paid',
  },
];

const mockApi = ({ overview = OVERVIEW, revenue = REVENUE, energy = ENERGY, load = LOAD, sessions = SESSIONS } = {}) => {
  api.get.mockImplementation((url) => {
    if (url === '/api/cpo/analytics/overview') return Promise.resolve(overview);
    if (url.startsWith('/api/cpo/analytics/revenue')) return Promise.resolve(revenue);
    if (url.startsWith('/api/cpo/analytics/energy')) return Promise.resolve(energy);
    if (url.startsWith('/api/cpo/analytics/telemetry')) return Promise.resolve(load);
    if (url.startsWith('/api/cpo/analytics/sessions')) {
      return Promise.resolve({ total: sessions.length, totals: {}, items: sessions, sessions });
    }
    return Promise.resolve([]);
  });
};

const renderPage = () => render(<MemoryRouter><CpoDashboard /></MemoryRouter>);

beforeEach(() => vi.clearAllMocks());
afterEach(() => vi.restoreAllMocks());

describe('CpoDashboard', () => {
  it('renders KPI tiles from the overview stats, each linking to its console page', async () => {
    mockApi();
    renderPage();

    await screen.findByText("Today’s revenue");
    expect(screen.getByText('₹340.00')).toBeInTheDocument();
    expect(screen.getByText('21.50 kWh')).toBeInTheDocument();
    expect(screen.getByText('2')).toBeInTheDocument();
    expect(screen.getByText('3 / 4')).toBeInTheDocument();

    const links = screen.getAllByRole('link');
    const hrefFor = (label) =>
      links.find((l) => l.textContent.includes(label))?.getAttribute('href');

    expect(hrefFor("Today’s revenue")).toBe('/cpo/sessions');
    expect(hrefFor('Active sessions')).toBe('/cpo/sessions?status=active');
    expect(hrefFor('Gateways online')).toBe('/cpo/gateways');
  });

  it('tints the gateways tile with an offline badge when some gateways are down', async () => {
    mockApi();
    renderPage();
    expect(await screen.findByText('1 offline')).toBeInTheDocument();
  });

  it('shows no offline badge when all gateways are online', async () => {
    mockApi({ overview: { ...OVERVIEW, gateways: { online: 4, total: 4 } } });
    renderPage();
    await screen.findByText('4 / 4');
    expect(screen.queryByText(/offline/)).not.toBeInTheDocument();
  });

  it('shows a skeleton KPI grid while the overview loads', () => {
    api.get.mockReturnValue(new Promise(() => {}));
    renderPage();
    const grid = document.querySelector('.kpi-grid[aria-hidden="true"]');
    expect(grid).toBeInTheDocument();
    expect(grid.children.length).toBe(4);
  });

  it('surfaces a retryable ErrorState for the KPI row on failure and recovers on retry', async () => {
    api.get.mockImplementation((url) => {
      if (url === '/api/cpo/analytics/overview') return Promise.reject(new Error('boom'));
      return Promise.resolve([]);
    });
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByText("Couldn't load your stats")).toBeInTheDocument();

    mockApi();
    await user.click(screen.getByRole('button', { name: 'Retry' }));

    await screen.findByText("Today’s revenue");
    expect(screen.queryByText("Couldn't load your stats")).not.toBeInTheDocument();
  });

  it('a failing energy chart does not blank the KPI row or the revenue chart', async () => {
    api.get.mockImplementation((url) => {
      if (url === '/api/cpo/analytics/overview') return Promise.resolve(OVERVIEW);
      if (url.startsWith('/api/cpo/analytics/energy')) return Promise.reject(new Error('down'));
      if (url.startsWith('/api/cpo/analytics/revenue')) return Promise.resolve(REVENUE);
      if (url.startsWith('/api/cpo/analytics/telemetry')) return Promise.resolve(LOAD);
      if (url.startsWith('/api/cpo/analytics/sessions')) {
        return Promise.resolve({ total: 1, totals: {}, items: SESSIONS, sessions: SESSIONS });
      }
      return Promise.resolve([]);
    });
    renderPage();

    expect(await screen.findByText("Couldn't load energy")).toBeInTheDocument();
    // KPI row and revenue chart still rendered from their own successful fetches.
    expect(screen.getByText("Today’s revenue")).toBeInTheDocument();
    expect(screen.getByText('Revenue (last 30 days)')).toBeInTheDocument();
  });

  it('lists recent sessions with charger, driver, energy, revenue and status', async () => {
    mockApi();
    renderPage();

    await screen.findByText('Bay A1');
    expect(screen.getByText('driver@amphive.test')).toBeInTheDocument();
    expect(screen.getByText('4.20 kWh')).toBeInTheDocument();
    expect(screen.getByText('Paid')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'View all' })).toHaveAttribute('href', '/cpo/sessions');
  });

  it('shows an empty state when there are no recent sessions', async () => {
    mockApi({ sessions: [] });
    renderPage();
    expect(await screen.findByText('No sessions yet')).toBeInTheDocument();
  });
});
