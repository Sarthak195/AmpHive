/**
 * useDocumentMeta — per-route <title>, description, canonical and robots.
 * =======================================================================
 * This is a client-rendered SPA with no SSR, so index.html can only carry ONE
 * static title. Before this hook every one of the ~35 routes showed the same
 * "AmpHive — Shared EV charging": indistinguishable in search results, in the
 * browser history, in a tab strip, and in a screen reader's page announcement.
 *
 * A deliberately small hand-rolled hook rather than react-helmet: the app needs
 * four tags, and a dependency that ships a whole context provider to set them
 * is not worth the bytes on a route-split bundle.
 *
 * What it manages, and why each one:
 *   title       — the tab, the history entry, and the first thing a screen
 *                 reader announces after navigation.
 *   description — the search-result snippet.
 *   canonical   — the driver app and the host console serve BYTE-IDENTICAL
 *                 HTML on two hostnames. Without a canonical that is a genuine
 *                 duplicate-content split.
 *   robots      — authenticated surfaces must not be indexed. Crawlers execute
 *                 JS, so a signed-in page CAN be reached and indexed; the
 *                 `noindex` default below (see `index: true` opt-in) is what
 *                 keeps /account, /session, the whole host console and the
 *                 admin console out of the index. nginx additionally sends
 *                 X-Robots-Tag on the console host as a belt-and-braces layer
 *                 that does not depend on JS running at all.
 *
 * Tags are written on mount/param-change and left in place on unmount — the
 * next route immediately overwrites them, and leaving the previous value up
 * for one frame is better than blanking the title mid-navigation.
 */

import { useEffect } from 'react';
import { SITE_DESCRIPTION, SITE_NAME, SITE_ORIGIN } from '../utils/legal';

const DEFAULT_TITLE = `${SITE_NAME} — Shared EV charging`;

/** Create the tag if it isn't there yet, then set its content. */
function upsertMeta(selector, attrs, content) {
  let el = document.head.querySelector(selector);
  if (!el) {
    el = document.createElement('meta');
    Object.entries(attrs).forEach(([k, v]) => el.setAttribute(k, v));
    document.head.appendChild(el);
  }
  el.setAttribute('content', content);
  return el;
}

function upsertLink(rel, href) {
  let el = document.head.querySelector(`link[rel="${rel}"]`);
  if (!el) {
    el = document.createElement('link');
    el.setAttribute('rel', rel);
    document.head.appendChild(el);
  }
  el.setAttribute('href', href);
  return el;
}

/**
 * @param {object}  opts
 * @param {string}  [opts.title]       Page title, without the site-name suffix.
 * @param {string}  [opts.description] Meta description / OG description.
 * @param {string}  [opts.path]        Canonical path on the driver origin, e.g. "/privacy".
 * @param {boolean} [opts.index]       Opt IN to indexing. Default false: the
 *                                     overwhelming majority of routes are
 *                                     authenticated, so "not indexed" is the
 *                                     safe default and public pages say so
 *                                     explicitly.
 */
export default function useDocumentMeta({ title, description, path, index = false } = {}) {
  useEffect(() => {
    const fullTitle = title ? `${title} · ${SITE_NAME}` : DEFAULT_TITLE;
    document.title = fullTitle;

    const desc = description || SITE_DESCRIPTION;
    upsertMeta('meta[name="description"]', { name: 'description' }, desc);

    // Open Graph / Twitter share previews follow the page, not just the site.
    upsertMeta('meta[property="og:title"]', { property: 'og:title' }, fullTitle);
    upsertMeta('meta[property="og:description"]', { property: 'og:description' }, desc);
    upsertMeta('meta[name="twitter:title"]', { name: 'twitter:title' }, fullTitle);
    upsertMeta('meta[name="twitter:description"]', { name: 'twitter:description' }, desc);

    if (path) {
      const canonical = `${SITE_ORIGIN}${path}`;
      upsertLink('canonical', canonical);
      upsertMeta('meta[property="og:url"]', { property: 'og:url' }, canonical);
    }

    upsertMeta(
      'meta[name="robots"]',
      { name: 'robots' },
      index ? 'index, follow' : 'noindex, nofollow',
    );
  }, [title, description, path, index]);
}
