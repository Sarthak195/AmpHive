/**
 * Privacy — the Privacy Policy (/privacy, public, no auth).
 * ========================================================
 * Written against what the code ACTUALLY does, not a template. Every claim
 * here was checked against source, and the checks are noted inline so this
 * page can be re-verified when behaviour changes:
 *
 *   - collected fields ......... backend/database/models.py (User, ChargingSession,
 *                                LedgerTransaction, Invoice, Notification,
 *                                PushSubscription, Reservation, ...)
 *   - IP / logs ................ backend/services/rate_limit.py client_ip,
 *                                backend/logging_config.py (addresses are masked)
 *   - third parties ............ Razorpay (services/payments.py), Google OAuth
 *                                (routers/auth.py), OpenStreetMap tiles
 *                                (components/MapComponent.jsx), Web Push
 *                                (services/notifications.py), SMTP
 *                                (services/email.py), Google Cloud (deploy/)
 *   - cookies .................. only the two short-lived httpOnly OAuth
 *                                state/nonce cookies (routers/auth.py)
 *   - browser location ......... pages/MapPage.jsx handleGeolocate — opt-in,
 *                                used in-browser only, never sent to us
 *   - retention ................ TELEMETRY_RETENTION_DAYS / GATEWAY_LOGS_
 *                                RETENTION_DAYS in deploy/config/.env.template
 *   - rights ................... GET /api/auth/me/export, DELETE /api/auth/me
 *
 * Deliberately NOT claimed anywhere on this page: certification, audit, or
 * compliance with any specific regime. It describes practice; it does not
 * assert legal conclusions.
 */

import { Link } from 'react-router-dom';
import PageHeader from '../components/ui/PageHeader';
import useDocumentMeta from '../hooks/useDocumentMeta';
import { SUPPORT_EMAIL, LAST_UPDATED } from '../utils/legal';

