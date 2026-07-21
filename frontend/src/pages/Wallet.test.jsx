/**
 * Wallet tests: balance + held-for-session line, the ledger table's
 * loading/error/empty states, and the top-up flow (amount validation,
 * order creation, Razorpay open, the three checkout outcomes — dismissed/
 * verified/failed — and the ?next= "back to your session" link). The
 * critical regression guard carried over from the old TopUp tests: /verify
 * is called with ONLY the Razorpay ids/signature, never a client amount.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, within, waitFor, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

import Wallet from './Wallet';
import api from '../api/client';

vi.mock('../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

const toast = { ok: vi.fn(), error: vi.fn(), info: vi.fn(), warn: vi.fn() };
vi.mock('../components/ui', () => ({
  useToast: () => toast,
}));

const refreshUser = vi.fn();
let mockUser = {
  email: 'driver@amphive.test',
  full_name: 'Driver',
  coin_balance: 640,
  available_balance: 640,
};
vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => ({ user: mockUser, refreshUser }),
}));

let mockConfig = { coin_inr_rate: 1 };
vi.mock('../contexts/ConfigContext', () => ({
  useConfig: () => mockConfig,
}));

const loadRazorpay = vi.fn();
vi.mock('../utils/razorpay', () => ({
  loadRazorpay: (...args) => loadRazorpay(...args),
}));

// Balance-after figures deliberately avoid the quick-amount values
// (100/200/500/1000) and the mock user's balance so text queries never
// collide with a chip button or the header balance figure.
const LEDGER = [
  {
    id: 1,
    amount: 140,
    transaction_type: 'topup',
    direction: 'credit',
    description: null,
    balance_after: 640,
    session_id: null,
    created_at: '2026-07-20T10:00:00Z',
  },
  {
    id: 2,
    amount: -25.5,
    transaction_type: 'session_debit',
    direction: 'debit',
    description: null,
    session_id: 42,
    balance_after: 614.5,
    created_at: '2026-07-19T09:00:00Z',
  },
];

let capturedOptions;
const rzpOpen = vi.fn();
const rzpOn = vi.fn();

class FakeRazorpay {
  constructor(options) {
    capturedOptions = options;
    this.open = rzpOpen;
    this.on = rzpOn;
  }
}

const renderWallet = (route = '/wallet') =>
  render(
    <MemoryRouter initialEntries={[route]}>
      <Wallet />
    </MemoryRouter>
  );

// Ledger fetch is the only async work on mount — waiting for a row is the
// stable "the page has settled" signal (the balance figure renders
// synchronously from the mocked user, so it never needs a wait itself).
const waitForSettled = () => screen.findByText('Top-up');

beforeEach(() => {
  vi.clearAllMocks();
  capturedOptions = undefined;
  mockUser = {
    email: 'driver@amphive.test',
    full_name: 'Driver',
    coin_balance: 640,
    available_balance: 640,
  };
  mockConfig = { coin_inr_rate: 1 };
  api.get.mockResolvedValue(LEDGER);
  loadRazorpay.mockResolvedValue(FakeRazorpay);
});

describe('balance card', () => {
  it('shows the ₹ balance and the 1:1 rate note', async () => {
    const { container } = renderWallet();
    await waitForSettled();
    expect(container.querySelector('.wallet-balance-figure')).toHaveTextContent('₹640.00');
    expect(screen.getByText(/1 coin = ₹1/)).toBeInTheDocument();
  });

  it('shows a held-balance line when a session is holding coins', async () => {
    mockUser = { ...mockUser, coin_balance: 640, available_balance: 560 };
    renderWallet();
    await waitForSettled();
    expect(screen.getByText(/reserved for the running session/)).toBeInTheDocument();
    expect(screen.getByText('₹80.00')).toBeInTheDocument();
  });

  it('omits the held-balance line when nothing is on hold', async () => {
    renderWallet();
    await waitForSettled();
    expect(screen.queryByText(/reserved for the running session/)).not.toBeInTheDocument();
  });

  it('omits the 1:1 rate note when the configured rate is not 1', async () => {
    mockConfig = { coin_inr_rate: 2 };
    renderWallet();
    await waitFor(() => expect(api.get).toHaveBeenCalled());
    expect(screen.queryByText(/1 coin = ₹1/)).not.toBeInTheDocument();
  });
});

describe('ledger', () => {
  it('renders rows with human type labels and signed amounts', async () => {
    renderWallet();
    await waitForSettled();
    const table = screen.getByRole('table');
    const rows = within(table).getAllByRole('row');

    expect(screen.getByText('Top-up')).toBeInTheDocument();
    expect(screen.getByText('Charging')).toBeInTheDocument();
    expect(screen.getByText('Session #42')).toBeInTheDocument();
    expect(within(rows[1]).getByText('₹140.00')).toBeInTheDocument();
    expect(within(rows[2]).getByText('-₹25.50')).toBeInTheDocument();
  });

  it('shows a retryable error instead of an empty state on failure', async () => {
    api.get.mockRejectedValueOnce(new Error('down'));
    renderWallet();

    expect(await screen.findByText("Couldn't load this")).toBeInTheDocument();
    expect(screen.queryByText('No wallet activity yet')).not.toBeInTheDocument();

    api.get.mockResolvedValue(LEDGER);
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('Top-up')).toBeInTheDocument();
  });

  it('shows the empty state only for a true zero-row ledger', async () => {
    api.get.mockResolvedValue([]);
    renderWallet();
    expect(await screen.findByText('No wallet activity yet')).toBeInTheDocument();
  });
});

describe('top-up amount picking', () => {
  it('defaults to the first quick amount and switches on click', async () => {
    renderWallet();
    await waitForSettled();
    expect(screen.getByRole('button', { name: 'Pay ₹100.00' })).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: '₹500.00' }));
    expect(screen.getByRole('button', { name: 'Pay ₹500.00' })).toBeInTheDocument();
  });

  it('rejects a custom amount below the minimum without calling the API', async () => {
    renderWallet();
    await waitForSettled();
    await userEvent.type(screen.getByLabelText(/custom amount/i), '10');
    await userEvent.click(screen.getByRole('button', { name: /^Pay/ }));

    expect(await screen.findByText(/Minimum top-up is ₹50.00/)).toBeInTheDocument();
    expect(api.post).not.toHaveBeenCalled();
  });

  it('rejects a custom amount above the maximum without calling the API', async () => {
    renderWallet();
    await waitForSettled();
    await userEvent.type(screen.getByLabelText(/custom amount/i), '20000');
    await userEvent.click(screen.getByRole('button', { name: /^Pay/ }));

    expect(await screen.findByText(/Maximum top-up is ₹10,000.00/)).toBeInTheDocument();
    expect(api.post).not.toHaveBeenCalled();
  });
});

describe('checkout flow', () => {
  it('creates the order and opens Razorpay checkout with it', async () => {
    api.post.mockResolvedValue({ order_id: 'order_123', amount: 10000, currency: 'INR', key_id: 'rzp_test' });
    renderWallet();
    await waitForSettled();

    await userEvent.click(screen.getByRole('button', { name: /^Pay/ }));

    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith('/api/payments/create-order', { amount_inr: 100 })
    );
    expect(capturedOptions.order_id).toBe('order_123');
    expect(capturedOptions.key).toBe('rzp_test');
    expect(rzpOpen).toHaveBeenCalled();
  });

  it('shows a neutral notice (not an error) when the checkout is dismissed', async () => {
    api.post.mockResolvedValue({ order_id: 'order_123', amount: 10000, currency: 'INR', key_id: 'rzp_test' });
    renderWallet();
    await waitForSettled();
    await userEvent.click(screen.getByRole('button', { name: /^Pay/ }));
    await waitFor(() => expect(capturedOptions).toBeDefined());

    act(() => capturedOptions.modal.ondismiss());

    const notice = await screen.findByText('Payment not completed — nothing was charged.');
    expect(notice.closest('.banner-info')).toBeInTheDocument();
    expect(toast.error).not.toHaveBeenCalled();
  });

  it('verifies with ONLY the Razorpay ids/signature — no client amount — and refreshes', async () => {
    api.post.mockImplementation((url) =>
      url === '/api/payments/create-order'
        ? Promise.resolve({ order_id: 'order_123', amount: 10000, currency: 'INR', key_id: 'rzp_test' })
        : Promise.resolve({ status: 'success', coins_credited: 100, new_balance: 740 })
    );
    renderWallet();
    await waitForSettled();
    await userEvent.click(screen.getByRole('button', { name: /^Pay/ }));
    await waitFor(() => expect(capturedOptions).toBeDefined());

    await act(async () => {
      await capturedOptions.handler({
        razorpay_order_id: 'order_123',
        razorpay_payment_id: 'pay_456',
        razorpay_signature: 'sig_789',
      });
    });

    const verifyCall = api.post.mock.calls.find(([url]) => url === '/api/payments/verify');
    expect(verifyCall[1]).toEqual({
      razorpay_order_id: 'order_123',
      razorpay_payment_id: 'pay_456',
      razorpay_signature: 'sig_789',
    });
    expect(refreshUser).toHaveBeenCalled();
    expect(toast.ok).toHaveBeenCalled();
    expect(await screen.findByText(/new balance ₹740.00/)).toBeInTheDocument();
  });

  it('shows the order id and a support-reference note when verification fails', async () => {
    api.post.mockImplementation((url) =>
      url === '/api/payments/create-order'
        ? Promise.resolve({ order_id: 'order_123', amount: 10000, currency: 'INR', key_id: 'rzp_test' })
        : Promise.reject(new Error('Invalid signature'))
    );
    renderWallet();
    await waitForSettled();
    await userEvent.click(screen.getByRole('button', { name: /^Pay/ }));
    await waitFor(() => expect(capturedOptions).toBeDefined());

    await act(async () => {
      await capturedOptions.handler({
        razorpay_order_id: 'order_123',
        razorpay_payment_id: 'pay_456',
        razorpay_signature: 'sig_789',
      });
    });

    expect(await screen.findByText(/verification failed/)).toBeInTheDocument();
    expect(screen.getByText('order_123')).toBeInTheDocument();
    expect(toast.error).toHaveBeenCalled();
  });

  it('shows a "Back to your session" link on success when ?next= is present', async () => {
    api.post.mockImplementation((url) =>
      url === '/api/payments/create-order'
        ? Promise.resolve({ order_id: 'order_123', amount: 10000, currency: 'INR', key_id: 'rzp_test' })
        : Promise.resolve({ status: 'success', coins_credited: 100, new_balance: 740 })
    );
    renderWallet('/wallet?next=%2Fsession');
    await waitForSettled();
    await userEvent.click(screen.getByRole('button', { name: /^Pay/ }));
    await waitFor(() => expect(capturedOptions).toBeDefined());

    await act(async () => {
      await capturedOptions.handler({
        razorpay_order_id: 'order_123',
        razorpay_payment_id: 'pay_456',
        razorpay_signature: 'sig_789',
      });
    });

    const link = await screen.findByRole('link', { name: 'Back to your session' });
    expect(link).toHaveAttribute('href', '/session');
  });

  it('hides the "Back to your session" link when ?next= is an open-redirect attempt', async () => {
    api.post.mockImplementation((url) =>
      url === '/api/payments/create-order'
        ? Promise.resolve({ order_id: 'order_123', amount: 10000, currency: 'INR', key_id: 'rzp_test' })
        : Promise.resolve({ status: 'success', coins_credited: 100, new_balance: 740 })
    );
    renderWallet('/wallet?next=https%3A%2F%2Fevil.com');
    await waitForSettled();
    await userEvent.click(screen.getByRole('button', { name: /^Pay/ }));
    await waitFor(() => expect(capturedOptions).toBeDefined());

    await act(async () => {
      await capturedOptions.handler({
        razorpay_order_id: 'order_123',
        razorpay_payment_id: 'pay_456',
        razorpay_signature: 'sig_789',
      });
    });

    await screen.findByText(/new balance ₹740.00/);
    expect(screen.queryByRole('link', { name: 'Back to your session' })).not.toBeInTheDocument();
  });

  it('surfaces order-creation failures without opening checkout', async () => {
    api.post.mockRejectedValue(new Error('Payment service is not configured.'));
    renderWallet();
    await waitForSettled();

    await userEvent.click(screen.getByRole('button', { name: /^Pay/ }));

    expect(await screen.findByText('Payment service is not configured.')).toBeInTheDocument();
    expect(rzpOpen).not.toHaveBeenCalled();
  });
});
