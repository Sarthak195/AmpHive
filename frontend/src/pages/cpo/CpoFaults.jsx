/**
 * AmpHive CPO Fault Console
 * ==========================
 * Operator console for gateway/plug safety alarms and the maintenance
 * workflow: lists events (reusing GET /api/cpo/events, same as CpoAlerts),
 * with severity + unacknowledged-only filters, and — for plug-scoped safety
 * faults (THERMAL_CUTOFF / OVERCURRENT_CUTOFF) — one-click "Put in
 * maintenance" / "Clear maintenance" actions via the dedicated
 * POST /api/cpo/plugs/{id}/maintenance endpoint. A top strip always shows
 * every plug currently IN maintenance so none get forgotten, independent of
 * the event filters below it.
 *
 * Live updates: SessionContext already owns the one shared Socket.io
 * connection and exposes `alarms` (fed by the `gateway_alarm` broadcast —
 * see CpoAlerts.jsx for the same pattern), so this page re-pulls its lists
 * whenever a new alarm arrives instead of polling on a timer.
 *
 * Data source: GET /api/cpo/events, GET /api/cpo/plugs, GET /api/cpo/profile,
 * POST /api/cpo/events/{id}/ack, POST /api/cpo/plugs/{id}/maintenance.
 */

import { useState, useEffect, useCallback, useMemo } from 'react';
import CpoLayout from '../../components/CpoLayout';
import api from '../../api/client';
import { useSession } from '../../contexts/SessionContext';

/** Severity filter options for the events list. */
const SEVERITY_FILTERS = [
  { value: '', label: 'All Severities' },
  { value: 'critical', label: 'Critical' },
  { value: 'warning', label: 'Warning' },
  { value: 'info', label: 'Info' },
];

const SEVERITY_BADGE = {
  critical: 'badge-danger',
  warning: 'badge-warning',
  info: 'badge-primary',
};

// Hardware safety faults the maintenance workflow applies to — mirrors the
// backend's MQTTManager._AUTO_MAINTENANCE_ALARMS (services/mqtt_manager.py):
// THERMAL_CUTOFF/OVERCURRENT_CUTOFF auto-enter MAINTENANCE server-side;
// UNAUTHORIZED_ON is deliberately excluded (an accountability signal, not a
// hardware fault) so it gets no maintenance action here either.
const MAINTENANCE_EVENT_TYPES = new Set(['THERMAL_CUTOFF', 'OVERCURRENT_CUTOFF']);

const timeAgo = (iso) => {
  if (!iso) return '';
  const secs = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (secs < 60) return `${secs}s ago`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
};

