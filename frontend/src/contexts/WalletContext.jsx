import React, { createContext, useState, useEffect, useContext } from 'react';

const WalletContext = createContext();

export const WalletProvider = ({ children }) => {
  const [balance, setBalance] = useState(0);

  useEffect(() => {
    // Mock fetch balance from localStorage
    const storedBalance = localStorage.getItem('amphive_wallet_balance');
    if (storedBalance) {
      setBalance(parseInt(storedBalance, 10));
    } else {
      // Default starting balance for testing
      setBalance(150);
      localStorage.setItem('amphive_wallet_balance', '150');
    }
  }, []);

  const topUp = (amount) => {
    const newBalance = balance + amount;
    setBalance(newBalance);
    localStorage.setItem('amphive_wallet_balance', newBalance.toString());
  };

  const deduct = (amount) => {
    const newBalance = Math.max(0, balance - amount);
    setBalance(newBalance);
    localStorage.setItem('amphive_wallet_balance', newBalance.toString());
  };

  return (
    <WalletContext.Provider value={{ balance, topUp, deduct }}>
      {children}
    </WalletContext.Provider>
  );
};

export const useWallet = () => useContext(WalletContext);