export default function Privacy() {
  useDocumentMeta({
    title: 'Privacy Policy',
    description:
      'What personal data AmpHive collects when you charge an EV, why we hold it, who processes it, how long we keep it, and how to export or delete it.',
    path: '/privacy',
    index: true,
  });

  return (
    <main className="page">
      <PageHeader
        title="Privacy Policy"
        sub={`How AmpHive handles your personal data. Last updated ${LAST_UPDATED}.`}
      />

      <div className="stack">
        <section className="card">
          <h2>1. Who we are</h2>
          <p className="text-2">
            AmpHive is a shared EV-charging service: hosts list the smart plug
            points they already own, and drivers pay per kWh to charge from
            them. This policy covers the AmpHive driver app
            (amphive.app), the host console (cpo.amphive.app), and the backend
            that serves both.
          </p>
          <p className="text-2">
            For any privacy question, correction or complaint, write to{' '}
            <a href={`mailto:${SUPPORT_EMAIL}`}>{SUPPORT_EMAIL}</a>. We aim to
            respond within 30 days.
          </p>
        </section>

        <section className="card">
          <h2>2. What we collect</h2>

          <h3>Account details</h3>
          <p className="text-2">
            Your name, email address, and a one-way hash of your password (we
            never store the password itself). If you sign in with Google we
            store the account identifier Google gives us and the verified email
            address on it — we do not receive your Google password.
          </p>

          <h3>Charging and payment records</h3>
          <p className="text-2">
            For every session: which charger, when it started and ended, energy
            delivered, and what it cost. For every top-up: the amount and the
            payment reference our payment processor returns. We also store the
            GST tax invoice issued for each billed session. We do{' '}
            <strong>not</strong> receive or store your card number, UPI PIN or
            bank credentials — those go directly to the payment processor.
          </p>

          <h3>Things you create in the app</h3>
          <p className="text-2">
            Charger groups you join, chargers you favourite or watch,
            reservations you book, problem reports and billing disputes you
            raise, and the notifications we have sent you.
          </p>

          <h3>Technical data</h3>
          <p className="text-2">
            Our servers record request logs containing your IP address, the
            time, the path requested and a request id. IP addresses are also
            held briefly in memory to enforce rate limits. Email addresses are{' '}
            <strong>masked in our application logs</strong>. If you turn on push
            notifications, we store the push subscription your browser issues.
          </p>

          <h3>Location</h3>
          <p className="text-2">
            The map can centre on your position if you press the locate button
            and your browser grants permission. That position is used{' '}
            <strong>in your browser only and is never sent to us or stored</strong>.
            The coordinates we do store are the chargers&apos; own locations,
            published by their hosts.
          </p>

          <h3>Charger telemetry</h3>
          <p className="text-2">
            While a session runs, the charger reports power and energy readings
            to us. These are what your bill is computed from, and they are
            linked to your session.
          </p>
        </section>

        <section className="card">
          <h2>3. Why we hold it</h2>
          <ul className="text-2">
            <li><strong>To run the service</strong> — authenticate you, start and stop charging, meter it, and bill it.</li>
            <li><strong>To take payment and issue invoices</strong> — including the GST invoice a billed session requires.</li>
            <li><strong>To keep the network safe and honest</strong> — detect unauthorised use of a charger, investigate billing disputes, and stop abuse of our endpoints.</li>
            <li><strong>To tell you what is happening</strong> — session updates, low-credit warnings, receipts and password resets.</li>
            <li><strong>To meet record-keeping obligations</strong> — tax and accounting records for money that changed hands.</li>
          </ul>
          <p className="text-3 text-sm">
            We do not sell your personal data, and we do not use it for
            advertising or profiling.
          </p>
        </section>

        <section className="card">
          <h2>4. Who else processes it</h2>
          <p className="text-2">
            We keep this list short on purpose. These are the only third
            parties that receive data through normal use:
          </p>
          <ul className="text-2">
            <li>
              <strong>Razorpay</strong> — our payment processor. When you add
              charging credit, your payment details go to Razorpay directly;
              we receive back only the amount, status and payment reference.
            </li>
            <li>
              <strong>Google</strong> — only if you choose &ldquo;Continue with
              Google&rdquo;. Google tells us the verified email address and a
              stable account identifier.
            </li>
            <li>
              <strong>OpenStreetMap</strong> — the charger map loads its map
              tiles from OpenStreetMap&apos;s servers, which means those servers
              see your IP address and the area of the map you are viewing.
            </li>
            <li>
              <strong>Your browser&apos;s push service</strong> (Google, Mozilla
              or Apple, depending on your browser) — only if you enable push
              notifications. It delivers the notification; the message content
              is encrypted to your browser.
            </li>
            <li>
              <strong>Our email provider</strong> — delivers verification,
              password-reset and receipt emails to your address.
            </li>
            <li>
              <strong>Google Cloud</strong> — hosts our servers, database and
              encrypted backups.
            </li>
          </ul>
          <p className="text-3 text-sm">
            We use <strong>no analytics, advertising or tracking services</strong>.
            There is no Google Analytics, no advertising pixel and no
            third-party tracker anywhere in the app.
          </p>
        </section>

        <section className="card">
          <h2>5. Cookies and local storage</h2>
          <p className="text-2">
            We set <strong>no advertising or analytics cookies</strong>, so
            there is no cookie banner to click through. Two things are stored on
            your device:
          </p>
          <ul className="text-2">
            <li>
              <strong>Your sign-in token</strong>, kept in your browser&apos;s
              local storage so you stay signed in. Signing out removes it and
              invalidates it on our side.
            </li>
            <li>
              <strong>Two short-lived cookies during Google sign-in only</strong>,
              which exist to prevent someone else&apos;s sign-in being swapped
              into your session. They are HTTP-only, secure, and are deleted as
              soon as sign-in completes.
            </li>
          </ul>
        </section>

        <section className="card">
          <h2>6. How long we keep it</h2>
          <ul className="text-2">
            <li><strong>Account details</strong> — until you close your account.</li>
            <li><strong>Charging, payment and invoice records</strong> — kept after closure, in anonymised form, because they are tax and accounting records and they form the host&apos;s earnings history.</li>
            <li><strong>Detailed charger telemetry</strong> — pruned automatically (currently after 90 days). Your session totals remain.</li>
            <li><strong>Forwarded charger diagnostic logs</strong> — pruned automatically (currently after 14 days).</li>
            <li><strong>Encrypted database backups</strong> — retained for 30 days, plus daily disk snapshots retained for 14 days. Data you delete disappears from backups as those windows roll over.</li>
          </ul>
        </section>

        <section className="card">
          <h2>7. Your choices</h2>
          <p className="text-2">
            You can do all of this yourself, from{' '}
            <Link to="/account">your account page</Link> — no email request
            needed:
          </p>
          <ul className="text-2">
            <li>
              <strong>Get a copy of your data.</strong> Download a machine-readable
              file containing your account details, charging history, wallet
              ledger, invoices, reservations, reports and notifications.
            </li>
            <li>
              <strong>Close your account.</strong> Your name, email address and
              linked Google identity are erased, your password is destroyed,
              and your notifications, saved chargers, push subscriptions and
              group memberships are deleted. Past charging and payment records
              are kept but no longer identify you. Any remaining charging
              credit is forfeited — it is a closed-loop credit that cannot be
              paid out as cash (see the{' '}
              <Link to="/charging-credit-terms">Charging Credit Terms</Link>).
            </li>
            <li><strong>Correct your details</strong> or ask a question by emailing us.</li>
            <li><strong>Turn push notifications off</strong> at any time from your account page or your browser settings.</li>
          </ul>
          <p className="text-3 text-sm">
            If you are unhappy with how we have handled your data, write to{' '}
            <a href={`mailto:${SUPPORT_EMAIL}`}>{SUPPORT_EMAIL}</a>. If you are
            in India, you may also raise the matter with the relevant data
            protection authority.
          </p>
        </section>

        <section className="card">
          <h2>8. Security</h2>
          <p className="text-2">
            Traffic to the app is served over HTTPS. Passwords are stored only
            as bcrypt hashes. Sign-in tokens can be revoked, and are revoked
            automatically when you sign out, reset your password or close your
            account. Chargers connect to us over an authenticated, encrypted
            MQTT link and can only publish data for their own hardware.
          </p>
          <p className="text-3 text-sm">
            No system is perfectly secure, and we make no guarantee that it is.
            If you find a vulnerability, please report it to{' '}
            <a href={`mailto:${SUPPORT_EMAIL}`}>{SUPPORT_EMAIL}</a> rather than
            disclosing it publicly.
          </p>
        </section>

        <section className="card">
          <h2>9. Where your data is processed</h2>
          <p className="text-2">
            Our servers and database run on Google Cloud. Some of the providers
            listed in section 4 — notably Google and your browser&apos;s push
            service — operate globally, so data handled by them may be
            processed outside India.
          </p>
        </section>

        <section className="card">
          <h2>10. Children</h2>
          <p className="text-2">
            AmpHive is not intended for children. You need to be able to enter
            into a contract and to pay for charging, so please do not create an
            account if you are under 18.
          </p>
        </section>

        <section className="card">
          <h2>11. Changes to this policy</h2>
          <p className="text-2">
            If we change what we collect or who processes it, we will update
            this page and change the &ldquo;last updated&rdquo; date above.
            Material changes will also be notified in the app.
          </p>
        </section>

        <section className="card">
          <h2>12. Automated decisions and AI</h2>
          <p className="text-2">
            AmpHive does not send your personal data to any AI or machine
            learning service, and no automated system makes decisions about you
            with legal or similarly significant effects. Billing is arithmetic
            on metered energy at a published rate, and a human host reviews
            every dispute.
          </p>
        </section>
      </div>
    </main>
  );
}
