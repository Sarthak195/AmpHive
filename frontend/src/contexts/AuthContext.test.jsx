/**
 * AuthContext tests: session restore on mount, login persisting the JWT,
 * failed-restore cleanup, and logout clearing everything. Children render
 * during the restore too — consumers branch on `loading` themselves (the
 * BootSplash gate lives in App, not here).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { AuthProvider, useAuth } from './AuthContext';
import api from '../api/client';

vi.mock('../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

const Probe = () => {
  const { user, login, logout, loading } = useAuth();
  return (
    <div>
      <div data-testid="user">{user ? user.email : 'anonymous'}</div>
      <div data-testid="loading">{String(loading)}</div>
      <button onClick={() => login('driver@amphive.test', 'pw').catch(() => {})}>login</button>
      <button onClick={logout}>logout</button>
    </div>
  );
};

const renderProbe = () =>
  render(
    <AuthProvider>
      <Probe />
    </AuthProvider>
  );

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
});

describe('session restore on mount', () => {
  it('renders children immediately while the restore is still in flight (loading=true)', async () => {
    localStorage.setItem('amphive_token', 'jwt-123');
    let resolveMe;
    api.get.mockReturnValue(new Promise((resolve) => { resolveMe = resolve; }));

    renderProbe();

    // Children are NOT withheld during loading — App-level code (BootSplash)
    // decides what to show; the context just exposes `loading`.
    expect(screen.getByTestId('user')).toHaveTextContent('anonymous');
    expect(screen.getByTestId('loading')).toHaveTextContent('true');

    resolveMe({ email: 'driver@amphive.test', role: 'driver' });
    await waitFor(() => expect(screen.getByTestId('loading')).toHaveTextContent('false'));
    expect(screen.getByTestId('user')).toHaveTextContent('driver@amphive.test');
  });

  it('restores the user via /api/auth/me when a token exists', async () => {
    localStorage.setItem('amphive_token', 'jwt-123');
    api.get.mockResolvedValue({ email: 'driver@amphive.test', role: 'driver' });

    renderProbe();

    expect(await screen.findByTestId('user')).toHaveTextContent('driver@amphive.test');
    expect(api.get).toHaveBeenCalledWith('/api/auth/me');
  });

  it('does not call /me without a token', async () => {
    renderProbe();

    expect(await screen.findByTestId('user')).toHaveTextContent('anonymous');
    expect(api.get).not.toHaveBeenCalled();
  });

  it('clears the stored token when restore fails (expired/invalid JWT)', async () => {
    localStorage.setItem('amphive_token', 'expired-jwt');
    localStorage.setItem('amphive_user', '{"email":"stale"}');
    api.get.mockRejectedValue(new Error('Authentication expired'));

    renderProbe();

    expect(await screen.findByTestId('user')).toHaveTextContent('anonymous');
    await waitFor(() => {
      expect(localStorage.getItem('amphive_token')).toBeNull();
      expect(localStorage.getItem('amphive_user')).toBeNull();
    });
  });
});

describe('login / logout', () => {
  it('login stores the JWT + user and updates state', async () => {
    api.post.mockResolvedValue({
      token: 'fresh-jwt',
      user: { email: 'driver@amphive.test', role: 'driver' },
    });
    renderProbe();
    await screen.findByTestId('user');

    await userEvent.click(screen.getByText('login'));

    expect(api.post).toHaveBeenCalledWith('/api/auth/login', {
      email: 'driver@amphive.test',
      password: 'pw',
    });
    expect(screen.getByTestId('user')).toHaveTextContent('driver@amphive.test');
    expect(localStorage.getItem('amphive_token')).toBe('fresh-jwt');
    expect(JSON.parse(localStorage.getItem('amphive_user')).email).toBe('driver@amphive.test');
  });

  it('login refreshes from /api/auth/me so fields missing from the AuthResponse land', async () => {
    api.post.mockResolvedValue({
      token: 'fresh-jwt',
      user: { email: 'driver@amphive.test', role: 'driver' },
    });
    api.get.mockResolvedValue({
      email: 'driver@amphive.test',
      role: 'driver',
      available_balance: 42,
      created_at: '2026-01-01T00:00:00Z',
      is_disabled: false,
    });
    renderProbe();
    await screen.findByTestId('user');

    await userEvent.click(screen.getByText('login'));

    // The optimistic set happens first, then the /me refresh lands the full shape.
    await waitFor(() => expect(api.get).toHaveBeenCalledWith('/api/auth/me'));
    await waitFor(() =>
      expect(JSON.parse(localStorage.getItem('amphive_user')).available_balance).toBe(42)
    );
  });

  it('logout revokes server-side then clears state and localStorage', async () => {
    api.post.mockImplementation((url) =>
      url === '/api/auth/login'
        ? Promise.resolve({ token: 'fresh-jwt', user: { email: 'driver@amphive.test', role: 'driver' } })
        : Promise.resolve({ status: 'logged_out' })
    );
    renderProbe();
    await screen.findByTestId('user');
    await userEvent.click(screen.getByText('login'));

    await userEvent.click(screen.getByText('logout'));

    expect(api.post).toHaveBeenCalledWith('/api/auth/logout');
    await waitFor(() => expect(screen.getByTestId('user')).toHaveTextContent('anonymous'));
    expect(localStorage.getItem('amphive_token')).toBeNull();
    expect(localStorage.getItem('amphive_user')).toBeNull();
  });

  it('logout still clears local state when the revoke request fails', async () => {
    api.post.mockImplementation((url) =>
      url === '/api/auth/login'
        ? Promise.resolve({ token: 'fresh-jwt', user: { email: 'driver@amphive.test', role: 'driver' } })
        : Promise.reject(new Error('network down'))
    );
    renderProbe();
    await screen.findByTestId('user');
    await userEvent.click(screen.getByText('login'));

    await userEvent.click(screen.getByText('logout'));

    await waitFor(() => expect(screen.getByTestId('user')).toHaveTextContent('anonymous'));
    expect(localStorage.getItem('amphive_token')).toBeNull();
  });
});
