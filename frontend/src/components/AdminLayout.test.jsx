/**
 * AdminLayout tests: nav links + active state, the volt/admin theme stamp,
 * the mobile off-canvas drawer (hamburger toggles it, scrim/route change
 * close it), the tenant-gated Operator console footer link, and sign out.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Routes, Route } from 'react-router-dom';

import AdminLayout from './AdminLayout';
import { useAuth } from '../contexts/AuthContext';

vi.mock('../contexts/AuthContext', () => ({ useAuth: vi.fn() }));

const navigateMock = vi.fn();
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom');
  return { ...actual, useNavigate: () => navigateMock };
});

/** jsdom has no matchMedia implementation — stub it so AdminLayout's mobile
 *  breakpoint check (which drives `inert` on the closed drawer) can run. */
const mockMatchMedia = (matches) => {
  window.matchMedia = vi.fn().mockImplementation((query) => ({
    matches,
    media: query,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  }));
};

const renderLayout = (path = '/admin/tenants') =>
  render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route element={<AdminLayout />}>
          <Route path="/admin" element={<div>Overview page</div>} />
          <Route path="/admin/tenants" element={<div>Tenants page</div>} />
          <Route path="/admin/users" element={<div>Users page</div>} />
        </Route>
      </Routes>
    </MemoryRouter>
  );

beforeEach(() => {
  vi.clearAllMocks();
  mockMatchMedia(false); // desktop by default
  useAuth.mockReturnValue({
    user: { email: 'root@amphive.test', role: 'admin' },
    logout: vi.fn().mockResolvedValue(),
  });
});

afterEach(() => {
  delete document.documentElement.dataset.theme;
  delete document.documentElement.dataset.accent;
});

describe('AdminLayout', () => {
  it('stamps the volt theme with the admin accent', () => {
    renderLayout();
    expect(document.documentElement.dataset.theme).toBe('volt');
    expect(document.documentElement.dataset.accent).toBe('admin');
  });

  it('renders the nav with links to every admin section and marks the active one', () => {
    renderLayout('/admin/tenants');
    for (const label of ['Overview', 'Tenants', 'Users', 'Payouts', 'Gateways', 'Disputes', 'Audit']) {
      expect(screen.getByRole('link', { name: label })).toBeInTheDocument();
    }
    expect(screen.getByRole('link', { name: 'Tenants' })).toHaveClass('active');
    expect(screen.getByRole('link', { name: 'Users' })).not.toHaveClass('active');
    expect(screen.getByText('Tenants page')).toBeInTheDocument();
  });

  it('does not mark Overview active on a nested admin route (exact match only)', () => {
    renderLayout('/admin/users');
    expect(screen.getByRole('link', { name: 'Overview' })).not.toHaveClass('active');
    expect(screen.getByRole('link', { name: 'Users' })).toHaveClass('active');
  });

  it('shows the matching section title in the mobile topbar', () => {
    renderLayout('/admin/users');
    expect(screen.getByText('Users', { selector: 'strong' })).toBeInTheDocument();
  });

  it('opens the drawer + scrim on hamburger click and closes on scrim click', async () => {
    renderLayout();
    const sidebar = () => screen.getByRole('link', { name: 'Overview' }).closest('aside');
    expect(sidebar()).not.toHaveClass('open');

    await userEvent.click(screen.getByRole('button', { name: /open menu/i }));
    expect(sidebar()).toHaveClass('open');

    const closeButtons = screen.getAllByRole('button', { name: /close menu/i });
    expect(closeButtons.length).toBeGreaterThan(1); // hamburger toggle + scrim

    const scrim = closeButtons.find((b) => b.className === 'console-scrim');
    await userEvent.click(scrim);
    expect(sidebar()).not.toHaveClass('open');
  });

  it('moves focus into the drawer on open; Escape closes it and restores focus to the toggle', async () => {
    renderLayout();
    const toggle = screen.getByRole('button', { name: 'Open menu' });

    await userEvent.click(toggle);
    expect(screen.getByRole('link', { name: 'Overview' })).toHaveFocus();

    await userEvent.keyboard('{Escape}');
    const sidebar = screen.getByRole('link', { name: 'Overview' }).closest('aside');
    expect(sidebar).not.toHaveClass('open');
    expect(screen.getByRole('button', { name: 'Open menu' })).toHaveFocus();
  });

  it('marks the closed sidebar inert on mobile, but never on desktop', async () => {
    mockMatchMedia(true); // mobile
    renderLayout();
    const sidebar = () => screen.getByRole('link', { name: 'Overview' }).closest('aside');
    expect(sidebar()).toHaveAttribute('inert');

    await userEvent.click(screen.getByRole('button', { name: 'Open menu' }));
    expect(sidebar()).not.toHaveAttribute('inert');
  });

  it('never marks the sidebar inert on desktop, even while the drawer is closed', () => {
    mockMatchMedia(false); // desktop
    renderLayout();
    expect(screen.getByRole('link', { name: 'Overview' }).closest('aside')).not.toHaveAttribute(
      'inert'
    );
  });

  it('omits the Operator console link when the admin has no tenant_id', () => {
    renderLayout();
    expect(screen.queryByRole('link', { name: /operator console/i })).not.toBeInTheDocument();
  });

  it('shows the Operator console link (pointing at the CPO host) for dual-role admins', () => {
    useAuth.mockReturnValue({
      user: { email: 'root@amphive.test', role: 'admin', tenant_id: 'ten_1' },
      logout: vi.fn().mockResolvedValue(),
    });
    renderLayout();
    expect(screen.getByRole('link', { name: /operator console/i })).toHaveAttribute(
      'href',
      expect.stringContaining('/cpo')
    );
  });

  it('links to the driver app and shows the signed-in email', () => {
    renderLayout();
    expect(screen.getByRole('link', { name: /driver app/i })).toBeInTheDocument();
    expect(screen.getByText('root@amphive.test')).toBeInTheDocument();
  });

  it('signs out and navigates to /login', async () => {
    const logout = vi.fn().mockResolvedValue();
    useAuth.mockReturnValue({ user: { email: 'root@amphive.test', role: 'admin' }, logout });
    renderLayout();

    await userEvent.click(screen.getByRole('button', { name: /sign out/i }));
    expect(logout).toHaveBeenCalled();
    expect(navigateMock).toHaveBeenCalledWith('/login');
  });
});
