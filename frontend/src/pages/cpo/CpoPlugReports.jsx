/**
 * CpoPlugReports (near-copy of CpoDisputes.jsx, minus the refund fields) —
 * driver-filed "report a problem with this charger" flags. No session, no
 * money: a report just moves through open -> acknowledged -> resolved with
 * an optional operator note (backend/database/models.py PlugReport).
 *
 * Filing a report ALSO writes a GatewayEvent (backend/routers/plugs.py
 * report_plug_problem), so the SAME report additionally surfaces as a
 * banner in the console's ambient CpoAlerts strip / Health badge — this
 * page is the durable, resolvable record of it.
 *
 * Data: GET /api/cpo/plug-reports[?status_filter=open|acknowledged|resolved]
 * (bare list, newest first). Plug name isn't on that shape, so this page
 * enriches rows best-effort from GET /api/cpo/plugs (same tenant-scoped
 * plug list CpoChargers uses) — a failed or partial enrichment never blocks
 * the table, it just leaves that cell as a raw plug id.
 *
 * Mutation: POST /api/cpo/plug-reports/{id}/resolve { action, note? }.
 */

import { useCallback, useEffect, useState } from 'react';
import { Flag } from 'lucide-react';
import CpoLayout from '../../components/CpoLayout';
import { PageHeader, DataTable, Tabs, ConfirmDialog, useToast } from '../../components/ui';
import api from '../../api/client';
import { plugReportCategoryLabel, plugReportStatusLabel, apiErrorCopy } from '../../utils/statusCopy';
import './CpoPlugReports.css';

const LIMIT = 200;

const STATUS_BADGE = {
  open: 'badge-warn',
  acknowledged: 'badge-info',
  resolved: 'badge-ok',
};

const TABS = [
  { id: 'open', label: 'Open' },
  { id: 'acknowledged', label: 'Acknowledged' },
  { id: 'resolved', label: 'Resolved' },
  { id: '', label: 'All' },
];

const formatDateTime = (iso) =>
  iso
    ? new Date(iso).toLocaleString('en-IN', {
        month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
      })
    : '—';

