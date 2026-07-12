/**
 * SessionContext multi-session tests: restore focuses the newest of ALL
 * active sessions, start prepends + focuses, stop removes the focused one,
 * and switchSession refocuses the live monitor.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { SessionProvider, useSession } from './SessionContext';
import api from '../api/client';

vi.mock('../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));
// The user object must be render-stable: SessionProvider keys effects on
// [user], so a fresh object each call would loop the socket effect forever.
const { MOCK_USER } = vi.hoisted(() => ({
  MOCK_USER: { id: 1, email: 'driver@amphive.test' },
}));
vi.mock('./AuthContext', () => ({
  useAuth: () => ({ user: MOCK_USER, refreshUser: vi.fn().mockResolvedValue(undefined) }),
}));
vi.mock('socket.io-client', () => ({
  io: vi.fn(() => ({
    on: vi.fn(),
    off: vi.fn(),
    emit: vi.fn(),
    disconnect: vi.fn(),
    id: 'sid-test',
  })),
}));

const TWO_SESSIONS = {
  active: true,
  sessions: [
    { session_id: 2, plug_id: 20, plug_name: 'Plug B', started_at: '2026-07-07T12:30:00+00:00' },
    { session_id: 1, plug_id: 10, plug_name: 'Plug A', started_at: '2026-07-07T12:00:00+00:00' },
  ],
  session_id: 2,
  plug_id: 20,
  plug_name: 'Plug B',
  started_at: '2026-07-07T12:30:00+00:00',
};

const Probe = () => {
  const { activeSessions, sessionId, isActive, focusedLimits, startSession, stopSession, switchSession } =
    useSession();
  return (
    <div>
      <div data-testid="focused">{String(sessionId)}</div>
      <div data-testid="isActive">{String(isActive)}</div>
      <div data-testid="limits">{JSON.stringify(focusedLimits) || 'null'}</div>
      <ul data-testid="sessions">
        {activeSessions.map((s) => (
          <li key={s.session_id}>{s.plug_name}</li>
        ))}
      </ul>
      <button onClick={() => startSession('7').catch(() => {})}>start</button>
      <button onClick={() => startSession('7', { max_kwh: 1.5 }).catch(() => {})}>start-kwh-limited</button>
      <button onClick={() => startSession('7', { max_duration_seconds: 1800 }).catch(() => {})}>start-time-limited</button>
      <button onClick={() => stopSession().catch(() => {})}>stop</button>
      <button onClick={() => switchSession(activeSessions[1])}>switch-to-older</button>
    </div>
  );
};

const renderProbe = () =>
  render(
    <SessionProvider>
      <Probe />
    </SessionProvider>
  );

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.setItem('amphive_token', 'jwt-123');
});

describe('restore on mount', () => {
  it('lists every active session and focuses the newest', async () => {
    api.get.mockResolvedValue(TWO_SESSIONS);
    renderProbe();

    await waitFor(() => expect(screen.getByTestId('focused')).toHaveTextContent('2'));
    expect(screen.getByTestId('sessions').children).toHaveLength(2);
    expect(screen.getByText('Plug A')).toBeInTheDocument();
    expect(screen.getByText('Plug B')).toBeInTheDocument();
    expect(screen.getByTestId('isActive')).toHaveTextContent('true');
  });

  it('stays idle with no active sessions', async () => {
    api.get.mockResolvedValue({ active: false, sessions: [] });
    renderProbe();

    await waitFor(() => expect(api.get).toHaveBeenCalledWith('/api/sessions/active'));
    expect(screen.getByTestId('focused')).toHaveTextContent('null');
    expect(screen.getByTestId('sessions').children).toHaveLength(0);
  });
});

describe('switchSession', () => {
  it('refocuses the live monitor on the chosen session', async () => {
    api.get.mockResolvedValue(TWO_SESSIONS);
    renderProbe();
    await waitFor(() => expect(screen.getByTestId('focused')).toHaveTextContent('2'));

    await userEvent.click(screen.getByText('switch-to-older'));

    expect(screen.getByTestId('focused')).toHaveTextContent('1');
    expect(screen.getByTestId('isActive')).toHaveTextContent('true');
  });
});

describe('startSession', () => {
  it('prepends the new session to the list and focuses it', async () => {
    api.get.mockResolvedValue({ active: false, sessions: [] });
    api.post.mockResolvedValue({
      status: 'started',
      session_id: 9,
      plug_id: 7,
      plug_name: 'Plug X',
    });
    renderProbe();
    await waitFor(() => expect(api.get).toHaveBeenCalled());

    await userEvent.click(screen.getByText('start'));

    // No limit chosen → ONLY plug_id is sent (backend defaults apply).
    expect(api.post).toHaveBeenCalledWith('/api/sessions/start', { plug_id: 7 });
    expect(screen.getByTestId('focused')).toHaveTextContent('9');
    expect(screen.getByText('Plug X')).toBeInTheDocument();
  });

  it('sends max_kwh when a limit is chosen and tracks the echoed limits', async () => {
    api.get.mockResolvedValue({ active: false, sessions: [] });
    api.post.mockResolvedValue({
      status: 'started',
      session_id: 9,
      plug_id: 7,
      plug_name: 'Plug X',
      max_kwh: 1.5,
      max_duration_seconds: 14400, // backend echoes the default it applied
    });
    renderProbe();
    await waitFor(() => expect(api.get).toHaveBeenCalled());

    await userEvent.click(screen.getByText('start-kwh-limited'));

    expect(api.post).toHaveBeenCalledWith('/api/sessions/start', { plug_id: 7, max_kwh: 1.5 });
    expect(screen.getByTestId('limits')).toHaveTextContent(
      JSON.stringify({ max_kwh: 1.5, max_duration_seconds: 14400 })
    );
  });

  it('sends max_duration_seconds for a time limit', async () => {
    api.get.mockResolvedValue({ active: false, sessions: [] });
    api.post.mockResolvedValue({
      status: 'started', session_id: 9, plug_id: 7, plug_name: 'Plug X',
    });
    renderProbe();
    await waitFor(() => expect(api.get).toHaveBeenCalled());

    await userEvent.click(screen.getByText('start-time-limited'));

    expect(api.post).toHaveBeenCalledWith(
      '/api/sessions/start', { plug_id: 7, max_duration_seconds: 1800 }
    );
  });
});

describe('focusedLimits restore', () => {
  it('carries the limits reported by /api/sessions/active into the focused session', async () => {
    api.get.mockResolvedValue({
      active: true,
      sessions: [
        {
          session_id: 5, plug_id: 3, plug_name: 'Plug L',
          started_at: '2026-07-12T09:00:00+00:00',
          max_kwh: 1.0, max_duration_seconds: 1800,
        },
      ],
      session_id: 5, plug_id: 3, plug_name: 'Plug L',
      started_at: '2026-07-12T09:00:00+00:00',
    });
    renderProbe();

    await waitFor(() => expect(screen.getByTestId('focused')).toHaveTextContent('5'));
    expect(screen.getByTestId('limits')).toHaveTextContent(
      JSON.stringify({ max_kwh: 1.0, max_duration_seconds: 1800 })
    );
  });

  it('leaves focusedLimits null for a legacy session without limit fields', async () => {
    api.get.mockResolvedValue(TWO_SESSIONS); // fixture has no limit fields
    renderProbe();

    await waitFor(() => expect(screen.getByTestId('focused')).toHaveTextContent('2'));
    expect(screen.getByTestId('limits')).toHaveTextContent('null');
  });
});

describe('stopSession', () => {
  it('stops the focused session and removes it from the list', async () => {
    api.get.mockResolvedValue(TWO_SESSIONS);
    api.post.mockResolvedValue({ status: 'completed', session_id: 2 });
    renderProbe();
    await waitFor(() => expect(screen.getByTestId('focused')).toHaveTextContent('2'));

    await userEvent.click(screen.getByText('stop'));

    expect(api.post).toHaveBeenCalledWith('/api/sessions/stop', { session_id: 2 });
    expect(screen.getByTestId('sessions').children).toHaveLength(1);
    expect(screen.getByText('Plug A')).toBeInTheDocument();
    expect(screen.queryByText('Plug B')).not.toBeInTheDocument();
    expect(screen.getByTestId('isActive')).toHaveTextContent('false');
  });
});
