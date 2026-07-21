/**
 * Wallet — driver's coin balance, top-up (Razorpay), and the full money
 * trail (ledger). Balance/available-balance come from AuthContext's user
 * object (GET /api/auth/me — coin_balance + available_balance, the latter
 * accounting for what any currently-running session already holds). The
 * coin↔₹ rate comes from ConfigContext, never hardcoded.
 *
 * Top-up flow mirrors the old TopUp.jsx: POST /api/payments/create-order →
 * loadRazorpay() → Checkout → POST /api/payments/verify with only the
 * Razorpay ids/signature (the client-sent amount is never trusted — see
 * backend/routers/payments.py). A dismissed checkout is a neutral notice,
 * not an error; a verify failure keeps the order id on screen as a support
 * reference.
 */

import { useState, useEffect, useCallback } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { Wallet as WalletIcon, Receipt } from 'lucide-react';
import PageHeader from '../components/ui/PageHeader';
import DataTable from '../components/ui/DataTable';
import Money from '../components/ui/Money';
import { useToast } from '../components/ui';
import api from '../api/client';
import { useAuth } from '../contexts/AuthContext';
import { useConfig } from '../contexts/ConfigContext';
import { loadRazorpay } from '../utils/razorpay';
import { formatINR } from '../utils/money';
import { txTypeLabel, apiErrorCopy } from '../utils/statusCopy';
import './Wallet.css';

const QUICK_AMOUNTS_INR = [100, 200, 500, 1000];
const MIN_AMOUNT_INR = 50;
const MAX_AMOUNT_INR = 10000;

const formatDate = (isoString) => (isoString ? new Date(isoString).toLocaleString() : '—');

const validateAmount = (value) => {
  if (value == null || Number.isNaN(value) || value <= 0) return 'Enter an amount to top up.';
  if (value < MIN_AMOUNT_INR) return `Minimum top-up is ${formatINR(MIN_AMOUNT_INR)}.`;
  if (value > MAX_AMOUNT_INR) return `Maximum top-up is ${formatINR(MAX_AMOUNT_INR)}.`;
  return '';
};

