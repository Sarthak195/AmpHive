/**
 * Account — read-only profile (no edit endpoint exists, so nothing here
 * fakes one), a password-reset trigger that reuses the existing
 * forgot-password flow, the web-push opt-in (moved here from
 * NotificationBell's footer per the redesign contract), a driver-only
 * link out to the host console, and the self-service data rights
 * ("Your data": export + account closure).
 *
 * Why the data-rights controls live HERE and not behind an email request:
 * the Privacy Policy promises the user can get a copy of their data and
 * close their account themselves, and links straight at this page. A policy
 * that names a right the product doesn't implement is the worst of both
 * worlds, so both are wired to real endpoints:
 *   GET    /api/auth/me/export  → the whole export document as JSON
 *   DELETE /api/auth/me         → closure (anonymise + purge + forfeit)
 */

import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  Mail,
  User as UserIcon,
  ShieldCheck,
  Bell,
  PlugZap,
  Download,
  Trash2,
} from 'lucide-react';
import PageHeader from '../components/ui/PageHeader';
import Skeleton from '../components/ui/Skeleton';
import Money from '../components/ui/Money';
import Modal from '../components/ui/Modal';
import { useToast } from '../components/ui';
import api from '../api/client';
import { useAuth } from '../contexts/AuthContext';
import { useConfig } from '../contexts/ConfigContext';
import useDocumentMeta from '../hooks/useDocumentMeta';
import { apiErrorCopy } from '../utils/statusCopy';
import { cpoOrigin } from '../utils/appHost';
import './Account.css';

// pushManager.subscribe wants the VAPID key as a Uint8Array.
const urlBase64ToUint8Array = (base64String) => {
  const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
  const raw = window.atob(base64);
  return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)));
};

const pushSupported = () =>
  'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;

const formatMemberSince = (iso) => {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return d.toLocaleDateString('en-IN', { year: 'numeric', month: 'long' });
};

// Filename the export is saved under. Fixed (not per-user) so a person who
// exports twice gets "…(1).json" from the browser rather than two files whose
// names differ only by an internal account id.
const EXPORT_FILENAME = 'amphive-data-export.json';

// Must match backend DELETE_ACCOUNT_CONFIRM_PHRASE exactly — the backend
// rejects anything else with a 400, so a typo here would make the button
// permanently un-confirmable.
const CLOSE_CONFIRM_PHRASE = 'DELETE MY ACCOUNT';

