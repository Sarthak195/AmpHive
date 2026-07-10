/**
 * SessionReceipt tests: renders the post-session billing summary from the stop
 * response, surfaces an auto-stop notice + low-balance shortfall, and dismisses
 * on the action buttons.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

import SessionReceipt from './SessionReceipt';
import { useSession } from '../contexts/SessionContext';

const navigateSpy = vi.fn();
vi.mock('react-router-dom', async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, useNavigate: () => navigateSpy };
});
vi.mock('../contexts/SessionContext', () => ({
  useSession: vi.fn(),
}));

const dismissSpy = vi.fn();

const RECEIPT = {
  status: 'completed',
  session_id: 42,
  plug_id: 2,
  plug_name: 'Volt-FastPlug-01',
  energy_kwh: 1.234,
  peak_power_w: 3200,
  coins_spent: 6.17,
  shortfall_coins: 0,
  balance_before: 100,
  balance_remaining: 93.83,
  duration_sec: 3725, // 1h 2m 5s
  started_at: '2026-07-10T10:00:00Z',
  ended_at: '2026-07-10T11:02:05Z',
  reason: null,
};

const renderReceipt = () => render(<MemoryRouter><SessionReceipt /></MemoryRouter>);

beforeEach(() => {
  vi.clearAllMocks();
  useSession.mockReturnValue({ receipt: RECEIPT, dismissReceipt: dismissSpy });
});

describe('SessionReceipt', () => {
  it('renders nothing when there is no receipt', () => {
    useSession.mockReturnValue({ receipt: null, dismissReceipt: dismissSpy });
    const { container } = renderReceipt();
    expect(container).toBeEmptyDOMElement();
  });

  it('shows energy, duration, cost and the balance transition', () => {
    renderReceipt();
    expect(screen.getByText('Session Complete')).toBeInTheDocument();
    expect(screen.getByText('Volt-FastPlug-01', { exact: false })).toBeInTheDocument();
    expect(screen.getByText('1.234 kWh')).toBeInTheDocument();
    expect(screen.getByText('1h 2m 5s')).toBeInTheDocument();
    expect(screen.getByText('−6.17 coins')).toBeInTheDocument();
    // Balance before → after
    expect(screen.getByText(/93\.83/)).toBeInTheDocument();
  });

  it('shows an auto-stop notice and a shortfall when present', () => {
    useSession.mockReturnValue({
      receipt: { ...RECEIPT, reason: 'auto-stopped: wallet balance exhausted', shortfall_coins: 2.5 },
      dismissReceipt: dismissSpy,
    });
    renderReceipt();
    expect(screen.getByText(/stopped automatically/)).toBeInTheDocument();
    expect(screen.getByText(/Uncollected/)).toBeInTheDocument();
    expect(screen.getByText('2.50 coins')).toBeInTheDocument();
  });

  it('dismisses and navigates on "Charge Again"', async () => {
    renderReceipt();
    await userEvent.click(screen.getByRole('button', { name: 'Charge Again' }));
    expect(dismissSpy).toHaveBeenCalled();
    expect(navigateSpy).toHaveBeenCalledWith('/');
  });

  it('dismisses and navigates on "View History"', async () => {
    renderReceipt();
    await userEvent.click(screen.getByRole('button', { name: 'View History' }));
    expect(dismissSpy).toHaveBeenCalled();
    expect(navigateSpy).toHaveBeenCalledWith('/history');
  });
});
