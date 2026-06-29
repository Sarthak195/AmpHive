/**
 * AmpHive Session Context
 * =======================
 * Manages the active charging session state using real backend APIs
 * and Server-Sent Events (SSE) for live telemetry streaming.
 *
 * Replaces the Phase 1 MockEventSource with a real EventSource
 * connected to GET /api/sessions/live/{session_id}.
 *
 * Flow:
 * 1. startSession(plugId) → POST /api/sessions/start → receives session_id
 * 2. Opens EventSource to /api/sessions/live/{session_id} for SSE telemetry
 * 3. stopSession() → POST /api/sessions/stop → closes SSE
 */

import React, { createContext, useState, useContext, useEffect, useCallback } from 'react';
import api from '../api/client';

const SessionContext = createContext();

const API_BASE = import.meta.env.VITE_API_URL || '';

export const SessionProvider = ({ children }) => {
  const [sessionData, setSessionData] = useState(null);
  const [sessionId, setSessionId] = useState(null);
  const [isActive, setIsActive] = useState(false);
  const [eventSource, setEventSource] = useState(null);
  const [error, setError] = useState(null);

  const startSession = useCallback(async (plugId) => {
    setError(null);
    try {
      // Call the backend to start the session and get the session_id
      const result = await api.post('/api/sessions/start', { plug_id: parseInt(plugId) });
      const newSessionId = result.session_id;
      setSessionId(newSessionId);
      setIsActive(true);

      // Open SSE connection for real-time telemetry
      const token = localStorage.getItem('amphive_token');
      const sseUrl = `${API_BASE}/api/sessions/live/${newSessionId}?token=${token}`;

      // EventSource doesn't natively support Authorization headers,
      // so we pass the token as a query parameter for SSE.
      // The backend should also accept ?token= for SSE endpoints.
      const sse = new EventSource(sseUrl);

      sse.addEventListener('telemetry', (event) => {
        try {
          const data = JSON.parse(event.data);
          setSessionData(data);
        } catch (e) {
          console.error('Failed to parse telemetry event:', e);
        }
      });

      sse.onerror = (err) => {
        console.warn('SSE connection error:', err);
        // SSE will auto-reconnect, but if it's a fatal error, clean up
        if (sse.readyState === EventSource.CLOSED) {
          setIsActive(false);
        }
      };

      setEventSource(sse);

      return result;
    } catch (err) {
      setError(err.message);
      throw err;
    }
  }, []);

  const stopSession = useCallback(async () => {
    setError(null);

    // Close the SSE connection
    if (eventSource) {
      eventSource.close();
      setEventSource(null);
    }

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
      // Still mark as inactive even if the API call fails
      setIsActive(false);
      throw err;
    }
  }, [eventSource, sessionId]);

  const clearSession = useCallback(() => {
    setSessionData(null);
    setSessionId(null);
    setIsActive(false);
    setError(null);
  }, []);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      if (eventSource) {
        eventSource.close();
      }
    };
  }, [eventSource]);

  return (
    <SessionContext.Provider value={{
      sessionData, sessionId, isActive, error,
      startSession, stopSession, clearSession,
    }}>
      {children}
    </SessionContext.Provider>
  );
};

export const useSession = () => useContext(SessionContext);
