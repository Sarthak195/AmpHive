/**
 * Signup tests: name/email/password registration, the live 8-72 char
 * password hint, client-side validation before the API is ever called, and
 * the post-verification-feature success flow — registration no longer logs
 * the driver in, so a successful sign-up flips the page to a "check your
 * email" confirmation (with a resend control) and does NOT navigate into the
 * app or persist an auth token. API failures (e.g. duplicate email) still
 * surface inline on the form.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom';

import Signup from './Signup';
import { useAuth } from '../contexts/AuthContext';
import { useConfig } from '../contexts/ConfigContext';
import api from '../api/client';

vi.mock('../contexts/AuthContext', () => ({ useAuth: vi.fn() }));
vi.mock('../contexts/ConfigContext', () => ({ useConfig: vi.fn() }));
vi.mock('../api/client', () => {
  const api = { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() };
  return { api, default: api, apiRequest: vi.fn() };
});

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

const fillForm = async () => {
  await userEvent.type(screen.getByLabelText('Full name'), 'New Driver');
  await userEvent.type(screen.getByLabelText('Email address'), 'new@amphive.test');
  await userEvent.type(screen.getByLabelText('Password'), 'password123');
};

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
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

  it('on success shows the "check your email" state without navigating into the app or storing a token', async () => {
    registerSpy.mockResolvedValue({ status: 'verification_sent', email: 'new@amphive.test' });
    renderSignup();

    await fillForm();
    await userEvent.click(screen.getByRole('button', { name: 'Create account' }));

    expect(registerSpy).toHaveBeenCalledWith('new@amphive.test', 'password123', 'New Driver');
    // Confirmation state, not a redirect into the app.
    expect(await screen.findByText(/check your email/i)).toBeInTheDocument();
    expect(screen.getByText(/new@amphive.test/)).toBeInTheDocument();
    expect(screen.queryByText('home page')).not.toBeInTheDocument();
    expect(screen.getByTestId('location-probe')).toHaveTextContent('/signup');
    // Register no longer logs the driver in — no token persisted.
    expect(localStorage.getItem('amphive_token')).toBeNull();
    // Resend affordance is present.
    expect(screen.getByRole('button', { name: 'Resend email' })).toBeInTheDocument();
  });

  it('resend action posts the email to /api/auth/resend-verification and shows a generic confirmation', async () => {
    registerSpy.mockResolvedValue({ status: 'verification_sent', email: 'new@amphive.test' });
    api.post.mockResolvedValue({ status: 'ok' });
    renderSignup();

    await fillForm();
    await userEvent.click(screen.getByRole('button', { name: 'Create account' }));
    await screen.findByText(/check your email/i);

    await userEvent.click(screen.getByRole('button', { name: 'Resend email' }));

    expect(api.post).toHaveBeenCalledWith('/api/auth/resend-verification', {
      email: 'new@amphive.test',
    });
    expect(await screen.findByText(/a new verification link is on its way/i)).toBeInTheDocument();
  });

  it('surfaces an API failure (e.g. email already registered) inline', async () => {
    registerSpy.mockRejectedValue(new Error('An account with that email already exists.'));
    renderSignup();

    await fillForm();
    await userEvent.click(screen.getByRole('button', { name: 'Create account' }));

    expect(await screen.findByText('An account with that email already exists.')).toBeInTheDocument();
    // Stays on the form — no confirmation state.
    expect(screen.getByLabelText('Full name')).toBeInTheDocument();
  });

  it('links back to Login', () => {
    renderSignup();
    expect(screen.getByRole('link', { name: 'Sign in' })).toHaveAttribute('href', '/login');
  });

  it('states consent to the Terms and the Privacy Policy at the point of submission', () => {
    renderSignup();

    const form = screen.getByRole('button', { name: 'Create account' }).closest('form');
    // The consent line lives inside the form, next to the button that acts on
    // it — not in a footer somebody scrolls past.
    expect(within(form).getByText(/by creating an account you agree/i)).toBeInTheDocument();
    expect(within(form).getByRole('link', { name: 'Terms of Service' })).toHaveAttribute(
      'href',
      '/terms'
    );
    expect(within(form).getByRole('link', { name: 'Privacy Policy' })).toHaveAttribute(
      'href',
      '/privacy'
    );
  });

  it('does not gate submission on a consent checkbox', async () => {
    registerSpy.mockResolvedValue({ status: 'verification_sent', email: 'new@amphive.test' });
    renderSignup();

    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument();
    await fillForm();
    await userEvent.click(screen.getByRole('button', { name: 'Create account' }));
    expect(registerSpy).toHaveBeenCalled();
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
