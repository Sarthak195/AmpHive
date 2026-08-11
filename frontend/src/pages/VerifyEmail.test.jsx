/**
 * VerifyEmail page tests (mirrors ResetPassword): the token is read from
 * ?token=..., POSTed to /api/auth/verify-email on mount; a 200 AuthResponse
 * is adopted via loginWithToken (logging the driver in) and redirects to Home;
 * a backend 400 (invalid/expired/used) shows the error + a resend control;
 * and a missing token short-circuits to the same resend affordance without
 * ever calling the API.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Routes, Route } from 'react-router-dom';

import VerifyEmail from './VerifyEmail';
import { useAuth } from '../contexts/AuthContext';
import api from '../api/client';

vi.mock('../contexts/AuthContext', () => ({ useAuth: vi.fn() }));
vi.mock('../api/client', () => {
  const api = { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() };
  return { api, default: api, apiRequest: vi.fn() };
});

const loginWithTokenSpy = vi.fn();

const renderPage = (entry = '/verify-email?token=tok123') =>
  render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route path="/verify-email" element={<VerifyEmail />} />
        <Route path="/" element={<div>home page</div>} />
      </Routes>
    </MemoryRouter>
  );

beforeEach(() => {
  vi.clearAllMocks();
  loginWithTokenSpy.mockResolvedValue(undefined);
  useAuth.mockReturnValue({ loginWithToken: loginWithTokenSpy });
});

describe('VerifyEmail page', () => {
  it('posts the query-string token, logs in with the returned token, and redirects Home', async () => {
    api.post.mockResolvedValue({
      token: 'fresh-jwt',
      user: { email: 'new@amphive.test', role: 'driver' },
    });
    renderPage('/verify-email?token=tok123');

    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith('/api/auth/verify-email', { token: 'tok123' })
    );
    expect(loginWithTokenSpy).toHaveBeenCalledWith('fresh-jwt');
    expect(await screen.findByText('home page')).toBeInTheDocument();
  });

  it('shows an invalid/expired error with a resend control on a backend 400', async () => {
    api.post.mockRejectedValue(new Error('This link is invalid or expired.'));
    renderPage('/verify-email?token=badtok');

    expect(await screen.findByText(/this link is invalid or expired/i)).toBeInTheDocument();
    expect(loginWithTokenSpy).not.toHaveBeenCalled();
    // Resend affordance (email unknown → input + button) is shown.
    expect(screen.getByLabelText('Email address')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Resend verification email' })).toBeInTheDocument();
  });

  it('handles a missing token gracefully without calling the API', async () => {
    renderPage('/verify-email');

    expect(await screen.findByText(/missing its token/i)).toBeInTheDocument();
    expect(api.post).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Resend verification email' })).toBeInTheDocument();
  });

  it('resend posts the entered email to /api/auth/resend-verification with a generic confirmation', async () => {
    api.post.mockRejectedValueOnce(new Error('This link is invalid or expired.'));
    renderPage('/verify-email?token=badtok');
    await screen.findByText(/this link is invalid or expired/i);

    api.post.mockResolvedValueOnce({ status: 'ok' });
    await userEvent.type(screen.getByLabelText('Email address'), 'new@amphive.test');
    await userEvent.click(screen.getByRole('button', { name: 'Resend verification email' }));

    expect(api.post).toHaveBeenCalledWith('/api/auth/resend-verification', {
      email: 'new@amphive.test',
    });
    expect(await screen.findByText(/a new verification link is on its way/i)).toBeInTheDocument();
  });
});
