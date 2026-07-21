/**
 * Session page (redesign v3, C4) — the live charging surface.
 *
 * - No active session and no receipt → a friendly interstitial ("No active
 *   charge" + Find a charger / Recent activity) instead of a redirect.
 * - Multiple active sessions → seg pills with each session's last-known
 *   live ₹; picking one refocuses the monitor (SessionContext.switchSession).
 * - After a stop → the receipt replaces the frozen monitor.
 *
 * SessionContext owns the socket/telemetry lifecycle — this page only reads.
 */

import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { PlugZap } from 'lucide-react';

import SessionMonitor from '../components/SessionMonitor';
import SessionReceipt from '../components/SessionReceipt';
import { useSession } from '../contexts/SessionContext';
import { useConfig } from '../contexts/ConfigContext';
import { PageHeader, EmptyState } from '../components/ui';
import { formatINR, coinsToINR } from '../utils/money';
import './Session.css';

const Session = () => {
  const { isActive, sessionData, activeSessions, sessionId, switchSession, receipt } =
    useSession();
  const { coin_inr_rate } = useConfig();

  // Last-known live cost per session — telemetry only streams for the focused
  // session, so remember each one's cost as it's focused to keep the seg
  // pills' ₹ meaningful after switching away.
  const [costs, setCosts] = useState({});
  const focusedCost = sessionData?.cost_coins;
  useEffect(() => {
    if (sessionId == null || focusedCost == null) return;
    setCosts((prev) =>
      prev[sessionId] === focusedCost ? prev : { ...prev, [sessionId]: focusedCost }
    );
  }, [sessionId, focusedCost]);

  // Only a genuinely active session earns the live monitor here — a finished
  // session's stale sessionData (status: 'completed') would otherwise keep
  // rendering the monitor (elapsed timer still ticking) after the receipt is
  // dismissed. The receipt has its own branch above, so this only decides
  // monitor vs. the no-active-charge interstitial.
  const hasSession = isActive;

  return (
    <div className="page session-page">
      <PageHeader
        eyebrow="Live"
        title="Charging"
        sub={
          receipt
            ? 'Your session has ended — here’s the summary.'
            : hasSession
              ? 'Live view of your charging session.'
              : undefined
        }
      />

      {receipt ? (
        <SessionReceipt />
      ) : hasSession ? (
        <>
          {activeSessions.length > 1 && (
            <div className="seg session-seg" aria-label="Active sessions">
              {activeSessions.map((s) => {
                const focused = s.session_id === sessionId;
                const cost = costs[s.session_id];
                return (
                  <button
                    key={s.session_id}
                    type="button"
                    className={`seg-item${focused ? ' active' : ''}`}
                    aria-pressed={focused}
                    onClick={() => switchSession(s)}
                  >
                    {s.plug_name}
                    <span className="num text-xs">
                      {cost != null ? formatINR(coinsToINR(cost, coin_inr_rate)) : '—'}
                    </span>
                  </button>
                );
              })}
            </div>
          )}
          <SessionMonitor />
        </>
      ) : (
        <EmptyState
          icon={PlugZap}
          title="No active charge"
          body="When you start charging, your live progress and cost show up here."
          action={
            <div className="session-empty-actions">
              <Link className="btn btn-primary" to="/">
                Find a charger
              </Link>
              <Link className="btn btn-quiet" to="/activity">
                Recent activity
              </Link>
            </div>
          }
        />
      )}
    </div>
  );
};

export default Session;
