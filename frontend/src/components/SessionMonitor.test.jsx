/**
 * SessionMonitor tests: the live-feed robustness UX added alongside the
 * firmware relay/amps work — the staleness ("Reconnecting…") banner, the
 * gateway-alarm banner, and the voltage/relay secondary line.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen } from '@testing-library/react';

import SessionMonitor from './SessionMonitor';
import { useSession } from '../contexts/SessionContext';

vi.mock('../contexts/SessionContext', () => ({
  useSession: vi.fn(),
}));

const baseData = {
  plug_id: 2,
  power_w: 1000,
  energy_kwh: 0.25,
  current_a: 4.3,
  voltage_v: 231,
  cost_coins: 1.25,
  relay_on: true,
  duration_sec: 60,
};

beforeEach(() => {
  vi.clearAllMocks();
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('SessionMonitor', () => {
  it('shows Active (not stale) with a recent telemetry frame', () => {
    useSession.mockReturnValue({
      sessionData: { ...baseData, is_stale: false },
      isActive: true,
      stopSession: vi.fn(),
      lastFrameAt: Date.now(),
      focusedStartedAt: new Date().toISOString(),
      alarms: [],
    });
    render(<SessionMonitor />);
    expect(screen.getByText('Active')).toBeInTheDocument();
    expect(screen.queryByText('Reconnecting…')).not.toBeInTheDocument();
    // Voltage + relay secondary line
    expect(screen.getByText('231 V')).toBeInTheDocument();
    expect(screen.getByText('ON')).toBeInTheDocument();
  });

  it('shows the Reconnecting banner when no frame has arrived recently', () => {
    useSession.mockReturnValue({
      sessionData: { ...baseData, is_stale: false },
      isActive: true,
      stopSession: vi.fn(),
      lastFrameAt: Date.now() - 30000, // 30 s ago → stale (>15 s)
      focusedStartedAt: new Date().toISOString(),
      alarms: [],
    });
    render(<SessionMonitor />);
    expect(screen.getByText('Reconnecting…')).toBeInTheDocument();
    expect(screen.getByText(/Live readings paused/)).toBeInTheDocument();
  });

  it('honors the server-side is_stale flag even with a fresh frame timestamp', () => {
    useSession.mockReturnValue({
      sessionData: { ...baseData, is_stale: true },
      isActive: true,
      stopSession: vi.fn(),
      lastFrameAt: Date.now(),
      focusedStartedAt: new Date().toISOString(),
      alarms: [],
    });
    render(<SessionMonitor />);
    expect(screen.getByText('Reconnecting…')).toBeInTheDocument();
  });

  it('surfaces a gateway alarm for the focused plug', () => {
    useSession.mockReturnValue({
      sessionData: { ...baseData, is_stale: false },
      isActive: true,
      stopSession: vi.fn(),
      lastFrameAt: Date.now(),
      focusedStartedAt: new Date().toISOString(),
      alarms: [
        { plug_id: 2, event_type: 'UNAUTHORIZED_ON', detail: 'Plug switched ON with no active session', received_at: Date.now() },
      ],
    });
    render(<SessionMonitor />);
    expect(screen.getByText(/Plug switched ON with no active session/)).toBeInTheDocument();
  });

  it('ignores an alarm for a different plug', () => {
    useSession.mockReturnValue({
      sessionData: { ...baseData, is_stale: false },
      isActive: true,
      stopSession: vi.fn(),
      lastFrameAt: Date.now(),
      focusedStartedAt: new Date().toISOString(),
      alarms: [
        { plug_id: 99, event_type: 'UNAUTHORIZED_ON', detail: 'other plug', received_at: Date.now() },
      ],
    });
    render(<SessionMonitor />);
    expect(screen.queryByText('other plug')).not.toBeInTheDocument();
  });
});
