/**
 * AmpHive Notification Bell
 * =========================
 * Driver notification center in the AppBar: unread badge, a drawer feed
 * (GET /api/notifications, refetched every time it opens), live prepend from
 * the shared socket's `notification` events, mark-read/mark-all-read, and
 * actionable rows (plug_id → /?plug=, session_id → /session, a topup type →
 * /wallet). The web-push opt-in itself now lives on the Account page.
 */

import { useState, useEffect, useCallback, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { Bell } from 'lucide-react';
import api from '../api/client';
import { useAuth } from '../contexts/AuthContext';
import { useSession } from '../contexts/SessionContext';
import StatusDot from './ui/StatusDot';
import './NotificationBell.css';

const SEVERITY_TONE = { critical: 'danger', warning: 'warn', info: 'info' };

const timeAgo = (iso) => {
  if (!iso) return '';
  const secs = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (secs < 60) return `${secs}s ago`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
};

const NotificationBell = () => {
  const { user } = useAuth();
  const { socket } = useSession();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState([]);
  const [unread, setUnread] = useState(0);
  const wrapRef = useRef(null);
  const bellButtonRef = useRef(null);

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

  // Refetch every time the drawer opens; close on outside click or Escape —
  // both restore focus to the bell button (mirrors CpoLayout's drawer-close
  // pattern).
  useEffect(() => {
    if (!open) return undefined;
    fetchFeed();
    const onPointerDown = (e) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target)) {
        // Prevent the browser's default mousedown "unfocus" behavior so our
        // own focus restore below actually sticks.
        e.preventDefault();
        setOpen(false);
        bellButtonRef.current?.focus();
      }
    };
    const onKeyDown = (e) => {
      if (e.key === 'Escape') {
        setOpen(false);
        bellButtonRef.current?.focus();
      }
    };
    document.addEventListener('mousedown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('mousedown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open, fetchFeed]);

  const markRead = useCallback(async (id) => {
    setItems((prev) => prev.map((n) => (n.id === id ? { ...n, read: true } : n)));
    setUnread((prev) => Math.max(0, prev - 1));
    try {
      await api.post(`/api/notifications/${id}/read`, {});
    } catch (err) {
      console.error('Failed to mark notification read:', err);
    }
  }, []);

  const markAllRead = async () => {
    setItems((prev) => prev.map((n) => ({ ...n, read: true })));
    setUnread(0);
    try {
      await api.post('/api/notifications/read-all', {});
    } catch (err) {
      console.error('Failed to mark all read:', err);
    }
  };

  const handleItemClick = (n) => {
    if (!n.read) markRead(n.id);
    setOpen(false);
    if (n.plug_id) navigate(`/?plug=${n.plug_id}`);
    else if (n.session_id) navigate('/session');
    else if (n.type && n.type.includes('topup')) navigate('/wallet');
  };

  if (!user) return null;

  return (
    <div className="notification-bell" ref={wrapRef}>
      <button
        ref={bellButtonRef}
        type="button"
        className="btn btn-ghost btn-icon"
        aria-label={`Notifications${unread ? ` (${unread} unread)` : ''}`}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <Bell size={18} aria-hidden="true" />
        {unread > 0 && (
          <span className="count-pill notification-bell-badge" data-testid="unread-badge">
            {unread > 99 ? '99+' : unread}
          </span>
        )}
      </button>

      {open && (
        <div className="user-menu-panel notification-panel" role="menu" aria-label="Notifications">
          <div className="row-between notification-panel-header">
            <strong>Notifications</strong>
            {unread > 0 && (
              <button type="button" className="btn btn-ghost btn-sm" onClick={markAllRead}>
                Mark all read
              </button>
            )}
          </div>

          {items.length === 0 ? (
            <p className="text-3 text-sm notification-panel-empty">
              Nothing yet — session updates, low-balance warnings and top-up confirmations will
              show up here.
            </p>
          ) : (
            <div className="notification-list">
              {items.map((n) => (
                <button
                  key={n.id}
                  type="button"
                  role="menuitem"
                  className={`notification-item${n.read ? '' : ' is-unread'}`}
                  onClick={() => handleItemClick(n)}
                >
                  <StatusDot tone={SEVERITY_TONE[n.severity] || 'info'} />
                  <span className="notification-item-body">
                    <span className="notification-item-title">{n.title}</span>
                    <span className="notification-item-text">{n.body}</span>
                    <span className="notification-item-time">{timeAgo(n.created_at)}</span>
                  </span>
                </button>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
};

export default NotificationBell;
