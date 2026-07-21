/**
 * CpoEarnings (redesign v3, D7) — the operator's earnings + payout ledger.
 *
 * Payouts are RECORDS only: requesting one snapshots the tenant's unsettled
 * earnings (watermark -> now); the money itself moves outside AmpHive
 * (bank/UPI) and the platform operator's admin console marks it paid once
 * that transfer has happened — this page never shows a "Mark paid" action.
 *
 * Data: GET /api/cpo/earnings (lifetime + unsettled legs), GET
 * /api/cpo/payouts (bare array, newest request first).
 * Mutations: POST /api/cpo/payouts (request), POST
 * /api/cpo/payouts/{id}/cancel (owner or admin; only while REQUESTED).
 */

import { useCallback, useEffect, useState } from 'react';
import { Wallet } from 'lucide-react';
import CpoLayout from '../../components/CpoLayout';
import { PageHeader, DataTable, ConfirmDialog, Money, ErrorState, useToast } from '../../components/ui';
import api from '../../api/client';
import { useConfig } from '../../contexts/ConfigContext';
import { formatINR } from '../../utils/money';
import { apiErrorCopy, payoutStatusLabel } from '../../utils/statusCopy';
import './CpoEarnings.css';

const PAYOUT_BADGE = {
  requested: 'badge-warn',
  paid: 'badge-ok',
  cancelled: 'badge-danger',
};

const formatDate = (iso) =>
  iso
    ? new Date(iso).toLocaleDateString('en-IN', { year: 'numeric', month: 'short', day: 'numeric' })
    : '—';

const formatDateTime = (iso) =>
  iso
    ? new Date(iso).toLocaleString('en-IN', {
        year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
      })
    : '—';

function EarningsCard({ title, sub, leg, feePct, rate, highlight }) {
  return (
    <div className="card earnings-card">
      <h2 className="earnings-card-title">{title}</h2>
      {sub && <p className="text-3 text-sm">{sub}</p>}
      <div className="earnings-stat-row">
        <div className="earnings-stat">
          <span className="earnings-stat-label">Gross</span>
          <span className="earnings-stat-value">
            <Money coins={leg?.gross_coins} rate={rate} showCoins />
          </span>
        </div>
        <div className="earnings-stat">
          <span className="earnings-stat-label">
            Platform fee{feePct != null ? ` (${feePct}%)` : ''}
          </span>
          <span className="earnings-stat-value">
            <Money coins={leg?.platform_fee_coins} rate={rate} showCoins />
          </span>
        </div>
        <div className="earnings-stat">
          <span className="earnings-stat-label">Net (yours)</span>
          <span className={`earnings-stat-value${highlight ? ' earnings-stat-value--net' : ''}`}>
            <Money coins={leg?.net_coins} rate={rate} showCoins />
          </span>
        </div>
      </div>
    </div>
  );
}