export default function Wallet() {
  const { user, refreshUser } = useAuth();
  const { coin_inr_rate: rate = 1 } = useConfig();
  const toast = useToast();
  const [searchParams] = useSearchParams();
  const next = searchParams.get('next');

  const [amount, setAmount] = useState(QUICK_AMOUNTS_INR[0]);
  const [customAmount, setCustomAmount] = useState('');
  const [amountError, setAmountError] = useState('');
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState(null); // { tone: 'info'|'ok'|'danger', body, orderRef? }

  const [ledger, setLedger] = useState([]);
  const [ledgerLoading, setLedgerLoading] = useState(true);
  const [ledgerError, setLedgerError] = useState(null);

  const fetchLedger = useCallback(async () => {
    setLedgerLoading(true);
    setLedgerError(null);
    try {
      const rows = await api.get('/api/wallet/ledger');
      setLedger(rows || []);
    } catch (err) {
      setLedgerError(err);
    } finally {
      setLedgerLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchLedger();
  }, [fetchLedger]);

  const coinBalance = user?.coin_balance ?? 0;
  const availableBalance = user?.available_balance ?? coinBalance;
  const heldCoins = Math.max(0, coinBalance - availableBalance);

  const pickAmount = (value) => {
    setAmount(value);
    setCustomAmount('');
    setAmountError('');
  };

  const handleCustomChange = (e) => {
    const raw = e.target.value;
    setCustomAmount(raw);
    setAmount(raw === '' ? null : Number(raw));
    setAmountError('');
  };

  const handlePay = async () => {
    const validationError = validateAmount(amount);
    if (validationError) {
      setAmountError(validationError);
      return;
    }
    setAmountError('');
    setNotice(null);
    setBusy(true);

    let order;
    try {
      order = await api.post('/api/payments/create-order', { amount_inr: amount });
    } catch (err) {
      toast.error(apiErrorCopy(err));
      setNotice({ tone: 'danger', body: apiErrorCopy(err) });
      setBusy(false);
      return;
    }

    let Razorpay;
    try {
      Razorpay = await loadRazorpay();
    } catch (err) {
      toast.error(apiErrorCopy(err));
      setNotice({ tone: 'danger', body: apiErrorCopy(err) });
      setBusy(false);
      return;
    }

    const rzp = new Razorpay({
      key: order.key_id,
      amount: order.amount,
      currency: order.currency,
      order_id: order.order_id,
      name: 'AmpHive',
      description: `Wallet top-up: ${formatINR(amount)}`,
      prefill: { email: user?.email || '', name: user?.full_name || '' },
      modal: {
        ondismiss: () => {
          setBusy(false);
          setNotice({ tone: 'info', body: 'Payment not completed — nothing was charged.' });
        },
      },
      handler: async (response) => {
        try {
          const result = await api.post('/api/payments/verify', {
            razorpay_order_id: response.razorpay_order_id,
            razorpay_payment_id: response.razorpay_payment_id,
            razorpay_signature: response.razorpay_signature,
          });
          await refreshUser();
          await fetchLedger();
          toast.ok(`${formatINR(amount)} added to your wallet.`);
          setNotice({
            tone: 'ok',
            body: `Payment successful — new balance ${formatINR(result.new_balance)}.`,
          });
        } catch (verifyErr) {
          toast.error(apiErrorCopy(verifyErr));
          setNotice({
            tone: 'danger',
            body: `Payment received but verification failed. ${apiErrorCopy(verifyErr)}`,
            orderRef: response.razorpay_order_id,
          });
        } finally {
          setBusy(false);
        }
      },
    });

    rzp.on('payment.failed', (response) => {
      const msg = response?.error?.description || 'Payment failed. Please try again.';
      toast.error(msg);
      setNotice({ tone: 'danger', body: msg, orderRef: order.order_id });
      setBusy(false);
    });

    rzp.open();
  };

  const ledgerColumns = [
    { key: 'created_at', label: 'Date', render: (tx) => formatDate(tx.created_at) },
    { key: 'transaction_type', label: 'Type', render: (tx) => txTypeLabel(tx.transaction_type) },
    {
      key: 'description',
      label: 'Description',
      render: (tx) => tx.description || (tx.session_id ? `Session #${tx.session_id}` : '—'),
    },
    {
      key: 'amount',
      label: 'Amount',
      num: true,
      render: (tx) => (
        <span className={tx.direction === 'credit' ? 'wallet-amount-credit' : 'wallet-amount-debit'}>
          <Money coins={tx.amount} rate={rate} />
        </span>
      ),
    },
    {
      key: 'balance_after',
      label: 'Balance after',
      num: true,
      render: (tx) => <Money coins={tx.balance_after} rate={rate} />,
    },
  ];

  return (
    <main className="page">
      <PageHeader title="Wallet" sub="Add money and see where it went." />

      <div className="stack">
        <section className="card wallet-balance-card">
          <p className="wallet-balance-label">
            <WalletIcon size={16} aria-hidden="true" />
            Balance
          </p>
          <p className="wallet-balance-figure">
            <Money coins={coinBalance} rate={rate} />
          </p>
          {heldCoins > 0 && (
            <p className="text-2 text-sm">
              <Money coins={heldCoins} rate={rate} /> reserved for the running session
            </p>
          )}
          {rate === 1 && (
            <p className="text-3 text-sm">1 coin = ₹1 — your balance is prepaid credit.</p>
          )}
        </section>

        <section className="card wallet-topup-card">
          <h2>Add money</h2>

          <div className="wallet-amount-row" role="group" aria-label="Choose a top-up amount">
            {QUICK_AMOUNTS_INR.map((amt) => {
              const selected = customAmount === '' && amount === amt;
              return (
                <button
                  key={amt}
                  type="button"
                  className={`btn btn-sm ${selected ? 'btn-primary' : 'btn-quiet'}`}
                  aria-pressed={selected}
                  onClick={() => pickAmount(amt)}
                >
                  {formatINR(amt)}
                </button>
              );
            })}
          </div>

          <div className="field">
            <label className="field-label" htmlFor="wallet-custom-amount">
              Or enter a custom amount
            </label>
            <input
              id="wallet-custom-amount"
              className="input"
              type="number"
              inputMode="decimal"
              min={MIN_AMOUNT_INR}
              max={MAX_AMOUNT_INR}
              placeholder={`${MIN_AMOUNT_INR} – ${MAX_AMOUNT_INR}`}
              value={customAmount}
              onChange={handleCustomChange}
              aria-invalid={amountError ? 'true' : undefined}
              aria-describedby={amountError ? 'wallet-amount-error' : undefined}
            />
            {amountError && (
              <p className="field-error" id="wallet-amount-error">
                {amountError}
              </p>
            )}
          </div>

          {notice && (
            <div
              className={`banner banner-${notice.tone}`}
              role={notice.tone === 'danger' ? 'alert' : 'status'}
              aria-live="polite"
            >
              <div>
                <p>{notice.body}</p>
                {notice.orderRef && (
                  <p className="wallet-order-ref">
                    Keep this reference if you need support: <span className="mono">{notice.orderRef}</span>
                  </p>
                )}
                {notice.tone === 'ok' && next && (
                  <Link to={next} className="btn btn-quiet btn-sm wallet-next-link">
                    Back to your session
                  </Link>
                )}
              </div>
            </div>
          )}

          <button type="button" className="btn btn-primary btn-lg btn-full" onClick={handlePay} disabled={busy}>
            {busy ? 'Processing…' : amount ? `Pay ${formatINR(amount)}` : 'Pay'}
          </button>
        </section>

        <section>
          <h2 className="wallet-ledger-heading">Activity</h2>
          <DataTable
            columns={ledgerColumns}
            rows={ledger}
            loading={ledgerLoading}
            error={ledgerError}
            onRetry={fetchLedger}
            emptyIcon={Receipt}
            emptyTitle="No wallet activity yet"
            emptyBody="Top up to add coins — every top-up and charge shows up here."
            collapse
          />
        </section>
      </div>
    </main>
  );
}
