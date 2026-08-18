/**
 * Modal — the app-wide dialog primitive (.overlay/.modal from primitives.css).
 * Portals to <body>, role="dialog" aria-modal labelled by its title, traps
 * Tab focus inside, closes on Escape or overlay click, and restores focus to
 * the trigger element on close. Sizes: sm | md (default) | lg.
 */

import { useEffect, useId, useRef } from 'react';
import { createPortal } from 'react-dom';

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

const SIZE_CLASS = { sm: ' modal-sm', lg: ' modal-lg' };

export default function Modal({ open, onClose, title, size = 'md', footer, children }) {
  const titleId = useId();
  const modalRef = useRef(null);
  const restoreRef = useRef(null);

  // On open: remember the trigger and move focus inside. On close/unmount:
  // give focus back to the trigger.
  useEffect(() => {
    if (!open) return undefined;
    restoreRef.current = document.activeElement;
    const node = modalRef.current;
    const first = node?.querySelector(FOCUSABLE);
    (first || node)?.focus();
    return () => {
      const prev = restoreRef.current;
      if (prev && typeof prev.focus === 'function' && document.contains(prev)) prev.focus();
    };
  }, [open]);

  if (!open) return null;

  const handleKeyDown = (e) => {
    if (e.key === 'Escape') {
      e.stopPropagation();
      onClose?.();
      return;
    }
    if (e.key !== 'Tab') return;
    // Focus trap: cycle Tab / Shift+Tab inside the dialog.
    const node = modalRef.current;
    if (!node) return;
    const items = Array.from(node.querySelectorAll(FOCUSABLE));
    if (items.length === 0) {
      e.preventDefault();
      return;
    }
    const first = items[0];
    const last = items[items.length - 1];
    const active = document.activeElement;
    if (e.shiftKey) {
      if (active === first || !node.contains(active)) {
        e.preventDefault();
        last.focus();
      }
    } else if (active === last || !node.contains(active)) {
      e.preventDefault();
      first.focus();
    }
  };

  const handleOverlayClick = (e) => {
    if (e.target === e.currentTarget) onClose?.();
  };

  return createPortal(
    // role="presentation": the scrim is a visual layer that happens to catch
    // the click-outside gesture. It exposes no semantics of its own, and every
    // action it offers has a real control inside the dialog (Escape, the close
    // button), so it must not read as an interactive element.
    <div className="overlay" role="presentation" onClick={handleOverlayClick} onKeyDown={handleKeyDown}>
      <div
        ref={modalRef}
        className={`modal${SIZE_CLASS[size] || ''}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
      >
        <h2 className="modal-title" id={titleId}>
          {title}
        </h2>
        {children}
        {footer && <div className="modal-actions">{footer}</div>}
      </div>
    </div>,
    document.body
  );
}
