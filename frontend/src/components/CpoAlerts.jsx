/**
 * CpoAlerts — the console's ambient alert strip.
 * ==============================================
 * Rendered by CpoLayout on EVERY console page (not just the dashboard):
 * unacknowledged critical/warning gateway events as banners, severity-sorted
 * (critical first, then newest). Info-level events stay on the Health page.
 *
 * Sources: GET /api/cpo/events?unacknowledged_only=true on a 60s poll, plus
 * an immediate refetch when the shared socket broadcasts a `gateway_alarm`
 * (via SessionContext). Acknowledging posts the ack, drops the banner and
 * refreshes TenantContext so the Health nav badge stays in step.
 */

import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { AlertTriangle, OctagonAlert } from 'lucide-react';
import api from '../api/client';
import usePoll from '../hooks/usePoll';
import { useSession } from '../contexts/SessionContext';
import { useTenant } from '../contexts/TenantContext';
import { useToast } from './ui';
import { eventTypeCopy, apiErrorCopy } from '../utils/statusCopy';
import './CpoAlerts.css';

/** Only critical/warning events interrupt every page. */
const SEVERITY_RANK = { critical: 0, warning: 1 };
const MAX_SHOWN = 4;

const timeAgo = (iso) => {
  if (!iso) return '';
  const secs = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (secs < 60) return 'just now';
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
};

const CpoAlerts = () => {
  // Null-safe: the CPO host may render before SessionProvider hands out a
  // socket, and unit tests mount without the provider entirely.
  const { alarms } = useSession() || {};
  const { refresh } = useTenant();
  const toast = useToast();
  const [events, setEvents] = useState(null); // null = first load pending
  const [failed, setFailed] = useState(false);
  const [ackBusy, setAckBusy] = useState(null);

  const fetchEvents = useCallback(async () => {
    try {
      const res = await api.get('/api/cpo/events?unacknowledged_only=true&limit=50');
      const list = Array.isArray(res) ? res : res?.items || [];
      setEvents(
        list
          .filter((e) => SEVERITY_RANK[e.severity] !== undefined)
          .sort(
            (a, b) =>
              SEVERITY_RANK[a.severity] - SEVERITY_RANK[b.severity] ||
              new Date(b.created_at || 0) - new Date(a.created_at || 0),
          ),
      );
      setFailed(false);
    } catch {
      setFailed(true);
    }
  }, []);

  usePoll(fetchEvents, 60_000);

  // A live socket alarm may reference an event we haven't fetched yet —
  // re-pull so the Acknowledge action targets a real row.
  useEffect(() => {
    if (alarms && alarms.length > 0) fetchEvents();
  }, [alarms, fetchEvents]);

  const acknowledge = async (id) => {
    setAckBusy(id);
    try {
      await api.post(`/api/cpo/events/${id}/ack`, {});
      setEvents((prev) => (prev || []).filter((e) => e.id !== id));
      refresh(); // keep the Health nav badge in step
    } catch (err) {
      toast.error(apiErrorCopy(err));
    } finally {
      setAckBusy(null);
    }
  };

  if (failed) {
    return (
      <div className="console-alerts">
        <div className="banner banner-danger console-alert" role="alert">
          <AlertTriangle size={18} aria-hidden="true" />
          <div className="console-alert-body">Couldn't check for new alerts.</div>
          <button type="button" className="btn btn-quiet btn-sm" onClick={fetchEvents}>
            Retry
          </button>
        </div>
      </div>
    );
  }

  if (!events || events.length === 0) return null;

  const shown = events.slice(0, MAX_SHOWN);

  return (
    <section className="console-alerts" aria-label="Active alerts" aria-live="polite">
      {shown.map((ev) => {
        const critical = ev.severity === 'critical';
        const Icon = critical ? OctagonAlert : AlertTriangle;
        return (
          <div
            key={ev.id}
            className={`banner ${critical ? 'banner-danger' : 'banner-warn'} console-alert`}
          >
            <Icon size={18} aria-hidden="true" />
            <div className="console-alert-body">
              <strong>{eventTypeCopy(ev.event_type)}</strong>
              <span className="console-alert-detail">
                {ev.detail || `Gateway ${ev.gateway_id}`}
                {ev.plug_id != null && ` · Plug ${ev.plug_id}`}
                {ev.created_at && ` · ${timeAgo(ev.created_at)}`}
              </span>
            </div>
            <button
              type="button"
              className="btn btn-quiet btn-sm"
              disabled={ackBusy === ev.id}
              onClick={() => acknowledge(ev.id)}
            >
              {ackBusy === ev.id ? 'Acknowledging…' : 'Acknowledge'}
            </button>
          </div>
        );
      })}
      <Link className="console-alerts-viewall" to="/cpo/health">
        {events.length > shown.length
          ? `View all ${events.length} alerts`
          : 'View all alerts'}
      </Link>
    </section>
  );
};

export default CpoAlerts;
