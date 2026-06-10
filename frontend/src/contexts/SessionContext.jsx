import React, { createContext, useState, useContext, useEffect, useCallback } from 'react';
import { MockEventSource } from '../api/mockSse';
import { useWallet } from './WalletContext';

const SessionContext = createContext();

export const SessionProvider = ({ children }) => {
  const [sessionData, setSessionData] = useState(null);
  const [isActive, setIsActive] = useState(false);
  const [eventSource, setEventSource] = useState(null);
  
  const { deduct } = useWallet();

  const startSession = useCallback((plugId) => {
    setIsActive(true);
    const sse = new MockEventSource(`/api/sessions/live?plug_id=${plugId}`);
    
    sse.onmessage = (event) => {
      const data = JSON.parse(event.data);
      setSessionData(data);
      
      // In a real app, the backend would deduct from the balance and stop the session if out of funds.
      // Here we just mock a deduction every 10 seconds for visual effect.
      if (data.duration_sec > 0 && data.duration_sec % 10 === 0) {
         deduct(2); // deduct 2 coins every 10 secs
      }
    };

    setEventSource(sse);
  }, [deduct]);

  const stopSession = useCallback(() => {
    if (eventSource) {
      eventSource.close();
      setEventSource(null);
    }
    setIsActive(false);
    // Keep last sessionData for receipt view
    if (sessionData) {
      setSessionData(prev => ({...prev, status: 'completed'}));
    }
  }, [eventSource, sessionData]);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      if (eventSource) {
        eventSource.close();
      }
    };
  }, [eventSource]);

  return (
    <SessionContext.Provider value={{ sessionData, isActive, startSession, stopSession }}>
      {children}
    </SessionContext.Provider>
  );
};

export const useSession = () => useContext(SessionContext);
