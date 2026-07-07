/**
 * AmpHive Session Page
 * ====================
 * Displays the live charging session monitor.
 * Redirects to home if no session is active.
 */

import { useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import SessionMonitor from '../components/SessionMonitor';
import { useSession } from '../contexts/SessionContext';

const Session = () => {
  const { isActive, sessionData, activeSessions, sessionId, switchSession } = useSession();
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

      {/* With more than one active session, let the user pick which one the
          live monitor follows (the stop button acts on the focused session) */}
      {activeSessions.length > 1 && (
        <div className="flex gap-2" style={{ marginBottom: '1rem', flexWrap: 'wrap' }}>
          {activeSessions.map((s) => (
            <button
              key={s.session_id}
              className={`btn btn-sm ${s.session_id === sessionId ? 'btn-primary' : 'btn-ghost'}`}
              onClick={() => switchSession(s)}
            >
              ⚡ {s.plug_name}
            </button>
          ))}
        </div>
      )}

      <SessionMonitor />
    </div>
  );
};

export default Session;
