/**
 * Contact — how to reach AmpHive (/contact, public, no auth).
 * ===========================================================
 * A published contact route is required by the Privacy Policy (it is where
 * data-rights requests and complaints go), expected by payment-processor
 * onboarding, and generally the thing a stranger looks for before trusting a
 * service with money.
 *
 * DELIBERATELY NOT A FORM. A public, unauthenticated contact form is a spam
 * relay and an abuse surface that would need its own rate limiting, captcha
 * and moderation — and there is no backend endpoint for one. A mailto is
 * honest about where the message goes. If a form is added later it needs a
 * server-side endpoint with the same per-IP + per-email caps the auth routes
 * use (backend/services/rate_limit.py).
 */

import { Link } from 'react-router-dom';
import { LifeBuoy, ShieldAlert, Scale } from 'lucide-react';
import PageHeader from '../components/ui/PageHeader';
import useDocumentMeta from '../hooks/useDocumentMeta';
import { SUPPORT_EMAIL } from '../utils/legal';

export default function Contact() {
  useDocumentMeta({
    title: 'Contact',
    description:
      'How to reach AmpHive: support for charging and billing, privacy and data requests, and responsible disclosure of security issues.',
    path: '/contact',
    index: true,
  });

  return (
    <main className="page">
      <PageHeader title="Contact" sub="One address, monitored by a human." />

      <div className="stack">
        <section className="card">
          <h2>
            <LifeBuoy size={18} aria-hidden="true" /> Support and billing
          </h2>
          <p className="text-2">
            Email <a href={`mailto:${SUPPORT_EMAIL}`}>{SUPPORT_EMAIL}</a>.
          </p>
          <p className="text-2">
            If it is about a charging session, include the{' '}
            <strong>session date and the charger name</strong> — you will find
            both in your activity history. For a payment, include the{' '}
            <strong>payment reference</strong> from your bank or UPI app. That
            is usually enough to resolve it in one reply.
          </p>
          <p className="text-3 text-sm">
            Think you were billed incorrectly? The fastest route is the{' '}
            <strong>Dispute this bill</strong> button on the session itself — it
            reaches the host who operates that charger directly, with the meter
            readings attached. See the{' '}
            <Link to="/refunds">Refunds &amp; Cancellation Policy</Link>.
          </p>
        </section>

        <section className="card">
          <h2>
            <Scale size={18} aria-hidden="true" /> Privacy, data and complaints
          </h2>
          <p className="text-2">
            Data requests, corrections and privacy complaints also go to{' '}
            <a href={`mailto:${SUPPORT_EMAIL}`}>{SUPPORT_EMAIL}</a>. We aim to
            respond within 30 days.
          </p>
          <p className="text-2">
            You may not need to write at all: you can{' '}
            <strong>download a copy of your data</strong> and{' '}
            <strong>close your account</strong> yourself from{' '}
            <Link to="/account">your account page</Link>. The{' '}
            <Link to="/privacy">Privacy Policy</Link> sets out exactly what each
            one does.
          </p>
        </section>

        <section className="card">
          <h2>
            <ShieldAlert size={18} aria-hidden="true" /> Security disclosure
          </h2>
          <p className="text-2">
            Found a vulnerability? Please report it to{' '}
            <a href={`mailto:${SUPPORT_EMAIL}`}>{SUPPORT_EMAIL}</a> rather than
            disclosing it publicly, and give us a reasonable window to fix it.
          </p>
          <p className="text-2">
            While testing, please do not access, modify or delete data
            belonging to anyone else, do not run denial-of-service tests against
            the live service, and do not interact with physical chargers you do
            not own.
          </p>
        </section>
      </div>
    </main>
  );
}
