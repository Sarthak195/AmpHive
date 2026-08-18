/**
 * Shared constants for the legal pages (/privacy, /terms, /refunds, /contact,
 * /charging-credit-terms) and the site footer.
 *
 * Kept in one place so the contact address and the "last updated" date can
 * never disagree between pages — a policy that names two different contacts
 * is worse than one that names none.
 *
 * NOTE for whoever maintains this: SUPPORT_EMAIL is published on public pages
 * and is the address people will use to exercise data rights, so it must be a
 * real, monitored mailbox. LAST_UPDATED must be bumped whenever the substance
 * of any legal page changes — the date is a claim about the document.
 */

export const SUPPORT_EMAIL = 'support@amphive.app';

// Human-readable date shown on every legal page.
export const LAST_UPDATED = '18 August 2026';

// The public origin used for canonical URLs and structured data. The driver
// app is the canonical, indexable surface; the host console is noindex.
export const SITE_ORIGIN = 'https://amphive.app';

export const SITE_NAME = 'AmpHive';

// One-line description reused by the document <meta name="description">
// default, the Open Graph tags and the WebSite structured data.
export const SITE_DESCRIPTION =
  'AmpHive is a shared EV charging platform. Find a charger nearby, plug in, and pay from your charging credit — or host your own chargers and earn.';