const CpoFaults = () => {
  const { alarms } = useSession();
  const [events, setEvents] = useState([]);
  const [plugs, setPlugs] = useState([]);
  const [tenantName, setTenantName] = useState('');
  const [loading, setLoading] = useState(true);
  const [severityFilter, setSeverityFilter] = useState('');
  const [unackOnly, setUnackOnly] = useState(true);
  const [actionError, setActionError] = useState('');
  const [busyKey, setBusyKey] = useState(null); // event id or `plug-<id>` currently in flight

  const plugsById = useMemo(() => {
    const map = {};
    for (const p of plugs) map[p.id] = p;
    return map;
  }, [plugs]);

  // Plugs currently in maintenance — the top strip, independent of the
  // event filters so a cleared/dismissed event can't hide a plug that's
  // still out of service.
  const maintenancePlugs = useMemo(
    () => plugs.filter((p) => p.status === 'maintenance'),
    [plugs],
  );

  const fetchData = useCallback(async () => {
    try {
      const params = new URLSearchParams();
      params.set('limit', '100');
      if (unackOnly) params.set('unacknowledged_only', 'true');
      if (severityFilter) params.set('severity', severityFilter);

      const [eventsRes, plugsRes, profileRes] = await Promise.all([
        api.get(`/api/cpo/events?${params.toString()}`),
        api.get('/api/cpo/plugs'),
        api.get('/api/cpo/profile'),
      ]);
      setEvents(eventsRes);
      setPlugs(plugsRes);
      setTenantName(profileRes.tenant?.name || '');
    } catch (err) {
      console.error('Failed to load fault console:', err);
    } finally {
      setLoading(false);
    }
  }, [unackOnly, severityFilter]);

  useEffect(() => { fetchData(); }, [fetchData]);

  // Live update: re-pull whenever a new gateway_alarm socket broadcast
  // arrives (same pattern as CpoAlerts — a live alarm may be for an event or
  // plug this page hasn't fetched yet).
  useEffect(() => {
    if (alarms && alarms.length > 0) {
      fetchData();
    }
  }, [alarms, fetchData]);

  const acknowledge = async (id) => {
    setBusyKey(id);
    setActionError('');
    try {
      await api.post(`/api/cpo/events/${id}/ack`, {});
      setEvents((prev) => prev.filter((e) => e.id !== id));
    } catch (err) {
      setActionError(err.message || 'Failed to acknowledge event.');
    } finally {
      setBusyKey(null);
    }
  };

  const setMaintenance = async (plugId, action) => {
    const key = `plug-${plugId}`;
    setBusyKey(key);
    setActionError('');
    try {
      await api.post(`/api/cpo/plugs/${plugId}/maintenance`, { action });
      await fetchData();
    } catch (err) {
      setActionError(err.message || `Failed to ${action === 'enter' ? 'enter' : 'clear'} maintenance.`);
    } finally {
      setBusyKey(null);
    }
  };

  return (
    <CpoLayout tenantName={tenantName}>
      <div className="cpo-page-header">
        <h1>Fault Console</h1>
        <p>Gateway/plug safety alarms and the maintenance workflow</p>
      </div>

      {/* Plugs currently in maintenance — always visible so nothing gets forgotten */}
      {maintenancePlugs.length > 0 && (
        <div className="glass" style={{ padding: '1rem 1.25rem', marginBottom: '1.5rem', borderRadius: 'var(--radius-md)' }}>
          <h3 style={{ margin: '0 0 0.75rem' }}>🔧 In Maintenance ({maintenancePlugs.length})</h3>
          <div className="flex flex-wrap gap-2">
            {maintenancePlugs.map((p) => (
              <div
                key={p.id}
                className="flex items-center gap-2"
                style={{
                  padding: '0.5rem 0.85rem',
                  borderRadius: 'var(--radius-md)',
                  background: 'var(--color-primary-glow)',
                  border: '1px solid var(--color-surface-border)',
                }}
              >
                <span style={{ fontWeight: 600 }}>{p.name}</span>
                <span style={{ color: 'var(--color-text-muted)', fontSize: '0.8rem' }}>gw {p.gateway_id}</span>
                <button
                  className="btn btn-ghost btn-sm"
                  disabled={busyKey === `plug-${p.id}`}
                  onClick={() => setMaintenance(p.id, 'clear')}
                >
                  Clear
                </button>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Filter Bar */}
      <div className="filter-bar">
        <select value={severityFilter} onChange={(e) => setSeverityFilter(e.target.value)}>
          {SEVERITY_FILTERS.map((f) => (
            <option key={f.value} value={f.value}>{f.label}</option>
          ))}
        </select>
        <label className="flex items-center gap-2" style={{ fontSize: '0.85rem', color: 'var(--color-text-secondary)', cursor: 'pointer' }}>
          <input
            type="checkbox"
            checked={unackOnly}
            onChange={(e) => setUnackOnly(e.target.checked)}
          />
          Unacknowledged only
        </label>
        <span style={{ color: 'var(--color-text-muted)', fontSize: '0.85rem' }}>
          {events.length} event{events.length !== 1 ? 's' : ''}
        </span>
      </div>

      {actionError && <p className="error-text">{actionError}</p>}

      {/* Events Table */}
      {loading ? (
        <div className="data-table-container">
          {[1, 2, 3].map((i) => (
            <div key={i} style={{ padding: '1rem', borderBottom: '1px solid hsla(0,0%,100%,0.04)' }}>
              <div className="skeleton" style={{ width: '100%', height: '18px' }} />
            </div>
          ))}
        </div>
      ) : events.length > 0 ? (
        <div className="data-table-container animate-fade-in">
          <table className="data-table">
            <thead>
              <tr>
                <th>Severity</th>
                <th>Event</th>
                <th>Gateway</th>
                <th>Plug</th>
                <th>Age</th>
                <th>Detail</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {events.map((ev) => {
                const plug = ev.plug_id != null ? plugsById[ev.plug_id] : null;
                const isMaintenanceFault = MAINTENANCE_EVENT_TYPES.has(ev.event_type) && ev.plug_id != null;
                const inMaintenance = plug?.status === 'maintenance';
                const plugBusy = busyKey === `plug-${ev.plug_id}`;
                return (
                  <tr key={ev.id}>
                    <td>
                      <span className={`badge ${SEVERITY_BADGE[ev.severity] || 'badge-primary'}`}>
                        {ev.severity}
                      </span>
                    </td>
                    <td style={{ color: 'var(--color-text-primary)', fontWeight: 500 }}>
                      {ev.event_type.replace(/_/g, ' ')}
                    </td>
                    <td style={{ fontFamily: 'monospace', fontSize: '0.82rem' }}>{ev.gateway_id}</td>
                    <td>
                      {ev.plug_id != null
                        ? (plug?.name || `#${ev.plug_id}`)
                        : <span style={{ color: 'var(--color-text-muted)' }}>—</span>}
                    </td>
                    <td style={{ color: 'var(--color-text-muted)' }}>{timeAgo(ev.created_at)}</td>
                    <td style={{ color: 'var(--color-text-secondary)', fontSize: '0.85rem' }}>
                      {ev.detail || '—'}
                    </td>
                    <td>
                      <div className="flex gap-2" style={{ flexWrap: 'wrap' }}>
                        {!ev.acknowledged && (
                          <button
                            className="btn btn-ghost btn-sm"
                            disabled={busyKey === ev.id}
                            onClick={() => acknowledge(ev.id)}
                          >
                            Acknowledge
                          </button>
                        )}
                        {isMaintenanceFault && !inMaintenance && (
                          <button
                            className="btn btn-ghost btn-sm"
                            style={{ color: 'var(--color-warning)' }}
                            disabled={plugBusy}
                            onClick={() => setMaintenance(ev.plug_id, 'enter')}
                          >
                            Put in maintenance
                          </button>
                        )}
                        {isMaintenanceFault && inMaintenance && (
                          <button
                            className="btn btn-ghost btn-sm"
                            style={{ color: 'var(--color-success)' }}
                            disabled={plugBusy}
                            onClick={() => setMaintenance(ev.plug_id, 'clear')}
                          >
                            Clear maintenance
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="empty-state glass">
          <div className="empty-state-icon">✅</div>
          <h3>No faults</h3>
          <p>
            {unackOnly
              ? 'No unacknowledged events. Nice and quiet.'
              : 'No events recorded yet.'}
          </p>
        </div>
      )}
    </CpoLayout>
  );
};

export default CpoFaults;
