/**
 * CpoHealth (redesign v3, D3) — the operator's fault console: gateway/plug
 * safety events with severity + unacknowledged filters, single and bulk
 * Acknowledge, and — for the hardware safety faults that can put a plug into
 * MAINTENANCE (THERMAL_CUTOFF / OVERCURRENT_CUTOFF, mirroring the backend's
 * MQTTManager._AUTO_MAINTENANCE_ALARMS) — one-click enter/clear maintenance.
 * A top strip always lists every plug currently in maintenance, independent
 * of whatever the event filters below are set to, so none get forgotten.
 *
 * Live updates: the shared socket (SessionContext) broadcasts `gateway_alarm`
 * into `alarms` — this page refetches on that signal, debounced to at most
 * once every 2s so a burst of alarms doesn't hammer the API.
 *
 * Data: GET /api/cpo/events (paginated {total,items}), GET /api/cpo/plugs,
 * GET /api/cpo/gateways (name resolution), POST /api/cpo/events/{id}/ack,
 * POST /api/cpo/plugs/{id}/maintenance.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { HeartPulse, ShieldCheck, Wrench } from 'lucide-react';
import CpoLayout from '../../components/CpoLayout';
import { PageHeader, DataTable, useToast } from '../../components/ui';
import api from '../../api/client';
import { useSession } from '../../contexts/SessionContext';
import { useTenant } from '../../contexts/TenantContext';
import { eventTypeCopy, apiErrorCopy } from '../../utils/statusCopy';
import './CpoHealth.css';

const LIMIT = 20;
const DEBOUNCE_MS = 2000;

const SEVERITY_OPTIONS = [
  { value: '', label: 'All severities' },
  { value: 'critical', label: 'Critical' },
  { value: 'warning', label: 'Warning' },
  { value: 'info', label: 'Info' },
];

const SEVERITY_BADGE = {
  critical: 'badge-danger',
  warning: 'badge-warn',
  info: 'badge-info',
};

// Hardware safety faults the maintenance workflow applies to. UNAUTHORIZED_ON
// is deliberately excluded (an accountability signal, not a hardware fault).
const MAINTENANCE_EVENT_TYPES = new Set(['THERMAL_CUTOFF', 'OVERCURRENT_CUTOFF']);

const timeAgo = (iso) => {
  if (!iso) return '';
  const secs = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (secs < 60) return 'just now';
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
};

// Content lives in its own component rendered INSIDE <CpoLayout> so that
// useTenant() resolves the real TenantProvider (mounted by CpoLayout) —
// called at the page level it would get the inert no-op fallback and the
// sidebar Health badge would only refresh on the 60s poll.
function CpoHealthContent() {
  const toast = useToast();
  const { alarms } = useSession() || {};
  const { refresh: refreshTenant } = useTenant();

  // ---- Events (primary, paginated) -------------------------------------
  const [events, setEvents] = useState([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [severityFilter, setSeverityFilter] = useState('');
  const [unackOnly, setUnackOnly] = useState(true);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [selectedIds, setSelectedIds] = useState(() => new Set());
  const [ackBusy, setAckBusy] = useState(null); // event id in flight
  const [bulkBusy, setBulkBusy] = useState(false);

  const fetchEvents = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams();
      params.set('limit', String(LIMIT));
      params.set('offset', String(offset));
      if (unackOnly) params.set('unacknowledged_only', 'true');
      if (severityFilter) params.set('severity', severityFilter);
      const res = await api.get(`/api/cpo/events?${params.toString()}`);
      setEvents(Array.isArray(res) ? res : res?.items || []);
      setTotal(Array.isArray(res) ? res.length : res?.total || 0);
      setSelectedIds(new Set());
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, [offset, unackOnly, severityFilter]);

  useEffect(() => {
    fetchEvents();
  }, [fetchEvents]);

  // ---- Supplementary lookups (plug/gateway names, maintenance strip) ----
  // Best-effort: a failure here degrades to raw ids instead of names — the
  // events table itself never breaks over it.
  const [plugs, setPlugs] = useState([]);
  const [gateways, setGateways] = useState([]);

  const fetchLookups = useCallback(async () => {
    const [plugsRes, gatewaysRes] = await Promise.allSettled([
      api.get('/api/cpo/plugs'),
      api.get('/api/cpo/gateways'),
    ]);
    if (plugsRes.status === 'fulfilled') {
      setPlugs(Array.isArray(plugsRes.value) ? plugsRes.value : plugsRes.value?.items || []);
    }
    if (gatewaysRes.status === 'fulfilled') {
      setGateways(
        Array.isArray(gatewaysRes.value) ? gatewaysRes.value : gatewaysRes.value?.items || []
      );
    }
  }, []);

  useEffect(() => {
    fetchLookups();
  }, [fetchLookups]);

  const plugsById = useMemo(() => {
    const map = {};
    for (const p of plugs) map[p.id] = p;
    return map;
  }, [plugs]);

  const gatewaysById = useMemo(() => {
    const map = {};
    for (const g of gateways) map[g.id] = g;
    return map;
  }, [gateways]);

  const maintenancePlugs = useMemo(() => plugs.filter((p) => p.status === 'maintenance'), [plugs]);

  // Live alarm → refetch, debounced to at most once every 2s.
  const fetchAllRef = useRef(() => {});
  useEffect(() => {
    fetchAllRef.current = () => {
      fetchEvents();
      fetchLookups();
    };
  });

  const lastFetchRef = useRef(0);
  const pendingTimerRef = useRef(null);
  useEffect(() => {
    if (!alarms || alarms.length === 0) return undefined;
    const now = Date.now();
    const elapsed = now - lastFetchRef.current;
    if (elapsed >= DEBOUNCE_MS) {
      lastFetchRef.current = now;
      fetchAllRef.current();
    } else {
      clearTimeout(pendingTimerRef.current);
      pendingTimerRef.current = setTimeout(() => {
        lastFetchRef.current = Date.now();
        fetchAllRef.current();
      }, DEBOUNCE_MS - elapsed);
    }
    return () => clearTimeout(pendingTimerRef.current);
    // fetchAllRef always holds the latest closure — only `alarms` should retrigger this.
  }, [alarms]);

  // ---- Acknowledge (single + bulk) --------------------------------------
  const acknowledge = async (id) => {
    setAckBusy(id);
    try {
      await api.post(`/api/cpo/events/${id}/ack`, {});
      setEvents((prev) => prev.filter((e) => e.id !== id));
      setSelectedIds((prev) => {
        const next = new Set(prev);
        next.delete(id);
        return next;
      });
      refreshTenant();
    } catch (err) {
      toast.error(apiErrorCopy(err));
    } finally {
      setAckBusy(null);
    }
  };

  const selectableIds = useMemo(
    () => events.filter((e) => !e.acknowledged).map((e) => e.id),
    [events]
  );
  const allSelected = selectableIds.length > 0 && selectableIds.every((id) => selectedIds.has(id));

  const toggleSelectAll = () => {
    setSelectedIds(allSelected ? new Set() : new Set(selectableIds));
  };

  const toggleSelectOne = (id) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const acknowledgeSelected = async () => {
    const ids = Array.from(selectedIds);
    if (ids.length === 0) return;
    setBulkBusy(true);
    try {
      const results = await Promise.allSettled(
        ids.map((id) => api.post(`/api/cpo/events/${id}/ack`, {}))
      );
      const okIds = ids.filter((_, i) => results[i].status === 'fulfilled');
      const failCount = ids.length - okIds.length;
      setEvents((prev) => prev.filter((e) => !okIds.includes(e.id)));
      setSelectedIds(new Set());
      refreshTenant();
      if (failCount > 0) {
        toast.error(`Acknowledged ${okIds.length}, ${failCount} failed. Try again for the rest.`);
      } else {
        toast.ok(`Acknowledged ${okIds.length} event${okIds.length === 1 ? '' : 's'}.`);
      }
    } finally {
      setBulkBusy(false);
    }
  };

  // ---- Maintenance enter/clear -------------------------------------------
  const [maintBusy, setMaintBusy] = useState(null); // plug id in flight

  const setMaintenance = async (plugId, action) => {
    setMaintBusy(plugId);
    try {
      await api.post(`/api/cpo/plugs/${plugId}/maintenance`, { action });
      toast.ok(action === 'enter' ? 'Charger put in maintenance.' : 'Charger cleared for service.');
      fetchLookups();
    } catch (err) {
      toast.error(apiErrorCopy(err));
    } finally {
      setMaintBusy(null);
    }
  };

  const columns = [
    {
      key: 'select',
      label: 'Select',
      render: (ev) =>
        ev.acknowledged ? (
          <span aria-hidden="true">—</span>
        ) : (
          <input
            type="checkbox"
            aria-label={`Select event ${eventTypeCopy(ev.event_type)}`}
            checked={selectedIds.has(ev.id)}
            onChange={() => toggleSelectOne(ev.id)}
          />
        ),
    },
    {
      key: 'severity',
      label: 'Severity',
      render: (ev) => (
        <span className={`badge ${SEVERITY_BADGE[ev.severity] || 'badge-info'}`}>{ev.severity}</span>
      ),
    },
    {
      key: 'event_type',
      label: 'Event',
      render: (ev) => (
        <span className="stack-sm">
          <span>{eventTypeCopy(ev.event_type)}</span>
          {ev.detail && <span className="text-3 text-xs">{ev.detail}</span>}
        </span>
      ),
    },
    {
      key: 'gateway_id',
      label: 'Gateway',
      render: (ev) => gatewaysById[ev.gateway_id]?.name || ev.gateway_id,
    },
    {
      key: 'plug_id',
      label: 'Plug',
      render: (ev) =>
        ev.plug_id != null ? plugsById[ev.plug_id]?.name || `#${ev.plug_id}` : '—',
    },
    {
      key: 'created_at',
      label: 'Time',
      render: (ev) => <span className="text-2 text-sm">{timeAgo(ev.created_at)}</span>,
    },
    {
      key: 'row_actions',
      label: 'Actions',
      render: (ev) => {
        const plug = ev.plug_id != null ? plugsById[ev.plug_id] : null;
        const isMaintFault = MAINTENANCE_EVENT_TYPES.has(ev.event_type) && ev.plug_id != null;
        const inMaintenance = plug?.status === 'maintenance';
        return (
          <span className="row health-row-actions">
            {!ev.acknowledged && (
              <button
                type="button"
                className="btn btn-quiet btn-sm"
                disabled={ackBusy === ev.id}
                onClick={() => acknowledge(ev.id)}
              >
                {ackBusy === ev.id ? 'Acknowledging…' : 'Acknowledge'}
              </button>
            )}
            {isMaintFault && !inMaintenance && (
              <button
                type="button"
                className="btn btn-quiet btn-sm"
                disabled={maintBusy === ev.plug_id}
                onClick={() => setMaintenance(ev.plug_id, 'enter')}
              >
                Put in maintenance
              </button>
            )}
            {isMaintFault && inMaintenance && (
              <button
                type="button"
                className="btn btn-quiet btn-sm"
                disabled={maintBusy === ev.plug_id}
                onClick={() => setMaintenance(ev.plug_id, 'clear')}
              >
                Clear maintenance
              </button>
            )}
          </span>
        );
      },
    },
  ];

  return (
    <>
      <PageHeader title="Health" sub="Gateway and charger safety events, and the maintenance workflow." />

      {maintenancePlugs.length > 0 && (
        <div className="well health-maintenance-strip">
          <h2 className="health-strip-title">
            <Wrench size={16} aria-hidden="true" />
            In maintenance ({maintenancePlugs.length})
          </h2>
          <ul className="health-strip-list">
            {maintenancePlugs.map((p) => (
              <li key={p.id} className="chip health-strip-chip">
                <span>{p.name}</span>
                <button
                  type="button"
                  className="btn btn-quiet btn-sm"
                  disabled={maintBusy === p.id}
                  onClick={() => setMaintenance(p.id, 'clear')}
                >
                  {maintBusy === p.id ? 'Clearing…' : 'Clear'}
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="filter-bar">
        <div className="field">
          <label className="field-label" htmlFor="health-severity">
            Severity
          </label>
          <select
            id="health-severity"
            className="select"
            value={severityFilter}
            onChange={(e) => {
              setSeverityFilter(e.target.value);
              setOffset(0);
            }}
          >
            {SEVERITY_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
        </div>
        <label className="checkbox-row">
          <input
            type="checkbox"
            checked={unackOnly}
            onChange={(e) => {
              setUnackOnly(e.target.checked);
              setOffset(0);
            }}
          />
          Unacknowledged only
        </label>
        <span className="filter-count">{total} event{total === 1 ? '' : 's'}</span>
      </div>

      {selectableIds.length > 0 && (
        <label className="checkbox-row health-select-all">
          <input type="checkbox" checked={allSelected} onChange={toggleSelectAll} />
          Select all {selectableIds.length} unacknowledged event{selectableIds.length === 1 ? '' : 's'} on this page
        </label>
      )}

      {selectedIds.size > 0 && (
        <div className="banner banner-info health-bulk-bar">
          <span>{selectedIds.size} selected</span>
          <button type="button" className="btn btn-quiet btn-sm" onClick={() => setSelectedIds(new Set())}>
            Clear selection
          </button>
          <button type="button" className="btn btn-primary btn-sm" disabled={bulkBusy} onClick={acknowledgeSelected}>
            {bulkBusy ? 'Acknowledging…' : 'Acknowledge selected'}
          </button>
        </div>
      )}

      <DataTable
        columns={columns}
        rows={events}
        loading={loading}
        error={error}
        onRetry={fetchEvents}
        emptyIcon={unackOnly ? ShieldCheck : HeartPulse}
        emptyTitle={unackOnly ? 'No unacknowledged events' : 'No events recorded'}
        emptyBody={
          unackOnly
            ? "Nice and quiet — nothing needs your attention right now."
            : 'Safety and firmware events will show up here as they happen.'
        }
        pagination={{ total, offset, limit: LIMIT, onPage: setOffset }}
        collapse
      />
    </>
  );
}

export default function CpoHealth() {
  return (
    <CpoLayout>
      <CpoHealthContent />
    </CpoLayout>
  );
}
