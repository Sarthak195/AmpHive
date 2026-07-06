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
 * 3. stopSession() → POST /api/sessions/stop
 *    - Automatically emits 'unsubscribe_session' and stops telemetry listener
 */

import React, { createContext, useState, useContext, useEffect, useCallback } from 'react';
import { io } from 'socket.io-client';
import api from '../api/client';
import { useAuth } from './AuthContext';

const SessionContext = createContext();

const API_BASE = import.meta.env.VITE_API_URL || '';

export const SessionProvider = ({ children }) => {
  const { user } = useAuth();
  const [sessionData, setSessionData] = useState(null);
  const [sessionId, setSessionId] = useState(null);
  const [isActive, setIsActive] = useState(false);
  const [socket, setSocket] = useState(null);
  const [error, setError] = useState(null);

  // Manage the Socket.io lifecycle (connect on login, disconnect on logout)
  useEffect(() => {
    if (!user) {
      if (socket) {
        socket.disconnect();
        setSocket(null);
      }
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

    setSocket(newSocket);

    return () => {
      newSocket.disconnect();
    };
  }, [user]);

  // Manage telemetry subscription reactively
  useEffect(() => {
    if (!socket || !sessionId || !isActive) return;

    const handleTelemetry = (data) => {
      setSessionData(data);
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

  const startSession = useCallback(async (plugId) => {
    setError(null);
    try {
      // Call backend to start session
      const result = await api.post('/api/sessions/start', { plug_id: parseInt(plugId) });
      const newSessionId = result.session_id;
      setSessionId(newSessionId);
      setIsActive(true);
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
        // Keep last sessionData for receipt view, but mark as completed
        setSessionData(prev => prev ? { ...prev, status: 'completed' } : null);
        setIsActive(false);
        return result;
      }
    } catch (err) {
      setError(err.message);
      setIsActive(false);
      throw err;
    }
  }, [sessionId]);

  const clearSession = useCallback(() => {
    setSessionData(null);
    setSessionId(null);
    setIsActive(false);
    setError(null);
  }, []);

  // Check for active session on mount or auth change
  useEffect(() => {
    const checkActiveSession = async () => {
      if (!user) {
        setSessionData(null);
        setSessionId(null);
        setIsActive(false);
        return;
      }

      try {
        const res = await api.get('/api/sessions/active');
        if (res.active) {
          setSessionId(res.session_id);
          setIsActive(true);
          setSessionData({
            plug_id: res.plug_id,
            plug_name: res.plug_name,
            status: 'charging',
            duration_sec: 0,
            power_w: 0.0,
            energy_kwh: 0.0,
            current_a: 0.0,
            cost_coins: 0.0
          });
        }
      } catch (err) {
        console.error('Failed to restore active session:', err);
      }
    };

    checkActiveSession();
  }, [user]);

  return (
    <SessionContext.Provider value={{
      socket,
      sessionData, sessionId, isActive, error,
      startSession, stopSession, clearSession,
    }}>
      {children}
    </SessionContext.Provider>
  );
};

export const useSession = () => useContext(SessionContext);
