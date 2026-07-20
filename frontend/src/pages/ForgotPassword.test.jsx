/**
 * ForgotPassword page tests: submitting the email posts to
 * /api/auth/forgot-password and always lands on the same generic
 * "if an account exists" message (the backend is enumeration-safe, and so is
 * this UI). Errors (rate limit / network) surface inline. Also covers the
 * "Forgot password?" entry link on the Login page.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Routes, Route } from 'react-router-dom';

import ForgotPassword from './ForgotPassword';
import Login from './Login';
import api from '../api/client';
import { useAuth } from '../contexts/AuthContext';

vi.mock('../api/client', () => {
  const api = { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() };
  return { api, default: api, apiRequest: vi.fn() };
});
vi.mock('../contexts/AuthContext', () => ({ useAuth: vi.fn() }));

const renderPage = () =>
  render(
    <MemoryRouter initialEntries={['/forgot-password']}>
      <Routes>
        <Route path="/forgot-password" element={<ForgotPassword />} />
        <Route path="/login" element={<div>login page</div>} />
      </Routes>
    </MemoryRouter>
  );

beforeEach(() => {
  vi.clearAllMocks();
  useAuth.mockReturnValue({ login: vi.fn(), register: vi.fn() });
});

describe('ForgotPassword page', () => {
  it('posts the email and shows the generic success message', async () => {
    api.post.mockResolvedValue({ status: 'ok' });
    renderPage();

    await userEvent.type(screen.getByLabelText('Email Address'), 'driver@amphive.test');
    await userEvent.click(screen.getByRole('button', { name: 'Send Reset Link' }));

    expect(api.post).toHaveBeenCalledWith('/api/auth/forgot-password', {
      email: 'driver@amphive.test',
    });
    expect(
      await screen.findByText(/if an account exists for that email/i)
    ).toBeInTheDocument();
    // The form is gone — no way to tell whether the address matched.
    expect(screen.queryByLabelText('Email Address')).not.toBeInTheDocument();
  });

  it('surfaces an API error (e.g. rate limit) inline and keeps the form', async () => {
    api.post.mockRejectedValue(new Error('Too many password reset attempts. Try again in 60 s.'));
    renderPage();

    await userEvent.type(screen.getByLabelText('Email Address'), 'driver@amphive.test');
    await userEvent.click(screen.getByRole('button', { name: 'Send Reset Link' }));

    expect(await screen.findByText(/too many password reset attempts/i)).toBeInTheDocument();
    expect(screen.getByLabelText('Email Address')).toBeInTheDocument();
  });

  it('links back to Sign In', () => {
    renderPage();
    expect(screen.getByRole('link', { name: 'Sign In' })).toHaveAttribute('href', '/login');
  });
});

describe('Login page entry point', () => {
  it('shows a "Forgot password?" link in sign-in mode', () => {
    render(
      <MemoryRouter initialEntries={['/login']}>
        <Routes>
          <Route path="/login" element={<Login />} />
        </Routes>
      </MemoryRouter>
    );
    expect(screen.getByRole('link', { name: 'Forgot password?' })).toHaveAttribute(
      'href', '/forgot-password'
    );
  });
});
