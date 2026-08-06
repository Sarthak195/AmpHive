/**
 * Signup tests: name/email/password registration, the live 8-72 char
 * password hint, client-side validation before the API is ever called, the
 * success toast + "straight in" redirect (honoring the same from/next
 * return-to-origin logic as Login so a QR deep-link funnel keeps working),
 * and API failure surfacing inline.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom';

import Signup from './Signup';
import { useAuth } from '../contexts/AuthContext';
import { useConfig } from '../contexts/ConfigContext';

vi.mock('../contexts/AuthContext', () => ({ useAuth: vi.fn() }));
vi.mock('../contexts/ConfigContext', () => ({ useConfig: vi.fn() }));

const toast = { ok: vi.fn(), error: vi.fn(), info: vi.fn(), warn: vi.fn() };
vi.mock('../components/ui', () => ({ useToast: () => toast }));

const registerSpy = vi.fn();

const LocationProbe = () => {
  const location = useLocation();
  return <div data-testid="location-probe">{location.pathname}{location.search}</div>;
};

const renderSignup = (initialEntry = '/signup') =>
  render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <LocationProbe />
      <Routes>
        <Route path="/signup" element={<Signup />} />
        <Route path="/" element={<div>home page</div>} />
      </Routes>
    </MemoryRouter>
  );

beforeEach(() => {
  vi.clearAllMocks();
  useAuth.mockReturnValue({ register: registerSpy });
  useConfig.mockReturnValue({ google_login_enabled: false });
});

describe('Signup', () => {
  it('shows a live hint that turns valid once the password reaches 8 characters', async () => {
    renderSignup();
    const password = screen.getByLabelText('Password');
    expect(screen.getByText(/at least 8 required/i)).toBeInTheDocument();

    await userEvent.type(password, 'short');
    expect(screen.getByText(/at least 8 required/i)).toHaveClass('is-invalid');

    await userEvent.type(password, '123');
    expect(screen.getByText(/at least 8 required/i)).toHaveClass('is-valid');
  });

  it('blocks submit client-side on a too-short password without calling the API', async () => {
    renderSignup();
    await userEvent.type(screen.getByLabelText('Full name'), 'New Driver');
    await userEvent.type(screen.getByLabelText('Email address'), 'new@amphive.test');
    await userEvent.type(screen.getByLabelText('Password'), 'short');
    await userEvent.click(screen.getByRole('button', { name: 'Create account' }));

    expect(registerSpy).not.toHaveBeenCalled();
    expect(screen.getByText(/must be 8-72 characters/i)).toBeInTheDocument();
  });

  it('registers, toasts success, and redirects to Home by default', async () => {
    registerSpy.mockResolvedValue({});
    renderSignup();

    await userEvent.type(screen.getByLabelText('Full name'), 'New Driver');
    await userEvent.type(screen.getByLabelText('Email address'), 'new@amphive.test');
    await userEvent.type(screen.getByLabelText('Password'), 'password123');
    await userEvent.click(screen.getByRole('button', { name: 'Create account' }));

    expect(registerSpy).toHaveBeenCalledWith('new@amphive.test', 'password123', 'New Driver');
    expect(await screen.findByText('home page')).toBeInTheDocument();
    expect(toast.ok).toHaveBeenCalledWith('Account created.');
  });

  it('honors a QR deep-link ?next= param on success', async () => {
    registerSpy.mockResolvedValue({});
    renderSignup('/signup?next=%2F%3Fplug%3D7');

    await userEvent.type(screen.getByLabelText('Full name'), 'New Driver');
    await userEvent.type(screen.getByLabelText('Email address'), 'new@amphive.test');
    await userEvent.type(screen.getByLabelText('Password'), 'password123');
    await userEvent.click(screen.getByRole('button', { name: 'Create account' }));

    await screen.findByText('home page');
    expect(screen.getByTestId('location-probe')).toHaveTextContent('/?plug=7');
  });

  it('rejects an open-redirect ?next= and falls back to Home', async () => {
    registerSpy.mockResolvedValue({});
    renderSignup('/signup?next=https%3A%2F%2Fevil.com');

    await userEvent.type(screen.getByLabelText('Full name'), 'New Driver');
    await userEvent.type(screen.getByLabelText('Email address'), 'new@amphive.test');
    await userEvent.type(screen.getByLabelText('Password'), 'password123');
    await userEvent.click(screen.getByRole('button', { name: 'Create account' }));

    expect(await screen.findByText('home page')).toBeInTheDocument();
  });

  it('surfaces an API failure (e.g. email already registered) inline', async () => {
    registerSpy.mockRejectedValue(new Error('An account with that email already exists.'));
    renderSignup();

    await userEvent.type(screen.getByLabelText('Full name'), 'New Driver');
    await userEvent.type(screen.getByLabelText('Email address'), 'new@amphive.test');
    await userEvent.type(screen.getByLabelText('Password'), 'password123');
    await userEvent.click(screen.getByRole('button', { name: 'Create account' }));

    expect(await screen.findByText('An account with that email already exists.')).toBeInTheDocument();
  });

  it('links back to Login', () => {
    renderSignup();
    expect(screen.getByRole('link', { name: 'Sign in' })).toHaveAttribute('href', '/login');
  });
});

describe('Google sign-in button (config-gated)', () => {
  it('is hidden when the backend reports Google sign-in unconfigured', () => {
    useConfig.mockReturnValue({ google_login_enabled: false });
    renderSignup();
    expect(screen.queryByRole('link', { name: /Continue with Google/i })).not.toBeInTheDocument();
  });

  it('renders a top-level link to the backend redirect when enabled', () => {
    useConfig.mockReturnValue({ google_login_enabled: true });
    renderSignup();
    const link = screen.getByRole('link', { name: /Continue with Google/i });
    expect(link).toHaveAttribute('href', '/api/auth/google/login');
  });
});
