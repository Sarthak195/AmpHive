/**
 * ResetPassword page tests: the token is read from ?token=..., the new
 * password (with live mismatch validation) posts to
 * /api/auth/reset-password, success shows the all-sessions-revoked notice
 * with a path back to /login, backend 400s (expired/used/unknown token)
 * surface verbatim with an inline "Request a new link" fallback (the #1
 * stale-link path), and a token-less visit gets the same fallback instead
 * of a doomed form.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Routes, Route } from 'react-router-dom';

import ResetPassword from './ResetPassword';
import api from '../api/client';

vi.mock('../api/client', () => {
  const api = { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() };
  return { api, default: api, apiRequest: vi.fn() };
});

const renderPage = (entry = '/reset-password?token=tok123') =>
  render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route path="/reset-password" element={<ResetPassword />} />
        <Route path="/login" element={<div>login page</div>} />
        <Route path="/forgot-password" element={<div>forgot page</div>} />
      </Routes>
    </MemoryRouter>
  );

const fillAndSubmit = async (password = 'newpassword1', confirm = password) => {
  await userEvent.type(screen.getByLabelText('New password'), password);
  await userEvent.type(screen.getByLabelText('Confirm new password'), confirm);
  await userEvent.click(screen.getByRole('button', { name: 'Set new password' }));
};

beforeEach(() => vi.clearAllMocks());

describe('ResetPassword page', () => {
  it('posts the query-string token with the new password, then routes to login', async () => {
    api.post.mockResolvedValue({ status: 'password_reset' });
    renderPage('/reset-password?token=tok123');

    await fillAndSubmit();

    expect(api.post).toHaveBeenCalledWith('/api/auth/reset-password', {
      token: 'tok123',
      password: 'newpassword1',
    });
    expect(await screen.findByText(/all your existing sessions have been signed out/i)).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Go to sign in' }));
    expect(await screen.findByText('login page')).toBeInTheDocument();
  });

  it('flags a mismatched confirmation live, without calling the API', async () => {
    renderPage();
    await userEvent.type(screen.getByLabelText('New password'), 'newpassword1');
    await userEvent.type(screen.getByLabelText('Confirm new password'), 'different1');

    expect(screen.getByText('Passwords do not match.')).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Set new password' }));
    expect(api.post).not.toHaveBeenCalled();
  });

  it('surfaces the backend 400 for an invalid/expired/used token with a request-new-link fallback', async () => {
    api.post.mockRejectedValue(new Error('Invalid or expired reset link. Please request a new one.'));
    renderPage();
    await fillAndSubmit();
    expect(await screen.findByText(/invalid or expired reset link/i)).toBeInTheDocument();
    // Form stays so the driver can see the fallback, and can retry once they have a new link.
    expect(screen.getByLabelText('New password')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Request a new link' })).toHaveAttribute(
      'href', '/forgot-password'
    );
  });

  it('shows the request-a-new-link fallback when the token is missing', () => {
    renderPage('/reset-password');
    expect(screen.getByText(/missing its token/i)).toBeInTheDocument();
    expect(screen.queryByLabelText('New password')).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Request a new link' })).toHaveAttribute(
      'href', '/forgot-password'
    );
  });
});
