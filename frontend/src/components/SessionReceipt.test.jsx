/**
 * SessionReceipt tests (redesign v3, C4): the ₹-first billing summary, the
 * shortfall help copy, the stopReasonCopy auto-stop banner, the GST-invoice
 * fetch (ok + failure), the DisputeModal hand-off and charge-again dismissal.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

import SessionReceipt from './SessionReceipt';
import { useSession } from '../contexts/SessionContext';
import { ToastProvider } from './ui';

const navigateSpy = vi.fn();
vi.mock('react-router-dom', async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, useNavigate: () => navigateSpy };
});
vi.mock('../contexts/SessionContext', () => ({ useSession: vi.fn() }));
vi.mock('../contexts/ConfigContext', () => ({
  useConfig: () => ({ coins_per_kwh: 5, coin_inr_rate: 1 }),
}));
vi.mock('./DisputeModal', () => ({
  default: ({ open, sessionId }) =>
    open ? <div data-testid="dispute-modal">dispute for {sessionId}</div> : null,
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
  duration_sec: 3725, // 1h 2m
  started_at: '2026-07-10T10:00:00Z',
  ended_at: '2026-07-10T11:02:05Z',
  reason: null,
};

const renderReceipt = () =>
  render(
    <MemoryRouter>
      <ToastProvider>
        <SessionReceipt />
      </ToastProvider>
    </MemoryRouter>
  );

const originalCreateObjectURL = URL.createObjectURL;
const originalRevokeObjectURL = URL.revokeObjectURL;

beforeEach(() => {
  vi.clearAllMocks();
  useSession.mockReturnValue({ receipt: RECEIPT, dismissReceipt: dismissSpy });
  localStorage.setItem('amphive_token', 'test-token');
  URL.createObjectURL = vi.fn(() => 'blob:mock');
  URL.revokeObjectURL = vi.fn();
  window.open = vi.fn();
});

afterEach(() => {
  URL.createObjectURL = originalCreateObjectURL;
  URL.revokeObjectURL = originalRevokeObjectURL;
  localStorage.clear();
  vi.unstubAllGlobals();
});

describe('SessionReceipt — summary', () => {
  it('renders nothing when there is no receipt', () => {
    useSession.mockReturnValue({ receipt: null, dismissReceipt: dismissSpy });
    renderReceipt();
    expect(screen.queryByText('Charging complete')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Charge again' })).not.toBeInTheDocument();
  });

  it('shows energy, duration and the ₹-first billing rows', () => {
    renderReceipt();
    expect(screen.getByText('Charging complete')).toBeInTheDocument();
    expect(screen.getByText(/Volt-FastPlug-01/)).toBeInTheDocument();
    expect(screen.getByText('1.23 kWh')).toBeInTheDocument();
    expect(screen.getByText('1h 2m')).toBeInTheDocument();
    expect(screen.getByText('−₹6.17')).toBeInTheDocument();
    expect(screen.getByText('₹93.83')).toBeInTheDocument(); // balance after
    expect(screen.queryByText(/couldn't be collected/i)).not.toBeInTheDocument();
  });

  it('explains an uncollected shortfall in plain language', () => {
    useSession.mockReturnValue({
      receipt: { ...RECEIPT, shortfall_coins: 2.5 },
      dismissReceipt: dismissSpy,
    });
    renderReceipt();
    expect(screen.getByText('₹2.50')).toBeInTheDocument();
    expect(
      screen.getByText(/The remaining ₹2\.50 couldn't be collected — it stays owed/)
    ).toBeInTheDocument();
  });

  it('shows the auto-stop reason through stopReasonCopy', () => {
    useSession.mockReturnValue({
      receipt: { ...RECEIPT, reason: 'auto-stopped: wallet balance exhausted' },
      dismissReceipt: dismissSpy,
    });
    renderReceipt();
    expect(
      screen.getByText('Stopped automatically — charging credit used up')
    ).toBeInTheDocument();
  });
});

describe('SessionReceipt — invoice', () => {
  it('fetches the GST invoice with the auth header, opens it, and defers revoking the blob URL', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      blob: async () => new Blob(['<html></html>'], { type: 'text/html' }),
    });
    vi.stubGlobal('fetch', fetchMock);
    const setTimeoutSpy = vi.spyOn(window, 'setTimeout');
    renderReceipt();
    await userEvent.click(screen.getByRole('button', { name: 'View GST invoice' }));

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/sessions/42/invoice?format=html'),
      { headers: { Authorization: 'Bearer test-token' } }
    );
    expect(window.open).toHaveBeenCalledWith('blob:mock', '_blank', 'noopener');

    // Revocation is deferred (not immediate) so the new tab has time to load
    // the blob first.
    expect(URL.revokeObjectURL).not.toHaveBeenCalled();
    const deferredRevoke = setTimeoutSpy.mock.calls.find(([, delay]) => delay === 60_000);
    expect(deferredRevoke).toBeTruthy();
    deferredRevoke[0]();
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:mock');
  });

  it('surfaces an invoice failure as an error toast (and opens nothing)', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 500 }));
    renderReceipt();
    await userEvent.click(screen.getByRole('button', { name: 'View GST invoice' }));

    expect(window.open).not.toHaveBeenCalled();
    expect(await screen.findByText(/Couldn't load the invoice/)).toBeInTheDocument();
  });

  it('hides the invoice action for a zero-cost session', () => {
    useSession.mockReturnValue({
      receipt: { ...RECEIPT, coins_spent: 0 },
      dismissReceipt: dismissSpy,
    });
    renderReceipt();
    expect(
      screen.queryByRole('button', { name: 'View GST invoice' })
    ).not.toBeInTheDocument();
  });
});

describe('SessionReceipt — actions', () => {
  it('opens the DisputeModal for this session from "Report an issue"', async () => {
    renderReceipt();
    await userEvent.click(screen.getByRole('button', { name: 'Report an issue' }));
    expect(screen.getByTestId('dispute-modal')).toHaveTextContent('dispute for 42');
  });

  it('dismisses the receipt and navigates home on "Charge again"', async () => {
    renderReceipt();
    await userEvent.click(screen.getByRole('button', { name: 'Charge again' }));
    expect(dismissSpy).toHaveBeenCalled();
    expect(navigateSpy).toHaveBeenCalledWith('/');
  });
});
