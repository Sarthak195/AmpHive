/**
 * AmpHive Auth Context
 * ====================
 * Manages user authentication state using JWT tokens.
 * Replaces the Phase 1 mock login with real backend API calls.
 *
 * Flow:
 * - On app load: checks localStorage for a saved JWT, calls GET /api/auth/me
 *   to verify it's still valid and load fresh user data (a cached user lets
 *   the app render first and /me revalidates in the background). /me also returns a
 *   renewed JWT, which replaces the stored one (sliding 7-day expiry).
 *   Only a 401/403 at boot signs out; a timeout or 5xx keeps the session.
 * - Login: POST /api/auth/login → saves JWT + user to localStorage.
 * - Register: POST /api/auth/register → same as login.
 * - Logout: revokes this device's session server-side, clears localStorage,
 *   resets state. logoutEverywhere signs out every device (Account and CPO
 *   Settings offer it behind a confirm step).
 */

import { createContext, useState, useEffect, useContext, useCallback } from 'react';
import api from '../api/client';
import { cachedUserId, clearStoredSession } from '../utils/snapshot';

const AuthContext = createContext();

// /api/auth/me returns the profile plus a freshly minted JWT (sliding
// renewal). Persist both, but only while the token we sent is still the
// stored one, so a /me in flight cannot resurrect a session after logout or
// overwrite another account's newer login. Returns the user without the token.
const storeMe = (sentToken, { token, ...userData }) => {
  if (localStorage.getItem('amphive_token') === sentToken) {
    if (token) localStorage.setItem('amphive_token', token);
    localStorage.setItem('amphive_user', JSON.stringify(userData));
  }
  return userData;
};

export const AuthProvider = ({ children }) => {
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);
  // False while the user (and so the coin balance) is the cached copy, true
  // once /me has answered in this page load. Money shown while false must be
  // marked as not live. userError: the last /me attempt failed (not a 401),
  // so the UI can offer a retry instead of "Updating..." forever.
  const [userFresh, setUserFresh] = useState(false);
  const [userError, setUserError] = useState(false);

  // On mount, restore the session. With a token AND a cached user object the
  // app renders at once from the cache (the route chunk and its data calls
  // start downloading in parallel with /me instead of after it) and /me
  // revalidates in the background. With a token but no usable cached user,
  // loading stays true until /me settles.
  useEffect(() => {
    const restoreSession = async () => {
      const token = localStorage.getItem('amphive_token');
      if (!token) {
        setLoading(false);
        return;
      }

      try {
        const cached = JSON.parse(localStorage.getItem('amphive_user'));
        if (cached && typeof cached === 'object' && !Array.isArray(cached)) {
          setUser(cached);
          setLoading(false);
        }
      } catch {
        // Unreadable cache: wait for /me below.
      }

      try {
        // Verify the token is still valid by calling the /me endpoint
        const me = await api.get('/api/auth/me');
        const userData = storeMe(token, me);
        // Apply only if the user did not log out or switch accounts while /me
        // was in flight: the stored token must still be the one sent or the
        // renewed one storeMe just wrote.
        const current = localStorage.getItem('amphive_token');
        if (current === token || (me.token && current === me.token)) {
          setUser(userData);
          setUserFresh(true);
          setUserError(false);
        }
      } catch (err) {
        console.warn('Session restore failed:', err.message);
        if (err.status === 401 || err.status === 403) {
          // Rejected token (expired, revoked) or disabled account: sign out.
          // The api client already clears storage on a 401, so a null stored
          // token also counts as ours; a different token is a newer login.
          const current = localStorage.getItem('amphive_token');
          if (current === token || current === null) {
            clearStoredSession();
            setUser(null);
          }
        } else {
          setUserError(true);
        }
        // Timeout, network drop or 5xx says nothing about the token: keep it
        // and the cached user; the next real 401 still signs out.
      } finally {
        setLoading(false);
      }
    };

    restoreSession();
  }, []);

  // Update local user state (e.g. after a wallet top-up changes the balance)
  const refreshUser = useCallback(async () => {
    try {
      const token = localStorage.getItem('amphive_token');
      setUser(storeMe(token, await api.get('/api/auth/me')));
      setUserFresh(true);
      setUserError(false);
    } catch (err) {
      console.error('Failed to refresh user:', err.message);
      setUserError(true);
    }
  }, []);

  const login = useCallback(async (email, password) => {
    const data = await api.post('/api/auth/login', { email, password });
    // A different account on this device: drop the previous one's snapshots.
    if (String(cachedUserId()) !== String(data.user?.id)) clearStoredSession();
    // Save JWT token and user data to localStorage
    localStorage.setItem('amphive_token', data.token);
    localStorage.setItem('amphive_user', JSON.stringify(data.user));
    // Optimistic set for responsiveness, then refresh from /me — the
    // AuthResponse's user shape lacks fields like available_balance,
    // created_at, is_disabled that the rest of the app expects.
    setUser(data.user);
    // The login response itself is live server data.
    setUserFresh(true);
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

    if (incoming && String(cachedUserId()) !== String(Number(incoming.sub))) clearStoredSession();
    localStorage.setItem('amphive_token', token);
    if (incoming) {
      const optimisticUser = { id: Number(incoming.sub), email: incoming.email, role: incoming.role };
      localStorage.setItem('amphive_user', JSON.stringify(optimisticUser));
      setUser(optimisticUser);
    }
    await refreshUser();
  }, [refreshUser]);

  // Register no longer logs the driver in. The backend now returns
  // 200 { status: 'verification_sent', email } and sends a verification
  // email instead of a token — the account is inert until the emailed
  // /verify-email?token=... link is opened (that page's verify-email call is
  // what mints the JWT, via loginWithToken). So this just POSTs and hands the
  // response back to the caller (Signup) to render its "check your email"
  // state; NO token/user is persisted here.
  const register = useCallback(async (email, password, fullName) => {
    return api.post('/api/auth/register', {
      email,
      password,
      full_name: fullName,
    });
  }, []);

  const clearLocalSession = useCallback(() => {
    setUser(null);
    setUserFresh(false);
    clearStoredSession();
  }, []);

  const logout = useCallback(async () => {
    // Signs out THIS device: the server revokes this token's sign-in session
    // (its sid), and the user's other devices stay signed in. Best-effort:
    // clear local state regardless of the result, so the user is always
    // logged out locally even if the request fails.
    try {
      await api.post('/api/auth/logout');
    } catch (err) {
      console.warn('Logout revoke failed (clearing local session anyway):', err.message);
    }
    clearLocalSession();
  }, [clearLocalSession]);

  const logoutEverywhere = useCallback(async () => {
    // Signs out EVERY device (server bumps token_version). NOT best-effort:
    // if the request fails the other devices are still signed in, so the
    // error is rethrown for the caller to show and this session is kept. (A
    // 401 here means this token was already dead; the api client has then
    // already cleared it and sent the user to sign in, which is correct.)
    await api.post('/api/auth/logout-everywhere');
    clearLocalSession();
  }, [clearLocalSession]);

  // Children always render — consumers branch on `loading` themselves
  // (App shows <BootSplash/> while `loading`, which ends at once when a cached
  // user exists and after /me otherwise).
  return (
    <AuthContext.Provider
      value={{
        user, login, register, loginWithToken, logout, logoutEverywhere, refreshUser, loading,
        userFresh, userError,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
};

export const useAuth = () => useContext(AuthContext);
