/**
 * GoogleCallback tests: the landing page for /auth/google/callback#code=...
 * (backend/routers/auth.py google_callback's success redirect). The backend
 * already finished the OAuth round-trip server-side, but hands back a
 * single-use, browser-bound CODE (not a raw JWT — that was the M5 login-CSRF
 * fix). This page's job is to POST that code to /api/auth/google/exchange to
 * obtain the real app JWT, hand it to AuthContext.loginWithToken(), and land
 * on Home; a missing code — or an exchange the server refuses (e.g. a planted
 * link with no matching nonce cookie) — renders the same error-state look as
 * Login instead of silently redirecting.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';

import GoogleCallback from './GoogleCallback';
import { useAuth } from '../contexts/AuthContext';
import api from '../api/client';

vi.mock('../contexts/AuthContext', () => ({ useAuth: vi.fn() }));
vi.mock('../api/client', () => ({ default: { post: vi.fn() } }));

const loginWithTokenSpy = vi.fn();

// pushState (not a `location.hash =` assignment) sets window.location.hash
// without triggering jsdom's "Not implemented: navigation" console warning —
// jsdom implements the History API fully, just not a real cross-document nav.
const setHash = (hash) => window.history.pushState(null, '', `/auth/google/callback${hash}`);

const renderCallback = () =>
  render(
    <MemoryRouter initialEntries={['/auth/google/callback']}>
      <Routes>
        <Route path="/auth/google/callback" element={<GoogleCallback />} />
        <Route path="/" element={<div>home page</div>} />
        <Route path="/login" element={<div>login page</div>} />
      </Routes>
    </MemoryRouter>
  );

beforeEach(() => {
  vi.clearAllMocks();
  useAuth.mockReturnValue({ loginWithToken: loginWithTokenSpy });
  setHash('');
});

describe('GoogleCallback', () => {
  it('exchanges the code from the fragment for a JWT, logs in, and redirects to Home', async () => {
    api.post.mockResolvedValue({ token: 'fake.jwt.token' });
    loginWithTokenSpy.mockResolvedValue(undefined);
    setHash('#code=abc123');

    renderCallback();

    expect(await screen.findByText('home page')).toBeInTheDocument();
    expect(api.post).toHaveBeenCalledWith('/api/auth/google/exchange', { code: 'abc123' });
    expect(loginWithTokenSpy).toHaveBeenCalledWith('fake.jwt.token');
  });

  it('renders the login error state when the fragment has no code', async () => {
    renderCallback();

    expect(await screen.findByText(/did not complete/i)).toBeInTheDocument();
    expect(api.post).not.toHaveBeenCalled();
    expect(loginWithTokenSpy).not.toHaveBeenCalled();
    expect(screen.getByRole('link', { name: 'Back to sign in' })).toHaveAttribute('href', '/login');
  });

  it('renders the error state when the exchange is refused (e.g. planted link / no nonce cookie)', async () => {
    api.post.mockRejectedValue(new Error('Invalid or expired sign-in. Please try again.'));
    setHash('#code=planted');

    renderCallback();

    expect(await screen.findByText('Invalid or expired sign-in. Please try again.')).toBeInTheDocument();
    expect(loginWithTokenSpy).not.toHaveBeenCalled();
  });

  it('renders the error state if loginWithToken rejects (e.g. server rejected the token)', async () => {
    api.post.mockResolvedValue({ token: 'fake.jwt.token' });
    loginWithTokenSpy.mockRejectedValue(new Error('Authentication expired. Please sign in again.'));
    setHash('#code=abc123');

    renderCallback();

    expect(await screen.findByText('Authentication expired. Please sign in again.')).toBeInTheDocument();
  });

  it('offers a Return-to-app path (not Back-to-sign-in) when already signed in as someone else', async () => {
    api.post.mockResolvedValue({ token: 'fake.jwt.token' });
    const err = new Error('You are already signed in with a different account. Sign out first to switch accounts.');
    err.code = 'already_signed_in';
    loginWithTokenSpy.mockRejectedValue(err);
    setHash('#code=abc123');

    renderCallback();

    expect(await screen.findByText(/already signed in with a different account/i)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Return to app' })).toHaveAttribute('href', '/');
    expect(screen.queryByRole('link', { name: 'Back to sign in' })).not.toBeInTheDocument();
  });

  it('scrubs the code from the URL immediately, even when the exchange later rejects', async () => {
    const replaceStateSpy = vi.spyOn(window.history, 'replaceState');
    api.post.mockRejectedValue(new Error('Invalid or expired sign-in. Please try again.'));
    setHash('#code=abc123');

    renderCallback();

    expect(await screen.findByText('Invalid or expired sign-in. Please try again.')).toBeInTheDocument();
    expect(replaceStateSpy).toHaveBeenCalledWith(null, '', '/auth/google/callback');
    expect(window.location.hash).toBe('');

    replaceStateSpy.mockRestore();
  });
});
