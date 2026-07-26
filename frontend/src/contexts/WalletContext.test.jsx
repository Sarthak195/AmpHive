/**
 * WalletContext tests: balance/availableBalance are derived straight from
 * AuthContext's user object (no local state) — falling back to 0 with no
 * user — and refreshBalance() delegates to AuthContext's refreshUser() so a
 * post-payment reload flows back into the displayed balance.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { WalletProvider, useWallet } from './WalletContext';

const { mockRefreshUser, mockUseAuth } = vi.hoisted(() => ({
  mockRefreshUser: vi.fn().mockResolvedValue(undefined),
  mockUseAuth: vi.fn(),
}));
vi.mock('./AuthContext', () => ({
  useAuth: () => mockUseAuth(),
}));

const Probe = () => {
  const wallet = useWallet();
  return (
    <div>
      <div data-testid="balance">{String(wallet.balance)}</div>
      <div data-testid="available">{String(wallet.availableBalance)}</div>
      <div data-testid="shape">{Object.keys(wallet).sort().join(',')}</div>
      <button onClick={() => wallet.refreshBalance()}>refresh</button>
    </div>
  );
};

const renderProbe = () =>
  render(
    <WalletProvider>
      <Probe />
    </WalletProvider>
  );

beforeEach(() => {
  vi.clearAllMocks();
  mockRefreshUser.mockResolvedValue(undefined);
});

describe('unauthenticated', () => {
  it('falls back balance and availableBalance to 0 with no user', () => {
    mockUseAuth.mockReturnValue({ user: null, refreshUser: mockRefreshUser });
    renderProbe();

    expect(screen.getByTestId('balance')).toHaveTextContent('0');
    expect(screen.getByTestId('available')).toHaveTextContent('0');
  });
});

describe('authenticated', () => {
  it('derives balance from coin_balance and availableBalance from available_balance', () => {
    mockUseAuth.mockReturnValue({
      user: { coin_balance: 120, available_balance: 90 },
      refreshUser: mockRefreshUser,
    });
    renderProbe();

    expect(screen.getByTestId('balance')).toHaveTextContent('120');
    expect(screen.getByTestId('available')).toHaveTextContent('90');
  });

  it('falls back availableBalance to the raw balance when the backend omits it (no concurrent-session hold)', () => {
    mockUseAuth.mockReturnValue({ user: { coin_balance: 75 }, refreshUser: mockRefreshUser });
    renderProbe();

    expect(screen.getByTestId('available')).toHaveTextContent('75');
  });
});

describe('refreshBalance', () => {
  it('delegates to refreshUser and reflects the updated balance once it resolves', async () => {
    mockUseAuth.mockReturnValue({ user: { coin_balance: 10 }, refreshUser: mockRefreshUser });
    const { rerender } = renderProbe();
    expect(screen.getByTestId('balance')).toHaveTextContent('10');

    // The real AuthContext's refreshUser() re-pulls /api/auth/me and updates
    // its own state; simulate that by changing what the mock returns once
    // refreshUser is invoked, then re-rendering the tree.
    mockRefreshUser.mockImplementation(async () => {
      mockUseAuth.mockReturnValue({ user: { coin_balance: 42 }, refreshUser: mockRefreshUser });
    });

    await userEvent.click(screen.getByText('refresh'));

    expect(mockRefreshUser).toHaveBeenCalledTimes(1);
    rerender(
      <WalletProvider>
        <Probe />
      </WalletProvider>
    );
    await waitFor(() => expect(screen.getByTestId('balance')).toHaveTextContent('42'));
  });
});

describe('shape', () => {
  it('exposes exactly balance, availableBalance, and refreshBalance', () => {
    mockUseAuth.mockReturnValue({ user: { coin_balance: 5 }, refreshUser: mockRefreshUser });
    renderProbe();

    expect(screen.getByTestId('shape')).toHaveTextContent(
      'availableBalance,balance,refreshBalance'
    );
  });
});
