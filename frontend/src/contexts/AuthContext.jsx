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

import { createContext, useState, useEffect, useContext, useCallback } from 'react';
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

  const login = useCallback(async (email, password) => {
    const data = await api.post('/api/auth/login', { email, password });
    // Save JWT token and user data to localStorage
    localStorage.setItem('amphive_token', data.token);
    localStorage.setItem('amphive_user', JSON.stringify(data.user));
    // Optimistic set for responsiveness, then refresh from /me — the
    // AuthResponse's user shape lacks fields like available_balance,
    // created_at, is_disabled that the rest of the app expects.
    setUser(data.user);
    await refreshUser();
    return data;
  }, [refreshUser]);

  // "Sign in with Google" (GoogleCallback page): the SPA has already traded
  // the single-use callback code for a normal app JWT (same shape/claims as
  // login()'s) via POST /api/auth/google/exchange — this is just the same
  // persist-then-restore tail login() does. The JWT payload (base64url, NOT
  // signature-verified here — verification already happened server-side when
  // this token was minted; get_current_user/refreshUser below are the actual
  // trust boundary) gives an optimistic {id, email, role} to render
  // immediately, same spirit as login()'s optimistic data.user.
  const loginWithToken = useCallback(async (token) => {
    // Parse the incoming (already server-verified) token for its subject +
    // optimistic fields. Malformed → null: we skip the optimistic render but
    // still persist + refreshUser (server is the real gate), matching the
    // prior behavior.
    let incoming = null;
    try {
      incoming = JSON.parse(atob(token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/')));
    } catch (err) {
      console.warn('Could not parse Google login token for optimistic user:', err.message);
    }

    // [M5 guard — defense in depth] Refuse to SILENTLY replace a still-valid
    // session that belongs to a DIFFERENT user. The backend nonce-binding is
    // what actually stops the planted-link login-CSRF; this guard additionally
    // ensures an unexpected callback can't swap the signed-in account out from
    // under someone without an explicit sign-out. A fresh login (no session)
    // or a re-auth of the SAME user proceeds smoothly.
    const existing = localStorage.getItem('amphive_token');
    if (existing && existing !== token && incoming) {
      let current = null;
      try {
        current = JSON.parse(atob(existing.split('.')[1].replace(/-/g, '+').replace(/_/g, '/')));
      } catch {
        // Unparseable existing token — leave `current` null so the guard below
        // treats it as no comparable session and the login proceeds.
      }
      const stillValid = current && (!current.exp || current.exp * 1000 > Date.now());
      const differentUser = current && String(current.sub) !== String(incoming.sub);
      if (stillValid && differentUser) {
        const err = new Error(
          'You are already signed in with a different account. Sign out first to switch accounts.'
        );
        err.code = 'already_signed_in';
        throw err; // caller (GoogleCallback) shows a return-to-app path; session untouched
      }
    }

    localStorage.setItem('amphive_token', token);
    if (incoming) {
      const optimisticUser = { id: Number(incoming.sub), email: incoming.email, role: incoming.role };
      localStorage.setItem('amphive_user', JSON.stringify(optimisticUser));
      setUser(optimisticUser);
    }
    await refreshUser();
  }, [refreshUser]);

  const register = useCallback(async (email, password, fullName) => {
    const data = await api.post('/api/auth/register', {
      email,
      password,
      full_name: fullName,
    });
    // Save JWT token and user data to localStorage
    localStorage.setItem('amphive_token', data.token);
    localStorage.setItem('amphive_user', JSON.stringify(data.user));
    // Optimistic set for responsiveness, then refresh from /me — same as
    // login (see comment there).
    setUser(data.user);
    await refreshUser();
    return data;
  }, [refreshUser]);

  const logout = useCallback(async () => {
    // Revoke the token server-side (bumps token_version → all this user's
    // tokens die). Best-effort: clear local state regardless of the result,
    // so the user is always logged out locally even if the request fails.
    try {
      await api.post('/api/auth/logout');
    } catch (err) {
      console.warn('Logout revoke failed (clearing local session anyway):', err.message);
    }
    setUser(null);
    localStorage.removeItem('amphive_token');
    localStorage.removeItem('amphive_user');
  }, []);

  // Children always render — consumers branch on `loading` themselves
  // (App holds the route tree behind <BootSplash/> until restore settles).
  return (
    <AuthContext.Provider value={{ user, login, register, loginWithToken, logout, refreshUser, loading }}>
      {children}
    </AuthContext.Provider>
  );
};

export const useAuth = () => useContext(AuthContext);
