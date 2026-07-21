/**
 * Activity (redesign v3, C6) — the driver's charging history + issue
 * reports, replacing the old History.jsx (which mixed in the wallet ledger —
 * that moved to Wallet.jsx).
 *
 * Two tabs:
 * - Charging sessions: GET /api/sessions/history?limit&offset. The backend
 *   is landing the new `{total, items}` paginated shape in parallel — this
 *   page also accepts the legacy bare array and simply omits the pager when
 *   there's no `total` to page against.
 * - Issue reports: GET /api/sessions/disputes/my (a plain array). Renders
 *   ErrorState-with-retry (never a fake empty list) until the backend lands
 *   it.
 *
 * Row click on either tab opens a self-contained session-detail modal (GET
 * /api/sessions/{id} — the same receipt-shape field set SessionReceipt
 * renders, but this markup/CSS is its own copy, not an import from C4) with
 * the GST-invoice fetch and the "Report an issue" hand-off to DisputeModal.
 */
import { useCallback, useEffect, useState } from 'react';
import { Receipt, FileWarning } from 'lucide-react';

import { PageHeader, Tabs, DataTable, Modal, Money, useToast } from '../components/ui';
import DisputeModal from '../components/DisputeModal';
import api from '../api/client';
import { useConfig } from '../contexts/ConfigContext';
import { formatKwh, formatKw, formatDuration } from '../utils/money';
import { sessionStatusLabel, disputeStatusLabel, stopReasonCopy } from '../utils/statusCopy';
import './Activity.css';

const SESSIONS_LIMIT = 20;
const BILLED_STATUSES = ['completed', 'paid', 'billed'];

const SESSION_STATUS_TONE = {
  active: 'badge-info',
  completed: 'badge-ok',
  paid: 'badge-ok',
  billed: 'badge-ok',
  cancelled: 'badge-danger',
  orphaned: 'badge-warn',
};

const DISPUTE_STATUS_TONE = {
  open: 'badge-warn',
  approved: 'badge-ok',
  rejected: 'badge-danger',
};

const formatDate = (isoString) => (isoString ? new Date(isoString).toLocaleString() : '—');

const canInvoiceSession = (s) =>
  s?.session_id != null && Number(s.coins_spent) > 0 && BILLED_STATUSES.includes(s.status);

const canDisputeSession = (s) => BILLED_STATUSES.includes(s?.status);

