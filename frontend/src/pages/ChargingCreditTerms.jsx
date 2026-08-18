/**
 * ChargingCreditTerms — the "Charging Credit Terms" page
 * (/charging-credit-terms, public, no auth).
 * =====================================================================
 * MOVED here from /terms (2026-08-18) so that /terms can hold the umbrella
 * Terms of Service. The substance below is UNCHANGED — it is the load-bearing
 * closed-loop declaration: AmpHive charging credit is a prepaid instrument
 * redeemable ONLY for EV charging on AmpHive — not a wallet, not withdrawable
 * as cash, not transferable. This framing (modelled on how licensed EV
 * networks present their charging wallets) is what keeps the credit inside the
 * RBI closed-system PPI exemption rather than reading as a general "wallet
 * service". Do not reword sections 4 and 7 without understanding that.
 *
 * Linked from the Terms of Service, the site footer, and the Charging Credit
 * page. /terms links here prominently so any existing external reference to
 * "the AmpHive terms" still reaches this text in one click.
 */

import { Link } from 'react-router-dom';
import PageHeader from '../components/ui/PageHeader';
import useDocumentMeta from '../hooks/useDocumentMeta';
import { SUPPORT_EMAIL } from '../utils/legal';

const MIN_INR = 50;
const MAX_INR = 10000;

export default function ChargingCreditTerms() {
  useDocumentMeta({
    title: 'Charging Credit Terms',
    description:
      'AmpHive charging credit is a closed-loop prepaid instrument redeemable only for EV charging on AmpHive: how it is added, how it is spent, and what it is not.',
    path: '/charging-credit-terms',
    index: true,
  });

  return (
    <main className="page">
      <PageHeader
        title="Charging Credit Terms"
        sub="How AmpHive charging credit works — and what it is not."
      />

      <div className="stack">
        <section className="card">
          <h2>1. What charging credit is</h2>
          <p className="text-2">
            AmpHive charging credit is a prepaid amount you add to your AmpHive
            account to pay for EV charging on the AmpHive network. It is a{' '}
            <strong>closed-loop prepaid instrument, redeemable only for EV
            charging services provided through AmpHive</strong>. Credit is
            denominated at <strong>1 credit = ₹1</strong>.
          </p>
        </section>

        <section className="card">
          <h2>2. Adding credit</h2>
          <p className="text-2">
            Credit is added through our payment processor (UPI, debit/credit
            card or net banking). The amount you pay is added to your account as
            charging credit. The minimum is {`₹${MIN_INR}`} and the maximum is{' '}
            {`₹${MAX_INR.toLocaleString('en-IN')}`} per top-up.
          </p>
        </section>

        <section className="card">
          <h2>3. How credit is spent</h2>
          <p className="text-2">
            Every charging session is metered by energy (kWh) at the host&apos;s
            published ₹/kWh rate. When you start a session we reserve an
            estimate of its cost against your credit; the{' '}
            <strong>actual metered cost is deducted from your credit balance
            when the session ends</strong>, and any unused reservation is
            released. A GST tax invoice is available for every billed session.
          </p>
        </section>

        <section className="card">
          <h2>4. What charging credit is not</h2>
          <p className="text-2">
            AmpHive charging credit is <strong>not a wallet</strong> and not a
            general-purpose payment instrument. Specifically:
          </p>
          <ul className="text-2">
            <li>It <strong>cannot be withdrawn as cash</strong> or redeemed for money.</li>
            <li>It is <strong>non-transferable</strong> — it cannot be moved to another user or account.</li>
            <li>It <strong>cannot be used to pay any third party</strong>; it is usable only for EV charging on AmpHive.</li>
          </ul>
        </section>

        <section className="card">
          <h2>5. Refunds</h2>
          <p className="text-2">
            Where a refund is due — for example a failed charging session or an
            approved dispute — the amount is <strong>credited back to your
            AmpHive charging credit</strong> (or, at AmpHive&apos;s discretion,
            to your original payment method). AmpHive does not make cash refunds
            of charging credit. The full process is in the{' '}
            <Link to="/refunds">Refunds &amp; Cancellation Policy</Link>.
          </p>
        </section>

        <section className="card">
          <h2>6. Expiry and interest</h2>
          <p className="text-2">
            Charging credit does not expire and does not earn any interest.
            Unused credit remains available for future charging on AmpHive.
          </p>
          <p className="text-2">
            If you <strong>close your account</strong>, any remaining credit is
            forfeited — because it cannot be paid out as cash, there is nowhere
            for it to go. Spend it before closing if you have a balance.
          </p>
        </section>

        <section className="card">
          <h2>7. Nature of the instrument</h2>
          <p className="text-2">
            AmpHive charging credit is a closed-system prepaid instrument issued
            by AmpHive solely for the purchase of EV charging services from
            AmpHive. It does not permit cash withdrawal, funds transfer to third
            parties, or payment to any merchant other than AmpHive, and is
            operated on that basis under the Reserve Bank of India&apos;s Master
            Direction on Prepaid Payment Instruments.
          </p>
          <p className="text-3 text-sm">
            Questions about charging credit? Email{' '}
            <a href={`mailto:${SUPPORT_EMAIL}`}>{SUPPORT_EMAIL}</a>, or see the{' '}
            <Link to="/terms">Terms of Service</Link>.
          </p>
        </section>
      </div>
    </main>
  );
}
