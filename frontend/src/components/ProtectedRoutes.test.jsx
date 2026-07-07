/**
 * Route-guard tests: ProtectedRoute (auth required) and CpoProtectedRoute
 * (cpo/admin role required, driver → /cpo onboarding, anonymous → /login).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';

import { ProtectedRoute, CpoProtectedRoute } from './ProtectedRoutes';
import { useAuth } from '../contexts/AuthContext';

vi.mock('../contexts/AuthContext', () => ({
  useAuth: vi.fn(),
}));

const renderGuarded = (guardedElement) =>
  render(
    <MemoryRouter initialEntries={['/secret']}>
      <Routes>
        <Route path="/secret" element={guardedElement} />
        <Route path="/login" element={<div>login page</div>} />
        <Route path="/cpo" element={<div>cpo setup page</div>} />
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
