/**
 * Account — read-only profile (no edit endpoint exists, so nothing here
 * fakes one), a password-reset trigger that reuses the existing
 * forgot-password flow, the web-push opt-in (moved here from
 * NotificationBell's footer per the redesign contract), and a driver-only
 * link out to the host console.
 */

import { useEffect, useState } from 'react';
import { Mail, User as UserIcon, ShieldCheck, Bell, PlugZap } from 'lucide-react';
import PageHeader from '../components/ui/PageHeader';
import Skeleton from '../components/ui/Skeleton';
import { useToast } from '../components/ui';
import api from '../api/client';
import { useAuth } from '../contexts/AuthContext';
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

export default function Account() {
  const { user } = useAuth();
  const toast = useToast();

  const [resetBusy, setResetBusy] = useState(false);
  const [resetSent, setResetSent] = useState(false);

  // 'checking' | 'unavailable' | 'off' | 'on' | 'denied'
  const [pushState, setPushState] = useState('checking');
  const [pushBusy, setPushBusy] = useState(false);

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
                Get session updates, low-balance warnings and top-up confirmations even when the app isn&apos;t open.
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
      </div>
    </main>
  );
}
