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

import React, { createContext, useContext, useCallback } from 'react';
import { useAuth } from './AuthContext';

const WalletContext = createContext();

export const WalletProvider = ({ children }) => {
  const { user, refreshUser } = useAuth();

  // Balance comes from the user object (updated from backend)
  const balance = user?.coin_balance ?? 0;

  /**
   * Refresh the wallet balance from the backend.
   * Called after a successful payment verification to show the updated balance.
   */
  const refreshBalance = useCallback(async () => {
    await refreshUser();
  }, [refreshUser]);

  return (
    <WalletContext.Provider value={{ balance, refreshBalance }}>
      {children}
    </WalletContext.Provider>
  );
};

export const useWallet = () => useContext(WalletContext);