export default function Account() {
  const { user, logout } = useAuth();
  const { coin_inr_rate: rate } = useConfig();
  const toast = useToast();

  useDocumentMeta({
    title: 'Account',
    description:
      'Your AmpHive profile, password, notification settings and data controls.',
    // No `path`/canonical and no `index`: this page is authenticated, so it
    // must stay out of the index (useDocumentMeta noindexes by default).
  });

  const [resetBusy, setResetBusy] = useState(false);
  const [resetSent, setResetSent] = useState(false);

  // 'checking' | 'unavailable' | 'off' | 'on' | 'denied'
  const [pushState, setPushState] = useState('checking');
  const [pushBusy, setPushBusy] = useState(false);

  // --- Your data: export + closure ---
  const [exportBusy, setExportBusy] = useState(false);
  const [closeOpen, setCloseOpen] = useState(false);
  const [closeConfirm, setCloseConfirm] = useState('');
  const [closePassword, setClosePassword] = useState('');
  const [closeError, setCloseError] = useState('');
  const [closeBusy, setCloseBusy] = useState(false);

  useEffect(() => {
    if (!pushSupported()) {
      setPushState('unavailable');
      return;
    }
    if (Notification.permission === 'denied') {
      setPushState('denied');
      return;
    }
    navigator.serviceWorker
      .getRegistration('/sw.js')
      .then(async (reg) => {
        const sub = reg && (await reg.pushManager.getSubscription());
        setPushState(sub ? 'on' : 'off');
      })
      .catch(() => setPushState('off'));
  }, []);

  const handleResetPassword = async () => {
    if (!user?.email) return;
    setResetBusy(true);
    try {
      await api.post('/api/auth/forgot-password', { email: user.email });
      setResetSent(true);
      toast.ok('Reset link sent — check your inbox.');
    } catch (err) {
      toast.error(apiErrorCopy(err));
    } finally {
      setResetBusy(false);
    }
  };

  const enablePush = async () => {
    setPushBusy(true);
    try {
      const { enabled, vapid_public_key: vapidKey } = await api.get('/api/notifications/push/public-key');
      if (!enabled) {
        setPushState('unavailable');
        return;
      }
      const permission = await Notification.requestPermission();
      if (permission !== 'granted') {
        setPushState(permission === 'denied' ? 'denied' : 'off');
        return;
      }
      const reg = await navigator.serviceWorker.register('/sw.js');
      const sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(vapidKey),
      });
      await api.post('/api/notifications/push/subscribe', sub.toJSON());
      setPushState('on');
      toast.ok('Push notifications enabled.');
    } catch (err) {
      toast.error(apiErrorCopy(err));
      setPushState('off');
    } finally {
      setPushBusy(false);
    }
  };

  const disablePush = async () => {
    setPushBusy(true);
    try {
      const reg = await navigator.serviceWorker.getRegistration('/sw.js');
      const sub = reg && (await reg.pushManager.getSubscription());
      if (sub) {
        await api.delete('/api/notifications/push/subscribe', { endpoint: sub.endpoint });
        await sub.unsubscribe();
      }
      setPushState('off');
      toast.ok('Push notifications disabled.');
    } catch (err) {
      toast.error(apiErrorCopy(err));
    } finally {
      setPushBusy(false);
    }
  };

  /**
   * Download the export.
   *
   * The endpoint needs the Authorization header, so it can't be a plain
   * <a href> — but unlike the GST-invoice downloads (Activity.jsx,
   * SessionReceipt.jsx) it returns *JSON*, which means it can go through the
   * normal api client instead of a raw fetch. That matters: the client is
   * what handles 401 (expired token → sign-in, with ?next= preserved), the
   * 20 s timeout, and FastAPI's `detail` unwrapping. The raw-fetch invoice
   * downloads bypass all three; there's no reason to repeat that here.
   *
   * The browser therefore never sees the response's Content-Disposition
   * header (fetch consumed it), so the save is driven client-side from a
   * Blob + a synthetic <a download>.
   */
  const handleExport = async () => {
    setExportBusy(true);
    try {
      const exportDoc = await api.get('/api/auth/me/export');
      const blob = new Blob([JSON.stringify(exportDoc, null, 2)], {
        type: 'application/json',
      });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = EXPORT_FILENAME;
      // Firefox only honours a programmatic click on a connected element.
      document.body.appendChild(link);
      link.click();
      link.remove();
      // Give the download time to start before reclaiming the object URL —
      // same reasoning (and window) as the invoice viewer in Activity.jsx.
      setTimeout(() => URL.revokeObjectURL(url), 60_000);
      toast.ok('Your data export has been downloaded.');
    } catch (err) {
      // The endpoint is capped at 5 exports/hour per account. The backend's
      // 429 detail carries the exact wait, which is more use than a generic
      // "try again later" — but say what the cap IS so the limit doesn't
      // read as a fault.
      toast.error(
        err?.status === 429
          ? `${apiErrorCopy(err)} Exports are limited to five an hour.`
          : apiErrorCopy(err)
      );
    } finally {
      setExportBusy(false);
    }
  };

  // A Google-only account has no usable password hash (the backend stores a
  // random unusable one), so no password could ever verify and the backend
  // doesn't ask for one. Prompting for it would be an unanswerable question.
  const isGoogleOnly = user?.auth_provider === 'google';
  const coinBalance = Number(user?.coin_balance ?? 0);
  const hasCredit = Number.isFinite(coinBalance) && coinBalance > 0;

  const confirmPhraseOk = closeConfirm.trim() === CLOSE_CONFIRM_PHRASE;
  const closeReady = confirmPhraseOk && (isGoogleOnly || closePassword.length > 0);

  const openCloseDialog = () => {
    setCloseConfirm('');
    setClosePassword('');
    setCloseError('');
    setCloseOpen(true);
  };

  const dismissCloseDialog = () => {
    if (!closeBusy) setCloseOpen(false);
  };

  const handleCloseAccount = async (e) => {
    e.preventDefault();
    if (!closeReady || closeBusy) return;
    setCloseBusy(true);
    setCloseError('');
    try {
      await api.delete('/api/auth/me', {
        confirm: closeConfirm.trim(),
        ...(isGoogleOnly ? {} : { password: closePassword }),
      });

      // Tear the session down through the same path the sign-out button
      // uses — it clears both localStorage keys AND the context state, which
      // hand-rolled localStorage.removeItem calls would leave half-done.
      // Its server-side revoke will 401 (closure already bumped
      // token_version, which is exactly what /logout does); logout() swallows
      // that and clears locally regardless.
      await logout();

      // A full-page replace, not navigate(): every provider still mounted
      // (wallet, notifications, session polling) holds data for an account
      // that no longer exists, so a clean reload of the anonymous app is the
      // honest end state. `replace` also keeps /account out of the back
      // stack, and lands after — so supersedes — the api client's 401 bounce
      // to /login.
      window.location.replace('/');
    } catch (err) {
      // 400 (phrase), 403 (password), 409 (active session / last operator)
      // and 429 all carry a `detail` that says exactly what to do next.
      setCloseError(apiErrorCopy(err));
      setCloseBusy(false);
    }
  };

  const memberSince = formatMemberSince(user?.created_at);

  return (
    <main className="page">
      <PageHeader title="Account" sub="Your profile, security and notification settings." />

      <div className="stack account-stack">
        <section className="card">
          <h2 className="account-card-title">
            <UserIcon size={18} aria-hidden="true" />
            Profile
          </h2>
          <dl className="account-profile-grid">
            <div>
              <dt className="field-label">Name</dt>
              <dd>{user?.full_name || '—'}</dd>
            </div>
            <div>
              <dt className="field-label">Email</dt>
              <dd className="account-profile-email">
                <Mail size={14} aria-hidden="true" />
                {user?.email || '—'}
              </dd>
            </div>
            {memberSince && (
              <div>
                <dt className="field-label">Member since</dt>
                <dd>{memberSince}</dd>
              </div>
            )}
          </dl>
        </section>

        <section className="card">
          <h2 className="account-card-title">
            <ShieldCheck size={18} aria-hidden="true" />
            Security
          </h2>
          <div className="stack-sm">
            <p className="text-2 text-sm">
              We&apos;ll email a link to {user?.email || 'your address'} so you can set a new password.
            </p>
            <button
              type="button"
              className="btn btn-quiet"
              onClick={handleResetPassword}
              disabled={resetBusy}
            >
              {resetBusy ? 'Sending…' : 'Reset your password'}
            </button>
            {resetSent && (
              <div className="banner banner-ok" role="status">
                Check your inbox — the link expires after a short while.
              </div>
            )}
          </div>
        </section>

        <section className="card">
          <h2 className="account-card-title">
            <Bell size={18} aria-hidden="true" />
            Notifications
          </h2>

          {pushState === 'checking' && <Skeleton lines={2} />}

          {pushState === 'unavailable' && (
            <p className="text-3 text-sm">Push notifications aren&apos;t supported in this browser.</p>
          )}

          {pushState === 'denied' && (
            <div className="banner banner-warn" role="alert">
              Push is blocked — allow notifications for this site in your browser settings.
            </div>
          )}

          {pushState === 'off' && (
            <div className="stack-sm">
              <p className="text-2 text-sm">
                Get session updates, low-balance warnings and credit confirmations even when the app isn&apos;t open.
              </p>
              <button
                type="button"
                className="btn btn-primary"
                onClick={enablePush}
                disabled={pushBusy}
              >
                {pushBusy ? 'Enabling…' : 'Enable push notifications'}
              </button>
            </div>
          )}

          {pushState === 'on' && (
            <div className="row-between">
              <span className="text-2 text-sm">Push notifications are on.</span>
              <button
                type="button"
                className="btn btn-quiet btn-sm"
                onClick={disablePush}
                disabled={pushBusy}
              >
                {pushBusy ? 'Disabling…' : 'Disable'}
              </button>
            </div>
          )}
        </section>

        {user?.role === 'driver' && (
          <section className="card">
            <h2 className="account-card-title">
              <PlugZap size={18} aria-hidden="true" />
              Host your chargers
            </h2>
            <div className="stack-sm">
              <p className="text-2 text-sm">
                Own a parking spot with a plug point? Set your own pricing and earn from it in the host console.
              </p>
              <a href={`${cpoOrigin()}/cpo`} className="btn btn-quiet">
                Host your chargers
              </a>
            </div>
          </section>
        )}

        <section className="card">
          <h2 className="account-card-title">
            <Download size={18} aria-hidden="true" />
            Your data
          </h2>

          <div className="stack-sm">
            <p className="text-2 text-sm">
              Download everything AmpHive holds about your account — profile,
              charging history, credit ledger, invoices, reservations, reports
              and notifications — as one JSON file. Per-second telemetry from
              your sessions isn&apos;t included; it&apos;s summarised into each
              session&apos;s energy and cost.
            </p>
            <button
              type="button"
              className="btn btn-quiet"
              onClick={handleExport}
              disabled={exportBusy}
            >
              {exportBusy ? 'Preparing your file…' : 'Download your data'}
            </button>
          </div>

          <div className="account-danger">
            <h3 className="account-danger-title">Close your account</h3>
            <p className="text-2 text-sm">
              Closing is permanent and takes effect immediately. Your name,
              email address and Google sign-in link are erased, and your
              notifications, saved chargers, push devices and group
              memberships are deleted. Past charging and payment records are
              kept but anonymised — they&apos;re tax records.{' '}
              {hasCredit ? (
                <>
                  Your remaining charging credit of{' '}
                  <Money coins={coinBalance} rate={rate} /> is forfeited.
                </>
              ) : (
                'Any remaining charging credit is forfeited.'
              )}{' '}
              <Link to="/privacy">See what happens to your data</Link>.
            </p>
            <button
              type="button"
              className="btn btn-danger"
              onClick={openCloseDialog}
            >
              <Trash2 size={16} aria-hidden="true" />
              Close your account
            </button>
          </div>
        </section>
      </div>

      {/* The most dangerous control in the app: every consequence is stated
          in full BEFORE the confirm field, the phrase must be typed (not
          clicked), and password accounts re-authenticate so an unattended
          open tab can't be used to close somebody's account. */}
      <Modal
        open={closeOpen}
        onClose={dismissCloseDialog}
        title="Close your account"
        size="sm"
        footer={
          <>
            <button
              type="button"
              className="btn btn-quiet"
              onClick={dismissCloseDialog}
              disabled={closeBusy}
            >
              Cancel
            </button>
            {/* Lives in the modal footer (outside the <form> in the DOM), so
                it is wired back to the form by id — keeping one submit path
                for both the button and the Enter key. */}
            <button
              type="submit"
              form="close-account-form"
              className="btn btn-danger-solid"
              disabled={!closeReady || closeBusy}
            >
              {closeBusy ? 'Closing…' : 'Close my account permanently'}
            </button>
          </>
        }
      >
        <form id="close-account-form" className="stack-sm" onSubmit={handleCloseAccount}>
          <p className="text-2">
            This can&apos;t be undone. When you close your account:
          </p>
          <ul className="account-close-consequences text-2 text-sm">
            <li>
              Your name, email address and Google sign-in link are erased and
              your password is destroyed. You can&apos;t sign in again, and the
              account can&apos;t be restored.
            </li>
            <li>
              Your notifications, saved chargers, push notification devices and
              group memberships are deleted. Upcoming reservations and queued
              charges are cancelled.
            </li>
            <li>
              Past charging sessions, credit ledger entries and GST invoices are{' '}
              <strong>kept</strong>, but anonymised so they no longer identify
              you — they&apos;re the operator&apos;s tax records.
            </li>
            <li>
              {hasCredit ? (
                <>
                  Your remaining charging credit of{' '}
                  <strong>
                    <Money coins={coinBalance} rate={rate} />
                  </strong>{' '}
                  is forfeited. Charging credit is prepaid balance for charging
                  only — it can&apos;t be paid out as cash or moved to another
                  account, so spend it before you close.
                </>
              ) : (
                <>
                  Any remaining charging credit is forfeited. It can&apos;t be
                  paid out as cash or moved to another account.
                </>
              )}
            </li>
          </ul>
          <p className="text-3 text-sm">
            The full detail is in the <Link to="/privacy">Privacy Policy</Link>.
          </p>

          <div className="field">
            <label className="field-label" htmlFor="close-confirm">
              Type {CLOSE_CONFIRM_PHRASE} to confirm
            </label>
            <input
              id="close-confirm"
              type="text"
              className="input"
              value={closeConfirm}
              onChange={(e) => setCloseConfirm(e.target.value)}
              aria-describedby="close-confirm-hint"
              autoComplete="off"
              autoCorrect="off"
              spellCheck={false}
            />
            <p id="close-confirm-hint" className="text-3 text-xs">
              Exactly as shown, in capitals.
            </p>
          </div>

          {!isGoogleOnly && (
            <div className="field">
              <label className="field-label" htmlFor="close-password">
                Your password
              </label>
              <input
                id="close-password"
                type="password"
                className="input"
                value={closePassword}
                onChange={(e) => setClosePassword(e.target.value)}
                aria-describedby="close-password-hint"
                autoComplete="current-password"
              />
              <p id="close-password-hint" className="text-3 text-xs">
                We ask again so nobody else can close your account from a tab
                you left open.
              </p>
            </div>
          )}

          {closeError && (
            <div className="banner banner-danger" role="alert">
              <p>{closeError}</p>
            </div>
          )}
        </form>
      </Modal>
    </main>
  );
}
