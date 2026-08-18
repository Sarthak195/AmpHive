/**
 * Refunds — the Refunds & Cancellation Policy (/refunds, public, no auth).
 * ========================================================================
 * A separately addressable policy page, which is what a payment processor's
 * onboarding checklist asks for (a paragraph inside another document does not
 * satisfy it). The substance describes the flows that actually exist in the
 * product:
 *
 *   - dispute a billed session ..... POST /api/sessions/{id}/dispute, resolved
 *                                    by the host (routers/cpo/_disputes.py);
 *                                    an approved dispute credits charging
 *                                    credit back
 *   - unused reservation ........... a session's authorization hold is released
 *                                    at finalisation (ChargingSession.hold_coins)
 *   - failed top-up ................ services/payments.py only credits a
 *                                    CAPTURED payment; an uncaptured payment is
 *                                    reversed by the processor
 *   - cancel a reservation ......... POST /api/reservations/{id}/cancel — free,
 *                                    since a reservation costs nothing up front
 *
 * Nothing here promises a cash refund of charging credit, because the credit is
 * a closed-loop instrument (see /charging-credit-terms) and the product has no
 * payout-to-driver path.
 */

import { Link } from 'react-router-dom';
import PageHeader from '../components/ui/PageHeader';
import useDocumentMeta from '../hooks/useDocumentMeta';
import { SUPPORT_EMAIL, LAST_UPDATED } from '../utils/legal';

export default function Refunds() {
  useDocumentMeta({
    title: 'Refunds & Cancellation Policy',
    description:
      'How AmpHive handles billing disputes, failed charging sessions, unused reservations, cancelled bookings and failed top-ups.',
    path: '/refunds',
    index: true,
  });

  return (
    <main className="page">
      <PageHeader
        title="Refunds &amp; Cancellation Policy"
        sub={`What happens when a charge goes wrong. Last updated ${LAST_UPDATED}.`}
      />

      <div className="stack">
        <section className="card">
          <h2>1. The short version</h2>
          <p className="text-2">
            You pay for the energy the charger actually delivers. If you were
            billed for energy you did not receive, raise a dispute and the host
            refunds it to your charging credit. Because charging credit is a{' '}
            <Link to="/charging-credit-terms">closed-loop instrument</Link>,
            refunds are made in charging credit rather than cash.
          </p>
        </section>

        <section className="card">
          <h2>2. A session was billed incorrectly</h2>
          <p className="text-2">
            Open the session in your activity history and choose{' '}
            <strong>Dispute this bill</strong>. Tell us what happened — for
            example the charger delivered nothing, it stopped early, or the
            energy figure looks wrong.
          </p>
          <ul className="text-2">
            <li>The dispute goes to the host who operates that charger, along with the metered readings for the session.</li>
            <li>If it is approved, the agreed amount is credited back to your charging credit and appears in your credit history.</li>
            <li>If it is rejected, you will see the host&apos;s reason. If you disagree, email <a href={`mailto:${SUPPORT_EMAIL}`}>{SUPPORT_EMAIL}</a>.</li>
            <li>Raise a dispute within 30 days of the session so the telemetry behind it is still available.</li>
          </ul>
          <p className="text-3 text-sm">
            Hosts are expected to review disputes in good faith and within a
            reasonable time. Where a host does not respond, contact us.
          </p>
        </section>

        <section className="card">
          <h2>3. A session never delivered any energy</h2>
          <p className="text-2">
            You are billed on metered energy, so a session that delivered
            nothing costs nothing. If a charger failed to start, or cut out
            immediately, you should see a zero or near-zero charge. If you were
            charged anyway, that is exactly what the dispute flow above is for.
          </p>
        </section>

        <section className="card">
          <h2>4. The reservation you did not use</h2>
          <p className="text-2">
            Reserving a charger costs nothing up front, and cancelling a
            reservation is free. If you do not turn up, the reservation simply
            lapses after the grace period and the charger is released for
            others — there is no no-show fee.
          </p>
        </section>

        <section className="card">
          <h2>5. Money reserved but not spent</h2>
          <p className="text-2">
            When a session starts we reserve an estimate of its cost against
            your charging credit so a charge cannot run past what you hold.
            That reservation is not a payment: when the session ends, only the
            metered cost is deducted and the rest is released automatically. No
            action is needed from you.
          </p>
        </section>

        <section className="card">
          <h2>6. A top-up that did not arrive</h2>
          <p className="text-2">
            Charging credit is added only when the payment processor confirms
            your payment was captured. If a payment fails or is left
            uncaptured, no credit is added and the processor reverses the
            amount to your original payment method under its own timelines —
            typically within 5–7 working days.
          </p>
          <p className="text-2">
            If money left your account but no credit appeared, email{' '}
            <a href={`mailto:${SUPPORT_EMAIL}`}>{SUPPORT_EMAIL}</a> with the
            payment reference from your bank or UPI app and we will trace it.
          </p>
        </section>

        <section className="card">
          <h2>7. Cash refunds of charging credit</h2>
          <p className="text-2">
            Charging credit cannot be withdrawn as cash or transferred — that is
            what makes it a closed-loop instrument rather than a wallet. Credit
            you have added is spent on charging. In exceptional cases AmpHive
            may, at its discretion, return an amount to your original payment
            method instead.
          </p>
          <p className="text-2">
            Closing your account forfeits any remaining credit, so spend it
            first if you have a balance.
          </p>
        </section>

        <section className="card">
          <h2>8. How to reach us</h2>
          <p className="text-2">
            Email <a href={`mailto:${SUPPORT_EMAIL}`}>{SUPPORT_EMAIL}</a> with
            the session or payment reference. See the{' '}
            <Link to="/contact">contact page</Link> for what to include.
          </p>
        </section>
      </div>
    </main>
  );
}
