/**
 * CpoDisputes (redesign v3, D7) — driver-filed disputes on finished sessions.
 * Approve credits the driver's wallet (defaulting to the full session cost,
 * capped at it) and writes a REFUND ledger row; reject just records a note.
 * A dispute resolves exactly once — the backend 409s if it's already closed.
 *
 * Data: GET /api/cpo/disputes[?status_filter=open|approved|rejected] (bare
 * list, newest first). Session cost + driver email aren't on that shape, so
 * this page enriches rows best-effort from GET
 * /api/cpo/analytics/sessions?days=3650&limit=200 (same tenant-scoped
 * endpoint CpoSessions uses) — a failed or partial enrichment never blocks
 * the table, it just leaves those cells as "—". The resolve dialog re-fetches
 * the authoritative session cost fresh via GET /api/sessions/{id} when it
 * opens, and "View session" reuses that same endpoint for a read-only detail
 * modal (CpoSessions' pattern).
 *
 * Mutation: POST /api/cpo/disputes/{id}/resolve { action, refund_coins?, note? }.
 */

import { useCallback, useEffect, useState } from 'react';
import { Scale } from 'lucide-react';
import CpoLayout from '../../components/CpoLayout';
import { PageHeader, DataTable, Tabs, Modal, ConfirmDialog, Money, useToast } from '../../components/ui';
import api from '../../api/client';
import { useConfig } from '../../contexts/ConfigContext';
import { coinsToINR, formatINR, formatKwh, formatDuration } from '../../utils/money';
import { disputeStatusLabel, sessionStatusLabel, apiErrorCopy } from '../../utils/statusCopy';
import './CpoDisputes.css';

const LIMIT = 200;
// "Full history" for the best-effort session enrichment lookup below — disputes
// are filed against already-finished sessions, which can predate any fixed
// recent window, so this intentionally reaches back further than the
// Sessions page's own day-range filters.
const ENRICHMENT_DAYS = 3650;

const STATUS_BADGE = {
  open: 'badge-warn',
  approved: 'badge-ok',
  rejected: 'badge-danger',
};

const TABS = [
  { id: 'open', label: 'Open' },
  { id: 'approved', label: 'Approved' },
  { id: 'rejected', label: 'Rejected' },
  { id: '', label: 'All' },
];

const formatDateTime = (iso) =>
  iso
    ? new Date(iso).toLocaleString('en-IN', {
        month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
      })
    : '—';

function Row({ label, children }) {
  return (
    <div className="row-between">
      <span className="text-3 text-sm">{label}</span>
      <span>{children}</span>
    </div>
  );
}

