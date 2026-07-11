/**
 * AmpHive CPO Alerts Feed
 * =======================
 * Shows gateway/plug operational events for the operator's tenant — safety
 * cutoffs (thermal / over-current), unauthorized-on alarms (a plug switched on
 * with no authorized session), and OTA lifecycle notices.
 *
 * Sources: GET /api/cpo/events (persisted history, newest first) merged with
 * live `gateway_alarm` socket broadcasts (via SessionContext, which owns the
 * one shared socket). Operators can acknowledge an event to clear it from the
 * active feed without losing the audit row.
 */

import { useState, useEffect, useCallback } from 'react';
import api from '../api/client';
import { useSession } from '../contexts/SessionContext';

const SEVERITY_STYLE = {
  critical: { color: 'var(--color-danger)', bg: 'hsla(0, 80%, 50%, 0.10)', border: 'hsla(0, 80%, 50%, 0.35)', icon: '🚨' },
  warning: { color: 'var(--color-warning, #f0a020)', bg: 'hsla(38, 90%, 50%, 0.10)', border: 'hsla(38, 90%, 50%, 0.35)', icon: '⚠️' },
  info: { color: 'var(--color-text-secondary)', bg: 'hsla(210, 20%, 50%, 0.10)', border: 'hsla(210, 20%, 50%, 0.30)', icon: 'ℹ️' },
};

const timeAgo = (iso) => {
  if (!iso) return '';
  const secs = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (secs < 60) return `${secs}s ago`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
};

const CpoAlerts = () => {
  const { alarms } = useSession();
  const [events, setEvents] = useState([]);
  const [loading, setLoading] = useState(true);

  const fetchEvents = useCallback(async () => {
    try {
      const res = await api.get('/api/cpo/events?unacknowledged_only=true&limit=50');
      setEvents(res);
    } catch (err) {
      console.error('Failed to load CPO events:', err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchEvents();
  }, [fetchEvents]);

  // A live socket alarm may reference an event our tenant owns but that we
  // haven't fetched yet — re-pull the list so the acknowledge action targets a
  // real row. (Broadcasts are global; the fetch is tenant-scoped, so anything
  // that isn't ours simply won't appear.)
  useEffect(() => {
    if (alarms && alarms.length > 0) {
      fetchEvents();
    }
  }, [alarms, fetchEvents]);

  const acknowledge = async (id) => {
    try {
      await api.post(`/api/cpo/events/${id}/ack`, {});
      setEvents((prev) => prev.filter((e) => e.id !== id));
    } catch (err) {
      console.error('Failed to acknowledge event:', err);
    }
  };

  if (loading) return null;
  if (events.length === 0) return null;

  return (
    <div className="glass" style={{ padding: '1.25rem 1.5rem', marginBottom: '2rem', borderRadius: 'var(--radius-md)' }}>
      <div className="flex justify-between items-center" style={{ marginBottom: '0.75rem' }}>
        <h3 style={{ margin: 0 }}>🔔 Active Alerts ({events.length})</h3>
      </div>
      <div className="flex flex-col gap-2">
        {events.map((ev) => {
          const s = SEVERITY_STYLE[ev.severity] || SEVERITY_STYLE.info;
          return (
            <div
              key={ev.id}
              className="flex justify-between items-center gap-3"
              style={{
                padding: '0.6rem 0.85rem',
                borderRadius: 'var(--radius-md)',
                background: s.bg,
                border: `1px solid ${s.border}`,
              }}
            >
              <div className="flex items-center gap-2" style={{ minWidth: 0 }}>
                <span style={{ fontSize: '1.1rem' }}>{s.icon}</span>
                <div style={{ minWidth: 0 }}>
                  <div style={{ color: s.color, fontWeight: 600, fontSize: '0.9rem' }}>
                    {ev.event_type.replace(/_/g, ' ')}
                    {ev.plug_id != null && (
                      <span style={{ color: 'var(--color-text-muted)', fontWeight: 400 }}> · plug {ev.plug_id}</span>
                    )}
                  </div>
                  <div style={{ color: 'var(--color-text-secondary)', fontSize: '0.8rem' }}>
                    {ev.detail || `Gateway ${ev.gateway_id}`}
                    <span style={{ color: 'var(--color-text-muted)' }}> · {timeAgo(ev.created_at)}</span>
                  </div>
                </div>
              </div>
              <button
                className="btn btn-ghost btn-sm"
                onClick={() => acknowledge(ev.id)}
                style={{ flexShrink: 0, whiteSpace: 'nowrap' }}
              >
                Dismiss
              </button>
            </div>
          );
        })}
      </div>
    </div>
  );
};

export default CpoAlerts;
