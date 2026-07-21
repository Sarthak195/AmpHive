/**
 * CpoReservations (redesign v3, D5) — plug bookings across the CPO's
 * chargers: who reserved what, and when.
 *
 * Two views behind a `.seg` toggle:
 * - List: GET /api/cpo/reservations?status&upcoming_only&limit&offset (the
 *   house {total, items} paginated shape) + operator cancel (POST
 *   /api/reservations/{id}/cancel — BOOKED only, 409 otherwise). Cancelling
 *   as the operator DOES notify the driver (services/notifications.notify,
 *   verified against backend/routers/reservations.py — not the no-notify
 *   case the old copy implied), so the confirm dialog says so.
 * - Day: a pure-CSS-grid timeline, one row per charger, 24 hourly columns
 *   (96 15-minute grid tracks under the hood so blocks land on their real
 *   start/end). Fetches a single generous page of reservations + the plug
 *   list once per view-entry and filters/positions everything client-side —
 *   there's no server-side date filter to page against. That page is capped
 *   at the house limit (200), so a tenant with more upcoming bookings than
 *   that may not see every one of them on far-out days; fine for the
 *   near-term scheduling glance this view is for.
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import { CalendarClock, CalendarX2, ChevronLeft, ChevronRight } from 'lucide-react';

import CpoLayout from '../../components/CpoLayout';
import { PageHeader, DataTable, ConfirmDialog, ErrorState, EmptyState, Skeleton, useToast } from '../../components/ui';
import api from '../../api/client';
import { apiErrorCopy, reservationStatusLabel } from '../../utils/statusCopy';
import './CpoReservations.css';

const RES_LIMIT = 20;
const DAY_FETCH_LIMIT = 200;
const DAY_COLUMNS = 96; // 24h * 4 (15-minute resolution)

const STATUS_OPTIONS = [
  { value: '', label: 'All statuses' },
  { value: 'booked', label: 'Booked' },
  { value: 'fulfilled', label: 'Fulfilled' },
  { value: 'cancelled', label: 'Cancelled' },
  { value: 'expired', label: 'Expired' },
];

const STATUS_TONE = {
  booked: 'badge-ok',
  fulfilled: 'badge-info',
  cancelled: 'badge-danger',
  expired: 'badge-warn',
};

// Render a booking window in the viewer's local timezone. Collapses the end
// to a bare time when it shares the start's calendar day.
const formatWindow = (startIso, endIso) => {
  if (!startIso || !endIso) return '—';
  const s = new Date(startIso);
  const e = new Date(endIso);
  const dateOpts = { month: 'short', day: 'numeric' };
  const timeOpts = { hour: '2-digit', minute: '2-digit' };
  const sameDay = s.toDateString() === e.toDateString();
  const startStr = `${s.toLocaleDateString('en-IN', dateOpts)}, ${s.toLocaleTimeString('en-IN', timeOpts)}`;
  const endStr = sameDay
    ? e.toLocaleTimeString('en-IN', timeOpts)
    : `${e.toLocaleDateString('en-IN', dateOpts)}, ${e.toLocaleTimeString('en-IN', timeOpts)}`;
  return `${startStr} – ${endStr}`;
};

const toDateStr = (d) => {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
};

const todayStr = () => toDateStr(new Date());

export default function CpoReservations() {
  const toast = useToast();
  const [view, setView] = useState('list');

  // ---- List view --------------------------------------------------------
  const [statusFilter, setStatusFilter] = useState('');
  const [upcomingOnly, setUpcomingOnly] = useState(false);
  const [reservations, setReservations] = useState([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const fetchList = useCallback(async (nextOffset = 0) => {
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams();
      params.set('limit', RES_LIMIT);
      params.set('offset', nextOffset);
      if (statusFilter) params.set('status', statusFilter);
      if (upcomingOnly) params.set('upcoming_only', 'true');

      const data = await api.get(`/api/cpo/reservations?${params.toString()}`);
      // Handle both the legacy bare-array shape and the paginated {total,items}
      // envelope (backend lands the envelope in parallel with this UI).
      const items = Array.isArray(data) ? data : data?.items || [];
      setReservations(items);
      setTotal(typeof data?.total === 'number' ? data.total : items.length);
      setOffset(nextOffset);
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, [statusFilter, upcomingOnly]);

  useEffect(() => {
    fetchList(0);
  }, [fetchList]);

  // ---- Cancel (list view only) -------------------------------------------
  const [cancelTarget, setCancelTarget] = useState(null);
  const [cancelling, setCancelling] = useState(false);

  const confirmCancel = async () => {
    if (!cancelTarget) return;
    setCancelling(true);
    try {
      await api.post(`/api/reservations/${cancelTarget.id}/cancel`, {});
      toast.ok('Reservation cancelled.');
      setCancelTarget(null);
      fetchList(offset);
    } catch (err) {
      toast.error(apiErrorCopy(err));
    } finally {
      setCancelling(false);
    }
  };

  // ---- Day view -----------------------------------------------------------
  const [selectedDate, setSelectedDate] = useState(todayStr);
  const [dayReservations, setDayReservations] = useState([]);
  const [dayPlugs, setDayPlugs] = useState([]);
  const [dayLoading, setDayLoading] = useState(true);
  const [dayError, setDayError] = useState(null);
  const [dayLoaded, setDayLoaded] = useState(false);

  const fetchDay = useCallback(async () => {
    setDayLoading(true);
    setDayError(null);
    try {
      const [resvRes, plugsRes] = await Promise.all([
        api.get(`/api/cpo/reservations?limit=${DAY_FETCH_LIMIT}`),
        api.get('/api/cpo/plugs'),
      ]);
      setDayReservations(Array.isArray(resvRes) ? resvRes : resvRes?.items || []);
      setDayPlugs(Array.isArray(plugsRes) ? plugsRes : []);
      setDayLoaded(true);
    } catch (err) {
      setDayError(err);
    } finally {
      setDayLoading(false);
    }
  }, []);

  useEffect(() => {
    if (view === 'day' && !dayLoaded) fetchDay();
  }, [view, dayLoaded, fetchDay]);

  const shiftDay = (delta) => {
    setSelectedDate((prev) => {
      const d = new Date(`${prev}T00:00:00`);
      d.setDate(d.getDate() + delta);
      return toDateStr(d);
    });
  };

  const reservationsByPlug = useMemo(() => {
    const dayStart = new Date(`${selectedDate}T00:00:00`);
    const dayEnd = new Date(dayStart.getTime() + 24 * 60 * 60 * 1000);
    const map = new Map();
    for (const p of dayPlugs) map.set(p.id, []);
    for (const r of dayReservations) {
      const s = new Date(r.start_at);
      const e = new Date(r.end_at);
      if (e <= dayStart || s >= dayEnd) continue; // no overlap with the picked day
      if (!map.has(r.plug_id)) map.set(r.plug_id, []);
      map.get(r.plug_id).push({
        ...r,
        _startMin: Math.max(0, (s - dayStart) / 60000),
        _endMin: Math.min(1440, (e - dayStart) / 60000),
      });
    }
    return map;
  }, [dayPlugs, dayReservations, selectedDate]);

  const blockStyle = (r) => {
    const startCol = Math.max(1, Math.floor(r._startMin / 15) + 1);
    const endCol = Math.min(DAY_COLUMNS + 1, Math.ceil(r._endMin / 15) + 1);
    return { gridColumn: `${startCol} / ${Math.max(startCol + 1, endCol)}` };
  };

  const columns = [
    { key: 'plug_name', label: 'Charger', render: (r) => r.plug_name || `Plug #${r.plug_id}` },
    {
      key: 'user_email',
      label: 'Driver',
      render: (r) => (
        <>
          <div>{r.user_name || '—'}</div>
          <div className="text-3 text-sm">{r.user_email}</div>
        </>
      ),
    },
    { key: 'start_at', label: 'Window (local)', render: (r) => formatWindow(r.start_at, r.end_at) },
    {
      key: 'status',
      label: 'Status',
      render: (r) => (
        <span className={`badge ${STATUS_TONE[r.status] || 'badge-info'}`}>
          {reservationStatusLabel(r.status)}
        </span>
      ),
    },
    { key: 'session_id', label: 'Session', render: (r) => (r.session_id != null ? `#${r.session_id}` : '—') },
    {
      key: 'actions',
      label: '',
      render: (r) =>
        r.status === 'booked' ? (
          <button type="button" className="btn btn-quiet btn-sm" onClick={() => setCancelTarget(r)}>
            Cancel
          </button>
        ) : (
          <span className="text-3">—</span>
        ),
    },
  ];

  return (
    <CpoLayout>
      <PageHeader
        eyebrow="Console"
        title="Reservations"
        sub="Plug bookings across your chargers — who reserved what, and when."
        actions={
          <div className="seg" role="group" aria-label="View">
            <button
              type="button"
              className={`seg-item${view === 'list' ? ' active' : ''}`}
              aria-pressed={view === 'list'}
              onClick={() => setView('list')}
            >
              List
            </button>
            <button
              type="button"
              className={`seg-item${view === 'day' ? ' active' : ''}`}
              aria-pressed={view === 'day'}
              onClick={() => setView('day')}
            >
              Day
            </button>
          </div>
        }
      />

      {view === 'list' ? (
        <>
          <div className="filter-bar">
            <select
              className="select"
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value)}
              aria-label="Status"
            >
              {STATUS_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>{opt.label}</option>
              ))}
            </select>

            <label className="checkbox-row">
              <input
                type="checkbox"
                checked={upcomingOnly}
                onChange={(e) => setUpcomingOnly(e.target.checked)}
              />
              Upcoming only
            </label>

            <span className="filter-count">{total} shown</span>
          </div>

          <DataTable
            columns={columns}
            rows={reservations}
            loading={loading}
            error={error}
            onRetry={() => fetchList(offset)}
            emptyIcon={CalendarClock}
            emptyTitle="No reservations found"
            emptyBody={
              statusFilter || upcomingOnly
                ? 'No reservations match these filters. Try widening them.'
                : 'Bookings will appear here once drivers reserve time slots on your chargers.'
            }
            pagination={{ total, offset, limit: RES_LIMIT, onPage: (o) => fetchList(o) }}
            collapse
          />
        </>
      ) : (
        <>
          <div className="resv-day-toolbar">
            <button
              type="button"
              className="btn btn-quiet btn-icon"
              aria-label="Previous day"
              onClick={() => shiftDay(-1)}
            >
              <ChevronLeft size={18} aria-hidden="true" />
            </button>
            <input
              type="date"
              className="input"
              value={selectedDate}
              onChange={(e) => setSelectedDate(e.target.value)}
              aria-label="Day"
            />
            <button
              type="button"
              className="btn btn-quiet btn-icon"
              aria-label="Next day"
              onClick={() => shiftDay(1)}
            >
              <ChevronRight size={18} aria-hidden="true" />
            </button>
            <button type="button" className="btn btn-quiet btn-sm" onClick={() => setSelectedDate(todayStr())}>
              Today
            </button>
          </div>

          {dayLoading ? (
            <Skeleton lines={6} />
          ) : dayError ? (
            <ErrorState error={dayError} onRetry={fetchDay} />
          ) : dayPlugs.length === 0 ? (
            <EmptyState
              icon={CalendarX2}
              title="No chargers yet"
              body="Add a charger to see its reservation timeline here."
            />
          ) : (
            <>
              <div className="row resv-day-legend">
                <span className="badge badge-ok">Booked</span>
                <span className="badge badge-info">Fulfilled</span>
                <span className="badge badge-danger">Cancelled</span>
                <span className="badge badge-warn">Expired</span>
              </div>

              <div className="resv-day-grid">
                <div className="resv-day-row resv-day-head">
                  <div className="resv-day-label" />
                  <div className="resv-day-track resv-day-hours">
                    {Array.from({ length: 24 }, (_, h) => (
                      <span key={h} className="resv-day-hour">{h}</span>
                    ))}
                  </div>
                </div>

                {dayPlugs.map((p) => (
                  <div className="resv-day-row" key={p.id}>
                    <div className="resv-day-label">{p.name}</div>
                    <div className="resv-day-track">
                      {(reservationsByPlug.get(p.id) || []).map((r) => (
                        <div
                          key={r.id}
                          className={`resv-block resv-block-${r.status}`}
                          style={blockStyle(r)}
                          title={`${r.user_name || r.user_email} · ${formatWindow(r.start_at, r.end_at)} · ${reservationStatusLabel(r.status)}`}
                        >
                          {r.user_name || r.user_email}
                        </div>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            </>
          )}
        </>
      )}

      <ConfirmDialog
        open={Boolean(cancelTarget)}
        onClose={() => setCancelTarget(null)}
        onConfirm={confirmCancel}
        title="Cancel this reservation?"
        body={`This frees up ${cancelTarget?.plug_name || `plug ${cancelTarget?.plug_id}`} immediately, and the driver will be notified.`}
        confirmLabel="Cancel reservation"
        tone="danger"
        busy={cancelling}
      />
    </CpoLayout>
  );
}