function Row({ label, children }) {
  return (
    <div className="activity-detail-row">
      <dt className="text-3 text-sm">{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

/** Self-contained session-detail modal — invoice + dispute hand-off. */
function SessionDetailModal({ open, session, rate, onClose, onDispute }) {
  const toast = useToast();
  const [invoiceBusy, setInvoiceBusy] = useState(false);

  if (!open || !session) return null;

  const {
    session_id, plug_id, plug_name, status, energy_kwh, peak_power_w,
    price_per_kwh, coins_spent, shortfall_coins, balance_remaining,
    duration_sec, started_at, reason,
  } = session;

  const invoiceable = canInvoiceSession(session);
  const disputable = canDisputeSession(session);

  // The GST invoice endpoint returns printable HTML (not JSON) and needs the
  // Bearer header, so it can't go through the api client — raw-fetch the
  // blob and open it in a new tab.
  const viewInvoice = async () => {
    setInvoiceBusy(true);
    try {
      const base = import.meta.env.VITE_API_URL || '';
      const token = localStorage.getItem('amphive_token');
      const res = await fetch(`${base}/api/sessions/${session_id}/invoice?format=html`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) throw new Error("Couldn't load the invoice. Please try again.");
      const blob = await res.blob();
      window.open(URL.createObjectURL(blob), '_blank');
    } catch (err) {
      toast.error(err?.message || "Couldn't load the invoice. Please try again.");
    } finally {
      setInvoiceBusy(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Session detail"
      size="sm"
      footer={
        <>
          <button type="button" className="btn btn-quiet" onClick={onClose}>
            Close
          </button>
          {invoiceable && (
            <button type="button" className="btn btn-quiet" onClick={viewInvoice} disabled={invoiceBusy}>
              {invoiceBusy ? 'Opening…' : 'Download GST invoice'}
            </button>
          )}
          {disputable && (
            <button type="button" className="btn btn-primary" onClick={() => onDispute(session_id)}>
              Report an issue
            </button>
          )}
        </>
      }
    >
      <div className="activity-detail">
        <p className="text-3 text-sm">
          {plug_name || (plug_id != null ? `Plug #${plug_id}` : 'Charger')}
          {started_at && ` · ${new Date(started_at).toLocaleString()}`}
        </p>
        <span className={`badge ${SESSION_STATUS_TONE[status] || 'badge-info'}`}>
          {sessionStatusLabel(status)}
        </span>

        <dl className="activity-detail-rows">
          <Row label="Energy delivered">
            <span className="num">{formatKwh(energy_kwh ?? 0)}</span>
          </Row>
          <Row label="Duration">
            <span className="num">{formatDuration(duration_sec)}</span>
          </Row>
          {peak_power_w != null && (
            <Row label="Peak power">
              <span className="num">{formatKw(peak_power_w)}</span>
            </Row>
          )}
          {price_per_kwh != null && (
            <Row label="Rate">
              <span className="num">
                <Money coins={price_per_kwh} rate={rate} />
                /kWh
              </span>
            </Row>
          )}
          <Row label="Charged">
            <span className="num activity-debit">
              −<Money coins={coins_spent ?? 0} rate={rate} />
            </span>
          </Row>
          {balance_remaining != null && (
            <Row label="Balance after">
              <Money coins={balance_remaining} rate={rate} />
            </Row>
          )}
          {shortfall_coins > 0 && (
            <Row label="Couldn't be collected">
              <span className="num activity-shortfall">
                <Money coins={shortfall_coins} rate={rate} />
              </span>
            </Row>
          )}
        </dl>

        {reason && <p className="text-3 text-sm">{stopReasonCopy(reason)}</p>}
      </div>
    </Modal>
  );
}

export default function Activity() {
  const { coin_inr_rate: rate = 1 } = useConfig();
  const toast = useToast();
  const [tab, setTab] = useState('sessions');

  // ---- Charging sessions -----------------------------------------------
  const [sessions, setSessions] = useState([]);
  const [sessionsTotal, setSessionsTotal] = useState(null); // null → legacy bare array, no pager
  const [sessionsOffset, setSessionsOffset] = useState(0);
  const [sessionsLoading, setSessionsLoading] = useState(true);
  const [sessionsError, setSessionsError] = useState(null);

  const fetchSessions = useCallback(async (offset = 0) => {
    setSessionsLoading(true);
    setSessionsError(null);
    try {
      const data = await api.get(`/api/sessions/history?limit=${SESSIONS_LIMIT}&offset=${offset}`);
      if (Array.isArray(data)) {
        setSessions(data);
        setSessionsTotal(null);
        setSessionsOffset(0);
      } else {
        setSessions(data?.items || []);
        setSessionsTotal(typeof data?.total === 'number' ? data.total : null);
        setSessionsOffset(offset);
      }
    } catch (err) {
      setSessionsError(err);
    } finally {
      setSessionsLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchSessions(0);
  }, [fetchSessions]);

  // ---- Issue reports ------------------------------------------------------
  const [disputes, setDisputes] = useState([]);
  const [disputesLoading, setDisputesLoading] = useState(true);
  const [disputesError, setDisputesError] = useState(null);

  const fetchDisputes = useCallback(async () => {
    setDisputesLoading(true);
    setDisputesError(null);
    try {
      const data = await api.get('/api/sessions/disputes/my');
      setDisputes(Array.isArray(data) ? data : data?.items || []);
    } catch (err) {
      setDisputesError(err);
    } finally {
      setDisputesLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchDisputes();
  }, [fetchDisputes]);

  // ---- Session detail modal ----------------------------------------------
  const [detail, setDetail] = useState(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);

  const openSessionDetail = useCallback(
    async (sessionId) => {
      if (sessionId == null || detailLoading) return;
      setDetailLoading(true);
      try {
        const data = await api.get(`/api/sessions/${sessionId}`);
        setDetail(data);
        setDetailOpen(true);
      } catch {
        toast.error('Details unavailable.');
      } finally {
        setDetailLoading(false);
      }
    },
    [detailLoading, toast]
  );

  const closeDetail = () => setDetailOpen(false);

  // ---- Dispute modal ------------------------------------------------------
  const [disputeSessionId, setDisputeSessionId] = useState(null);

  const openDispute = (sessionId) => {
    setDetailOpen(false);
    setDisputeSessionId(sessionId);
  };

  const closeDispute = () => setDisputeSessionId(null);

  const handleDisputeSubmitted = () => {
    setDisputeSessionId(null);
    fetchDisputes();
  };

  const sessionColumns = [
    { key: 'started_at', label: 'Date', render: (s) => formatDate(s.started_at) },
    {
      key: 'plug_name',
      label: 'Charger',
      render: (s) => s.plug_name || (s.plug_id != null ? `Plug #${s.plug_id}` : '—'),
    },
    { key: 'energy_kwh', label: 'Energy', num: true, render: (s) => formatKwh(s.energy_kwh) },
    {
      key: 'coins_spent',
      label: 'Cost',
      num: true,
      render: (s) => <Money coins={s.coins_spent} rate={rate} />,
    },
    {
      key: 'status',
      label: 'Status',
      render: (s) => (
        <span className={`badge ${SESSION_STATUS_TONE[s.status] || 'badge-info'}`}>
          {sessionStatusLabel(s.status)}
        </span>
      ),
    },
  ];

  const disputeColumns = [
    { key: 'created_at', label: 'Date', render: (d) => formatDate(d.created_at) },
    {
      key: 'session_id',
      label: 'Session',
      render: (d) => (d.session_id != null ? `Session #${d.session_id}` : '—'),
    },
    {
      key: 'reason',
      label: 'Reason',
      render: (d) => <span className="activity-reason-cell">{d.reason}</span>,
    },
    {
      key: 'status',
      label: 'Status',
      render: (d) => (
        <span className={`badge ${DISPUTE_STATUS_TONE[d.status] || 'badge-info'}`}>
          {disputeStatusLabel(d.status)}
        </span>
      ),
    },
    {
      key: 'resolution',
      label: 'Resolution',
      render: (d) =>
        d.status === 'open' ? (
          <span className="text-3 text-sm">Awaiting review</span>
        ) : (
          <span className="text-2 text-sm">
            {d.resolution_note || 'No note added'}
            {d.refund_coins > 0 && (
              <>
                {' · '}
                <Money coins={d.refund_coins} rate={rate} /> refunded
              </>
            )}
          </span>
        ),
    },
  ];

  return (
    <main className="page activity-page">
      <PageHeader title="Activity" sub="Your charging sessions and issue reports." />

      <Tabs
        tabs={[
          { id: 'sessions', label: 'Charging sessions' },
          { id: 'disputes', label: 'Issue reports', count: disputes.length || undefined },
        ]}
        active={tab}
        onChange={setTab}
        ariaLabel="Activity"
      />

      <div className="activity-tabpanel">
        {tab === 'sessions' ? (
          <DataTable
            columns={sessionColumns}
            rows={sessions}
            loading={sessionsLoading}
            error={sessionsError}
            onRetry={() => fetchSessions(sessionsOffset)}
            emptyIcon={Receipt}
            emptyTitle="No charging sessions yet"
            emptyBody="Start a charge from the map or dashboard — it'll show up here."
            onRowClick={(row) => openSessionDetail(row.id)}
            pagination={
              sessionsTotal != null
                ? {
                    total: sessionsTotal,
                    offset: sessionsOffset,
                    limit: SESSIONS_LIMIT,
                    onPage: (offset) => fetchSessions(offset),
                  }
                : undefined
            }
            collapse
          />
        ) : (
          <DataTable
            columns={disputeColumns}
            rows={disputes}
            loading={disputesLoading}
            error={disputesError}
            onRetry={fetchDisputes}
            emptyIcon={FileWarning}
            emptyTitle="No issue reports"
            emptyBody="Report a problem from a session's detail view and it'll show up here."
            onRowClick={(row) => openSessionDetail(row.session_id)}
            collapse
          />
        )}
      </div>

      <SessionDetailModal
        open={detailOpen}
        session={detail}
        rate={rate}
        onClose={closeDetail}
        onDispute={openDispute}
      />

      <DisputeModal
        open={disputeSessionId != null}
        onClose={closeDispute}
        sessionId={disputeSessionId}
        onSubmitted={handleDisputeSubmitted}
      />
    </main>
  );
}
