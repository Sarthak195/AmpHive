/**
 * SessionReceipt — post-session summary (redesign v3, C4).
 *
 * Reads the stop response from SessionContext (`receipt`): energy, duration,
 * ₹ charged, balance after, an uncollected-shortfall row with plain-language
 * help, and the auto-stop reason via stopReasonCopy. Actions: view the GST
 * invoice (raw fetch — HTML, needs the Bearer header, opens in a new tab —
 * not a download), report an issue (DisputeModal) and charge again.
 */

import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { CheckCircle2 } from 'lucide-react';

import { useSession } from '../contexts/SessionContext';
import { useConfig } from '../contexts/ConfigContext';
import { Money, useToast } from './ui';
import api from '../api/client';
import DisputeModal from './DisputeModal';
import SessionPowerCurve from './SessionPowerCurve';
import { stopReasonCopy, isAutoStopReason } from '../utils/statusCopy';
import { formatINR, coinsToINR, formatKwh, formatKw, formatDuration } from '../utils/money';

const Row = ({ label, children, strong }) => (
  <div className="receipt-row">
    <dt className="text-3 text-sm">{label}</dt>
    <dd className={strong ? 'receipt-strong' : undefined}>{children}</dd>
  </div>
);

const SessionReceipt = () => {
  const { receipt, dismissReceipt } = useSession();
  const { coin_inr_rate, co2_avoided_kg_per_kwh: co2PerKwh } = useConfig();
  const navigate = useNavigate();
  const toast = useToast();

  const [disputeOpen, setDisputeOpen] = useState(false);
  const [invoiceBusy, setInvoiceBusy] = useState(false);
  // F8V: the driver's own net CO2 factor (their vehicle figures), else the
  // standard one from /api/config. A failed fetch keeps the standard factor.
  const [ownCo2PerKwh, setOwnCo2PerKwh] = useState(null);
  const hasReceipt = receipt != null;
  useEffect(() => {
    if (!hasReceipt) return undefined;
    let cancelled = false;
    api.get('/api/me/vehicle')
      .then((res) => {
        if (!cancelled && Number.isFinite(res?.co2_avoided_kg_per_kwh)) {
          setOwnCo2PerKwh(res.co2_avoided_kg_per_kwh);
        }
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [hasReceipt]);

  if (!receipt) return null;

  const {
    session_id, plug_name, energy_kwh, peak_power_w, coins_spent,
    shortfall_coins, balance_before, balance_remaining, duration_sec,
    ended_at, reason, max_kwh, idle_fee_coins,
  } = receipt;

  const chargedInr = coinsToINR(coins_spent ?? 0, coin_inr_rate);
  const shortfallInr = coinsToINR(shortfall_coins ?? 0, coin_inr_rate);
  const autoStopped = isAutoStopReason(reason);
  // Estimate only: energy x the server's net per-kWh factor (petrol CO2 for the
  // same distance minus grid CO2). Hidden until /api/config supplies it.
  const factor = ownCo2PerKwh ?? co2PerKwh;
  const co2Kg = factor > 0 ? (energy_kwh ?? 0) * factor : null;
  // The invoice endpoint 400s on a zero-cost session — only offer it when the
  // session actually billed something.
  const canInvoice = session_id != null && Number(coins_spent) > 0;

  const chargeAgain = () => {
    dismissReceipt();
    navigate('/');
  };

  // The GST invoice endpoint returns printable HTML (not JSON) and needs the
  // Bearer header, so it can't go through the api client — raw-fetch the blob
  // and open it in a new tab. Issues the invoice server-side on first view.
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
      const url = URL.createObjectURL(blob);
      window.open(url, '_blank', 'noopener');
      // Give the new tab time to load the blob before reclaiming it.
      setTimeout(() => URL.revokeObjectURL(url), 60_000);
    } catch (err) {
      toast.error(err?.message || "Couldn't load the invoice. Please try again.");
    } finally {
      setInvoiceBusy(false);
    }
  };

  return (
    <section className="session-receipt card anim-rise" aria-label="Charging receipt">
      <header className="receipt-head">
        <CheckCircle2 className="receipt-check" aria-hidden="true" />
        <h2>Charging complete</h2>
        <p className="text-3 text-sm">
          {plug_name || 'Charger'}
          {ended_at && ` · ${new Date(ended_at).toLocaleString()}`}
        </p>
      </header>

      {autoStopped && (
        <div className="banner banner-warn">
          <p>{stopReasonCopy(reason)}</p>
        </div>
      )}

      <dl className="receipt-rows">
        <Row label="Energy delivered" strong>
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
        {co2Kg != null && co2Kg >= 0.005 && (
          <Row label="CO₂ avoided (estimate)">
            <span className="num">≈ {co2Kg.toFixed(2)} kg</span>
          </Row>
        )}
        {/* [C1] The idle fee is already inside "Charged"; shown on its own line. */}
        {Number(idle_fee_coins) > 0 && (
          <Row label="Idle fee (included)">
            <span className="num"><Money coins={idle_fee_coins} rate={coin_inr_rate} /></span>
          </Row>
        )}
        <Row label="Charged" strong>
          <span className="receipt-debit num">−{formatINR(chargedInr)}</span>
        </Row>
        <Row label="Balance after">
          <span className="text-3 num">{formatINR(coinsToINR(balance_before ?? 0, coin_inr_rate))} → </span>
          <strong><Money coins={balance_remaining ?? 0} rate={coin_inr_rate} /></strong>
        </Row>
        {shortfall_coins > 0 && (
          <Row label="Couldn't be collected">
            <span className="receipt-shortfall num">{formatINR(shortfallInr)}</span>
          </Row>
        )}
      </dl>

      {session_id != null && <SessionPowerCurve sessionId={session_id} limitKwh={max_kwh} endedAt={ended_at} />}

      {shortfall_coins > 0 && (
        <p className="receipt-help text-3 text-sm">
          The remaining {formatINR(shortfallInr)} couldn&apos;t be collected — it stays owed
          on your account.
        </p>
      )}

      <div className="receipt-actions">
        {canInvoice && (
          <button
            type="button"
            className="btn btn-quiet"
            onClick={viewInvoice}
            disabled={invoiceBusy}
          >
            {invoiceBusy ? 'Opening…' : 'View GST invoice'}
          </button>
        )}
        {session_id != null && (
          <button type="button" className="btn btn-quiet" onClick={() => setDisputeOpen(true)}>
            Report an issue
          </button>
        )}
        <button type="button" className="btn btn-primary" onClick={chargeAgain}>
          Charge again
        </button>
      </div>

      {/* Rendered only while open so both the current and the rebuilt modal
          ({open,onClose,sessionId,onSubmitted} contract) behave. */}
      {disputeOpen && (
        <DisputeModal
          open
          onClose={() => setDisputeOpen(false)}
          sessionId={session_id}
          onSubmitted={() => setDisputeOpen(false)}
        />
      )}
    </section>
  );
};

export default SessionReceipt;
