/**
 * AmpHive Notification Bell
 * =========================
 * Driver notification center in the navbar: unread badge, dropdown feed
 * (GET /api/notifications), live prepend from the shared socket's
 * `notification` events, mark-read/mark-all-read, and a Web Push opt-in
 * (service worker + pushManager.subscribe against the backend VAPID key).
 */

import { useState, useEffect, useCallback, useRef } from 'react';
import api from '../api/client';
import { useAuth } from '../contexts/AuthContext';
import { useSession } from '../contexts/SessionContext';

const SEVERITY_ICON = { critical: '🚨', warning: '⚠️', info: '🔔' };

const timeAgo = (iso) => {
  if (!iso) return '';
  const secs = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (secs < 60) return `${secs}s ago`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
};

// pushManager.subscribe wants the VAPID key as a Uint8Array.
const urlBase64ToUint8Array = (base64String) => {
  const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
  const raw = window.atob(base64);
  return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)));
};

const pushSupported = () =>
  'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;

const NotificationBell = () => {
  const { user } = useAuth();
  const { socket } = useSession();
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState([]);
  const [unread, setUnread] = useState(0);
  // 'unavailable' | 'off' | 'on' | 'denied'
  const [pushState, setPushState] = useState('unavailable');
  const panelRef = useRef(null);

  const fetchFeed = useCallback(async () => {
    try {
      const res = await api.get('/api/notifications?limit=20');
      setItems(res.notifications);
      setUnread(res.unread_count);
    } catch (err) {
      console.error('Failed to load notifications:', err);
    }
  }, []);

  useEffect(() => {
    if (user) fetchFeed();
    else {
      setItems([]);
      setUnread(0);
    }
  }, [user, fetchFeed]);

  // Live prepend from the shared socket (server targets our user room).
  useEffect(() => {
    if (!socket) return;
    const handleNotification = (n) => {
      setItems((prev) => [n, ...prev].slice(0, 20));
      setUnread((prev) => prev + 1);
    };
    socket.on('notification', handleNotification);
    return () => socket.off('notification', handleNotification);
  }, [socket]);

  // Detect whether this browser already holds an active push subscription.
  useEffect(() => {
    if (!user || !pushSupported()) return;
    if (Notification.permission === 'denied') {
      setPushState('denied');
      return;
    }
    navigator.serviceWorker.getRegistration('/sw.js').then(async (reg) => {
      const sub = reg && (await reg.pushManager.getSubscription());
      setPushState(sub ? 'on' : 'off');
    }).catch(() => setPushState('off'));
  }, [user]);

  // Close on outside click.
  useEffect(() => {
    if (!open) return;
    const onClick = (e) => {
      if (panelRef.current && !panelRef.current.contains(e.target)) setOpen(false);
    };
    document.addEventListener('mousedown', onClick);
    return () => document.removeEventListener('mousedown', onClick);
  }, [open]);

  const markRead = async (id) => {
    setItems((prev) => prev.map((n) => (n.id === id ? { ...n, read: true } : n)));
    setUnread((prev) => Math.max(0, prev - 1));
    try {
      await api.post(`/api/notifications/${id}/read`, {});
    } catch (err) {
      console.error('Failed to mark notification read:', err);
    }
  };

  const markAllRead = async () => {
    setItems((prev) => prev.map((n) => ({ ...n, read: true })));
    setUnread(0);
    try {
      await api.post('/api/notifications/read-all', {});
    } catch (err) {
      console.error('Failed to mark all read:', err);
    }
  };

  const enablePush = async () => {
    try {
      const { enabled, vapid_public_key } = await api.get('/api/notifications/push/public-key');
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
        applicationServerKey: urlBase64ToUint8Array(vapid_public_key),
      });
      await api.post('/api/notifications/push/subscribe', sub.toJSON());
      setPushState('on');
    } catch (err) {
      console.error('Failed to enable push:', err);
      setPushState('off');
    }
  };

  const disablePush = async () => {
    try {
      const reg = await navigator.serviceWorker.getRegistration('/sw.js');
      const sub = reg && (await reg.pushManager.getSubscription());
      if (sub) {
        await api.delete('/api/notifications/push/subscribe', { endpoint: sub.endpoint });
        await sub.unsubscribe();
      }
      setPushState('off');
    } catch (err) {
      console.error('Failed to disable push:', err);
    }
  };

  if (!user) return null;

  return (
    <div ref={panelRef} style={{ position: 'relative' }}>
      <button
        className="btn btn-ghost btn-sm"
        aria-label={`Notifications${unread ? ` (${unread} unread)` : ''}`}
        onClick={() => setOpen((o) => !o)}
        style={{ position: 'relative', fontSize: '1.05rem', padding: '0.35rem 0.5rem' }}
      >
        🔔
        {unread > 0 && (
          <span
            data-testid="unread-badge"
            style={{
              position: 'absolute', top: '-4px', right: '-4px',
              background: 'var(--color-danger)', color: '#fff',
              borderRadius: '999px', fontSize: '0.65rem', fontWeight: 700,
              minWidth: '1.1rem', height: '1.1rem', lineHeight: '1.1rem',
              textAlign: 'center', padding: '0 3px',
            }}
          >
            {unread > 99 ? '99+' : unread}
          </span>
        )}
      </button>

      {open && (
        // Positioning lives in .notification-panel (global.css): absolute
        // under the bell on desktop, viewport-fixed sheet on phones — the
        // bell isn't at the screen edge, so a right-anchored 360px panel
        // would overflow the LEFT edge of a 360-430px viewport.
        <div className="glass notification-panel">
          <div className="flex justify-between items-center" style={{ marginBottom: '0.5rem' }}>
            <strong style={{ fontSize: '0.95rem' }}>Notifications</strong>
            {unread > 0 && (
              <button className="btn btn-ghost btn-sm" onClick={markAllRead}>
                Mark all read
              </button>
            )}
          </div>

          {items.length === 0 ? (
            <div style={{ color: 'var(--color-text-muted)', fontSize: '0.85rem', padding: '0.75rem 0' }}>
              Nothing yet — session updates, low-balance warnings and top-up
              confirmations will show up here.
            </div>
          ) : (
            <div className="flex flex-col gap-2">
              {items.map((n) => (
                <div
                  key={n.id}
                  onClick={() => !n.read && markRead(n.id)}
                  style={{
                    padding: '0.55rem 0.7rem',
                    borderRadius: 'var(--radius-md)',
                    background: n.read ? 'transparent' : 'hsla(73, 100%, 50%, 0.08)',
                    border: '1px solid var(--color-surface-border)',
                    cursor: n.read ? 'default' : 'pointer',
                  }}
                >
                  <div style={{ fontWeight: 600, fontSize: '0.85rem' }}>
                    {SEVERITY_ICON[n.severity] || '🔔'} {n.title}
                    {!n.read && (
                      <span style={{ color: 'var(--color-primary)', marginLeft: '0.4rem', fontSize: '0.7rem' }}>●</span>
                    )}
                  </div>
                  <div style={{ color: 'var(--color-text-secondary)', fontSize: '0.8rem', marginTop: '0.15rem' }}>
                    {n.body}
                  </div>
                  <div style={{ color: 'var(--color-text-muted)', fontSize: '0.72rem', marginTop: '0.15rem' }}>
                    {timeAgo(n.created_at)}
                  </div>
                </div>
              ))}
            </div>
          )}

          {pushSupported() && pushState !== 'unavailable' && (
            <div
              style={{
                marginTop: '0.75rem', paddingTop: '0.6rem',
                borderTop: '1px solid var(--color-surface-border)',
                fontSize: '0.8rem',
              }}
            >
              {pushState === 'on' ? (
                <div className="flex justify-between items-center">
                  <span style={{ color: 'var(--color-text-secondary)' }}>Push notifications on ✓</span>
                  <button className="btn btn-ghost btn-sm" onClick={disablePush}>Disable</button>
                </div>
              ) : pushState === 'denied' ? (
                <span style={{ color: 'var(--color-text-muted)' }}>
                  Push blocked — allow notifications for this site in your browser settings.
                </span>
              ) : (
                <button className="btn btn-primary btn-sm" onClick={enablePush} style={{ width: '100%' }}>
                  Enable push notifications
                </button>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
};

export default NotificationBell;
