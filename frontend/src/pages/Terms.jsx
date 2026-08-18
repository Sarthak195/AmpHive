/**
 * Terms — the umbrella Terms of Service (/terms, public, no auth).
 * ================================================================
 * /terms used to hold ONLY the charging-credit instrument terms. Those are
 * load-bearing (they are what keeps the credit inside the RBI closed-system
 * PPI exemption rather than reading as a "wallet service"), so they were moved
 * verbatim to /charging-credit-terms rather than diluted into this page, and
 * this page links to them prominently.
 *
 * What is here is the user agreement the app previously had nothing of: who
 * may use the service, what a host is responsible for versus what AmpHive is
 * responsible for, acceptable use, suspension, liability and governing law.
 *
 * Written against what the product actually does. NOT reviewed by a lawyer —
 * see the notice at the foot of the page, which says so on the page itself
 * rather than only in a comment.
 */

import { Link } from 'react-router-dom';
import PageHeader from '../components/ui/PageHeader';
import useDocumentMeta from '../hooks/useDocumentMeta';
import { SUPPORT_EMAIL, LAST_UPDATED } from '../utils/legal';

export default function Terms() {
  useDocumentMeta({
    title: 'Terms of Service',
    description:
      'The agreement between you and AmpHive: who can use the service, what drivers and hosts are each responsible for, acceptable use, and how billing disputes are handled.',
    path: '/terms',
    index: true,
  });

  return (
    <main className="page">
      <PageHeader
        title="Terms of Service"
        sub={`The agreement between you and AmpHive. Last updated ${LAST_UPDATED}.`}
      />

      <div className="stack">
        <section className="card">
          <h2>1. Agreement</h2>
          <p className="text-2">
            By creating an AmpHive account or using the service, you agree to
            these terms, to the{' '}
            <Link to="/charging-credit-terms">Charging Credit Terms</Link>,
            the <Link to="/refunds">Refunds &amp; Cancellation Policy</Link>,
            and the <Link to="/privacy">Privacy Policy</Link>. If you do not
            agree, do not use the service.
          </p>
        </section>

        <section className="card">
          <h2>2. What AmpHive is — and is not</h2>
          <p className="text-2">
            AmpHive is a platform. Hosts (charge point operators) list charging
            points they own and control; drivers find them, charge, and pay per
            unit of energy. <strong>AmpHive does not own, install, inspect or
            maintain the chargers, the sockets, or the electrical installation
            behind them</strong> — the host does. AmpHive meters the session and
            handles the money.
          </p>
          <p className="text-2">
            AmpHive is not an electricity supplier and does not resell
            electricity as a licensed distributor; a host is charging you for
            the use of their charging point and the energy delivered through it.
          </p>
        </section>

        <section className="card">
          <h2>3. Your account</h2>
          <ul className="text-2">
            <li>You must be 18 or older and able to enter into a contract.</li>
            <li>Give accurate details, and verify your email address when asked.</li>
            <li>One account per person. Keep your password to yourself — activity under your account is your responsibility.</li>
            <li>Tell us at <a href={`mailto:${SUPPORT_EMAIL}`}>{SUPPORT_EMAIL}</a> if you think someone else has access to it.</li>
          </ul>
        </section>

        <section className="card">
          <h2>4. Charging</h2>
          <ul className="text-2">
            <li>You are responsible for your vehicle, your cable, and for plugging in and unplugging safely.</li>
            <li>Follow whatever access rules the host has set for the site — opening hours, parking, and where you may park.</li>
            <li>Sessions are metered by the charger. A session may be stopped automatically if your charging credit runs out, if a safety limit trips, if the charger goes offline, or if you hit a limit you set yourself.</li>
            <li>Do not use a charger you have not been given access to, and do not attempt to draw power outside a session.</li>
          </ul>
        </section>

        <section className="card">
          <h2>5. Pricing and billing</h2>
          <p className="text-2">
            Each host sets their own rate per kWh, shown before you start.
            When you start a session we reserve an estimate of its cost against
            your charging credit; the actual metered cost is deducted when the
            session ends and any unused reservation is released. A GST tax
            invoice is available for every billed session.
          </p>
          <p className="text-2">
            If you believe a session was billed incorrectly, raise a dispute from
            your activity history. The host reviews it and can issue a refund of
            charging credit. See the{' '}
            <Link to="/refunds">Refunds &amp; Cancellation Policy</Link>.
          </p>
        </section>

        <section className="card">
          <h2>6. If you host chargers</h2>
          <ul className="text-2">
            <li>You confirm you are entitled to make the charging point available — you own it, or the owner has agreed.</li>
            <li>You are responsible for the safety and legality of your electrical installation and for any local permissions you need.</li>
            <li>Keep your published rate, location and availability accurate.</li>
            <li>Earnings are settled to you through the payout flow in the host console, less the platform fee shown there.</li>
            <li>You must review billing disputes on your chargers in good faith and within a reasonable time.</li>
            <li>Do not use the operator console to access data about drivers beyond what you need to run your site and settle billing.</li>
          </ul>
        </section>

        <section className="card">
          <h2>7. Acceptable use</h2>
          <p className="text-2">Do not:</p>
          <ul className="text-2">
            <li>tamper with a charger, its wiring, or its metering, or try to obtain energy without an authorised session;</li>
            <li>probe, scan, overload or otherwise attack the service, or try to reach data that is not yours;</li>
            <li>use another person&apos;s account, or impersonate anyone;</li>
            <li>use the service for anything unlawful, or in a way that endangers people or property;</li>
            <li>scrape or resell data from the service.</li>
          </ul>
          <p className="text-3 text-sm">
            Security researchers: we welcome reports. Email{' '}
            <a href={`mailto:${SUPPORT_EMAIL}`}>{SUPPORT_EMAIL}</a> rather than
            disclosing publicly, and do not access other people&apos;s data
            while testing.
          </p>
        </section>

        <section className="card">
          <h2>8. Suspension and closure</h2>
          <p className="text-2">
            We may suspend or close an account that breaches these terms, that
            is being used fraudulently, or where we are required to. You can
            close your own account at any time from your account page — see the{' '}
            <Link to="/privacy">Privacy Policy</Link> for exactly what is
            deleted and what is retained, and note that remaining charging
            credit is forfeited on closure.
          </p>
        </section>

        <section className="card">
          <h2>9. Availability</h2>
          <p className="text-2">
            We do not guarantee that the service, any particular charger, or the
            connection to it will be available or uninterrupted. Chargers depend
            on the host&apos;s power and internet connection. We may change or
            withdraw features.
          </p>
        </section>

        <section className="card">
          <h2>10. Liability</h2>
          <p className="text-2">
            To the extent permitted by law, AmpHive is not liable for damage to
            your vehicle or equipment, for loss caused by a charger being
            unavailable or interrupted, or for indirect or consequential loss.
            Nothing here limits liability that cannot be limited by law,
            including for death or personal injury caused by negligence, or for
            fraud.
          </p>
          <p className="text-2">
            Where AmpHive is liable, our total liability for any claim is
            limited to the charging fees you paid through AmpHive in the three
            months before the event giving rise to the claim.
          </p>
        </section>

        <section className="card">
          <h2>11. Changes</h2>
          <p className="text-2">
            We may update these terms. The &ldquo;last updated&rdquo; date above
            changes when we do, and material changes are notified in the app.
            Continuing to use the service after a change means you accept it.
          </p>
        </section>

        <section className="card">
          <h2>12. Governing law</h2>
          <p className="text-2">
            These terms are governed by the laws of India, and the courts of
            India have jurisdiction over any dispute arising from them.
          </p>
        </section>

        <section className="card">
          <h2>13. Contact</h2>
          <p className="text-2">
            Questions, complaints or notices:{' '}
            <a href={`mailto:${SUPPORT_EMAIL}`}>{SUPPORT_EMAIL}</a>. See the{' '}
            <Link to="/contact">contact page</Link> for what to include so we
            can help quickly.
          </p>
        </section>

        <section className="card">
          <h2 className="text-sm">Legal review notice</h2>
          <p className="text-3 text-sm">
            These terms were drafted to describe how AmpHive actually operates.
            They have <strong>not</strong> been reviewed by a qualified lawyer,
            and they are not legal advice. Before relying on them commercially,
            have them reviewed — particularly sections 2, 6 and 10, and the
            regulatory framing in the{' '}
            <Link to="/charging-credit-terms">Charging Credit Terms</Link>.
          </p>
        </section>
      </div>
    </main>
  );
}