export default function CpoDisputes() {
  const { coin_inr_rate: rate = 1 } = useConfig();
  const toast = useToast();

  const [statusFilter, setStatusFilter] = useState('open');
  const [disputes, setDisputes] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const fetchDisputes = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams();
      params.set('limit', String(LIMIT));
      if (statusFilter) params.set('status_filter', statusFilter);
      const res = await api.get(`/api/cpo/disputes?${params.toString()}`);
      setDisputes(Array.isArray(res) ? res : res?.items || []);
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, [statusFilter]);

  useEffect(() => {
    fetchDisputes();
  }, [fetchDisputes]);

  // ---- Best-effort session enrichment (cost + driver email + plug) --------
  const [sessionMap, setSessionMap] = useState({});

  useEffect(() => {
    let cancelled = false;
    api
      .get(`/api/cpo/analytics/sessions?days=${ENRICHMENT_DAYS}&limit=200`)
      .then((res) => {
        if (cancelled) return;
        const items = Array.isArray(res) ? res : res?.items || res?.sessions || [];
        setSessionMap(Object.fromEntries(items.map((s) => [s.id, s])));
      })
      .catch(() => {
        // Best-effort only — the table falls back to raw ids.
      });
    return () => {
      cancelled = true;
    };
    // Fetched once — this enrichment map covers sessions regardless of which
    // dispute status tab is active.
  }, []);

  // ---- Session detail modal (read-only, "View session") -------------------
  const [detail, setDetail] = useState(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);

  const openDetail = async (dispute) => {
    setDetailLoading(true);
    setDetailOpen(true);
    setDetail(null);
    try {
      const data = await api.get(`/api/sessions/${dispute.session_id}`);
      setDetail(data);
    } catch {
      toast.error('Session details unavailable.');
      setDetailOpen(false);
    } finally {
      setDetailLoading(false);
    }
  };

  // ---- Resolve (approve / reject), both driven through ConfirmDialog ------
  const [resolveTarget, setResolveTarget] = useState(null);
  const [resolveAction, setResolveAction] = useState(null); // 'approve' | 'reject'
  const [resolveOpen, setResolveOpen] = useState(false);
  const [resolveBusy, setResolveBusy] = useState(false);
  const [resolveError, setResolveError] = useState('');
  const [costLoading, setCostLoading] = useState(false);
  const [sessionCostCoins, setSessionCostCoins] = useState(null);
  const [refundInr, setRefundInr] = useState('');
  const [resolveNote, setResolveNote] = useState('');

  const openApprove = async (dispute) => {
    setResolveTarget(dispute);
    setResolveAction('approve');
    setResolveError('');
    setResolveNote('');
    setSessionCostCoins(null);
    setRefundInr('');
    setResolveOpen(true);
    setCostLoading(true);
    try {
      const sessionDetail = await api.get(`/api/sessions/${dispute.session_id}`);
      const cost = Number(sessionDetail.coins_spent || 0);
      setSessionCostCoins(cost);
      setRefundInr(cost > 0 ? coinsToINR(cost, rate).toFixed(2) : '');
    } catch {
      // Best-effort — the backend still enforces the real cap on submit.
    } finally {
      setCostLoading(false);
    }
  };

  const openReject = (dispute) => {
    setResolveTarget(dispute);
    setResolveAction('reject');
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

    if (resolveAction === 'reject' && !resolveNote.trim()) {
      setResolveError('A note is required to reject a dispute.');
      return;
    }

    let refundCoins;
    if (resolveAction === 'approve' && refundInr !== '') {
      const inrValue = Number(refundInr);
      if (Number.isNaN(inrValue) || inrValue <= 0) {
        setResolveError('Enter a refund amount greater than ₹0.');
        return;
      }
      const capInr = sessionCostCoins != null ? coinsToINR(sessionCostCoins, rate) : null;
      if (capInr != null && inrValue > capInr + 0.005) {
        setResolveError(`Refund can't exceed this session's cost (${formatINR(capInr)}).`);
        return;
      }
      refundCoins = Math.round((inrValue / (rate || 1)) * 100) / 100;
    }

    setResolveBusy(true);
    try {
      const body = { action: resolveAction };
      if (resolveAction === 'approve' && refundCoins != null) body.refund_coins = refundCoins;
      if (resolveNote.trim()) body.note = resolveNote.trim();
      await api.post(`/api/cpo/disputes/${resolveTarget.id}/resolve`, body);
      setResolveOpen(false);
      toast.ok(resolveAction === 'approve' ? 'Dispute approved and refunded.' : 'Dispute rejected.');
      await fetchDisputes();
    } catch (err) {
      setResolveError(apiErrorCopy(err));
    } finally {
      setResolveBusy(false);
    }
  };

  const columns = [
    {
      key: 'session_id',
      label: 'Session',
      render: (d) => {
        const s = sessionMap[d.session_id];
        return (
          <button type="button" className="btn btn-quiet btn-sm" onClick={() => openDetail(d)}>
            #{d.session_id}{s?.plug_name ? ` · ${s.plug_name}` : ''}
          </button>
        );
      },
    },
    {
      key: 'driver',
      label: 'Driver',
      render: (d) => sessionMap[d.session_id]?.user_email || `Driver #${d.driver_user_id}`,
    },
    { key: 'reason', label: 'Reason' },
    {
      key: 'session_cost',
      label: 'Session cost',
      num: true,
      render: (d) =>
        sessionMap[d.session_id] ? <Money coins={sessionMap[d.session_id].coins_spent} rate={rate} /> : '—',
    },
    {
      key: 'status',
      label: 'Status',
      render: (d) => (
        <span className="stack-sm">
          <span className={`badge ${STATUS_BADGE[d.status] || 'badge-info'}`}>{disputeStatusLabel(d.status)}</span>
          {d.status !== 'open' && (d.refund_coins || d.resolution_note) && (
            <span className="text-3 text-xs">
              {d.refund_coins ? <>Refunded <Money coins={d.refund_coins} rate={rate} />. </> : ''}
              {d.resolution_note || ''}
            </span>
          )}
        </span>
      ),
    },
    { key: 'created_at', label: 'Filed', render: (d) => formatDateTime(d.created_at) },
    {
      key: 'actions',
      label: 'Actions',
      render: (d) =>
        d.status === 'open' ? (
          <span className="row disputes-row-actions">
            <button type="button" className="btn btn-primary btn-sm" onClick={() => openApprove(d)}>
              Approve
            </button>
            <button type="button" className="btn btn-danger btn-sm" onClick={() => openReject(d)}>
              Reject
            </button>
          </span>
        ) : (
          <span aria-hidden="true">—</span>
        ),
    },
  ];

  return (
    <CpoLayout>
      <PageHeader title="Disputes" sub="Review and resolve driver disputes on finished sessions." />

      <Tabs
        tabs={TABS}
        active={statusFilter}
        onChange={setStatusFilter}
        ariaLabel="Filter disputes by status"
      />

      <DataTable
        columns={columns}
        rows={disputes}
        loading={loading}
        error={error}
        onRetry={fetchDisputes}
        emptyIcon={Scale}
        emptyTitle={statusFilter === 'open' ? 'No open disputes' : 'No disputes'}
        emptyBody={
          statusFilter === 'open'
            ? "Nice and quiet — nothing needs your review right now."
            : 'Driver disputes on finished sessions show up here.'
        }
        collapse
      />

      {/* Read-only session detail */}
      <Modal
        open={detailOpen}
        onClose={() => setDetailOpen(false)}
        title="Session detail"
        size="sm"
        footer={
          <button type="button" className="btn btn-quiet" onClick={() => setDetailOpen(false)}>
            Close
          </button>
        }
      >
        {detailLoading && <div className="stack-sm" aria-hidden="true">
          <div className="skeleton skeleton-text" />
          <div className="skeleton skeleton-text" />
          <div className="skeleton skeleton-text" />
        </div>}
        {detail && !detailLoading && (
          <div className="stack-sm">
            <Row label="Charger">{detail.plug_name}</Row>
            <Row label="Status">
              <span className="badge badge-info">{sessionStatusLabel(detail.status)}</span>
            </Row>
            <Row label="Started"><span className="num">{formatDateTime(detail.started_at)}</span></Row>
            <Row label="Ended"><span className="num">{formatDateTime(detail.ended_at)}</span></Row>
            <Row label="Duration"><span className="num">{formatDuration(detail.duration_sec)}</span></Row>
            <Row label="Energy"><span className="num">{formatKwh(detail.energy_kwh)}</span></Row>
            <Row label="Cost"><span className="num"><Money coins={detail.coins_spent} rate={rate} /></span></Row>
          </div>
        )}
      </Modal>

      {/* Resolve: approve (refund input) / reject (required note) */}
      <ConfirmDialog
        open={resolveOpen}
        onClose={closeResolve}
        onConfirm={confirmResolve}
        title={resolveAction === 'approve' ? 'Approve dispute' : 'Reject dispute'}
        confirmLabel={resolveAction === 'approve' ? 'Approve & refund' : 'Reject dispute'}
        tone={resolveAction === 'approve' ? 'primary' : 'danger'}
        busy={resolveBusy || costLoading}
        busyLabel={costLoading ? 'Loading session cost…' : 'Working…'}
        body={
          <div className="stack-sm">
            <p className="text-2">
              Session <strong>#{resolveTarget?.session_id}</strong>: {resolveTarget?.reason}
            </p>

            {resolveAction === 'approve' && (
              <div className="field">
                <label className="field-label" htmlFor="dispute-refund-amount">
                  Refund amount
                </label>
                <input
                  id="dispute-refund-amount"
                  className="input"
                  type="number"
                  min="0"
                  step="0.01"
                  value={refundInr}
                  disabled={costLoading}
                  onChange={(e) => setRefundInr(e.target.value)}
                  placeholder="Full session cost"
                />
                <span className="field-help">
                  {sessionCostCoins != null
                    ? `Up to ${formatINR(coinsToINR(sessionCostCoins, rate))} — this session's cost. Leave blank to refund the full amount.`
                    : "Leave blank to refund the driver's full session cost."}
                </span>
              </div>
            )}

            <div className="field">
              <label className="field-label" htmlFor="dispute-resolve-note">
                Note{resolveAction === 'reject' ? '' : ' (optional)'}
              </label>
              <textarea
                id="dispute-resolve-note"
                className="textarea"
                value={resolveNote}
                onChange={(e) => setResolveNote(e.target.value)}
                placeholder={
                  resolveAction === 'reject'
                    ? 'Explain why this dispute is being rejected…'
                    : 'Add context for your records (optional)…'
                }
                required={resolveAction === 'reject'}
              />
            </div>

            {resolveError && <p className="field-error">{resolveError}</p>}
          </div>
        }
      />
    </CpoLayout>
  );
}
