/**
 * Login redirect tests: after a successful login/register, the driver
 * returns to wherever ProtectedRoute (or Home's QR/deep-link guard) sent
 * them from — carried as router state.from — instead of always bouncing to
 * Home. This is the other half of the QR-deep-link "survives the login
 * redirect" requirement (see Home.test.jsx for the ?plug= prefill half).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom';

import Login from './Login';
import { useAuth } from '../contexts/AuthContext';

vi.mock('../contexts/AuthContext', () => ({ useAuth: vi.fn() }));

const loginSpy = vi.fn();
const registerSpy = vi.fn();

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
  await userEvent.type(screen.getByLabelText('Email Address'), 'driver@amphive.test');
  await userEvent.type(screen.getByLabelText('Password'), 'password123');
  await userEvent.click(screen.getByRole('button', { name: 'Sign In' }));
};

beforeEach(() => {
  vi.clearAllMocks();
  useAuth.mockReturnValue({ login: loginSpy, register: registerSpy });
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

  it('applies the same return-to-origin logic after registering a new account', async () => {
    registerSpy.mockResolvedValue({});
    renderLogin({ pathname: '/login', state: { from: { pathname: '/', search: '?plug=7' } } });

    // Toggle to register mode — while signed out (isRegister=false), this
    // toggle button is the sole one reading "Create Account" (the submit
    // button still reads "Sign In" at this point).
    await userEvent.click(screen.getByRole('button', { name: 'Create Account' }));
    await userEvent.type(screen.getByLabelText('Full Name'), 'New Driver');
    await userEvent.type(screen.getByLabelText('Email Address'), 'new@amphive.test');
    await userEvent.type(screen.getByLabelText('Password'), 'password123');
    // Now the toggle reads "Sign In" and the submit button reads "Create
    // Account", so this uniquely targets the submit button.
    await userEvent.click(screen.getByRole('button', { name: 'Create Account' }));

    expect(registerSpy).toHaveBeenCalled();
    expect(await screen.findByTestId('location-probe')).toHaveTextContent('/?plug=7');
  });
});
