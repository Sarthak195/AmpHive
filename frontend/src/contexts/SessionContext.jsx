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

import { createContext, useState, useContext, useEffect, useCallback } from 'react';
import { io } from 'socket.io-client';
import api from '../api/client';
import { useAuth } from './AuthContext';

const SessionContext = createContext();

const API_BASE = import.meta.env.VITE_API_URL || '';

export const SessionProvider = ({ children }) => {
  const { user } = useAuth();
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
  // Recent gateway alarms (safety cutoff / unauthorized-on / OTA), newest first.
  const [alarms, setAlarms] = useState([]);

  // Manage the Socket.io lifecycle (connect on login, disconnect on logout).
  // The cleanup below disconnects the previous socket on every user change,
  // so the logout branch only has to clear the state handle.
  useEffect(() => {
    if (!user) {
      setSocket(null);
      return;
    }

    const token = localStorage.getItem('amphive_token');
    const newSocket = io(API_BASE, {
      auth: { token },
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
  }, [user]);

  // Manage telemetry subscription reactively
  useEffect(() => {
    if (!socket || !sessionId || !isActive) return;

    const handleTelemetry = (data) => {
      setSessionData(data);
      setLastFrameAt(Date.now());
    };

    socket.on('telemetry', handleTelemetry);
    socket.emit('subscribe_session', { session_id: sessionId });
    console.log(`Subscribed to Socket.io telemetry for session ${sessionId}`);

    return () => {
      socket.off('telemetry', handleTelemetry);
      socket.emit('unsubscribe_session', { session_id: sessionId });
      console.log(`Unsubscribed from Socket.io telemetry for session ${sessionId}`);
    };
  }, [socket, sessionId, isActive]);

  const refreshActiveSessions = useCallback(async () => {
    const res = await api.get('/api/sessions/active');
    setActiveSessions(res.sessions || []);
    return res;
  }, []);

  // Point the live monitor at one of the active sessions (resubscribes
  // telemetry via the effect above).
  const switchSession = useCallback((session) => {
    setSessionId(session.session_id);
    setIsActive(true);
    setFocusedStartedAt(session.started_at || new Date().toISOString());
    setLastFrameAt(null);
    setSessionData({
      plug_id: session.plug_id,
      plug_name: session.plug_name,
      status: 'charging',
      duration_sec: 0,
      power_w: 0.0,
      energy_kwh: 0.0,
      current_a: 0.0,
      cost_coins: 0.0
    });
  }, []);

  const startSession = useCallback(async (plugId) => {
    setError(null);
    try {
      // Call backend to start session
      const result = await api.post('/api/sessions/start', { plug_id: parseInt(plugId) });
      const startedAt = new Date().toISOString();
      setActiveSessions(prev => [
        {
          session_id: result.session_id,
          plug_id: result.plug_id,
          plug_name: result.plug_name,
          started_at: startedAt,
        },
        ...prev,
      ]);
      setSessionId(result.session_id);
      setIsActive(true);
      setFocusedStartedAt(startedAt);
      setLastFrameAt(null);
      setSessionData(null);
      return result;
    } catch (err) {
      setError(err.message);
      throw err;
    }
  }, []);

  const stopSession = useCallback(async () => {
    setError(null);
    try {
      if (sessionId) {
        const result = await api.post('/api/sessions/stop', { session_id: sessionId });
        setActiveSessions(prev => prev.filter(s => s.session_id !== sessionId));
        // Keep last sessionData for receipt view, but mark as completed
        setSessionData(prev => prev ? { ...prev, status: 'completed' } : null);
        setIsActive(false);
        return result;
      }
    } catch (err) {
      setError(err.message);
      setIsActive(false);
      // The stop may have failed because the session was already finalized
      // server-side (reaper/other tab) — re-sync the list from the backend.
      refreshActiveSessions().catch(() => {});
      throw err;
    }
  }, [sessionId, refreshActiveSessions]);

  const clearSession = useCallback(() => {
    setSessionData(null);
    setSessionId(null);
    setIsActive(false);
    setFocusedStartedAt(null);
    setLastFrameAt(null);
    setError(null);
  }, []);

  // Check for active sessions on mount or auth change; focus the newest.
  useEffect(() => {
    const checkActiveSession = async () => {
      if (!user) {
        setActiveSessions([]);
        setSessionData(null);
        setSessionId(null);
        setIsActive(false);
        return;
      }

      try {
        const res = await refreshActiveSessions();
        if (res.active) {
          switchSession(res.sessions[0]);
        }
      } catch (err) {
        console.error('Failed to restore active session:', err);
      }
    };

    checkActiveSession();
  }, [user, refreshActiveSessions, switchSession]);

  return (
    <SessionContext.Provider value={{
      socket,
      activeSessions, sessionData, sessionId, isActive, error,
      lastFrameAt, focusedStartedAt, alarms,
      startSession, stopSession, clearSession, switchSession,
    }}>
      {children}
    </SessionContext.Provider>
  );
};

export const useSession = () => useContext(SessionContext);
