/**
 * Navbar host-gating tests: the driver host shows driver links plus the
 * modest external "Apply to host chargers" link (no in-app CPO Portal /
 * Become a Host links); the CPO host hides driver navigation and shows
 * only the operator link for cpo/admin roles.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

import Navbar from './Navbar';
import { useAuth } from '../contexts/AuthContext';
import { isCpoHost, isSplitHost, cpoOrigin } from '../utils/appHost';

vi.mock('../contexts/AuthContext', () => ({ useAuth: vi.fn() }));
vi.mock('../contexts/WalletContext', () => ({ useWallet: () => ({ balance: 0 }) }));
vi.mock('./NotificationBell', () => ({ default: () => null }));
vi.mock('../utils/appHost', () => ({
  isCpoHost: vi.fn(),
  isSplitHost: vi.fn(),
  cpoOrigin: vi.fn(),
}));

const renderNavbar = () =>
  render(
    <MemoryRouter>
      <Navbar />
    </MemoryRouter>
  );

beforeEach(() => {
  vi.clearAllMocks();
  isSplitHost.mockReturnValue(true);
  cpoOrigin.mockReturnValue('https://cpo.amphive.duckdns.org');
});

describe('Navbar on the driver host', () => {
  beforeEach(() => isCpoHost.mockReturnValue(false));

  it('shows driver links and the external apply-to-host link for a signed-in driver', () => {
    useAuth.mockReturnValue({ user: { email: 'driver@amphive.test', role: 'driver' }, logout: vi.fn() });
    renderNavbar();
    expect(screen.getByText('Home')).toBeInTheDocument();
    expect(screen.getByText('Top Up')).toBeInTheDocument();
    const apply = screen.getByRole('link', { name: /apply to host chargers/i });
    expect(apply).toHaveAttribute('href', 'https://cpo.amphive.duckdns.org/cpo');
    expect(screen.queryByText(/CPO Portal/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Become a Host/)).not.toBeInTheDocument();
  });

  it('shows no CPO Portal link even for cpo-role users (portal lives on the CPO origin)', () => {
    useAuth.mockReturnValue({ user: { email: 'op@amphive.test', role: 'cpo' }, logout: vi.fn() });
    renderNavbar();
    expect(screen.queryByText(/CPO Portal/)).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: /apply to host chargers/i })).toBeInTheDocument();
  });
});

describe('Navbar on the CPO host', () => {
  beforeEach(() => isCpoHost.mockReturnValue(true));

  it('hides driver navigation and shows the CPO Portal link for cpo/admin', () => {
    useAuth.mockReturnValue({ user: { email: 'op@amphive.test', role: 'cpo' }, logout: vi.fn() });
    renderNavbar();
    expect(screen.queryByText('Home')).not.toBeInTheDocument();
    expect(screen.queryByText('Top Up')).not.toBeInTheDocument();
    expect(screen.queryByText('Groups')).not.toBeInTheDocument();
    expect(screen.getByText(/CPO Portal/)).toBeInTheDocument();
  });

  it('shows no nav links at all for a driver-role user', () => {
    useAuth.mockReturnValue({ user: { email: 'driver@amphive.test', role: 'driver' }, logout: vi.fn() });
    renderNavbar();
    expect(screen.queryByText('Home')).not.toBeInTheDocument();
    expect(screen.queryByText(/CPO Portal/)).not.toBeInTheDocument();
    expect(screen.queryByText(/apply to host chargers/i)).not.toBeInTheDocument();
  });
});
