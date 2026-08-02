/**
 * GoogleCallback tests: the landing page for /auth/google/callback#token=...
 * (backend/routers/auth.py google_callback's success redirect). The backend
 * already finished the OAuth round-trip server-side — this page's only job
 * is to pull `token` out of the URL fragment, hand it to
 * AuthContext.loginWithToken(), and land on Home; a missing/malformed
 * fragment renders the same error-state look as Login instead of silently
 * redirecting.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';

import GoogleCallback from './GoogleCallback';
import { useAuth } from '../contexts/AuthContext';

vi.mock('../contexts/AuthContext', () => ({ useAuth: vi.fn() }));

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
  it('reads the token from the URL fragment, logs in, and redirects to Home', async () => {
    loginWithTokenSpy.mockResolvedValue(undefined);
    setHash('#token=fake.jwt.token');

    renderCallback();

    expect(await screen.findByText('home page')).toBeInTheDocument();
    expect(loginWithTokenSpy).toHaveBeenCalledWith('fake.jwt.token');
  });

  it('renders the login error state when the fragment has no token', async () => {
    renderCallback();

    expect(await screen.findByText(/did not complete/i)).toBeInTheDocument();
    expect(loginWithTokenSpy).not.toHaveBeenCalled();
    expect(screen.getByRole('link', { name: 'Back to sign in' })).toHaveAttribute('href', '/login');
  });

  it('renders the error state if loginWithToken rejects (e.g. server rejected the token)', async () => {
    loginWithTokenSpy.mockRejectedValue(new Error('Authentication expired. Please sign in again.'));
    setHash('#token=fake.jwt.token');

    renderCallback();

    expect(await screen.findByText('Authentication expired. Please sign in again.')).toBeInTheDocument();
  });

  it('scrubs the token from the URL immediately, even when loginWithToken later rejects', async () => {
    const replaceStateSpy = vi.spyOn(window.history, 'replaceState');
    loginWithTokenSpy.mockRejectedValue(new Error('Authentication expired. Please sign in again.'));
    setHash('#token=fake.jwt.token');

    renderCallback();

    expect(await screen.findByText('Authentication expired. Please sign in again.')).toBeInTheDocument();
    expect(replaceStateSpy).toHaveBeenCalledWith(null, '', '/auth/google/callback');
    expect(window.location.hash).toBe('');

    replaceStateSpy.mockRestore();
  });
});
