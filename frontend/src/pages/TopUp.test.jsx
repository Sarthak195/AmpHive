/**
 * TopUp payment-handler tests. The critical one: the /verify call must NOT
 * send an amount — the backend credits the Razorpay-confirmed amount (the
 * client-trusted amount was the wallet-inflation vuln fixed 2026-07-05).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

import TopUp from './TopUp';
import api from '../api/client';

vi.mock('../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));
vi.mock('../contexts/WalletContext', () => ({
  useWallet: () => ({ balance: '100.00', refreshBalance: refreshBalanceSpy }),
}));
vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => ({ user: { email: 'driver@amphive.test', full_name: 'Driver' } }),
}));

const refreshBalanceSpy = vi.fn();

const ORDER = {
  key_id: 'rzp_test_key',
  amount: 10000, // paise
  currency: 'INR',
  order_id: 'order_123',
};

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

const renderTopUp = () =>
  render(
    <MemoryRouter>
      <TopUp />
    </MemoryRouter>
  );

beforeEach(() => {
  vi.clearAllMocks();
  capturedOptions = undefined;
  window.Razorpay = FakeRazorpay;
});

describe('handlePayment', () => {
  it('creates the order and opens Razorpay checkout with it', async () => {
    api.post.mockResolvedValue(ORDER);
    renderTopUp();

    await userEvent.click(screen.getByRole('button', { name: /^Pay/ }));

    expect(api.post).toHaveBeenCalledWith('/api/payments/create-order', {
      amount_inr: 100, // the default selected option
    });
    expect(capturedOptions.order_id).toBe('order_123');
    expect(capturedOptions.key).toBe('rzp_test_key');
    expect(rzpOpen).toHaveBeenCalled();
  });

  it('verifies with ONLY the Razorpay ids/signature — no client amount', async () => {
    api.post.mockImplementation((url) =>
      url === '/api/payments/create-order'
        ? Promise.resolve(ORDER)
        : Promise.resolve({ coins_credited: 100, new_balance: '200.00' })
    );
    renderTopUp();
    await userEvent.click(screen.getByRole('button', { name: /^Pay/ }));

    // Simulate Razorpay reporting a successful payment.
    await act(async () => {
      await capturedOptions.handler({
        razorpay_order_id: 'order_123',
        razorpay_payment_id: 'pay_456',
        razorpay_signature: 'sig_789',
      });
    });

    const verifyCall = api.post.mock.calls.find(([url]) => url === '/api/payments/verify');
    expect(verifyCall).toBeDefined();
    // toEqual is exact: proves amount_inr is NOT part of the payload.
    expect(verifyCall[1]).toEqual({
      razorpay_order_id: 'order_123',
      razorpay_payment_id: 'pay_456',
      razorpay_signature: 'sig_789',
    });
    expect(refreshBalanceSpy).toHaveBeenCalled();
    expect(await screen.findByText(/100 coins added/)).toBeInTheDocument();
  });

  it('shows an error and skips checkout when the SDK is not loaded', async () => {
    api.post.mockResolvedValue(ORDER);
    delete window.Razorpay;
    renderTopUp();

    await userEvent.click(screen.getByRole('button', { name: /^Pay/ }));

    expect(await screen.findByText(/Razorpay SDK not loaded/)).toBeInTheDocument();
    expect(api.post).not.toHaveBeenCalledWith('/api/payments/verify', expect.anything());
  });

  it('surfaces order-creation failures', async () => {
    api.post.mockRejectedValue(new Error('Insufficient funds are not a thing for topups'));
    renderTopUp();

    await userEvent.click(screen.getByRole('button', { name: /^Pay/ }));

    expect(
      await screen.findByText(/Insufficient funds are not a thing for topups/)
    ).toBeInTheDocument();
    expect(rzpOpen).not.toHaveBeenCalled();
  });
});
