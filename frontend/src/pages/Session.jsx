/**
 * AmpHive Session Page
 * ====================
 * Displays the live charging session monitor.
 * Redirects to home if no session is active.
 */

import React, { useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import SessionMonitor from '../components/SessionMonitor';
import { useSession } from '../contexts/SessionContext';

const Session = () => {
  const { isActive, sessionData } = useSession();
  const navigate = useNavigate();

  // If user navigated here manually without a session, bounce them back to home
  useEffect(() => {
    if (!isActive && !sessionData) {
      navigate('/');
    }
  }, [isActive, sessionData, navigate]);

  return (
    <div className="page-container" style={{ maxWidth: '800px' }}>
      <header style={{ marginBottom: '1.5rem' }}>
        <button
          onClick={() => navigate('/')}
          className="btn btn-ghost btn-sm"
          style={{ marginBottom: '0.5rem' }}
        >
          ← Back to Dashboard
        </button>
      </header>

      <SessionMonitor />
    </div>
  );
};

export default Session;
