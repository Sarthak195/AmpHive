/**
 * AmpHive Session Context
 * =======================
 * Manages the active charging session state using real backend APIs
 * and Socket.io for live telemetry streaming.
 *
 * Replaces the Server-Sent Events (SSE) implementation with a robust
 * bi-directional WebSocket connection.
 *
 * Flow:
 * 1. user logs in → opens Socket.io connection using JWT auth
 * 2. startSession(plugId) → POST /api/sessions/start → receives session_id
 *    - Automatically emits 'subscribe_session' and listens for 'telemetry' events
 * 3. stopSession() → POST /api/sessions/stop (stops the *focused* session)
 *    - Automatically emits 'unsubscribe_session' and stops telemetry listener
 *
 * A user can hold several active sessions at once (backend cap, default 2):
 * `activeSessions` lists them all, and switchSession(s) refocuses the live
 * monitor (telemetry subscription follows the focused session).
 */

import { createContext, useState, useContext, useEffect, useCallback, useRef } from 'react';
import { io } from 'socket.io-client';
import api from '../api/client';
import { useAuth } from './AuthContext';
import { readSnapshot, writeSnapshot } from '../utils/snapshot';

const SessionContext = createContext();

const API_BASE = import.meta.env.VITE_API_URL || '';

// Telemetry statuses the backend reports once a session is no longer live
// (services/telemetry.py TelemetryStore.end_session sets "completed" when a
// session is finalized — by the driver's own Stop, a limit/wallet/hold
// exhaustion auto-stop, or the stale-session reaper). Only "completed"
// exists today; matched via a helper so a future terminal value is a
// one-line change.
const isTerminalStatus = (status) => status === 'completed';

