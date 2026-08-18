/**
 * RouteAnnouncer — tells assistive tech that the page changed.
 * ===========================================================
 * A full page load announces the new document. An SPA route change does not:
 * React swaps the DOM, the URL updates, and a screen-reader user gets
 * *silence*. They then have to hunt for what changed, on every navigation.
 *
 * Two things happen on each location change:
 *   1. the new document title is pushed into a polite live region, which is
 *      what a browser announces after a real navigation;
 *   2. focus is moved to the top of the page, so the next Tab starts from the
 *      new content rather than from wherever the old page's focus happened to
 *      be (a link in a nav that may not even exist any more).
 *
 * The title is read on a microtask delay because useDocumentMeta sets
 * document.title in the newly-mounted page's own effect, which runs after this
 * one on the same commit.
 */

import { useEffect, useRef, useState } from 'react';
import { useLocation } from 'react-router-dom';

export default function RouteAnnouncer() {
  const { pathname } = useLocation();
  const [message, setMessage] = useState('');
  const first = useRef(true);

  useEffect(() => {
    // Don't announce the initial load — the browser already did.
    if (first.current) {
      first.current = false;
      return;
    }
    const timer = setTimeout(() => {
      setMessage(document.title || 'Page changed');
      // Reset focus so keyboard navigation restarts from the new page.
      const target = document.querySelector('main') || document.body;
      if (target) {
        target.setAttribute('tabindex', '-1');
        target.focus({ preventScroll: true });
        target.addEventListener('blur', () => target.removeAttribute('tabindex'), { once: true });
      }
    }, 0);
    return () => clearTimeout(timer);
  }, [pathname]);

  return (
    <div aria-live="polite" aria-atomic="true" className="sr-only">
      {message}
    </div>
  );
}