export default function CpoEarnings() {
  const { coin_inr_rate: rate = 1 } = useConfig();
  const toast = useToast();

  const [earnings, setEarnings] = useState(null);
  const [payouts, setPayouts] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const fetchData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [earningsRes, payoutsRes] = await Promise.all([
        api.get('/api/cpo/earnings'),
        api.get('/api/cpo/payouts'),
      ]);
      setEarnings(earningsRes);
      setPayouts(Array.isArray(payoutsRes) ? payoutsRes : payoutsRes?.items || []);
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  const unsettled = earnings?.unsettled;
  const hasUnsettled = Number(unsettled?.net_coins || 0) > 0;
  const alreadyPending = payouts.some((p) => p.status === 'requested');
  const disabledReason = alreadyPending
    ? 'A payout request is already pending.'
    : !hasUnsettled
      ? "There's nothing unsettled to pay out yet."
      : '';

  // ---- Request payout ----------------------------------------------------
  const [requestOpen, setRequestOpen] = useState(false);
  const [requestBusy, setRequestBusy] = useState(false);
  const [requestError, setRequestError] = useState('');

  const openRequest = () => {
    setRequestError('');
    setRequestOpen(true);
  };

  const confirmRequest = async () => {
    setRequestBusy(true);
    setRequestError('');
    try {
      await api.post('/api/cpo/payouts', {});
      setRequestOpen(false);
      toast.ok('Payout requested.');
      await fetchData();
    } catch (err) {
      setRequestError(apiErrorCopy(err));
    } finally {
      setRequestBusy(false);
    }
  };

  // ---- Cancel a REQUESTED payout ------------------------------------------
  const [cancelTarget, setCancelTarget] = useState(null);
  const [cancelBusy, setCancelBusy] = useState(false);
  const [cancelError, setCancelError] = useState('');

  const openCancel = (payout) => {
    setCancelError('');
    setCancelTarget(payout);
  };

  const confirmCancel = async () => {
    if (!cancelTarget) return;
    setCancelBusy(true);
    setCancelError('');
    try {
      await api.post(`/api/cpo/payouts/${cancelTarget.id}/cancel`, {});
      setCancelTarget(null);
      toast.ok('Payout cancelled.');
      await fetchData();
    } catch (err) {
      setCancelError(apiErrorCopy(err));
    } finally {
      setCancelBusy(false);
    }
  };

  const columns = [
    { key: 'id', label: 'ID', render: (p) => <span className="text-3">#{p.id}</span> },
    {
      key: 'period',
      label: 'Period',
      render: (p) => `${formatDate(p.period_start)} → ${formatDate(p.period_end)}`,
    },
    { key: 'gross_coins', label: 'Gross', num: true, render: (p) => <Money coins={p.gross_coins} rate={rate} /> },
    { key: 'platform_fee_coins', label: 'Fee', num: true, render: (p) => <Money coins={p.platform_fee_coins} rate={rate} /> },
    {
      key: 'net_coins',
      label: 'Net',
      num: true,
      render: (p) => <span className="earnings-stat-value--net"><Money coins={p.net_coins} rate={rate} /></span>,
    },
    {
      key: 'status',
      label: 'Status',
      render: (p) => (
        <span className={`badge ${PAYOUT_BADGE[p.status] || 'badge-info'}`}>{payoutStatusLabel(p.status)}</span>
      ),
    },
    { key: 'requested_at', label: 'Requested', render: (p) => formatDateTime(p.requested_at) },
    { key: 'paid_at', label: 'Paid', render: (p) => formatDateTime(p.paid_at) },
    {
      key: 'actions',
      label: 'Actions',
      render: (p) =>
        p.status === 'requested' ? (
          <button type="button" className="btn btn-quiet btn-sm" onClick={() => openCancel(p)}>
            Cancel
          </button>
        ) : (
          <span aria-hidden="true">—</span>
        ),
    },
  ];

  return (
    <CpoLayout>
      <PageHeader
        title="Earnings & payouts"
        sub="What your chargers have earned, and settlement of what you're owed."
        actions={
          <div className="earnings-request-action">
            <button
              type="button"
              className="btn btn-primary"
              onClick={openRequest}
              disabled={loading || Boolean(disabledReason)}
            >
              Request payout
            </button>
            {disabledReason && <span className="text-3 text-sm">{disabledReason}</span>}
          </div>
        }
      />

      <div className="banner banner-info earnings-settlement-banner">
        Payouts are records — the transfer itself happens outside AmpHive (bank
        transfer / UPI). Requesting one snapshots what you're owed; the
        platform operator marks it paid once the transfer is made.
      </div>

      {error ? (
        <ErrorState error={error} onRetry={fetchData} />
      ) : (
        <>
          {loading ? (
            <div className="earnings-card-grid" aria-hidden="true">
              {[1, 2].map((i) => (
                <div key={i} className="card earnings-card">
                  <div className="skeleton skeleton-title" />
                  <div className="earnings-stat-row">
                    {[1, 2, 3].map((j) => (
                      <div key={j} className="skeleton skeleton-text" />
                    ))}
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div className="earnings-card-grid">
              <EarningsCard
                title="Lifetime earnings"
                sub="All completed sessions to date"
                leg={earnings?.lifetime}
                feePct={earnings?.platform_fee_pct}
                rate={rate}
              />
              <EarningsCard
                title="Unsettled earnings"
                sub={`Window: ${formatDate(unsettled?.period_start)} → ${formatDate(unsettled?.period_end)}`}
                leg={unsettled}
                feePct={earnings?.platform_fee_pct}
                rate={rate}
                highlight
              />
            </div>
          )}

          <p className="text-3 text-sm earnings-coin-note">
            Coins are AmpHive's internal ledger unit — 1 coin = {formatINR(rate)}.
          </p>

          <h2 className="earnings-section-title">Payout history</h2>

          <DataTable
            columns={columns}
            rows={payouts}
            loading={loading}
            error={null}
            emptyIcon={Wallet}
            emptyTitle="No payouts yet"
            emptyBody="Once your chargers have unsettled earnings, request a payout to snapshot what you're owed. The transfer itself happens outside the app."
            collapse
          />
        </>
      )}

      <ConfirmDialog
        open={requestOpen}
        onClose={() => !requestBusy && setRequestOpen(false)}
        onConfirm={confirmRequest}
        title="Request payout"
        confirmLabel="Request payout"
        tone="primary"
        busy={requestBusy}
        busyLabel="Requesting…"
        body={
          <div className="stack-sm">
            <p className="text-2">
              This snapshots your unsettled earnings
              {hasUnsettled && (
                <>
                  {' '}— net <strong><Money coins={unsettled?.net_coins} rate={rate} showCoins /></strong>
                </>
              )}
              {' '}into a payout request for the platform operator to settle.
            </p>
            <p className="text-3 text-sm">
              The transfer happens outside the app (bank/UPI). Only one request
              can be pending at a time; you can cancel it later if needed.
            </p>
            {requestError && <p className="field-error">{requestError}</p>}
          </div>
        }
      />

      <ConfirmDialog
        open={Boolean(cancelTarget)}
        onClose={() => !cancelBusy && setCancelTarget(null)}
        onConfirm={confirmCancel}
        title="Cancel payout"
        confirmLabel="Cancel payout"
        tone="danger"
        busy={cancelBusy}
        busyLabel="Cancelling…"
        body={
          <div className="stack-sm">
            <p className="text-2">
              Cancel payout <strong>#{cancelTarget?.id}</strong> (net{' '}
              <Money coins={cancelTarget?.net_coins} rate={rate} showCoins />)? Its
              earnings window is freed and will be covered by your next payout
              request.
            </p>
            {cancelError && <p className="field-error">{cancelError}</p>}
          </div>
        }
      />
    </CpoLayout>
  );
}
