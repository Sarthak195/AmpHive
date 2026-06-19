/**
 * AmpHive Auth Context
 * ====================
 * Manages user authentication state using JWT tokens.
 * Replaces the Phase 1 mock login with real backend API calls.
 *
 * Flow:
 * - On app load: checks localStorage for a saved JWT, calls GET /api/auth/me
 *   to verify it's still valid and load fresh user data.
 * - Login: POST /api/auth/login → saves JWT + user to localStorage.
 * - Register: POST /api/auth/register → same as login.
 * - Logout: clears localStorage, resets state.
 */

import React, { createContext, useState, useEffect, useContext, useCallback } from 'react';
import api from '../api/client';

const AuthContext = createContext();

export const AuthProvider = ({ children }) => {
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);

  // On mount, check if a valid JWT token exists and restore the session
  useEffect(() => {
    const restoreSession = async () => {
      const token = localStorage.getItem('amphive_token');
      if (!token) {
        setLoading(false);
        return;
      }

      try {
        // Verify the token is still valid by calling the /me endpoint
        const userData = await api.get('/api/auth/me');
        setUser(userData);
      } catch (err) {
        // Token is invalid or expired — clear it
        console.warn('Session restore failed:', err.message);
        localStorage.removeItem('amphive_token');
        localStorage.removeItem('amphive_user');
      } finally {
        setLoading(false);
      }
    };

    restoreSession();
  }, []);

  const login = useCallback(async (email, password) => {
    const data = await api.post('/api/auth/login', { email, password });
    // Save JWT token and user data to localStorage
    localStorage.setItem('amphive_token', data.token);
    localStorage.setItem('amphive_user', JSON.stringify(data.user));
    setUser(data.user);
    return data;
  }, []);

  const register = useCallback(async (email, password, fullName) => {
    const data = await api.post('/api/auth/register', {
      email,
      password,
      full_name: fullName,
    });
    // Save JWT token and user data to localStorage
    localStorage.setItem('amphive_token', data.token);
    localStorage.setItem('amphive_user', JSON.stringify(data.user));
    setUser(data.user);
    return data;
  }, []);

  const logout = useCallback(() => {
    setUser(null);
    localStorage.removeItem('amphive_token');
    localStorage.removeItem('amphive_user');
  }, []);

  // Update local user state (e.g. after a wallet top-up changes the balance)
  const refreshUser = useCallback(async () => {
    try {
      const userData = await api.get('/api/auth/me');
      setUser(userData);
      localStorage.setItem('amphive_user', JSON.stringify(userData));
    } catch (err) {
      console.error('Failed to refresh user:', err.message);
    }
  }, []);

  return (
    <AuthContext.Provider value={{ user, login, register, logout, refreshUser, loading }}>
      {!loading && children}
    </AuthContext.Provider>
  );
};

export const useAuth = () => useContext(AuthContext);
