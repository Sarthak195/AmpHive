/**
 * Login redirect tests: after a successful sign-in, the driver returns to
 * wherever ProtectedRoute (or Home's QR/deep-link guard) sent them from —
 * carried as router state.from, taking priority over the `?next=` query
 * param — instead of always bouncing to Home. This is the other half of the
 * QR-deep-link "survives the login redirect" requirement (see Home.test.jsx
 * for the ?plug= prefill half). Register mode has moved to Signup — this
 * page is sign-in only.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom';

import Login from './Login';
import { useAuth } from '../contexts/AuthContext';

vi.mock('../contexts/AuthContext', () => ({ useAuth: vi.fn() }));

const loginSpy = vi.fn();

// Renders alongside the routed content so tests can assert on the actual
// resulting location (pathname + search) rather than on page text, which
// wouldn't reveal whether the query string survived the round trip.
const LocationProbe = () => {
  const location = useLocation();
  return <div data-testid="location-probe">{location.pathname}{location.search}</div>;
};

const renderLogin = (initialEntry = '/login') =>
  render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <LocationProbe />
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route path="/" element={<div>home page</div>} />
        <Route path="/topup" element={<div>topup page</div>} />
      </Routes>
    </MemoryRouter>
  );

const submitLogin = async () => {
  await userEvent.type(screen.getByLabelText('Email address'), 'driver@amphive.test');
  await userEvent.type(screen.getByLabelText('Password'), 'password123');
  await userEvent.click(screen.getByRole('button', { name: 'Sign in' }));
};

beforeEach(() => {
  vi.clearAllMocks();
  useAuth.mockReturnValue({ login: loginSpy });
});

describe('Login redirect target', () => {
  it('defaults to Home when there is no prior location', async () => {
    loginSpy.mockResolvedValue({});
    renderLogin('/login');
    await submitLogin();
    expect(await screen.findByText('home page')).toBeInTheDocument();
    expect(screen.getByTestId('location-probe')).toHaveTextContent('/');
  });

  it('returns to a plain protected route (e.g. /topup) carried via state.from', async () => {
    loginSpy.mockResolvedValue({});
    renderLogin({ pathname: '/login', state: { from: { pathname: '/topup', search: '' } } });
    await submitLogin();
    expect(await screen.findByText('topup page')).toBeInTheDocument();
  });

  it('returns to a QR deep link, preserving the ?plug= query string', async () => {
    loginSpy.mockResolvedValue({});
    renderLogin({ pathname: '/login', state: { from: { pathname: '/', search: '?plug=42' } } });
    await submitLogin();
    expect(await screen.findByTestId('location-probe')).toHaveTextContent('/?plug=42');
  });

  it('falls back to the ?next= param when there is no state.from', async () => {
    loginSpy.mockResolvedValue({});
    renderLogin('/login?next=%2F%3Fplug%3D7');
    await submitLogin();
    expect(await screen.findByTestId('location-probe')).toHaveTextContent('/?plug=7');
  });

  it('state.from wins over ?next= when both are present', async () => {
    loginSpy.mockResolvedValue({});
    renderLogin({
      pathname: '/login',
      search: '?next=%2Ftopup',
      state: { from: { pathname: '/', search: '?plug=9' } },
    });
    await submitLogin();
    expect(await screen.findByTestId('location-probe')).toHaveTextContent('/?plug=9');
  });

  it('surfaces a login failure inline and stays on the page', async () => {
    loginSpy.mockRejectedValue(new Error('Incorrect email or password.'));
    renderLogin('/login');
    await submitLogin();
    expect(await screen.findByText('Incorrect email or password.')).toBeInTheDocument();
    expect(screen.getByLabelText('Email address')).toBeInTheDocument();
  });

  it('links to Signup and Forgot password', () => {
    renderLogin('/login');
    expect(screen.getByRole('link', { name: 'Create an account' })).toHaveAttribute('href', '/signup');
    expect(screen.getByRole('link', { name: 'Forgot password?' })).toHaveAttribute('href', '/forgot-password');
  });
});
