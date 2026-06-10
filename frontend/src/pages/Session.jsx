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
    <div className="container" style={{ maxWidth: '800px', marginTop: '2rem' }}>
      <header style={{ marginBottom: '2rem' }}>
        <button 
          onClick={() => navigate('/')}
          style={{ 
            background: 'none', 
            border: 'none', 
            color: 'var(--color-primary)', 
            cursor: 'pointer',
            fontSize: '1rem',
            marginBottom: '1rem'
          }}
        >
          ← Back to Dashboard
        </button>
      </header>

      <SessionMonitor />
    </div>
  );
};

export default Session;
