/**
 * SessionMonitor tests: the live-feed robustness UX added alongside the
 * firmware relay/amps work — the staleness ("Reconnecting…") banner, the
 * gateway-alarm banner, and the voltage/relay secondary line.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, act } from '@testing-library/react';

import SessionMonitor from './SessionMonitor';
import { useSession } from '../contexts/SessionContext';
import { useWallet } from '../contexts/WalletContext';

vi.mock('../contexts/SessionContext', () => ({
  useSession: vi.fn(),
}));
vi.mock('../contexts/WalletContext', () => ({
  useWallet: vi.fn(() => ({ balance: 1000 })), // plenty by default → no low-balance banner
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
  // Default: ample balance so the low-balance banner is off unless a test opts in.
  useWallet.mockReturnValue({ balance: 1000 });
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

  it('shows a low-balance warning as accrued cost nears the wallet balance', () => {
    useWallet.mockReturnValue({ balance: 12 }); // cost 10 → remaining 2, below the 10-coin floor
    useSession.mockReturnValue({
      sessionData: { ...baseData, cost_coins: 10, is_stale: false },
      isActive: true,
      stopSession: vi.fn(),
      lastFrameAt: Date.now(),
      focusedStartedAt: new Date().toISOString(),
      alarms: [],
    });
    render(<SessionMonitor />);
    expect(screen.getByText(/Low balance/)).toBeInTheDocument();
    expect(screen.getByText(/stop\s+automatically/)).toBeInTheDocument();
  });

  it('does not warn when the balance comfortably covers the accrued cost', () => {
    useWallet.mockReturnValue({ balance: 1000 });
    useSession.mockReturnValue({
      sessionData: { ...baseData, cost_coins: 10, is_stale: false },
      isActive: true,
      stopSession: vi.fn(),
      lastFrameAt: Date.now(),
      focusedStartedAt: new Date().toISOString(),
      alarms: [],
    });
    render(<SessionMonitor />);
    expect(screen.queryByText(/Low balance/)).not.toBeInTheDocument();
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

  it('shows energy-limit progress toward the session max_kwh', () => {
    useSession.mockReturnValue({
      sessionData: { ...baseData, energy_kwh: 0.42, is_stale: false },
      isActive: true,
      stopSession: vi.fn(),
      lastFrameAt: Date.now(),
      focusedStartedAt: new Date().toISOString(),
      focusedLimits: { max_kwh: 1.0, max_duration_seconds: null },
      alarms: [],
    });
    render(<SessionMonitor />);
    expect(screen.getByText('0.42 / 1.00 kWh')).toBeInTheDocument();
    expect(screen.getByText(/stops automatically/)).toBeInTheDocument();
  });

  it('shows elapsed-vs-limit time when a duration limit is set', () => {
    useSession.mockReturnValue({
      sessionData: { ...baseData, is_stale: false },
      isActive: true,
      stopSession: vi.fn(),
      lastFrameAt: Date.now(),
      focusedStartedAt: new Date().toISOString(),
      focusedLimits: { max_kwh: null, max_duration_seconds: 14400 },
      alarms: [],
    });
    render(<SessionMonitor />);
    // Elapsed ticks from ~0; the limit side is the fixed 4 h cap.
    expect(screen.getByText(/\/ 04:00:00/)).toBeInTheDocument();
    expect(screen.getByText(/stops automatically/)).toBeInTheDocument();
  });

  it('shows both limits when both are set', () => {
    useSession.mockReturnValue({
      sessionData: { ...baseData, energy_kwh: 0.42, is_stale: false },
      isActive: true,
      stopSession: vi.fn(),
      lastFrameAt: Date.now(),
      focusedStartedAt: new Date().toISOString(),
      focusedLimits: { max_kwh: 1.0, max_duration_seconds: 1800 },
      alarms: [],
    });
    render(<SessionMonitor />);
    expect(screen.getByText('0.42 / 1.00 kWh')).toBeInTheDocument();
    expect(screen.getByText(/\/ 00:30:00/)).toBeInTheDocument();
  });

  it('renders no limit banner for a legacy session without limits', () => {
    useSession.mockReturnValue({
      sessionData: { ...baseData, is_stale: false },
      isActive: true,
      stopSession: vi.fn(),
      lastFrameAt: Date.now(),
      focusedStartedAt: new Date().toISOString(),
      focusedLimits: null,
      alarms: [],
    });
    render(<SessionMonitor />);
    expect(screen.queryByText(/stops automatically/)).not.toBeInTheDocument();
  });

  it('opens the limit editor and PATCHes the new target on save', async () => {
    const updateLimits = vi.fn().mockResolvedValue({
      status: 'updated', max_kwh: 2, max_duration_seconds: 1800,
    });
    useSession.mockReturnValue({
      sessionData: { ...baseData, energy_kwh: 0.42, is_stale: false },
      isActive: true,
      stopSession: vi.fn(),
      updateLimits,
      sessionId: 7,
      lastFrameAt: Date.now(),
      focusedStartedAt: new Date().toISOString(),
      focusedLimits: { max_kwh: 1.0, max_duration_seconds: 1800 },
      alarms: [],
    });
    render(<SessionMonitor />);

    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
    // Prefilled from the current limits; bump the energy target 1 → 2 kWh.
    fireEvent.change(screen.getByLabelText(/Energy limit/i), { target: { value: '2' } });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /Save limit/i }));
    });

    // 0.5 h stayed → 1800 s; new kWh sent alongside it.
    expect(updateLimits).toHaveBeenCalledWith(7, { max_kwh: 2, max_duration_seconds: 1800 });
  });

  it('rejects an empty limit edit without calling the API', () => {
    const updateLimits = vi.fn();
    useSession.mockReturnValue({
      sessionData: { ...baseData, is_stale: false },
      isActive: true,
      stopSession: vi.fn(),
      updateLimits,
      sessionId: 7,
      lastFrameAt: Date.now(),
      focusedStartedAt: new Date().toISOString(),
      focusedLimits: { max_kwh: 1.0, max_duration_seconds: 1800 },
      alarms: [],
    });
    render(<SessionMonitor />);

    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
    fireEvent.change(screen.getByLabelText(/Energy limit/i), { target: { value: '' } });
    fireEvent.change(screen.getByLabelText(/Time limit/i), { target: { value: '' } });
    fireEvent.click(screen.getByRole('button', { name: /Save limit/i }));

    expect(screen.getByText(/Enter an energy/i)).toBeInTheDocument();
    expect(updateLimits).not.toHaveBeenCalled();
  });
});