export const SessionProvider = ({ children }) => {
  const { user, refreshUser } = useAuth();
  // A user can hold several active sessions (backend caps them, default 2).
  // `activeSessions` lists them all; `sessionId`/`sessionData`/`isActive`
  // track the *focused* one — the session the live monitor is subscribed to.
  const [activeSessions, setActiveSessions] = useState([]);
  const [sessionData, setSessionData] = useState(null);
  const [sessionId, setSessionId] = useState(null);
  const [isActive, setIsActive] = useState(false);
  const [socket, setSocket] = useState(null);
  const [error, setError] = useState(null);
  // Epoch ms of the last telemetry frame for the focused session — lets the UI
  // detect a dropped gateway/socket (no frames) even though the server-side
  // is_stale flag can only ride on a frame that actually arrives.
  const [lastFrameAt, setLastFrameAt] = useState(null);
  // ISO start time of the focused session, so the elapsed timer ticks smoothly
  // client-side instead of freezing between (or after losing) telemetry frames.
  const [focusedStartedAt, setFocusedStartedAt] = useState(null);
  // The focused session's stop conditions ({ max_kwh, max_duration_seconds },
  // both nullable) — the backend auto-stops at these, and the monitor shows
  // progress toward them ("0.42 / 1.00 kWh · stops automatically"). Comes
  // from the start response or from /api/sessions/active on restore/switch.
  const [focusedLimits, setFocusedLimits] = useState(null);
  // The focused session's own authorization hold (coins reserved for it at
  // start, resized on limit edits) — nullable for legacy pre-hold sessions.
  // This — not the driver's whole wallet balance — is the exhaustion
  // threshold the backend auto-stops against (services/mqtt/telemetry.py
  // _maybe_auto_stop_on_exhaustion), so the low-balance banner needs it to
  // warn/clear at the same point the backend actually stops charging.
  const [focusedHoldCoins, setFocusedHoldCoins] = useState(null);
  // Recent gateway alarms (safety cutoff / unauthorized-on / OTA), newest first.
  const [alarms, setAlarms] = useState([]);
  // The final billing summary from the most recent stop, shown as a receipt.
  const [receipt, setReceipt] = useState(null);
  // True until the first /api/sessions/active restore settles, so the page
  // shows a loading state instead of flashing "No active charge" for a
  // session that is in fact running.
  const [restoring, setRestoring] = useState(true);
  // Instant open: `activeSessions` is seeded from the last saved list
  // (utils/snapshot.js) while the restore runs. sessionsStale stays true
  // until a /api/sessions/active call succeeds (never cleared on failure),
  // so the UI marks those rows as not live. Only the list is seeded: never
  // the focused session, so nothing subscribes to a session that may have
  // ended. sessionsSavedAt: when the seeded list was saved (null = none).
  const [sessionsStale, setSessionsStale] = useState(false);
  const [sessionsSavedAt, setSessionsSavedAt] = useState(null);
  const [sessionsError, setSessionsError] = useState(false);
  // Effects below key on the user's id, not the user object: refreshUser()
  // (after login, stop, limit edits, top-ups) always builds a new object, and
  // keying on it tore down the socket and zeroed the monitor each time.
  const userId = user?.id ?? null;
  const userIdRef = useRef(userId);
  useEffect(() => {
    userIdRef.current = userId;
  }, [userId]);
  // Epoch ms since which the monitor has been waiting on the live feed: set
  // on every subscribe, including the re-subscribe after a socket reconnect.
  // SessionMonitor uses it to tell a gateway still waking from its 10 s idle
  // cadence (a small "Updating" hint) from a feed that has really dropped.
  const [watchSince, setWatchSince] = useState(null);

  // Mirrors for socket handlers, which must read current state without
  // re-subscribing on every change. sessionIdRef also keeps
  // refreshActiveSessions from depending on `sessionId` (which would
  // re-trigger the mount-restore effect below on every start or switch).
  const sessionIdRef = useRef(null);
  const isActiveRef = useRef(false);
  const activeSessionsRef = useRef([]);
  useEffect(() => {
    sessionIdRef.current = sessionId;
    isActiveRef.current = isActive;
    activeSessionsRef.current = activeSessions;
  }, [sessionId, isActive, activeSessions]);
  // The session whose stop the driver started from this tab: stopSession owns
  // its receipt, refocus and wallet refresh, so the ended path below skips it.
  const stoppingIdRef = useRef(null);

  // Manage the Socket.io lifecycle (connect on login, disconnect on logout).
  // The cleanup below disconnects the previous socket on every user change,
  // so the logout branch only has to clear the state handle.
  useEffect(() => {
    if (!userId) {
      setSocket(null);
      return;
    }

    // Read the token on every (re)connect: the socket now outlives a
    // same-user re-login, which can store a new token.
    const newSocket = io(API_BASE, {
      auth: (cb) => cb({ token: localStorage.getItem('amphive_token') }),
      transports: ['websocket', 'polling'],
    });

    newSocket.on('connect', () => {
      console.log('Socket.io connected (sid:', newSocket.id, ')');
    });

    newSocket.on('connect_error', (err) => {
      console.error('Socket.io connection error:', err);
    });

    newSocket.on('disconnect', (reason) => {
      console.log('Socket.io disconnected:', reason);
    });

    // Gateway alarms (safety cutoff, unauthorized-on, OTA notices) are
    // broadcast to all clients; keep the most recent handful so the UI can
    // warn the driver (e.g. someone physically switched their plug on/off).
    const handleAlarm = (event) => {
      setAlarms((prev) => [{ ...event, received_at: Date.now() }, ...prev].slice(0, 20));
    };
    newSocket.on('gateway_alarm', handleAlarm);

    setSocket(newSocket);

    return () => {
      newSocket.off('gateway_alarm', handleAlarm);
      newSocket.disconnect();
    };
  }, [userId]);

  const refreshActiveSessions = useCallback(async (focusSessionId = null) => {
    const res = await api.get('/api/sessions/active');
    const sessions = res.sessions || [];
    setActiveSessions(sessions);
    writeSnapshot('sessions', userIdRef.current, sessions);
    setSessionsStale(false);
    setSessionsError(false);
    // Backfill the *focused* session's enriched fields (hold_coins, limits)
    // from the canonical GET — a caller like startSession may not have them
    // in its own response, and without this the low-balance warning (which
    // reads focusedHoldCoins) stays dead until a full reload.
    // Prefer an explicitly-passed id: right after startSession's
    // setSessionId the sessionIdRef sync effect hasn't committed yet, and a
    // stale ref could match (and mis-backfill from) a DIFFERENT session that
    // is still in the list.
    const targetId = focusSessionId ?? sessionIdRef.current;
    const focused = sessions.find((s) => s.session_id === targetId);
    if (focused) {
      setFocusedHoldCoins(focused.hold_coins ?? null);
      setFocusedLimits(
        focused.max_kwh != null || focused.max_duration_seconds != null
          ? { max_kwh: focused.max_kwh ?? null, max_duration_seconds: focused.max_duration_seconds ?? null }
          : null
      );
    }
    return res;
  }, []);

  // Point the live monitor at one of the active sessions (resubscribes
  // telemetry via the effect above).
  const switchSession = useCallback((session) => {
    setSessionId(session.session_id);
    setIsActive(true);
    setReceipt(null);
    setFocusedStartedAt(session.started_at || new Date().toISOString());
    setFocusedLimits(
      session.max_kwh != null || session.max_duration_seconds != null
        ? {
            max_kwh: session.max_kwh ?? null,
            max_duration_seconds: session.max_duration_seconds ?? null,
          }
        : null
    );
    setFocusedHoldCoins(session.hold_coins ?? null);
    setLastFrameAt(null);
    // Seed from the REST row (/api/sessions/active carries energy_kwh and
    // cost_coins) so a restored session shows its real kWh and cost at once;
    // live telemetry frames overwrite this as they arrive. Power stays 0 until
    // the first frame, since the row has no instantaneous reading.
    setSessionData({
      plug_id: session.plug_id,
      plug_name: session.plug_name,
      status: 'charging',
      duration_sec: 0,
      power_w: 0.0,
      energy_kwh: Number(session.energy_kwh) || 0,
      current_a: 0.0,
      cost_coins: Number(session.cost_coins) || 0,
    });
  }, []);

  // The backend ended the focused session (limit, wallet or hold exhaustion
  // auto-stop, the stale-session reaper, a reservation clear-out) with no
  // stop call from this tab. Refocus a still-charging sibling, or show this
  // session's bill (GET /api/sessions/{id} has the stop response's receipt
  // shape), and refresh the wallet the session was just debited from.
  // `remaining` defaults to the local list minus the ended session.
  const endFocusedSession = useCallback((endedId, remaining) => {
    if (stoppingIdRef.current === endedId) return;
    const rest = remaining ?? activeSessionsRef.current.filter((s) => s.session_id !== endedId);
    setActiveSessions(rest);
    writeSnapshot('sessions', userIdRef.current, rest);
    refreshUser().catch(() => {});
    if (rest.length > 0) {
      switchSession(rest[0]);
      return;
    }
    // Leave the monitor only once the bill is in, so the page goes straight
    // to the receipt with no "No active charge" in between. Skipped if the
    // driver has started or focused another session since.
    const settle = (bill) => {
      if (sessionIdRef.current !== endedId) return;
      if (bill) setReceipt(bill);
      setIsActive(false);
    };
    api.get(`/api/sessions/${endedId}`).then(settle).catch(() => settle(null));
  }, [refreshUser, switchSession]);

  // Manage the telemetry subscription for the focused session.
  useEffect(() => {
    if (!socket || !sessionId || !isActive) return;

    const handleTelemetry = (data) => {
      // Frames carry plug_id but no session id: drop a late frame for a
      // session this view has just switched away from.
      const plugId = activeSessionsRef.current.find((s) => s.session_id === sessionId)?.plug_id;
      if (plugId != null && data?.plug_id != null && data.plug_id !== plugId) return;
      // Merge, not replace: a frame has no plug_name, so replacing dropped
      // the charger name the REST row seeded.
      setSessionData((prev) => ({ ...prev, ...data }));
      setLastFrameAt(Date.now());
      // Without this the UI never learns the backend ended the session: it
      // keeps a live "Stop charging" button that 400s when clicked.
      if (isTerminalStatus(data?.status)) endFocusedSession(sessionId);
    };

    // socket.io fires 'connect' again on every auto-reconnect, and the new
    // sid is in no rooms (the server joins only user_{id} on connect), so
    // re-subscribe each time or live telemetry stays dead after a blip.
    const subscribe = () => {
      setWatchSince(Date.now());
      socket.emit('subscribe_session', { session_id: sessionId });
    };
    socket.on('telemetry', handleTelemetry);
    socket.on('connect', subscribe);
    if (socket.connected) subscribe();
    else setWatchSince(Date.now());

    return () => {
      socket.off('telemetry', handleTelemetry);
      socket.off('connect', subscribe);
      socket.emit('unsubscribe_session', { session_id: sessionId });
    };
  }, [socket, sessionId, isActive, endFocusedSession]);

  // Optional `limits`: { max_kwh?, max_duration_seconds? } — a user-chosen
  // stop condition ("only charge 1 kWh"). Keys are only sent when set.
  // [Opt-in charging limits] A driver who picks no limit sends neither key —
  // the backend persists no limit at all (charge until stopped), not a
  // hidden default duration/energy.
  const startSession = useCallback(async (plugId, limits = null) => {
    setError(null);
    try {
      // Call backend to start session
      const payload = { plug_id: parseInt(plugId) };
      if (limits?.max_kwh != null) payload.max_kwh = limits.max_kwh;
      if (limits?.max_duration_seconds != null) payload.max_duration_seconds = limits.max_duration_seconds;
      if (limits?.auto_stop_when_full) payload.auto_stop_when_full = true;
      const result = await api.post('/api/sessions/start', payload);
      const startedAt = new Date().toISOString();
      // The backend echoes the EFFECTIVE limits (user-chosen, or null/null
      // when none was set); fall back to what we sent for older backends
      // that don't echo yet.
      const effectiveLimits = {
        max_kwh: result.max_kwh ?? limits?.max_kwh ?? null,
        max_duration_seconds: result.max_duration_seconds ?? limits?.max_duration_seconds ?? null,
      };
      setActiveSessions(prev => [
        {
          session_id: result.session_id,
          plug_id: result.plug_id,
          plug_name: result.plug_name,
          started_at: startedAt,
          ...effectiveLimits,
        },
        ...prev,
      ]);
      setSessionId(result.session_id);
      setIsActive(true);
      setReceipt(null);
      setFocusedStartedAt(startedAt);
      setFocusedLimits(
        effectiveLimits.max_kwh != null || effectiveLimits.max_duration_seconds != null
          ? effectiveLimits
          : null
      );
      // Echoed only by newer backends; older ones leave this null until the
      // refresh below (or a switchSession) fills it in.
      setFocusedHoldCoins(result.hold_coins ?? null);
      setLastFrameAt(null);
      // A fresh session is at zero; the plug name must be seeded because
      // frames (merged over this) carry none.
      setSessionData({
        plug_id: result.plug_id, plug_name: result.plug_name, status: 'starting',
        power_w: 0, energy_kwh: 0, cost_coins: 0,
      });
      // The start response may omit hold_coins/cost_coins entirely — pull
      // the enriched /api/sessions/active so the low-balance warning has a
      // real threshold for THIS session without waiting for a full reload.
      refreshActiveSessions(result.session_id).catch(() => {});
      return result;
    } catch (err) {
      setError(err.message);
      throw err;
    }
  }, [refreshActiveSessions]);

  const stopSession = useCallback(async () => {
    setError(null);
    try {
      if (sessionId) {
        stoppingIdRef.current = sessionId;
        const result = await api.post('/api/sessions/stop', { session_id: sessionId });
        const remaining = activeSessions.filter(s => s.session_id !== sessionId);
        setActiveSessions(remaining);
        writeSnapshot('sessions', userIdRef.current, remaining);
        if (remaining.length > 0) {
          // Another concurrent session (a user can hold up to
          // MAX_ACTIVE_SESSIONS_PER_USER) is still charging — refocus the
          // live monitor on it instead of stranding it behind this session's
          // now-inactive state.
          switchSession(remaining[0]);
        } else {
          // Keep last sessionData for receipt view, but mark as completed
          setSessionData(prev => prev ? { ...prev, status: 'completed' } : null);
          setIsActive(false);
          // Show the final billing summary as a receipt (only meaningful
          // when nothing else is still live to show instead).
          setReceipt(result);
        }
        // Refresh the wallet so the balance updates immediately after the debit.
        refreshUser().catch(() => {});
        return result;
      }
    } catch (err) {
      setError(err.message);
      stoppingIdRef.current = null;
      // The stop may have failed because the session was already finalized
      // server-side (reaper/other tab), or transiently (network blip) while
      // it's still very much ACTIVE. Blanket setIsActive(false) here would
      // strand a still-charging sibling (or even this same session, if the
      // stop didn't actually take) behind a dead "no active session" view —
      // re-sync from the backend and only clear focus once nothing remains.
      try {
        const res = await refreshActiveSessions();
        const remaining = res.sessions || [];
        const stillFocused = remaining.some((s) => s.session_id === sessionId);
        if (stillFocused) {
          // The stop didn't actually take — it's still active server-side.
          // Leave it focused rather than disturbing its live telemetry.
          setIsActive(true);
        } else if (remaining.length > 0) {
          // This session is gone, but a concurrent sibling is still
          // charging — refocus onto it instead of stranding it.
          switchSession(remaining[0]);
        } else {
          setIsActive(false);
        }
      } catch {
        // Couldn't even re-sync — fall back to the old behavior rather than
        // leaving a possibly-stale isActive=true with nothing to verify it.
        setIsActive(false);
      }
      throw err;
    }
  }, [sessionId, activeSessions, switchSession, refreshActiveSessions, refreshUser]);

  // Update a RUNNING session's stop conditions ("start now, set the target
  // later"). PATCHes the backend, then reflects the returned limits into the
  // focused-limits display and the activeSessions list. The change is enforced
  // backend-side within ~1 s (routers/sessions.py update_session_limits); only
  // the keys provided are sent. Defaults to the focused session when no id.
  const updateLimits = useCallback(async (targetId, limits) => {
    const id = targetId ?? sessionId;
    if (!id) return;
    const payload = {};
    if (limits?.max_kwh != null) payload.max_kwh = limits.max_kwh;
    if (limits?.max_duration_seconds != null) payload.max_duration_seconds = limits.max_duration_seconds;
    const result = await api.patch(`/api/sessions/${id}/limits`, payload);
    const next = {
      max_kwh: result?.max_kwh ?? null,
      max_duration_seconds: result?.max_duration_seconds ?? null,
    };
    // The PATCH may resize this session's hold; keep the focused hold in step
    // with the new limits, or the low-balance check compares a raised limit
    // against the old, smaller hold and warns too early.
    if (result && 'hold_coins' in result) next.hold_coins = result.hold_coins;
    if (id === sessionId) {
      setFocusedLimits(
        next.max_kwh != null || next.max_duration_seconds != null
          ? { max_kwh: next.max_kwh, max_duration_seconds: next.max_duration_seconds }
          : null
      );
      if ('hold_coins' in next) setFocusedHoldCoins(next.hold_coins);
    }
    setActiveSessions((prev) =>
      prev.map((s) => (s.session_id === id ? { ...s, ...next } : s))
    );
    // A max_kwh/max_duration_seconds change can grow or shrink this
    // session's auth hold (routers/sessions.py update_session_limits) —
    // refresh the wallet so available_balance reflects it immediately,
    // matching stopSession's post-debit refresh.
    refreshUser().catch(() => {});
    return result;
  }, [sessionId, refreshUser]);

  const dismissReceipt = useCallback(() => setReceipt(null), []);

  const clearSession = useCallback(() => {
    setSessionData(null);
    setSessionId(null);
    setIsActive(false);
    setFocusedStartedAt(null);
    setFocusedLimits(null);
    setFocusedHoldCoins(null);
    setLastFrameAt(null);
    setReceipt(null);
    setError(null);
  }, []);

  // Check for active sessions on mount or auth change; focus the newest.
  // Also the Retry for a failed restore.
  const restoreSessions = useCallback(async () => {
    if (!userId) {
      setActiveSessions([]);
      setSessionData(null);
      setSessionId(null);
      setIsActive(false);
      setSessionsStale(false);
      setSessionsSavedAt(null);
      setRestoring(false);
      return;
    }

    const snap = readSnapshot('sessions', userId);
    if (Array.isArray(snap?.data)) setActiveSessions(snap.data);
    setSessionsSavedAt(Array.isArray(snap?.data) ? snap.savedAt : null);
    setSessionsStale(true);
    setSessionsError(false);
    setRestoring(true);
    try {
      const res = await refreshActiveSessions();
      if (res.active) {
        switchSession(res.sessions[0]);
      }
    } catch (err) {
      console.error('Failed to restore active session:', err);
      setSessionsError(true);
    } finally {
      setRestoring(false);
    }
  }, [userId, refreshActiveSessions, switchSession]);

  useEffect(() => {
    restoreSessions();
  }, [restoreSessions]);

  // Re-sync with the backend when something may have changed that this tab
  // was not told about: a queued charge auto-started, expired or failed, a
  // backend stop, or a socket reconnect (pushes sent while it was down are
  // lost).
  const resync = useCallback(async () => {
    // Captured before the await: a session started while this GET is in
    // flight is missing from its list but is not ended.
    const focusedId = sessionIdRef.current;
    try {
      const res = await refreshActiveSessions();
      const sessions = res.sessions || [];
      if (sessionIdRef.current !== focusedId) return;
      if (isActiveRef.current && !sessions.some((s) => s.session_id === focusedId)) {
        endFocusedSession(focusedId, sessions);
        return;
      }
      refreshUser().catch(() => {});
      if (!isActiveRef.current && sessions.length > 0) switchSession(sessions[0]);
    } catch {
      // The next push, reconnect or reload re-syncs.
    }
  }, [refreshActiveSessions, refreshUser, switchSession, endFocusedSession]);

  useEffect(() => {
    if (!socket) return undefined;
    const handleNotification = (n) => {
      const type = n?.type || '';
      if (type.startsWith('queued_charge_') || type === 'session_stopped') resync();
    };
    // The first connect is covered by the mount restore above.
    let connectedBefore = !!socket.connected;
    const handleConnect = () => {
      if (connectedBefore) resync();
      connectedBefore = true;
    };
    socket.on('notification', handleNotification);
    socket.on('connect', handleConnect);
    return () => {
      socket.off('notification', handleNotification);
      socket.off('connect', handleConnect);
    };
  }, [socket, resync]);

  return (
    <SessionContext.Provider value={{
      socket,
      activeSessions, sessionData, sessionId, isActive, error, restoring,
      sessionsStale, sessionsSavedAt, sessionsError, retrySessions: restoreSessions,
      lastFrameAt, watchSince, focusedStartedAt, focusedLimits, focusedHoldCoins, alarms, receipt,
      startSession, stopSession, updateLimits, clearSession, switchSession, dismissReceipt,
    }}>
      {children}
    </SessionContext.Provider>
  );
};

export const useSession = () => useContext(SessionContext);