export default function CpoPlugReports() {
  const toast = useToast();

  const [statusFilter, setStatusFilter] = useState('open');
  const [reports, setReports] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const fetchReports = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams();
      params.set('limit', String(LIMIT));
      if (statusFilter) params.set('status_filter', statusFilter);
      const res = await api.get(`/api/cpo/plug-reports?${params.toString()}`);
      setReports(Array.isArray(res) ? res : res?.items || []);
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, [statusFilter]);

  useEffect(() => {
    fetchReports();
  }, [fetchReports]);

  // ---- Best-effort plug-name enrichment ------------------------------------
  const [plugMap, setPlugMap] = useState({});

  useEffect(() => {
    let cancelled = false;
    api
      .get('/api/cpo/plugs')
      .then((res) => {
        if (cancelled) return;
        const items = Array.isArray(res) ? res : res?.items || [];
        setPlugMap(Object.fromEntries(items.map((p) => [p.id, p])));
      })
      .catch(() => {
        // Best-effort only — the table falls back to a raw plug id.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // ---- Resolve (acknowledge / resolve), both driven through ConfirmDialog --
  const [resolveTarget, setResolveTarget] = useState(null);
  const [resolveAction, setResolveAction] = useState(null); // 'acknowledge' | 'resolve'
  const [resolveOpen, setResolveOpen] = useState(false);
  const [resolveBusy, setResolveBusy] = useState(false);
  const [resolveError, setResolveError] = useState('');
  const [resolveNote, setResolveNote] = useState('');

  const openAcknowledge = (report) => {
    setResolveTarget(report);
    setResolveAction('acknowledge');
    setResolveError('');
    setResolveNote('');
    setResolveOpen(true);
  };

  const openResolve = (report) => {
    setResolveTarget(report);
    setResolveAction('resolve');
    setResolveError('');
    setResolveNote('');
    setResolveOpen(true);
  };

  const closeResolve = () => {
    if (!resolveBusy) setResolveOpen(false);
  };

  const confirmResolve = async () => {
    if (!resolveTarget) return;
    setResolveError('');
    setResolveBusy(true);
    try {
      const body = { action: resolveAction };
      if (resolveNote.trim()) body.note = resolveNote.trim();
      await api.post(`/api/cpo/plug-reports/${resolveTarget.id}/resolve`, body);
      setResolveOpen(false);
      toast.ok(resolveAction === 'resolve' ? 'Report resolved.' : 'Report acknowledged.');
      await fetchReports();
    } catch (err) {
      setResolveError(apiErrorCopy(err));
    } finally {
      setResolveBusy(false);
    }
  };

  const columns = [
    {
      key: 'plug_id',
      label: 'Charger',
      render: (r) => plugMap[r.plug_id]?.name || `Charger #${r.plug_id}`,
    },
    {
      key: 'category',
      label: 'Category',
      render: (r) => plugReportCategoryLabel(r.category),
    },
    { key: 'description', label: 'Description' },
    {
      key: 'status',
      label: 'Status',
      render: (r) => (
        <span className="stack-sm">
          <span className={`badge ${STATUS_BADGE[r.status] || 'badge-info'}`}>{plugReportStatusLabel(r.status)}</span>
          {r.status !== 'open' && r.resolution_note && (
            <span className="text-3 text-xs">{r.resolution_note}</span>
          )}
        </span>
      ),
    },
    { key: 'created_at', label: 'Filed', render: (r) => formatDateTime(r.created_at) },
    {
      key: 'actions',
      label: 'Actions',
      render: (r) =>
        r.status === 'resolved' ? (
          <span aria-hidden="true">—</span>
        ) : (
          <span className="row plug-reports-row-actions">
            {r.status === 'open' && (
              <button type="button" className="btn btn-quiet btn-sm" onClick={() => openAcknowledge(r)}>
                Acknowledge
              </button>
            )}
            <button type="button" className="btn btn-primary btn-sm" onClick={() => openResolve(r)}>
              Resolve
            </button>
          </span>
        ),
    },
  ];

  return (
    <CpoLayout>
      <PageHeader
        title="Plug reports"
        sub="Problems drivers have flagged on your chargers — no session or refund attached."
      />

      <Tabs
        tabs={TABS}
        active={statusFilter}
        onChange={setStatusFilter}
        ariaLabel="Filter plug reports by status"
      >
        <DataTable
          columns={columns}
          rows={reports}
          loading={loading}
          error={error}
          onRetry={fetchReports}
          emptyIcon={Flag}
          emptyTitle={statusFilter === 'open' ? 'No open reports' : 'No reports'}
          emptyBody={
            statusFilter === 'open'
              ? "Nice and quiet — nothing's been flagged right now."
              : 'Driver-flagged charger problems show up here.'
          }
          collapse
        />
      </Tabs>

      {/* Resolve: acknowledge (no close) / resolve (closes it out) */}
      <ConfirmDialog
        open={resolveOpen}
        onClose={closeResolve}
        onConfirm={confirmResolve}
        title={resolveAction === 'resolve' ? 'Resolve report' : 'Acknowledge report'}
        confirmLabel={resolveAction === 'resolve' ? 'Mark resolved' : 'Mark acknowledged'}
        tone="primary"
        busy={resolveBusy}
        body={
          <div className="stack-sm">
            <p className="text-2">
              <strong>{plugMap[resolveTarget?.plug_id]?.name || `Charger #${resolveTarget?.plug_id}`}</strong>
              {' — '}
              {plugReportCategoryLabel(resolveTarget?.category)}: {resolveTarget?.description}
            </p>

            <div className="field">
              <label className="field-label" htmlFor="plug-report-resolve-note">
                Note (optional)
              </label>
              <textarea
                id="plug-report-resolve-note"
                className="textarea"
                value={resolveNote}
                onChange={(e) => setResolveNote(e.target.value)}
                placeholder="Add context for your records (optional)…"
              />
            </div>

            {resolveError && <p className="field-error">{resolveError}</p>}
          </div>
        }
      />
    </CpoLayout>
  );
}
