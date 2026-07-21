/**
 * CpoEarnings (redesign v3, D7) — the operator's earnings + payout ledger.
 *
 * Payouts are RECORDS only: requesting one snapshots the tenant's unsettled
 * earnings (watermark -> now); the money itself moves outside AmpHive
 * (bank/UPI) and the platform operator's admin console marks it paid once
 * that transfer has happened — this page never shows a "Mark paid" action.
 *
 * Data: GET /api/cpo/earnings (lifetime + unsettled legs, plus the offline
 * top-up pool figure), GET /api/cpo/payouts (bare array, newest request
 * first), GET /api/cpo/topups (paginated {total,items}).
 * Mutations: POST /api/cpo/payouts (request), POST
 * /api/cpo/payouts/{id}/cancel (owner or admin; only while REQUESTED),
 * POST /api/cpo/topups (credit a driver's wallet from cash collected
 * offline — capped at the tenant's available top-up pool, a two-step
 * Modal-then-ConfirmDialog flow stating the money math plainly).
 */

import { useCallback, useEffect, useState } from 'react';
import { HandCoins, Wallet } from 'lucide-react';
import CpoLayout from '../../components/CpoLayout';
import Modal from '../../components/ui/Modal';
import { PageHeader, DataTable, ConfirmDialog, Money, ErrorState, useToast } from '../../components/ui';
import api from '../../api/client';
import { useConfig } from '../../contexts/ConfigContext';
import { coinsToINR, formatINR } from '../../utils/money';
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

  // ---- Offline (cash) top-ups ---------------------------------------------
  const [topups, setTopups] = useState([]);
  const [topupsTotal, setTopupsTotal] = useState(0);
  const [topupsOffset, setTopupsOffset] = useState(0);
  const [topupsLoading, setTopupsLoading] = useState(true);
  const [topupsError, setTopupsError] = useState(null);
  const TOPUPS_PAGE_SIZE = 20;

  const fetchTopups = useCallback(async (offset = 0) => {
    setTopupsLoading(true);
    setTopupsError(null);
    try {
      const data = await api.get(`/api/cpo/topups?limit=${TOPUPS_PAGE_SIZE}&offset=${offset}`);
      setTopups(data?.items || []);
      setTopupsTotal(data?.total || 0);
      setTopupsOffset(offset);
    } catch (err) {
      setTopupsError(err);
    } finally {
      setTopupsLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchTopups(0);
  }, [fetchTopups]);

  const pool = earnings?.topup_pool;
  const availablePool = Number(pool?.available_coins || 0);

  const [creditOpen, setCreditOpen] = useState(false);
  const [creditForm, setCreditForm] = useState({ driver_email: '', amount_coins: '', note: '' });
  const [creditFormError, setCreditFormError] = useState('');
  const [creditConfirmOpen, setCreditConfirmOpen] = useState(false);
  const [creditBusy, setCreditBusy] = useState(false);
  const [creditError, setCreditError] = useState('');

  const openCredit = () => {
    setCreditForm({ driver_email: '', amount_coins: '', note: '' });
    setCreditFormError('');
    setCreditOpen(true);
  };

  const submitCreditForm = (e) => {
    e.preventDefault();
    const email = creditForm.driver_email.trim();
    const amount = Number(creditForm.amount_coins);
    if (!email) {
      setCreditFormError('Enter the driver’s email.');
      return;
    }
    if (!amount || amount <= 0) {
      setCreditFormError('Enter an amount greater than 0.');
      return;
    }
    if (amount > availablePool) {
      setCreditFormError(
        `That's more than your available pool (${formatINR(coinsToINR(availablePool, rate))}). Reduce the amount.`
      );
      return;
    }
    setCreditFormError('');
    setCreditOpen(false);
    setCreditError('');
    setCreditConfirmOpen(true);
  };

  const confirmCredit = async () => {
    setCreditBusy(true);
    setCreditError('');
    try {
      await api.post('/api/cpo/topups', {
        driver_email: creditForm.driver_email.trim(),
        amount_coins: Number(creditForm.amount_coins),
        note: creditForm.note.trim() || undefined,
      });
      setCreditConfirmOpen(false);
      toast.ok('Driver credited.');
      await fetchData();
      await fetchTopups(0);
    } catch (err) {
      setCreditError(apiErrorCopy(err));
    } finally {
      setCreditBusy(false);
    }
  };

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

  const topupColumns = [
    {
      key: 'driver',
      label: 'Driver',
      render: (t) => t.driver_email || `#${t.driver_user_id ?? '—'}`,
    },
    { key: 'amount_coins', label: 'Amount', num: true, render: (t) => <Money coins={t.amount_coins} rate={rate} showCoins /> },
    { key: 'note', label: 'Note', render: (t) => t.note || <span className="text-3">—</span> },
    { key: 'actor', label: 'Credited by', render: (t) => t.actor_email || '—' },
    { key: 'created_at', label: 'Date', render: (t) => formatDateTime(t.created_at) },
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

          <div className="card earnings-topup-card">
            <div className="earnings-topup-header">
              <div>
                <h2 className="earnings-card-title">Offline top-ups</h2>
                <p className="text-3 text-sm">
                  Credit collected in cash — it comes out of your unsettled earnings, it's
                  never created from nothing.
                </p>
              </div>
              <button
                type="button"
                className="btn btn-primary"
                onClick={openCredit}
                disabled={loading || availablePool <= 0}
              >
                <HandCoins size={16} aria-hidden="true" />
                Credit a driver
              </button>
            </div>
            <div className="earnings-stat-row">
              <div className="earnings-stat">
                <span className="earnings-stat-label">Available to top up</span>
                <span className="earnings-stat-value earnings-stat-value--net">
                  <Money coins={availablePool} rate={rate} showCoins />
                </span>
              </div>
              {Number(pool?.already_issued_coins || 0) > 0 && (
                <div className="earnings-stat">
                  <span className="earnings-stat-label">Already credited this window</span>
                  <span className="earnings-stat-value">
                    <Money coins={pool.already_issued_coins} rate={rate} showCoins />
                  </span>
                </div>
              )}
            </div>
            {availablePool <= 0 && (
              <p className="text-3 text-sm">
                There's nothing unsettled to top up with right now.
              </p>
            )}
          </div>

          <h2 className="earnings-section-title">Top-up history</h2>

          <DataTable
            columns={topupColumns}
            rows={topups}
            loading={topupsLoading}
            error={topupsError}
            onRetry={() => fetchTopups(topupsOffset)}
            emptyIcon={HandCoins}
            emptyTitle="No offline top-ups yet"
            emptyBody="Credit a driver's wallet for a cash payment collected at the charger — it draws from your own unsettled earnings."
            pagination={{
              total: topupsTotal,
              offset: topupsOffset,
              limit: TOPUPS_PAGE_SIZE,
              onPage: fetchTopups,
            }}
            collapse
          />

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

      <Modal open={creditOpen} onClose={() => setCreditOpen(false)} title="Credit a driver">
        <form className="stack" onSubmit={submitCreditForm}>
          <div className="field">
            <label className="field-label" htmlFor="topup-driver-email">
              Driver's email
            </label>
            <input
              id="topup-driver-email"
              type="email"
              className="input"
              value={creditForm.driver_email}
              onChange={(e) => setCreditForm((f) => ({ ...f, driver_email: e.target.value }))}
              placeholder="driver@example.com"
              autoFocus
              required
            />
          </div>
          <div className="field">
            <label className="field-label" htmlFor="topup-amount">
              Amount (coins)
            </label>
            <input
              id="topup-amount"
              type="number"
              min="0"
              step="0.01"
              className="input"
              value={creditForm.amount_coins}
              onChange={(e) => setCreditForm((f) => ({ ...f, amount_coins: e.target.value }))}
              placeholder="0.00"
              required
            />
            <p className="field-help">
              Up to <Money coins={availablePool} rate={rate} showCoins /> available right now.
            </p>
          </div>
          <div className="field">
            <label className="field-label" htmlFor="topup-note">
              Note (optional)
            </label>
            <input
              id="topup-note"
              className="input"
              value={creditForm.note}
              onChange={(e) => setCreditForm((f) => ({ ...f, note: e.target.value }))}
              placeholder="e.g. cash, pump 3"
              maxLength={500}
            />
          </div>
          {creditFormError && <p className="field-error">{creditFormError}</p>}
          <div className="modal-actions">
            <button type="button" className="btn btn-quiet" onClick={() => setCreditOpen(false)}>
              Cancel
            </button>
            <button type="submit" className="btn btn-primary">
              Continue
            </button>
          </div>
        </form>
      </Modal>

      <ConfirmDialog
        open={creditConfirmOpen}
        onClose={() => !creditBusy && setCreditConfirmOpen(false)}
        onConfirm={confirmCredit}
        title="Credit this driver?"
        confirmLabel="Credit driver"
        tone="primary"
        busy={creditBusy}
        busyLabel="Crediting…"
        body={
          <div className="stack-sm">
            <p className="text-2">
              Credit <strong>{creditForm.driver_email}</strong> with{' '}
              <strong>
                <Money coins={Number(creditForm.amount_coins) || 0} rate={rate} showCoins />
              </strong>
              . Their wallet balance rises immediately, and this comes straight out of your
              unsettled earnings pool — it will reduce any bank payout you request later for
              this same window.
            </p>
            {creditForm.note && (
              <p className="text-3 text-sm">Note: {creditForm.note}</p>
            )}
            {creditError && <p className="field-error">{creditError}</p>}
          </div>
        }
      />
    </CpoLayout>
  );
}
