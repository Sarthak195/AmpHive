/**
 * AmpHive Wallet Context
 * ======================
 * Manages the user's coin balance, sourced from the backend.
 * Replaces the Phase 1 localStorage mock with real API-driven state.
 *
 * Balance is derived from the user object in AuthContext.
 * Top-up triggers a Razorpay checkout flow and refreshes the balance
 * after successful payment verification.
 */

import { createContext, useContext, useCallback } from 'react';
import { useAuth } from './AuthContext';

const WalletContext = createContext();

export const WalletProvider = ({ children }) => {
  const { user, refreshUser } = useAuth();

  // Balance comes from the user object (updated from backend)
  const balance = user?.coin_balance ?? 0;
  // Available balance accounts for holds from other concurrent sessions
  // (a second active session's hold reduces what's free to spend/estimate
  // against). Falls back to the raw balance for backends that don't send it.
  const availableBalance = user?.available_balance ?? balance;

  /**
   * Refresh the wallet balance from the backend.
   * Called after a successful payment verification to show the updated balance.
   */
  const refreshBalance = useCallback(async () => {
    await refreshUser();
  }, [refreshUser]);

  return (
    <WalletContext.Provider value={{ balance, availableBalance, refreshBalance }}>
      {children}
    </WalletContext.Provider>
  );
};

export const useWallet = () => useContext(WalletContext);
