/**
 * Route-guard tests: ProtectedRoute (auth required), CpoProtectedRoute
 * (cpo/admin role required, driver → /cpo onboarding, anonymous → /login)
 * and AdminProtectedRoute (admin only, non-admins → /cpo/dashboard).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom';

import { ProtectedRoute, CpoProtectedRoute, AdminProtectedRoute } from './ProtectedRoutes';
import { useAuth } from '../contexts/AuthContext';

vi.mock('../contexts/AuthContext', () => ({
  useAuth: vi.fn(),
}));

// Stands in for the real Login page: renders the same "login page" text the
// existing tests below assert on, plus a probe for the router state the
// guard's <Navigate> stashed (`state.from`) so Login can send the driver
// back to exactly where they started — e.g. a QR deep link's `/?plug=<id>`
// — instead of always bouncing to Home.
const LoginFromProbe = () => {
  const location = useLocation();
  const from = location.state?.from;
  return (
    <div>
      <div>login page</div>
      <div data-testid="from-probe">{from ? `${from.pathname}${from.search}` : 'none'}</div>
    </div>
  );
};

const renderGuarded = (guardedElement, initialEntry = '/secret') =>
  render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <Routes>
        <Route path="/secret" element={guardedElement} />
        <Route path="/login" element={<LoginFromProbe />} />
        <Route path="/cpo" element={<div>cpo setup page</div>} />
        <Route path="/cpo/dashboard" element={<div>cpo dashboard page</div>} />
      </Routes>
    </MemoryRouter>
  );

beforeEach(() => {
  vi.clearAllMocks();
});

describe('ProtectedRoute', () => {
  it('redirects anonymous users to /login', () => {
    useAuth.mockReturnValue({ user: null });
    renderGuarded(<ProtectedRoute><div>secret content</div></ProtectedRoute>);
    expect(screen.getByText('login page')).toBeInTheDocument();
    expect(screen.queryByText('secret content')).not.toBeInTheDocument();
  });

  it('renders children for a signed-in user', () => {
    useAuth.mockReturnValue({ user: { email: 'driver@amphive.test', role: 'driver' } });
    renderGuarded(<ProtectedRoute><div>secret content</div></ProtectedRoute>);
    expect(screen.getByText('secret content')).toBeInTheDocument();
  });

  it('carries the original location (path + query) as router state so Login can return here', () => {
    useAuth.mockReturnValue({ user: null });
    renderGuarded(<ProtectedRoute><div>secret content</div></ProtectedRoute>, '/secret?x=1');
    expect(screen.getByTestId('from-probe')).toHaveTextContent('/secret?x=1');
  });
});

describe('CpoProtectedRoute', () => {
  it('redirects anonymous users to /login', () => {
    useAuth.mockReturnValue({ user: null });
    renderGuarded(<CpoProtectedRoute><div>cpo dashboard</div></CpoProtectedRoute>);
    expect(screen.getByText('login page')).toBeInTheDocument();
  });

  it('redirects authenticated non-CPO users to /cpo onboarding', () => {
    useAuth.mockReturnValue({ user: { email: 'driver@amphive.test', role: 'driver' } });
    renderGuarded(<CpoProtectedRoute><div>cpo dashboard</div></CpoProtectedRoute>);
    expect(screen.getByText('cpo setup page')).toBeInTheDocument();
    expect(screen.queryByText('cpo dashboard')).not.toBeInTheDocument();
  });

  it.each(['cpo', 'admin'])('renders children for the %s role', (role) => {
    useAuth.mockReturnValue({ user: { email: 'op@amphive.test', role } });
    renderGuarded(<CpoProtectedRoute><div>cpo dashboard</div></CpoProtectedRoute>);
    expect(screen.getByText('cpo dashboard')).toBeInTheDocument();
  });
});

describe('AdminProtectedRoute', () => {
  it('redirects anonymous users to /login, carrying the original location', () => {
    useAuth.mockReturnValue({ user: null });
    renderGuarded(<AdminProtectedRoute><div>admin content</div></AdminProtectedRoute>, '/secret?x=1');
    expect(screen.getByText('login page')).toBeInTheDocument();
    expect(screen.getByTestId('from-probe')).toHaveTextContent('/secret?x=1');
  });

  it.each(['driver', 'cpo'])('redirects the authenticated %s role to /cpo/dashboard', (role) => {
    useAuth.mockReturnValue({ user: { email: 'user@amphive.test', role } });
    renderGuarded(<AdminProtectedRoute><div>admin content</div></AdminProtectedRoute>);
    expect(screen.getByText('cpo dashboard page')).toBeInTheDocument();
    expect(screen.queryByText('admin content')).not.toBeInTheDocument();
  });

  it('renders children for the admin role', () => {
    useAuth.mockReturnValue({ user: { email: 'root@amphive.test', role: 'admin' } });
    renderGuarded(<AdminProtectedRoute><div>admin content</div></AdminProtectedRoute>);
    expect(screen.getByText('admin content')).toBeInTheDocument();
  });
});
