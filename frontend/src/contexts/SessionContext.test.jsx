/**
 * SessionContext multi-session tests: restore focuses the newest of ALL
 * active sessions, start prepends + focuses, stop removes the focused one,
 * and switchSession refocuses the live monitor.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { SessionProvider, useSession } from './SessionContext';
import api from '../api/client';

vi.mock('../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));
// The user object must be render-stable: SessionProvider keys effects on
// [user], so a fresh object each call would loop the socket effect forever.
// mockRefreshUser is likewise a single shared spy (not re-created per render)
// so tests can assert on its call count.
const { MOCK_USER, mockRefreshUser } = vi.hoisted(() => ({
  MOCK_USER: { id: 1, email: 'driver@amphive.test' },
  mockRefreshUser: vi.fn().mockResolvedValue(undefined),
}));
vi.mock('./AuthContext', () => ({
  useAuth: () => ({ user: MOCK_USER, refreshUser: mockRefreshUser }),
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
  const {
    activeSessions, sessionId, isActive, focusedLimits, focusedHoldCoins,
    startSession, stopSession, switchSession, updateLimits,
  } = useSession();
  return (
    <div>
      <div data-testid="focused">{String(sessionId)}</div>
      <div data-testid="isActive">{String(isActive)}</div>
      <div data-testid="limits">{JSON.stringify(focusedLimits) || 'null'}</div>
      <div data-testid="holdCoins">{String(focusedHoldCoins)}</div>
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
      <button onClick={() => updateLimits(2, { max_kwh: 2.5 }).catch(() => {})}>update-focused-limit</button>
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
    api.get
      .mockResolvedValueOnce({ active: false, sessions: [] }) // initial mount restore
      .mockResolvedValue({
        // startSession's own follow-up refreshActiveSessions() re-sync.
        active: true,
        sessions: [
          { session_id: 9, plug_id: 7, plug_name: 'Plug X', started_at: '2026-07-07T12:00:00+00:00' },
        ],
        session_id: 9, plug_id: 7, plug_name: 'Plug X',
      });
    api.post.mockResolvedValue({
      status: 'started',
      session_id: 9,
      plug_id: 7,
      plug_name: 'Plug X',
    });
    renderProbe();
    await waitFor(() => expect(api.get).toHaveBeenCalled());

    await userEvent.click(screen.getByText('start'));

    // No limit chosen → ONLY plug_id is sent (opt-in limits: the backend
    // persists no duration/energy cap — the session charges until stopped).
    expect(api.post).toHaveBeenCalledWith('/api/sessions/start', { plug_id: 7 });
    expect(screen.getByTestId('focused')).toHaveTextContent('9');
    await waitFor(() => expect(screen.getByText('Plug X')).toBeInTheDocument());
  });

  it('sends max_kwh when a limit is chosen and tracks the echoed limits', async () => {
    api.get.mockResolvedValue({ active: false, sessions: [] });
    api.post.mockResolvedValue({
      status: 'started',
      session_id: 9,
      plug_id: 7,
      plug_name: 'Plug X',
      max_kwh: 1.5,
      // [Opt-in charging limits] duration wasn't set, so the backend echoes
      // null back — not a hidden default — even though max_kwh was chosen.
      max_duration_seconds: null,
    });
    renderProbe();
    await waitFor(() => expect(api.get).toHaveBeenCalled());

    await userEvent.click(screen.getByText('start-kwh-limited'));

    expect(api.post).toHaveBeenCalledWith('/api/sessions/start', { plug_id: 7, max_kwh: 1.5 });
    expect(screen.getByTestId('limits')).toHaveTextContent(
      JSON.stringify({ max_kwh: 1.5, max_duration_seconds: null })
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

describe('startSession — hold_coins backfill', () => {
  it('backfills focusedHoldCoins from the follow-up /api/sessions/active refresh when the start response omits it (so the low-balance warning is not dead until a reload)', async () => {
    api.get
      .mockResolvedValueOnce({ active: false, sessions: [] }) // initial mount restore
      .mockResolvedValue({
        // The enriched GET the earlier pass shipped — reports hold_coins for
        // the session that was just started.
        active: true,
        sessions: [
          {
            session_id: 9, plug_id: 7, plug_name: 'Plug X',
            started_at: '2026-07-07T12:00:00+00:00', hold_coins: 30,
          },
        ],
        session_id: 9, plug_id: 7, plug_name: 'Plug X',
      });
    api.post.mockResolvedValue({
      // Older/legacy start response — no hold_coins field.
      status: 'started', session_id: 9, plug_id: 7, plug_name: 'Plug X',
    });
    renderProbe();
    await waitFor(() => expect(api.get).toHaveBeenCalled());

    await userEvent.click(screen.getByText('start'));

    expect(screen.getByTestId('focused')).toHaveTextContent('9');
    // Settles once the fire-and-forget refreshActiveSessions() resolves.
    await waitFor(() => expect(screen.getByTestId('holdCoins')).toHaveTextContent('30'));
  });
});

describe('focusedHoldCoins restore', () => {
  it('carries the hold_coins reported by /api/sessions/active into the focused session', async () => {
    api.get.mockResolvedValue({
      active: true,
      sessions: [
        {
          session_id: 5, plug_id: 3, plug_name: 'Plug L',
          started_at: '2026-07-12T09:00:00+00:00',
          hold_coins: 42.5,
        },
      ],
      session_id: 5, plug_id: 3, plug_name: 'Plug L',
      started_at: '2026-07-12T09:00:00+00:00',
    });
    renderProbe();

    await waitFor(() => expect(screen.getByTestId('focused')).toHaveTextContent('5'));
    expect(screen.getByTestId('holdCoins')).toHaveTextContent('42.5');
  });

  it('leaves focusedHoldCoins null for a legacy session without a hold', async () => {
    api.get.mockResolvedValue(TWO_SESSIONS); // fixture has no hold_coins field
    renderProbe();

    await waitFor(() => expect(screen.getByTestId('focused')).toHaveTextContent('2'));
    expect(screen.getByTestId('holdCoins')).toHaveTextContent('null');
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

describe('updateLimits', () => {
  it('PATCHes the focused session limits and reflects the returned values', async () => {
    api.get.mockResolvedValue(TWO_SESSIONS);
    api.patch.mockResolvedValue({
      status: 'updated', session_id: 2, max_kwh: 2.5, max_duration_seconds: 3600,
    });
    renderProbe();
    await waitFor(() => expect(screen.getByTestId('focused')).toHaveTextContent('2'));

    await userEvent.click(screen.getByText('update-focused-limit'));

    // Only the field set is sent; the returned limits land in focusedLimits.
    expect(api.patch).toHaveBeenCalledWith('/api/sessions/2/limits', { max_kwh: 2.5 });
    expect(screen.getByTestId('limits')).toHaveTextContent(
      JSON.stringify({ max_kwh: 2.5, max_duration_seconds: 3600 })
    );
  });

  it('refreshes the wallet after resizing the hold, so a grown/shrunk hold is reflected immediately', async () => {
    api.get.mockResolvedValue(TWO_SESSIONS);
    api.patch.mockResolvedValue({
      status: 'updated', session_id: 2, max_kwh: 2.5, max_duration_seconds: 3600,
    });
    renderProbe();
    await waitFor(() => expect(screen.getByTestId('focused')).toHaveTextContent('2'));
    mockRefreshUser.mockClear();

    await userEvent.click(screen.getByText('update-focused-limit'));

    await waitFor(() => expect(mockRefreshUser).toHaveBeenCalled());
  });
});

describe('stopSession', () => {
  it('stops the focused session, removes it from the list, and refocuses the remaining one (does not strand it)', async () => {
    api.get.mockResolvedValue(TWO_SESSIONS);
    api.post.mockResolvedValue({ status: 'completed', session_id: 2 });
    renderProbe();
    await waitFor(() => expect(screen.getByTestId('focused')).toHaveTextContent('2'));

    await userEvent.click(screen.getByText('stop'));

    expect(api.post).toHaveBeenCalledWith('/api/sessions/stop', { session_id: 2 });
    expect(screen.getByTestId('sessions').children).toHaveLength(1);
    expect(screen.getByText('Plug A')).toBeInTheDocument();
    expect(screen.queryByText('Plug B')).not.toBeInTheDocument();
    // A second concurrent session (Plug A) is still active — stay focused
    // and active on it instead of leaving the driver at a dead-end.
    expect(screen.getByTestId('focused')).toHaveTextContent('1');
    expect(screen.getByTestId('isActive')).toHaveTextContent('true');
  });

  it('marks the session ended (not stranded) when stopping the LAST active session', async () => {
    api.get.mockResolvedValue({
      active: true,
      sessions: [
        { session_id: 1, plug_id: 10, plug_name: 'Plug A', started_at: '2026-07-07T12:00:00+00:00' },
      ],
      session_id: 1, plug_id: 10, plug_name: 'Plug A', started_at: '2026-07-07T12:00:00+00:00',
    });
    api.post.mockResolvedValue({ status: 'completed', session_id: 1, coins_spent: 4.2 });
    renderProbe();
    await waitFor(() => expect(screen.getByTestId('focused')).toHaveTextContent('1'));

    await userEvent.click(screen.getByText('stop'));

    expect(screen.getByTestId('sessions').children).toHaveLength(0);
    expect(screen.getByTestId('isActive')).toHaveTextContent('false');
  });

  it('keeps a still-active sibling focused when the stop request fails (does not strand it)', async () => {
    api.get.mockResolvedValueOnce(TWO_SESSIONS); // initial mount restore
    api.post.mockRejectedValue(new Error('Network error'));
    renderProbe();
    await waitFor(() => expect(screen.getByTestId('focused')).toHaveTextContent('2'));

    // The failed stop triggers a re-sync — the backend still reports Plug A
    // (session 1) as active; Plug B (session 2, the one that failed to stop)
    // is gone.
    api.get.mockResolvedValueOnce({
      active: true,
      sessions: [
        { session_id: 1, plug_id: 10, plug_name: 'Plug A', started_at: '2026-07-07T12:00:00+00:00' },
      ],
      session_id: 1, plug_id: 10, plug_name: 'Plug A', started_at: '2026-07-07T12:00:00+00:00',
    });

    await userEvent.click(screen.getByText('stop'));

    await waitFor(() => expect(screen.getByTestId('focused')).toHaveTextContent('1'));
    expect(screen.getByTestId('isActive')).toHaveTextContent('true');
    expect(screen.getByText('Plug A')).toBeInTheDocument();
  });

  it('keeps isActive true and the same session focused when a failed stop turns out to still be active server-side', async () => {
    api.get.mockResolvedValueOnce(TWO_SESSIONS); // initial mount restore
    api.post.mockRejectedValue(new Error('Network error'));
    renderProbe();
    await waitFor(() => expect(screen.getByTestId('focused')).toHaveTextContent('2'));

    // Re-sync after the failure shows the "stopped" session (2) is actually
    // still active server-side (the stop request didn't take) alongside its
    // sibling.
    api.get.mockResolvedValueOnce(TWO_SESSIONS);

    await userEvent.click(screen.getByText('stop'));

    await waitFor(() => expect(screen.getByTestId('sessions').children).toHaveLength(2));
    expect(screen.getByTestId('focused')).toHaveTextContent('2');
    expect(screen.getByTestId('isActive')).toHaveTextContent('true');
  });

  it('clears isActive when a failed stop re-sync shows nothing remains active', async () => {
    api.get.mockResolvedValueOnce({
      active: true,
      sessions: [
        { session_id: 1, plug_id: 10, plug_name: 'Plug A', started_at: '2026-07-07T12:00:00+00:00' },
      ],
      session_id: 1, plug_id: 10, plug_name: 'Plug A', started_at: '2026-07-07T12:00:00+00:00',
    });
    api.post.mockRejectedValue(new Error('Network error'));
    renderProbe();
    await waitFor(() => expect(screen.getByTestId('focused')).toHaveTextContent('1'));

    api.get.mockResolvedValueOnce({ active: false, sessions: [] });

    await userEvent.click(screen.getByText('stop'));

    await waitFor(() => expect(screen.getByTestId('isActive')).toHaveTextContent('false'));
  });
});

describe('handleTelemetry — backend-driven auto-stop', () => {
  it('marks the session ended when a telemetry frame reports a terminal status, instead of leaving a stale live Stop button', async () => {
    let telemetryHandler;
    const { io } = await import('socket.io-client');
    io.mockReturnValueOnce({
      on: vi.fn((event, cb) => { if (event === 'telemetry') telemetryHandler = cb; }),
      off: vi.fn(),
      emit: vi.fn(),
      disconnect: vi.fn(),
      id: 'sid-test',
    });

    api.get.mockResolvedValue({
      active: true,
      sessions: [
        { session_id: 1, plug_id: 10, plug_name: 'Plug A', started_at: '2026-07-07T12:00:00+00:00' },
      ],
      session_id: 1, plug_id: 10, plug_name: 'Plug A', started_at: '2026-07-07T12:00:00+00:00',
    });
    renderProbe();
    await waitFor(() => expect(screen.getByTestId('focused')).toHaveTextContent('1'));
    await waitFor(() => expect(telemetryHandler).toBeInstanceOf(Function));

    // Backend auto-stopped (limit/wallet/hold exhaustion, or the stale-session
    // reaper) — the telemetry stream's final frame carries status "completed"
    // (services/telemetry.py TelemetryStore.end_session).
    act(() => {
      telemetryHandler({ status: 'completed', energy_kwh: 0.5, cost_coins: 3.0 });
    });

    await waitFor(() => expect(screen.getByTestId('isActive')).toHaveTextContent('false'));
    expect(screen.getByTestId('sessions').children).toHaveLength(0);
  });

  it('does not end the session for an ordinary in-progress telemetry frame', async () => {
    let telemetryHandler;
    const { io } = await import('socket.io-client');
    io.mockReturnValueOnce({
      on: vi.fn((event, cb) => { if (event === 'telemetry') telemetryHandler = cb; }),
      off: vi.fn(),
      emit: vi.fn(),
      disconnect: vi.fn(),
      id: 'sid-test',
    });

    api.get.mockResolvedValue(TWO_SESSIONS);
    renderProbe();
    await waitFor(() => expect(screen.getByTestId('focused')).toHaveTextContent('2'));
    await waitFor(() => expect(telemetryHandler).toBeInstanceOf(Function));

    act(() => {
      telemetryHandler({ status: 'charging', energy_kwh: 0.1, cost_coins: 0.5 });
    });

    expect(screen.getByTestId('isActive')).toHaveTextContent('true');
    expect(screen.getByTestId('sessions').children).toHaveLength(2);
  });
});
