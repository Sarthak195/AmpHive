/**
 * safePath — guards `?next=`/`state.from`-style redirect targets against
 * open-redirect abuse. Only a same-app relative path is ever safe to hand to
 * navigate()/<Link>: a single leading '/' and nothing that a browser or
 * router could reinterpret as pointing at another origin — no
 * protocol-relative `//evil.com`, no backslash variant `/\evil.com` (some
 * URL parsers normalize `\` to `/`), and no absolute URL with any scheme
 * (`https:`, `javascript:`, etc.).
 */
export function isSafeInternalPath(p) {
  if (typeof p !== 'string' || p.length === 0) return false;
  if (p[0] !== '/') return false;
  if (p[1] === '/' || p[1] === '\\') return false;
  if (/^[a-z][a-z0-9+.-]*:/i.test(p)) return false;
  return true;
}
