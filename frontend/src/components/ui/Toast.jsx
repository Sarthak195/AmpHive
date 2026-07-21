/**
 * Toast — app-wide transient notices.
 * <ToastProvider> wraps the app once (main.jsx) and renders the aria-live
 * .toasts region; `useToast()` gives pages { ok, error, warn, info }. Each
 * toast auto-dismisses after 5s and carries a manual dismiss button.
 *
 * `toastBus` is the module-level escape hatch so non-component code (e.g.
 * api/client interceptors) can push toasts too — it forwards to whichever
 * provider is mounted.
 */
/* eslint-disable react-refresh/only-export-components -- provider + hook +
   bus intentionally live together; losing fast-refresh here is acceptable
   (same policy as src/contexts/). */

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { X } from 'lucide-react';

const ToastContext = createContext(null);

const AUTO_DISMISS_MS = 5000;

// Module-level singleton: provider instances subscribe; non-component code
// publishes. If no provider is mounted the push is a silent no-op.
const listeners = new Set();
const publish = (tone, message) => listeners.forEach((l) => l(tone, message));

export const toastBus = {
  ok: (message) => publish('ok', message),
  error: (message) => publish('danger', message),
  warn: (message) => publish('warn', message),
  info: (message) => publish('info', message),
};

export function ToastProvider({ children }) {
  const [toasts, setToasts] = useState([]);
  const nextIdRef = useRef(0);
  const timersRef = useRef(new Map());

  const dismiss = useCallback((id) => {
    setToasts((t) => t.filter((x) => x.id !== id));
    const timer = timersRef.current.get(id);
    if (timer) {
      clearTimeout(timer);
      timersRef.current.delete(id);
    }
  }, []);

  const push = useCallback(
    (tone, message) => {
      const id = ++nextIdRef.current;
      setToasts((t) => [...t, { id, tone, message }]);
      timersRef.current.set(id, setTimeout(() => dismiss(id), AUTO_DISMISS_MS));
    },
    [dismiss]
  );

  // Receive pushes from the module-level bus.
  useEffect(() => {
    listeners.add(push);
    return () => listeners.delete(push);
  }, [push]);

  // Clear pending auto-dismiss timers on unmount.
  useEffect(() => {
    const timers = timersRef.current;
    return () => {
      timers.forEach((t) => clearTimeout(t));
      timers.clear();
    };
  }, []);

  const api = useMemo(
    () => ({
      ok: (message) => push('ok', message),
      error: (message) => push('danger', message),
      warn: (message) => push('warn', message),
      info: (message) => push('info', message),
    }),
    [push]
  );

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div className="toasts" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} className={`toast toast-${t.tone}`}>
            <div className="toast-body">
              <p className="toast-msg">{t.message}</p>
            </div>
            <button
              type="button"
              className="btn btn-ghost btn-icon btn-sm"
              aria-label="Dismiss notification"
              onClick={() => dismiss(t.id)}
            >
              <X size={16} aria-hidden="true" />
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

/** Toast methods { ok, error, warn, info }. Falls back to the bus (no-op
 *  without a mounted provider) so callers never crash. */
export function useToast() {
  return useContext(ToastContext) || toastBus;
}
