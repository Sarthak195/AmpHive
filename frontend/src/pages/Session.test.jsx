/**
 * Session page tests (redesign v3, C4): the no-session interstitial (no
 * redirect), the receipt-over-monitor swap after a stop, and the
 * multi-session seg pills with per-session live ₹ + switchSession.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

import Session from './Session';
import { useSession } from '../contexts/SessionContext';

vi.mock('../contexts/SessionContext', () => ({ useSession: vi.fn() }));
vi.mock('../contexts/ConfigContext', () => ({
  useConfig: () => ({ coins_per_kwh: 5, coin_inr_rate: 1 }),
}));
vi.mock('../components/SessionMonitor', () => ({
  default: () => <div data-testid="monitor" />,
}));
vi.mock('../components/SessionReceipt', () => ({
  default: () => <div data-testid="receipt" />,
}));

const SESSIONS = [
  { session_id: 7, plug_id: 2, plug_name: 'Garage plug', started_at: '2026-07-21T10:00:00Z' },
  { session_id: 9, plug_id: 4, plug_name: 'Porch plug', started_at: '2026-07-21T10:05:00Z' },
];

const base = {
  isActive: false,
  sessionData: null,
  activeSessions: [],
  sessionId: null,
  switchSession: vi.fn(),
  receipt: null,
};

const renderPage = () =>
  render(
    <MemoryRouter>
      <Session />
    </MemoryRouter>
  );

beforeEach(() => {
  vi.clearAllMocks();
  useSession.mockReturnValue(base);
});

describe('Session page — no active charge', () => {
  it('shows the interstitial with Find a charger / Recent activity instead of redirecting', () => {
    renderPage();
    expect(screen.getByText('No active charge')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Find a charger' })).toHaveAttribute('href', '/');
    expect(screen.getByRole('link', { name: 'Recent activity' })).toHaveAttribute(
      'href',
      '/activity'
    );
    expect(screen.queryByTestId('monitor')).not.toBeInTheDocument();
    expect(screen.queryByTestId('receipt')).not.toBeInTheDocument();
  });
});

describe('Session page — live monitor', () => {
  it('renders the monitor (and no seg pills) for a single active session', () => {
    useSession.mockReturnValue({
      ...base,
      isActive: true,
      sessionId: 7,
      sessionData: { plug_id: 2, plug_name: 'Garage plug', cost_coins: 1.25 },
      activeSessions: [SESSIONS[0]],
    });
    renderPage();
    expect(screen.getByTestId('monitor')).toBeInTheDocument();
    expect(screen.queryByText('No active charge')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Garage plug/ })).not.toBeInTheDocument();
  });

  it('shows seg pills with the focused session marked and its live ₹', () => {
    useSession.mockReturnValue({
      ...base,
      isActive: true,
      sessionId: 7,
      sessionData: { plug_id: 2, plug_name: 'Garage plug', cost_coins: 1.25 },
      activeSessions: SESSIONS,
    });
    renderPage();
    const focused = screen.getByRole('button', { name: /Garage plug/ });
    expect(focused).toHaveAttribute('aria-pressed', 'true');
    expect(focused).toHaveTextContent('₹1.25');
    // The other session hasn't streamed telemetry yet — no ₹ known.
    const other = screen.getByRole('button', { name: /Porch plug/ });
    expect(other).toHaveAttribute('aria-pressed', 'false');
    expect(other).toHaveTextContent('—');
  });

  it('refocuses the monitor via switchSession when another pill is picked', async () => {
    const switchSession = vi.fn();
    useSession.mockReturnValue({
      ...base,
      isActive: true,
      sessionId: 7,
      sessionData: { plug_id: 2, plug_name: 'Garage plug', cost_coins: 1.25 },
      activeSessions: SESSIONS,
      switchSession,
    });
    renderPage();
    await userEvent.click(screen.getByRole('button', { name: /Porch plug/ }));
    expect(switchSession).toHaveBeenCalledWith(SESSIONS[1]);
  });
});

describe('Session page — after a stop', () => {
  it('shows the receipt instead of the frozen monitor', () => {
    useSession.mockReturnValue({
      ...base,
      receipt: { session_id: 42, coins_spent: 6.17 },
    });
    renderPage();
    expect(screen.getByTestId('receipt')).toBeInTheDocument();
    expect(screen.queryByTestId('monitor')).not.toBeInTheDocument();
    expect(screen.queryByText('No active charge')).not.toBeInTheDocument();
  });

  it('shows the no-active-charge interstitial (not the stale monitor) after the receipt is dismissed', () => {
    // isActive is false and receipt has been cleared, but sessionData still
    // holds the finished session's last frame (status: 'completed') — the
    // interstitial should win, not a frozen/ticking monitor.
    useSession.mockReturnValue({
      ...base,
      isActive: false,
      receipt: null,
      sessionData: { plug_id: 2, plug_name: 'Garage plug', cost_coins: 6.17, status: 'completed' },
    });
    renderPage();
    expect(screen.getByText('No active charge')).toBeInTheDocument();
    expect(screen.queryByTestId('monitor')).not.toBeInTheDocument();
    expect(screen.queryByTestId('receipt')).not.toBeInTheDocument();
  });
});
