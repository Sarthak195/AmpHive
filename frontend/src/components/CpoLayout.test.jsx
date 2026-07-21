/**
 * CpoLayout tests: volt theme stamped, grouped nav with correct hrefs +
 * startsWith-active state, count-pill badges wired to TenantContext counts
 * (hidden at zero/null), org name from the profile, CpoAlerts + page content
 * rendered, footer (email / driver-app link / sign out), and the mobile
 * drawer (hamburger opens, Esc closes and restores focus).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

import CpoLayout from './CpoLayout';
import { useAuth } from '../contexts/AuthContext';
import { useTenant } from '../contexts/TenantContext';

vi.mock('../contexts/AuthContext', () => ({ useAuth: vi.fn() }));
vi.mock('../contexts/TenantContext', () => ({
  TenantProvider: ({ children }) => children,
  useTenant: vi.fn(),
}));
vi.mock('./NotificationBell', () => ({ default: () => <div data-testid="bell" /> }));
vi.mock('./CpoAlerts', () => ({ default: () => <div data-testid="alerts" /> }));
vi.mock('../utils/appHost', () => ({ driverOrigin: () => 'https://amphive.app' }));

const logout = vi.fn();

const NAV_LINKS = [
  ['Dashboard', '/cpo/dashboard'],
  ['Chargers', '/cpo/chargers'],
  ['Gateways', '/cpo/gateways'],
  ['Groups', '/cpo/groups'],
  ['Health', '/cpo/health'],
  ['Sessions', '/cpo/sessions'],
  ['Reservations', '/cpo/reservations'],
  ['Pricing', '/cpo/pricing'],
  ['Earnings', '/cpo/earnings'],
  ['Invoices', '/cpo/invoices'],
  ['Disputes', '/cpo/disputes'],
  ['Settings', '/cpo/settings'],
];

const renderLayout = (path = '/cpo/dashboard') =>
  render(
    <MemoryRouter initialEntries={[path]}>
      <CpoLayout>
        <div data-testid="page-content">page body</div>
      </CpoLayout>
    </MemoryRouter>
  );

beforeEach(() => {
  vi.clearAllMocks();
  useAuth.mockReturnValue({
    user: { email: 'op@amphive.test', full_name: 'Olive Operator', role: 'cpo' },
    logout,
  });
  useTenant.mockReturnValue({
    profile: { tenant: { name: 'Volt Yard' } },
    counts: { unackedEvents: 3, openDisputes: 1, pendingCapacity: 0 },
    loading: false,
    refresh: vi.fn(),
  });
});

describe('CpoLayout', () => {
  it('stamps the volt theme on the document', () => {
    renderLayout();
    expect(document.documentElement.dataset.theme).toBe('volt');
  });

  it('renders the brand, org name, alerts strip and the page content', () => {
    renderLayout();
    expect(screen.getByText('AmpHive Console')).toBeInTheDocument();
    expect(screen.getByText('Volt Yard')).toBeInTheDocument();
    expect(screen.getByTestId('alerts')).toBeInTheDocument();
    expect(screen.getByTestId('page-content')).toBeInTheDocument();
  });

  it('renders every grouped nav link with its href, plus the group headers', () => {
    renderLayout();
    for (const [label, href] of NAV_LINKS) {
      expect(screen.getByRole('link', { name: new RegExp(`^${label}`) })).toHaveAttribute(
        'href',
        href
      );
    }
    for (const group of ['Infrastructure', 'Operations', 'Revenue']) {
      expect(screen.getByText(group)).toBeInTheDocument();
    }
  });

  it('shows count-pill badges for Health/Disputes and hides zero/null ones', () => {
    const { unmount } = renderLayout();
    expect(screen.getByRole('link', { name: /Health\s*3/ })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /Disputes\s*1/ })).toBeInTheDocument();
    // pendingCapacity is 0 → no pill on Groups.
    expect(screen.queryByRole('link', { name: /Groups\s*0/ })).not.toBeInTheDocument();
    unmount();

    // Null counts (fetch failed) render no pills at all.
    useTenant.mockReturnValue({
      profile: null,
      counts: { unackedEvents: null, openDisputes: null, pendingCapacity: null },
      loading: false,
      refresh: vi.fn(),
    });
    renderLayout();
    expect(screen.queryByRole('link', { name: /Health\s*(null|\d)/ })).not.toBeInTheDocument();
  });

  it('marks the current section active via startsWith (nested paths included)', () => {
    renderLayout('/cpo/chargers');
    expect(screen.getByRole('link', { name: /^Chargers/ })).toHaveAttribute(
      'aria-current',
      'page'
    );
    expect(screen.getByRole('link', { name: /^Dashboard/ })).not.toHaveAttribute('aria-current');
  });

  it('footer shows the signed-in email, links to the driver app, and signs out', async () => {
    renderLayout();
    expect(screen.getByText('op@amphive.test')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /driver app/i })).toHaveAttribute(
      'href',
      'https://amphive.app'
    );

    await userEvent.click(screen.getByRole('button', { name: /sign out/i }));
    expect(logout).toHaveBeenCalled();
  });

  it('hamburger opens the drawer; Escape closes it and restores focus', async () => {
    renderLayout();
    const sidebar = document.querySelector('.console-sidebar');
    expect(sidebar).not.toHaveClass('open');

    const toggle = screen.getByRole('button', { name: 'Open menu' });
    await userEvent.click(toggle);
    expect(sidebar).toHaveClass('open');
    expect(toggle).toHaveAttribute('aria-expanded', 'true');

    await userEvent.keyboard('{Escape}');
    expect(sidebar).not.toHaveClass('open');
    expect(toggle).toHaveFocus();
  });

  it('scrim click closes the drawer', async () => {
    renderLayout();
    await userEvent.click(screen.getByRole('button', { name: 'Open menu' }));
    await userEvent.click(screen.getByRole('button', { name: 'Close menu' }));
    expect(document.querySelector('.console-sidebar')).not.toHaveClass('open');
  });
});
